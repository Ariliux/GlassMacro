"""GlassMacro - AFK Rivals FFA: pick Grenade Launcher + Random x3, then jump.

Why this exists rather than a coordinate macro: the weapon grid MOVES. The
"LIMITED TIME EVENT" banner pushes it down about a row, and Random sits
directly above Grenade Launcher in the same column - so a recorded click picks
the wrong weapon whenever the layout shifts. TinyTask replays coordinates and
cannot see that happen.

This finds the Random tile on screen and works out everything relative to it,
so the grid can sit wherever it likes. The picker appearing is also the signal
that a new round started, so no separate match-end detection is needed: picker
up means choose a loadout, picker down means hold jump, which respawns you and
skips end screens.
"""
import ctypes
import ctypes.wintypes as wintypes
import json
import os
import io
import queue
import re
import shutil
import sys
import subprocess
import threading
import time

import cv2
import numpy as np
from PIL import ImageGrab

import customtkinter as ctk
import tkinter as tk
import tkinter.font as tkfont
import keyboard

APP_NAME, APP_VER = "GlassMacro", "1.0.5"

# Calibration lives in AppData, never beside the exe: a PyInstaller onefile
# build unpacks to a temp folder that is deleted on exit, so anything saved
# next to the program is gone by the next launch.
_LOCAL = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
DATA_DIR = os.path.join(_LOCAL, "GlassMacro")
# This was called Rivals AFK first. Carry an existing setup across once, so the
# rename does not quietly throw away someone's calibration. The screenshot
# folders are left behind - they are diagnostics, and can run to hundreds of MB.
_OLD_DIR = os.path.join(_LOCAL, "RivalsAFK")
if not os.path.isdir(DATA_DIR) and os.path.isdir(_OLD_DIR):
    try:
        shutil.copytree(_OLD_DIR, DATA_DIR,
                        ignore=shutil.ignore_patterns("picks", "stalls"))
    except Exception:
        pass
os.makedirs(DATA_DIR, exist_ok=True)


def resource(name):
    """A file shipped inside the exe. PyInstaller unpacks those to _MEIPASS."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


# Run from source, Windows groups the window under python and shows its icon on
# the taskbar, not ours. Not for the built exe: there the id comes from the exe
# path, which is what lets a pinned shortcut group with the running window.
if not getattr(sys, "frozen", False):
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "GlassMacro.App")
    except Exception:
        pass
CAL_PATH = os.path.join(DATA_DIR, "calibration.json")
TEMPLATE_PATH = os.path.join(DATA_DIR, "random_tile.png")
# The launcher gets its own picture so the click can be CHECKED rather than
# trusted. Matching Random alone was not enough: one bad match put the click
# on Permafrost - a different row AND column - because every position is
# computed from that single anchor.
GL_TEMPLATE_PATH = os.path.join(DATA_DIR, "launcher_tile.png")
# Every pick gets photographed with a marker drawn where it clicked. Three
# runs picked Permafrost, Minigun and RPG - different rows AND columns each
# time - and no amount of adjusting an offset fixes a target that moves.
# The picture says what it actually saw.
SHOT_DIR = os.path.join(DATA_DIR, "picks")
STALL_DIR = os.path.join(DATA_DIR, "stalls")
# Nothing recognised for this long means something is on screen that no detector
# knows. A heartbeat reading "no picker (match 0.61)" for eight and a half hours
# says nothing about WHAT - so take a picture instead of a number.
STALL_AFTER = 300.0        # 5 minutes with nothing recognised
STALL_EVERY = 900.0        # then one more every 15

# A long stretch with nothing recognised is usually an EMPTY server - alive,
# nobody to kill you, so the picker never comes back. That is the best case, not
# a fault: the point is time in-game, and not dying is more of it. So it is left
# alone. (Rivals also reloads by itself; a red 0 0 means empty until you die.)
# A full screen PNG is a few MB and a long session takes hundreds of them, so
# only the most recent are kept. Stalls get a bigger allowance because they are
# the rare ones worth looking at.
KEEP_PICKS = 40
KEEP_STALLS = 60
# Every click on something it DETECTED (a dialog, a Join prompt) gets a picture
# too. A log line reading "clicking the right-hand button at (1190, 530)" was
# unexplainable afterwards - nothing showed what it had seen there.
EVENT_DIR = os.path.join(DATA_DIR, "events")
KEEP_EVENTS = 40


def trim_folder(folder, keep):
    """Delete all but the newest `keep` files. Never raises."""
    try:
        files = [os.path.join(folder, f) for f in os.listdir(folder)]
        files = [f for f in files if os.path.isfile(f)]
        files.sort(key=os.path.getmtime, reverse=True)
        for old in files[keep:]:
            try:
                os.remove(old)
            except Exception:
                pass
    except Exception:
        pass
# The in-window log dies with the app, which made an overnight run
# impossible to explain: eight hours of silence between the last pick and a
# disconnect, with no way to tell whether it was jumping at nothing or
# Roblox had simply lost focus. This file survives.
LOG_PATH = os.path.join(DATA_DIR, "log.txt")
# Jumping logs nothing, so a quiet stretch is ambiguous. A line every few
# minutes saying what it can see turns that into a readable record.
HEARTBEAT = 180.0

# Rivals. Retry on the "Connection Failed" dialog never actually reconnects, so
# the fallback is to close Roblox and rejoin the place directly through the
# roblox:// handler.
RIVALS_PLACE = "17625359962"


def save_cal(cal):
    """Write the calibration, dropping anything not JSON-safe.

    load_cal() parks the launcher template in this dict under "_gl_tile". It is
    a numpy array, so a plain json.dump throws PART WAY THROUGH and leaves a
    truncated file - which silently destroyed a whole calibration, ffa_path and
    all. Everything private starts with an underscore, so strip those.
    """
    with io.open(CAL_PATH, "w", encoding="utf-8") as fh:
        json.dump({k: v for k, v in cal.items() if not k.startswith("_")},
                  fh, indent=2)
RELAUNCH_WAIT = 45.0        # seconds to let Roblox start and load in
# How long to watch for the weapon picker before deciding this is the main
# lobby. This is a GUESS, and a weak one: an FFA intermission looks exactly the
# same - in the game, waiting, no picker - so a short window makes the macro
# try to scroll a menu that is not there. Long enough that a match starting
# resolves it by itself. Replace this with a real lobby detector when there is
# a screenshot of the Play screen to work from.
JOIN_WATCH = 30.0

# After scrolling to the bottom the list settles slightly lower than where the
# cursor sat while the wheel was turning, so the click goes a little above the
# spot it scrolled at.
FFA_CLICK_UP = 20
# The final Play button sits slightly higher than the taught point when you are
# spectating a match in progress, which is where a join usually lands.
GO_CLICK_UP = 10

# The end of a round shows a scoreboard for ~20s and the next match starts on
# its own. The Join prompt can flash up during that, and clicking it cuts the
# transition short for no reason. So the button has to STAY there this long
# before it counts as actually being stuck in spectate.
JOIN_BUTTON_PATIENCE = 12.0

# After a match ends there is a scoreboard, then map choosing, then the next
# match - a minute of screens that are none of the macro's business. Acting
# during them is how it "chose" a map and then decided a map screen was a
# connection dialog and restarted Roblox. So for this long after the weapon
# picker was last seen, it does nothing but jump.
POST_MATCH_QUIET = 60.0

# After the last Play click, how long to wait for the weapon picker before
# deciding the join did not take, and how many times to walk the menu again.
# Clicking through the menu is not the same as landing in a match - it can end
# up spectating, with a "Join this game!" prompt and no picker.
REJOIN_CONFIRM = 10.0
REJOIN_TRIES = 3

# Everything below was worked out on a real setup and is shipped so nobody else
# has to. The ONLY thing a new user teaches is where the weapons are, because
# that genuinely differs - the grid moves whenever an event adds or removes a
# weapon.
#
# The menu path is stored as FRACTIONS of the screen, not pixels, so it still
# lands correctly on a display that is not 1920x1080.
DEFAULT_FFA_FRAC = [
    [0.500000, 0.820370],   # Play, in the lobby
    [0.499479, 0.707407],   # Free For All, once scrolled to the bottom
    [0.584375, 0.697222],   # Play, on the Free For All screen
]
DEFAULT_THRESHOLD = 0.82
DEFAULT_RANDOMS = 3         # slots to fill after the launcher
DEFAULT_NUDGE3 = 20         # the third tab's Random sits this much higher
DEFAULT_GL_NUDGE = [0, 0]
DEFAULT_SCROLL_SECS = 4.0


def default_ffa_path():
    """The shipped menu path, in this screen's pixels."""
    sw, sh = _user32.GetSystemMetrics(0), _user32.GetSystemMetrics(1)
    return [[int(round(fx * sw)), int(round(fy * sh))]
            for fx, fy in DEFAULT_FFA_FRAC]

GRAB = 110              # px captured around the cursor during calibration
SETTLE = 0.45           # the menu animates in; clicking instantly misses
BETWEEN_CLICKS = 0.55
# Only long enough to stop it fighting the menu while that animates. It used
# to be 8s, which meant dying part way through choosing left the macro idle
# for the rest of the countdown while the picker sat open waiting. Against
# someone killing you the instant you spawn, even a couple of seconds is too
# long to be standing still.
REPICK_LOCKOUT = 1.0

_user32 = ctypes.windll.user32

# ---- same palette as SolariMacro, so the two tools look related -----------
# Ice on deep blue-black - glass, not the violet it shared with SolariMacro.
# Surfaces step up in brightness BG < PANEL < CARD < CARD_HI; with no shadows
# available in Tk, that layering plus a 1px border is the only depth there is.
ACCENT, ACCENT_SOFT, ACCENT_DEEP = "#5ecbff", "#a5e4ff", "#1f7fbf"
INK = "#06131d"                         # text on an accent-filled button
BG, PANEL, CARD, CARD_HI = "#070b11", "#0c121b", "#111a26", "#182334"
LINE, TEXT, SUBTLE, MUTED = "#203047", "#e8f0f8", "#8ea2b8", "#56687e"
GREEN, RED, AMBER = "#4ade80", "#f87171", "#fbbf24"
VIOLET, VIOLET_SOFT = ACCENT, ACCENT_SOFT   # older call sites use these names


# ----------------------------------------------------------------- input ---
# SendInput, not PostMessage: Roblox reads gameplay input from Raw Input, which
# only reaches the focused window.
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE, MOUSEEVENTF_ABSOLUTE = 0x0001, 0x8000
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
KEYEVENTF_KEYUP, KEYEVENTF_SCANCODE = 0x0002, 0x0008
SCAN_SPACE = 0x39


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _UNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _UNION)]


def _send(inp):
    _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


# Roblox needs time to notice the cursor moved before the click lands. With
# only 20ms between the two, it still believed the mouse was wherever it had
# been sitting and selected THAT tile - which is why a screenshot could show the
# crosshair dead centre on Grenade Launcher while the game handed over
# Distortion, Minigun or RPG depending on where the mouse happened to rest.
MOVE_SETTLE = 0.14


def _move_rel(dx, dy):
    """One relative mouse move. This is what Roblox actually listens to."""
    _send(INPUT(type=INPUT_MOUSE,
                u=_UNION(mi=MOUSEINPUT(dx=int(dx), dy=int(dy), mouseData=0,
                                       dwFlags=MOUSEEVENTF_MOVE, time=0,
                                       dwExtraInfo=None))))


def move_to(x, y, tries=60):
    """Walk the cursor to x,y with RELATIVE moves, correcting as it goes.

    Roblox takes mouse input from Raw Input, the same as the keyboard. Absolute
    SendInput moves and SetCursorPos both shift the Windows cursor without
    producing a raw event, so Roblox's own cursor never followed and it kept
    selecting whatever was under the position it still believed in.

    Relative moves do produce raw events. They also pass through pointer
    acceleration, so the distance asked for is not the distance travelled -
    hence stepping towards the target and re-reading the real cursor each time
    rather than computing one jump.
    """
    for _ in range(tries):
        p = wintypes.POINT()
        _user32.GetCursorPos(ctypes.byref(p))
        dx, dy = int(x) - p.x, int(y) - p.y
        if abs(dx) <= 1 and abs(dy) <= 1:
            return True
        # Windows "enhance pointer precision" scales a delta by how fast it
        # looks, so one big step lands nowhere near where it was asked to.
        # Long moves go in chunks, and the last few pixels go one at a time,
        # where acceleration has no effect to apply.
        far = max(abs(dx), abs(dy))
        step = 40 if far > 120 else (12 if far > 30 else 3)
        sx = max(-step, min(step, dx))
        sy = max(-step, min(step, dy))
        _move_rel(sx, sy)
        time.sleep(0.010)
    p = wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(p))
    return abs(p.x - int(x)) <= 3 and abs(p.y - int(y)) <= 3


def click(x, y, settle=0.05):
    """Move there for real, then press. False if the cursor never arrived."""
    if not move_to(x, y):
        return False
    time.sleep(MOVE_SETTLE)
    for flags in (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP):
        _send(INPUT(type=INPUT_MOUSE,
                    u=_UNION(mi=MOUSEINPUT(dx=0, dy=0, mouseData=0,
                                           dwFlags=flags, time=0,
                                           dwExtraInfo=None))))
        time.sleep(0.04)
    time.sleep(settle)
    return True


MOUSEEVENTF_WHEEL = 0x0800


def scroll_down_for(x, y, seconds):
    """Hold the wheel down at x,y for a while, to reach the bottom of a list.

    Counting notches meant the click position only worked if the list happened
    to land exactly where it was when the spot was taught. Scrolling to the END
    instead makes the bottom of the list the reference, which is the same every
    time no matter where the list started.
    """
    move_to(x, y)
    time.sleep(0.15)
    end = time.time() + max(0.0, float(seconds))
    while time.time() < end:
        _send(INPUT(type=INPUT_MOUSE,
                    u=_UNION(mi=MOUSEINPUT(dx=0, dy=0, mouseData=-120,
                                           dwFlags=MOUSEEVENTF_WHEEL, time=0,
                                           dwExtraInfo=None))))
        time.sleep(0.05)


def click_here():
    """Left-click without moving the cursor.

    Deliberately no move: while jumping we are in the game, not a menu, and
    yanking the mouse would swing the camera. Only the button is sent.
    """
    for flags in (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP):
        _send(INPUT(type=INPUT_MOUSE,
                    u=_UNION(mi=MOUSEINPUT(dx=0, dy=0, mouseData=0,
                                           dwFlags=flags, time=0,
                                           dwExtraInfo=None))))
        time.sleep(0.02)


def tap_key(scan):
    for up in (0, KEYEVENTF_KEYUP):
        _send(INPUT(type=INPUT_KEYBOARD,
                    u=_UNION(ki=KEYBDINPUT(wVk=0, wScan=scan,
                                           dwFlags=KEYEVENTF_SCANCODE | up,
                                           time=0, dwExtraInfo=None))))
        time.sleep(0.03)


def tap_space():
    tap_key(SCAN_SPACE)


def roblox_hwnd():
    found = []

    def cb(h, _):
        n = ctypes.create_unicode_buffer(256)
        _user32.GetWindowTextW(h, n, 256)
        if _user32.IsWindowVisible(h) and n.value.strip() == "Roblox":
            found.append(h)
        return True

    _user32.EnumWindows(
        ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)(cb), 0)
    return found[0] if found else 0


def focused():
    h = roblox_hwnd()
    return bool(h) and _user32.GetForegroundWindow() == h


# Every click and every detector here is lined up for Roblox FULLSCREEN on a
# 1920x1080 screen. In a window the title bar and taskbar shift everything, so
# the macro puts it back in fullscreen itself.
SUPPORTED_SCREEN = (1920, 1080)
SCAN_F11 = 0x57
# Roblox switches ITSELF to fullscreen part way through starting up when that
# is the saved setting, and F11 is a toggle - pressed during that switch it
# would flip the window straight back. So Roblox has to have sat windowed, in
# front, for this long first.
FULLSCREEN_PATIENCE = 3.0
# Per Roblox window. Two, not one or three: if the test below ever misreads a
# window, two presses toggle it there and back and leave it exactly as it was,
# rather than flipping someone's game every few seconds forever.
FULLSCREEN_TRIES = 2
# The menus re-lay out for the new size after the switch; clicking the join
# path straight away would aim at where things were a moment ago.
FULLSCREEN_SETTLE = 1.5


def screen_size():
    """Primary screen in real pixels (CustomTkinter makes us DPI aware)."""
    return _user32.GetSystemMetrics(0), _user32.GetSystemMetrics(1)


# Every click and detector is lined up for 100% Windows display scaling - the
# only setup GlassMacro is tested on. A 1080p laptop usually ships at 150%.
SUPPORTED_SCALING = 100


def display_scaling():
    """Windows display scaling of the MAIN screen, in percent (100, 125...).

    The main screen, not wherever this window sits: that is the screen every
    screenshot here is taken of. Needs DPI awareness (CustomTkinter turns it
    on), or Windows answers 100% whatever the truth is.
    """
    try:
        u = ctypes.WinDLL("user32")          # own handle: own argtypes
        u.MonitorFromPoint.restype = wintypes.HMONITOR
        u.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        sh = ctypes.WinDLL("shcore")
        sh.GetDpiForMonitor.argtypes = [wintypes.HMONITOR, ctypes.c_int,
                                        ctypes.POINTER(ctypes.c_uint),
                                        ctypes.POINTER(ctypes.c_uint)]
        mon = u.MonitorFromPoint(wintypes.POINT(0, 0), 1)  # the primary one
        dx, dy = ctypes.c_uint(), ctypes.c_uint()
        if sh.GetDpiForMonitor(mon, 0, ctypes.byref(dx), ctypes.byref(dy)) == 0:
            return round(dx.value * 100 / 96)
    except Exception:
        pass
    try:
        return round(_user32.GetDpiForSystem() * 100 / 96)
    except Exception:
        return SUPPORTED_SCALING


def roblox_fullscreen(h):
    """True if the Roblox window covers the whole screen, None if unreadable.

    Fullscreen Roblox is a borderless window exactly the size of the screen.
    Windowed - even maximised - never is: a maximised window stops at the
    taskbar and hangs its border 8px past every edge. Compared against the
    primary screen, because that is the one every screenshot here is taken of.
    """
    r = wintypes.RECT()
    if not h or not _user32.GetWindowRect(h, ctypes.byref(r)):
        return None
    sw, sh = screen_size()
    return (r.left, r.top, r.right, r.bottom) == (0, 0, sw, sh)


def cursor_pos():
    p = wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(p))
    return int(p.x), int(p.y)


def grab_gray():
    return cv2.cvtColor(np.array(ImageGrab.grab()), cv2.COLOR_RGB2GRAY)


# ------------------------------------------------------------ detection ---
def load_cal():
    if not (os.path.exists(CAL_PATH) and os.path.exists(TEMPLATE_PATH)):
        return None, None
    try:
        with open(CAL_PATH, encoding="utf-8") as fh:
            cal = json.load(fh)
        tile = cv2.imread(TEMPLATE_PATH, cv2.IMREAD_GRAYSCALE)
        if tile is None:
            return None, None
        cal["_gl_tile"] = cv2.imread(GL_TEMPLATE_PATH, cv2.IMREAD_GRAYSCALE)
        return cal, tile
    except Exception:
        return None, None


# How far the grid extends from Random - about five tiles across and four
# down, with slack. Searching this whole area instead of one spot is what makes
# the macro survive the roster changing: a weekend event unlocked an extra
# weapon, and when it ended on the Monday everything shifted one place left,
# moving Grenade Launcher from row 2 column 1 to row 1 column 5. A stored
# offset pointed at Minigun.
GRID_RIGHT, GRID_DOWN, GRID_UP, GRID_LEFT = 820, 620, 90, 90


def find_launcher_in_grid(cal, random_pos, threshold):
    """Hunt the whole weapon grid for the launcher. (score, (x, y) or None)."""
    gl = cal.get("_gl_tile")
    if gl is None:
        return None, None
    try:
        shot = grab_gray()
    except Exception:
        return 0.0, None
    h, w = shot.shape
    th, tw = gl.shape
    x0 = max(0, random_pos[0] - GRID_LEFT)
    y0 = max(0, random_pos[1] - GRID_UP)
    x1 = min(w, random_pos[0] + GRID_RIGHT)
    y1 = min(h, random_pos[1] + GRID_DOWN)
    win = shot[y0:y1, x0:x1]
    if win.shape[0] < th or win.shape[1] < tw:
        return 0.0, None
    res = cv2.matchTemplate(win, gl, cv2.TM_CCOEFF_NORMED)
    _, score, _, loc = cv2.minMaxLoc(res)
    if score < threshold:
        return score, None
    return score, (x0 + loc[0] + tw // 2, y0 + loc[1] + th // 2)


def verify_launcher(cal, expect, threshold, search=30):
    """Is the Grenade Launcher really where we think? (score, (x, y) or None).

    Searches a small window AROUND the expected point rather than testing one
    pixel, so slight drift is corrected instead of fatal. The window is kept
    well under one tile: weapon art is mostly dark barrels, several tiles look
    alike in grayscale, and a wide search let the Grenade Launcher template
    match the Minigun sitting next to it.
    """
    gl = cal.get("_gl_tile")
    if gl is None:
        return None, None            # older calibration: nothing to check with
    shot = grab_gray()
    h, w = shot.shape
    th, tw = gl.shape
    x0 = max(0, expect[0] - tw // 2 - search)
    y0 = max(0, expect[1] - th // 2 - search)
    x1 = min(w, expect[0] + tw // 2 + search)
    y1 = min(h, expect[1] + th // 2 + search)
    win = shot[y0:y1, x0:x1]
    if win.shape[0] < th or win.shape[1] < tw:
        return 0.0, None
    res = cv2.matchTemplate(win, gl, cv2.TM_CCOEFF_NORMED)
    _, score, _, loc = cv2.minMaxLoc(res)
    if score < threshold:
        return score, None
    return score, (x0 + loc[0] + tw // 2, y0 + loc[1] + th // 2)


# Roblox has (at least) two of these dialogs and they are the SAME widget:
#   Disconnected     -> Leave / Reconnect   button at (992, 626), 175x36
#   Connection Failed -> Cancel / Retry     button at (1052, 626), 175x36
# Same size, same height on screen; the right-hand button is the one to press.
# Retry is dimmer than Reconnect and its background is the loading screen
# rather than black, which is why a white-only, near-black-only test caught
# just the first one.
#
# Measured across both dialogs, the weapon picker, the melee tab, in-game, and
# five real pick screenshots: these two together fire on the dialogs and on
# nothing else.
DIALOG_DARK = 60.0        # dimmed background behind a modal
DIALOG_LIGHT = 150        # the button is lighter than anything around it


def roblox_running():
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq RobloxPlayerBeta.exe"],
            capture_output=True, text=True, timeout=10,
            creationflags=0x08000000).stdout
        return "RobloxPlayerBeta" in out
    except Exception:
        return False


def relaunch_roblox(place_id, log=None):
    """Close Roblox and rejoin the place through the roblox:// handler.

    Retry on the Connection Failed dialog does not reconnect - pressing it just
    fails again - so when clicking the dialog has not helped, the only way back
    in is a fresh client.
    """
    log = log or (lambda m: None)
    log("closing Roblox")
    try:
        subprocess.run(["taskkill", "/F", "/IM", "RobloxPlayerBeta.exe"],
                       capture_output=True, timeout=15,
                       creationflags=0x08000000)
    except Exception as exc:
        log(f"  could not close it: {exc}")
    time.sleep(4.0)
    url = f"roblox://experiences/start?placeId={place_id}"
    log(f"launching {url}")
    try:
        os.startfile(url)
    except Exception as exc:
        log(f"  could not launch: {exc}")
        return False
    return True


def in_lobby():
    """True when the main hub is on screen.

    A POSITIVE test, which the start-up check badly needed. Waiting for the
    weapon picker and giving up cannot tell the hub from an FFA intermission -
    both are "in the game, no picker" - so starting during an intermission made
    the macro try to scroll a menu that was not there.

    The red "To Hub" button is the signal: a solid rounded rectangle of
    saturated red, low and left of centre. Checked against the hub, both
    spectate screens, all three weapon tabs, in-game, both connection dialogs
    and six gameplay screenshots - including one full of bright red characters.
    No false positives.
    """
    try:
        img = np.array(ImageGrab.grab())[:, :, ::-1]      # RGB -> BGR
    except Exception:
        return None
    h, w = img.shape[:2]
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    red = (cv2.inRange(hsv, (0, 140, 110), (10, 255, 255))
           | cv2.inRange(hsv, (170, 140, 110), (180, 255, 255)))
    nlab, _, stats, cent = cv2.connectedComponentsWithStats(red, 8)
    for i in range(1, nlab):
        x, y, bw, bh, area = stats[i]
        cx, cy = cent[i]
        if not (0.06 * w <= bw <= 0.20 * w and 0.04 * h <= bh <= 0.09 * h):
            continue
        if area < bw * bh * 0.75:
            continue
        if not (cy > 0.75 * h and 0.25 * w < cx < 0.55 * w):
            continue
        return (float(cx), float(cy))
    return None


def find_join_button():
    """Centre of the green "Join this game!" button while spectating, or None.

    Joining through the menu often lands in SPECTATE rather than in the match -
    a "Join this game!" prompt with a green play button, and no weapon picker.
    Clicking the menu again from there does nothing, because the menu is gone.

    Found by colour, so it needs no teaching. Rivals maps are full of green, but
    grass is not a small saturated rectangle low and centre of the screen -
    tested against both spectate screens, the picker, the melee tab, in-game,
    both connection dialogs and six real gameplay screenshots.

    The death screen's green RESPAWN button sits in the same spot and used to
    match too - once per death, which was 3,321 of 4,755 lines in one 10h log.
    WIDTH is what separates them: Join measured 110-112px, Respawn 192px (both
    on 1920x1080), so the cap is 0.08 of the screen, 153px. Respawn needs no
    handling of its own - tapping space, which the macro does anyway, is it.
    """
    try:
        img = np.array(ImageGrab.grab())[:, :, ::-1]      # RGB -> BGR
    except Exception:
        return None
    h, w = img.shape[:2]
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    # Lime only. Join measured hue 39-48; up to 85 also took in TEAL, and a
    # killer's pixel-art sniper skin (hue 85, 124x89) passed every other test
    # below - 41 false "Join prompt" lines in half an hour, and a real click.
    mask = cv2.inRange(hsv, (35, 120, 120), (62, 255, 255))
    red = (cv2.inRange(hsv, (0, 140, 90), (10, 255, 255))
           | cv2.inRange(hsv, (170, 140, 90), (180, 255, 255)))
    rn, _, rstats, rcent = cv2.connectedComponentsWithStats(red, 8)
    nlab, _, stats, cent = cv2.connectedComponentsWithStats(mask, 8)
    best = None
    for i in range(1, nlab):
        x, y, bw, bh, area = stats[i]
        cx, cy = cent[i]
        # 0.08, not wider: Respawn is 0.10 of the screen and must not count
        if not (0.03 * w <= bw <= 0.08 * w and 0.025 * h <= bh <= 0.09 * h):
            continue
        if area < bw * bh * 0.55:               # solid, not a scatter of leaves
            continue
        if not (0.55 * h < cy < 0.85 * h and 0.30 * w < cx < 0.75 * w):
            continue
        # A button's shape: Join is 2.07 wide for every 1 tall. Weapon art
        # that happens to be green is blockier (the sniper skin was 1.39).
        if not 1.6 <= bw / bh <= 2.6:
            continue
        # The white play arrow in the middle - 45% of the centre on both Join
        # shots, 0% on the sniper skin.
        core = img[y + bh // 4: y + 3 * bh // 4, x + bw // 3: x + 2 * bw // 3]
        if core.size == 0 or float((core.min(axis=2) > 200).mean()) < 0.15:
            continue
        # And its neighbour: spectate always puts the red leave button just
        # to the LEFT, level with it (49-56px gap on both shots).
        if not any(0 <= x - (rstats[j][0] + rstats[j][2]) <= 90
                   and abs(rcent[j][1] - cy) < bh * 0.5
                   and rstats[j][3] >= bh * 0.6 and rstats[j][2] >= bw * 0.5
                   for j in range(1, rn)):
            continue
        if best is None or area > best[2]:
            best = (float(cx), float(cy), area)
    return (best[0], best[1]) if best else None


def find_reconnect():
    """Centre of the Reconnect / Retry button, or None. Reads the screen only.

    Roblox draws this dialog itself, so it never moves: on every real one
    measured - Disconnected with two different error texts, and Connection
    Failed - the button is 175x36 centred on (1052, 626) of 1920x1080, under a
    neutral grey panel. The first version accepted any light button on a dark
    screen, and fired on the weapon picker over a dark map (a 200x47 tab at
    650, 340). Clicking that, then still seeing it 6s later, restarts Roblox
    and throws away the session - so this pins the size, place and panel.
    """
    try:
        img = np.array(ImageGrab.grab())[:, :, ::-1]      # RGB -> BGR
    except Exception:
        return None
    if float(img.mean()) > DIALOG_DARK:
        return None                                       # the game is running

    h, w = img.shape[:2]
    white = cv2.inRange(img, (DIALOG_LIGHT,) * 3, (255, 255, 255))
    nlab, _, stats, cent = cv2.connectedComponentsWithStats(white, 8)
    best = None
    for i in range(1, nlab):
        x, y, bw, bh, area = stats[i]
        # 175x36 is 0.091 x 0.033 of the screen
        if not (0.075 * w <= bw <= 0.11 * w and 0.026 * h <= bh <= 0.042 * h):
            continue
        if area < bw * bh * 0.55:           # a filled button, not an outline
            continue
        cx, cy = cent[i]
        # (1052, 626) is 0.548, 0.580 - with room for the left-hand button
        if not (0.44 * w < cx < 0.62 * w and 0.52 * h < cy < 0.66 * h):
            continue
        # the dialog's grey panel just above the button: neutral, not tinted.
        # Real ones measured B/G/R within 4 of each other; the picker's
        # "panel" was 25/49/81.
        panel = img[max(0, y - 80):max(0, y - 40), x:x + bw].reshape(-1, 3)
        if panel.size == 0:
            continue
        b, g, r = panel.mean(axis=0)
        if max(b, g, r) - min(b, g, r) > 15 or not 35 <= (b + g + r) / 3 <= 110:
            continue
        # Leave/Reconnect and Cancel/Retry both put the one we want on the
        # RIGHT, so take the rightmost if more than one qualifies
        if best is None or cx > best[0]:
            best = (float(cx), float(cy))
    return best


def save_event_shot(tag, pos=None):
    """Save the screen with `pos` circled, as events/<time>_<tag>.png.

    Returns the file name, or None. Never raises - a picture is not worth
    stopping the macro for.
    """
    try:
        os.makedirs(EVENT_DIR, exist_ok=True)
        img = np.array(ImageGrab.grab())[:, :, ::-1].copy()     # RGB -> BGR
        if pos:
            p = (int(pos[0]), int(pos[1]))
            cv2.circle(img, p, 34, (0, 0, 255), 3)
            cv2.drawMarker(img, p, (0, 0, 255), cv2.MARKER_CROSS, 22, 2)
        name = time.strftime("%m%d_%H%M%S") + f"_{tag}.png"
        cv2.imwrite(os.path.join(EVENT_DIR, name), img)
        trim_folder(EVENT_DIR, KEEP_EVENTS)
        return name
    except Exception:
        return None


def find_random(cal, tile, threshold, shot=None):
    """(score, (x, y) or None) for the Random tile's calibration point."""
    if shot is None:
        shot = grab_gray()
    res = cv2.matchTemplate(shot, tile, cv2.TM_CCOEFF_NORMED)
    _, score, _, loc = cv2.minMaxLoc(res)
    if score < threshold:
        return score, None
    ax, ay = cal["anchor_offset"]
    return score, (loc[0] + ax, loc[1] + ay)


# ------------------------------------------------------------------ app ---
# ------------------------------------------------------------ 1.0.3 look ---
# Quiet Premium: one type family, a strict size scale, and depth from layered
# panels plus 1px hairlines - Tk has no blur or shadow, so that is all there is.
INSET, HAIRLINE, SHEEN = "#0a1019", "#172538", "#28405c"
ACCENT_DIM, GREEN_DIM = "#0f2a3d", "#0f2a1d"
AMBER_DIM, RED_DIM = "#2b230f", "#2a1418"
KEYCAP_LINE = "#2c4058"


def blend(c1, c2, t):
    """Mix two #rrggbb colours: t=0 is c1, t=1 is c2. Tk has no alpha."""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def span(secs):
    """'2h 14m' for a duration, '14m' under an hour, '40s' under a minute."""
    s = int(max(0, secs))
    if s >= 3600:
        return f"{s // 3600}h {s % 3600 // 60:02d}m"
    if s >= 60:
        return f"{s // 60}m"
    return f"{s}s"


def draw_gem(canvas, x, y, size, dim=False):
    """The faceted gem from the icon, in four polygons (a 20x20 design)."""
    k = size / 20.0
    faces = (((4, 3, 16, 3, 20, 8, 10, 19, 0, 8), "#1f7fbf"),
             ((4, 3, 16, 3, 13, 8, 7, 8), "#a5e4ff"),
             ((0, 8, 7, 8, 10, 19), "#5ecbff"),
             ((13, 8, 20, 8, 10, 19), "#2b93d1"))
    for pts, colour in faces:
        if dim:
            colour = blend(colour, PANEL, 0.55)
        canvas.create_polygon([x + p * k if i % 2 == 0 else y + p * k
                               for i, p in enumerate(pts)],
                              fill=colour, outline="")



# ------------------------------------------------------------- updates ---
# One read-only request to GitHub's public API: "what is the newest release?".
# It sends nothing about the user beyond what any web request carries, and it
# can be switched off in Settings. Everything else in the app stays offline.
REPO = "Ariliux/GlassMacro"
RELEASES_URL = f"https://github.com/{REPO}/releases/latest"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
UPDATE_EVERY = 12 * 3600       # runs last for days, so check again now and then
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")


def version_tuple(v):
    """'v1.0.3' -> (1, 0, 3); anything without a number -> ().

    Only the first three numbers count, so a tag like 'v1.0.4-x64' can't look
    newer than 1.0.4 itself."""
    return tuple(int(n) for n in re.findall(r"\d+", str(v))[:3])


def latest_release(timeout=6.0):
    """(version, page url) of the newest published release, or None.

    Never raises - no network, a GitHub outage or a rate limit all just mean
    "no news", and the app carries on exactly as before.
    """
    import urllib.request
    req = urllib.request.Request(LATEST_API, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{APP_NAME}/{APP_VER}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        tag = str(data.get("tag_name") or "")
        if data.get("draft") or data.get("prerelease") or not version_tuple(tag):
            return None
        url = str(data.get("html_url") or "")
        if not url.startswith(f"https://github.com/{REPO}/"):
            url = RELEASES_URL           # only ever open our own release page
        return tag.lstrip("vV"), url
    except Exception:
        return None


def load_settings():
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_settings(data):
    """Write settings.json in one piece: a half-written file can't happen."""
    try:
        tmp = SETTINGS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, SETTINGS_PATH)
    except Exception:
        pass


class GlassButton(ctk.CTkFrame):
    """The big Start / Stop button: a glyph, a word and an F8 keycap.

    A CTkButton holds one string, and 'Start    F8' spaced out by hand read
    like a placeholder. This is a frame that behaves like a button. Its
    configure() takes the options the app sets and passes anything else (the
    bg_color a parent frame pushes down) straight through.
    """

    def __init__(self, master, command, font, glyph_font, key_font):
        super().__init__(master, height=46, corner_radius=12, border_width=1,
                         fg_color=ACCENT, border_color=ACCENT)
        self._command = command
        self._fill, self._hover = ACCENT, ACCENT_SOFT
        self._hovering = False
        self._row = ctk.CTkFrame(self, fg_color="transparent")
        self._row.place(relx=0.5, rely=0.5, anchor="center")
        self._glyph = ctk.CTkLabel(self._row, text="▶", font=glyph_font,
                                   text_color=INK, height=20)
        self._glyph.pack(side="left", padx=(0, 8))
        self._text = ctk.CTkLabel(self._row, text="Start", font=font,
                                  text_color=INK, height=20)
        self._text.pack(side="left")
        self._key = ctk.CTkFrame(self._row, corner_radius=6, border_width=1,
                                 fg_color=PANEL, border_color=KEYCAP_LINE)
        self._key.pack(side="left", padx=(12, 0))
        self._keytext = ctk.CTkLabel(self._key, text="F8", font=key_font,
                                     text_color=SUBTLE, height=16)
        self._keytext.pack(padx=7, pady=2)
        for w in (self, self._row, self._glyph, self._text, self._key,
                  self._keytext):
            w.bind("<Enter>", self._enter, add="+")
            w.bind("<Leave>", self._leave, add="+")
            w.bind("<ButtonRelease-1>", self._click, add="+")
            try:
                w.configure(cursor="hand2")
            except Exception:
                pass

    def _paint_fill(self, colour):
        super().configure(fg_color=colour)
        for w in (self._row, self._glyph, self._text, self._key):
            try:
                w.configure(bg_color=colour)
            except Exception:
                pass

    def _inside(self):
        x, y = self.winfo_pointerxy()
        return (self.winfo_rootx() <= x < self.winfo_rootx() + self.winfo_width()
                and self.winfo_rooty() <= y < self.winfo_rooty()
                + self.winfo_height())

    def _enter(self, _e=None):
        if not self._hovering:
            self._hovering = True
            self._paint_fill(self._hover)

    def _leave(self, _e=None):
        # moving from the frame onto its own label fires Leave too
        if self._hovering and not self._inside():
            self._hovering = False
            self._paint_fill(self._fill)

    def _click(self, _e=None):
        if self._inside() and self._command:
            self._command()

    def configure(self, **kw):
        mine = {}
        for k in ("text", "glyph", "fg_color", "hover_color", "text_color",
                  "glyph_color", "border_color", "key_fg", "key_border",
                  "key_text"):
            if k in kw:
                mine[k] = kw.pop(k)
        if kw:
            super().configure(**kw)
        if "text" in mine:
            self._text.configure(text=mine["text"])
        if "glyph" in mine:
            self._glyph.configure(text=mine["glyph"])
        if "text_color" in mine:
            self._text.configure(text_color=mine["text_color"])
        if "glyph_color" in mine:
            self._glyph.configure(text_color=mine["glyph_color"])
        if "border_color" in mine:
            super().configure(border_color=mine["border_color"])
        if "key_fg" in mine:
            self._key.configure(fg_color=mine["key_fg"])
            self._keytext.configure(bg_color=mine["key_fg"])
        if "key_border" in mine:
            self._key.configure(border_color=mine["key_border"])
        if "key_text" in mine:
            self._keytext.configure(text_color=mine["key_text"])
        if "hover_color" in mine:
            self._hover = mine["hover_color"]
        if "fg_color" in mine:
            self._fill = mine["fg_color"]
        if "fg_color" in mine or "hover_color" in mine:
            self._paint_fill(self._hover if self._hovering else self._fill)

    config = configure


class GlassMacro(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        self.title(APP_NAME)
        self._fit_to_screen()
        self.configure(fg_color=BG)
        self._set_icon()
        self.after(50, self._style_titlebar)

        # session stats shown on the status card
        self.session_start = None
        self.n_picks = 0
        self.n_joins = 0
        self._fs = self._fresh_fs()

        self.cal, self.tile = load_cal()
        self.settings = load_settings()
        self.running = False
        self.watching = False
        self.calibrating = False
        self._q = queue.Queue()

        self._build()
        self._drain()
        threading.Thread(target=self._update_worker, daemon=True).start()
        keyboard.add_hotkey("f8", self._hotkey)
        self.log(f"--- {APP_NAME} v{APP_VER} opened ---")
        self._warn_display()
        self.protocol("WM_DELETE_WINDOW", self._close)

    # -- cross-thread UI. tkinter's after() is not thread safe, so workers
    # -- post callables here and the main thread runs them.
    def _ui(self, fn):
        try:
            self._q.put_nowait(fn)
        except Exception:
            pass

    def _drain(self):
        for _ in range(40):
            try:
                self._q.get_nowait()()
            except queue.Empty:
                break
            except Exception:
                pass
        self.after(50, self._drain)

    # ---------------------------------------------------------------- ui --
    def _set_icon(self):
        ico = resource("glassmacro.ico")
        if not os.path.exists(ico):
            return
        try:
            self.iconbitmap(ico)
            # CustomTkinter puts its own icon back shortly after start-up
            self.after(250, lambda: self.iconbitmap(ico))
        except Exception:
            pass

    def _style_titlebar(self):
        """Colour the Windows title bar to match, so it reads as one piece.

        Windows 11 only; older versions ignore the attributes and keep the
        normal dark bar.
        """
        def ref(hex_colour):                      # COLORREF is 0x00BBGGRR
            r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
            return ctypes.c_int(b << 16 | g << 8 | r)
        try:
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            dwm = ctypes.windll.dwmapi.DwmSetWindowAttribute
            for attr, val in ((20, ctypes.c_int(1)),     # dark mode
                              (35, ref(BG)),             # caption
                              (36, ref(SUBTLE)),         # caption text
                              (34, ref(LINE))):          # border
                dwm(hwnd, attr, ctypes.byref(val), ctypes.sizeof(val))
        except Exception:
            pass

    def _fit_to_screen(self, want_h=820, min_h=720):
        """540x820 where it fits; shorter where it doesn't.

        A 1080p laptop at the usual 150% scaling has only about 688 logical
        pixels above the taskbar, so a fixed 820 hung off the bottom of the
        screen. CustomTkinter scales the size but not the position, so the
        position is worked out in real pixels.
        """
        try:
            s = ctk.ScalingTracker.get_window_scaling(self)
        except Exception:
            s = 1.0
        try:
            r = wintypes.RECT()
            ctypes.windll.user32.SystemParametersInfoW(0x30, 0,
                                                       ctypes.byref(r), 0)
            left, top, w, h = r.left, r.top, r.right - r.left, r.bottom - r.top
        except Exception:
            left, top, (w, h) = 0, 0, screen_size()
        room = int(h / s) - 48               # title bar and a little air
        height = max(560, min(want_h, room))
        self.minsize(500, max(560, min(min_h, room)))
        x = left + max(0, (w - round(540 * s)) // 2)
        y = top + max(0, (h - round((height + 32) * s)) // 2)
        self.geometry(f"540x{height}+{x}+{y}")

    # ------------------------------------------------------ fonts & sizes --
    def _init_fonts(self):
        """Segoe UI Variable on Windows 11, plain Segoe UI on Windows 10.

        Tk only knows normal and bold, so semibold has to be its own family.
        """
        try:
            fams = set(tkfont.families(self))
        except Exception:
            fams = set()
        if "Segoe UI Variable Text" in fams:
            self._fam = "Segoe UI Variable Text"
        else:
            self._fam = "Segoe UI"
        if "Segoe UI Variable Text Semibold" in fams:
            self._fam_semi = "Segoe UI Variable Text Semibold"
        elif "Segoe UI Semibold" in fams:
            self._fam_semi = "Segoe UI Semibold"
        else:
            self._fam_semi = self._fam
        self._fam_sym = "Segoe UI Symbol" if "Segoe UI Symbol" in fams \
            else self._fam
        self._fonts = {}

    def F(self, size, semi=False, bold=False, family=None):
        key = (size, semi, bold, family)
        if key not in self._fonts:
            fam = family or (self._fam_semi if semi else self._fam)
            self._fonts[key] = ctk.CTkFont(family=fam, size=size,
                                           weight="bold" if bold else "normal")
        return self._fonts[key]

    def _px(self, n):
        """Scale a raw Tk (canvas / text tag) size for Windows display
        scaling. CustomTkinter scales its own widgets; plain Tk ones it
        does not."""
        try:
            return max(1, round(n * ctk.ScalingTracker.get_widget_scaling(self)))
        except Exception:
            return n

    def _tkfont(self, size, semi=False, family=None):
        fam = family or (self._fam_semi if semi else self._fam)
        return (fam, -self._px(size))

    # ------------------------------------------------------------- build --
    def _build(self):
        self._init_fonts()
        self._setup_active = False          # the weapon guide is mid-flow
        self._guide = {"step": 0, "done": set(), "t0": 0.0, "msg": "",
                       "msg_colour": SUBTLE, "bad": 0}
        self._guide_visible = None
        self._run_t0 = None                 # start of the current / last run
        self._was_running = False
        self._last_run = None
        self._dot_colour = MUTED
        self._run_mode = "IDLE"
        self._state_title = ""
        self._state_since = 0.0
        self._pulse_t = 0.0
        self._feed_top = None
        self._feed_rows = 0
        self._tab = "activity"
        self._full_log = False
        self._slider_guard = False

        sw, sh = screen_size()
        self._screen_ok = (sw, sh) == SUPPORTED_SCREEN
        self._scaling = display_scaling()

        # ---- header: gem, wordmark, version, screen ----
        head = ctk.CTkFrame(self, fg_color="transparent", height=44)
        head.pack(fill="x", padx=20, pady=(8, 4))
        gem = tk.Canvas(head, width=self._px(20), height=self._px(20), bg=BG,
                        highlightthickness=0, bd=0)
        draw_gem(gem, 0, 0, self._px(20))
        gem.pack(side="left", pady=10)
        ctk.CTkLabel(head, text="Glass", text_color=ACCENT,
                     font=self.F(15, semi=True)).pack(side="left",
                                                      padx=(8, 0))
        ctk.CTkLabel(head, text="Macro", text_color=TEXT,
                     font=self.F(15, semi=True)).pack(side="left")
        ctk.CTkLabel(head, text=APP_VER, text_color=MUTED,
                     font=self.F(11)).pack(side="left", padx=(6, 0),
                                           pady=(3, 0))
        self.lbl_res = ctk.CTkLabel(head, font=self.F(11), text="")
        self._paint_res(sw, sh, self._scaling)
        self.lbl_res.pack(side="right")
        self.lnk_update = ctk.CTkLabel(head, text="", text_color=ACCENT,
                                       font=self.F(11, semi=True),
                                       cursor="hand2")
        self._update_url = RELEASES_URL
        self.lnk_update.bind("<Button-1>", lambda _e: self._open_update())

        self.main = ctk.CTkFrame(self, fg_color="transparent")
        self.main.pack(fill="both", expand=True, padx=20, pady=(0, 16))

        self._build_hero()
        self._build_guide()
        self._build_weapons()
        self._build_tabs()
        self._build_activity()
        self._build_settings()

        self._show_tab("activity")
        self._show_cal()
        self._show_ffa()
        self._show_playtime(None)
        self._tick()
        self._animate()

    # ---- the status card ----
    def _card(self, parent, **kw):
        opts = dict(fg_color=CARD, corner_radius=16, border_width=1,
                    border_color=LINE)
        opts.update(kw)
        return ctk.CTkFrame(parent, **opts)

    def _sheen(self, card):
        """A 1px lighter line along the card's top edge - the glass edge."""
        # plain Tk: CustomTkinter will not size a widget from place()
        line = tk.Frame(card, height=1, bg=SHEEN, bd=0, highlightthickness=0)
        line.place(x=self._px(18), y=self._px(1), relwidth=1.0,
                   width=-self._px(36))

    def _build_hero(self):
        self.hero = self._card(self.main)
        self._sheen(self.hero)
        body = ctk.CTkFrame(self.hero, fg_color="transparent")
        body.pack(fill="x", padx=18, pady=18)

        top = ctk.CTkFrame(body, fg_color="transparent")
        top.pack(fill="x")
        d = self._px(16)
        self.dotc = tk.Canvas(top, width=d, height=d, bg=CARD,
                              highlightthickness=0, bd=0)
        self.dotc.pack(side="left", padx=(0, 10))
        c = d / 2
        self._halo = self.dotc.create_oval(c, c, c, c, fill=CARD, outline="")
        r = self._px(4)
        self._core = self.dotc.create_oval(c - r, c - r, c + r, c + r,
                                           fill=MUTED, outline="")
        self.lbl_state = ctk.CTkLabel(top, text="Ready", text_color=TEXT,
                                      font=self.F(18, semi=True), anchor="w",
                                      height=24)
        self.lbl_state.pack(side="left")
        self.lbl_since = ctk.CTkLabel(top, text="", text_color=MUTED,
                                      font=self.F(11), height=24)
        self.lbl_since.pack(side="right")
        self.lbl_detail = ctk.CTkLabel(body, text="", text_color=SUBTLE,
                                       font=self.F(12), anchor="w",
                                       justify="left", wraplength=420,
                                       height=18)
        self.lbl_detail.pack(fill="x", padx=(26, 0), pady=(2, 0))
        self._rewrap_on(body, (self.lbl_detail,), indent=26)

        # stat strip: playtime gets the room, the counters sit beside it
        strip = ctk.CTkFrame(body, fg_color=INSET, corner_radius=12,
                             border_width=1, border_color=HAIRLINE)
        strip.pack(fill="x", pady=(16, 0))
        strip.grid_columnconfigure(0, weight=2, uniform="s")
        strip.grid_columnconfigure(2, weight=1, uniform="s")
        strip.grid_columnconfigure(4, weight=1, uniform="s")

        play = ctk.CTkFrame(strip, fg_color="transparent")
        play.grid(row=0, column=0, sticky="nsew", padx=(16, 12), pady=12)
        self.lbl_play_cap = ctk.CTkLabel(play, text="PLAYTIME",
                                         text_color=MUTED, height=14,
                                         font=self.F(11, semi=True),
                                         anchor="w")
        self.lbl_play_cap.pack(fill="x")
        num = ctk.CTkFrame(play, fg_color="transparent")
        num.pack(fill="x", pady=(2, 0))
        self._num = []
        for i, (size, colour) in enumerate(((32, TEXT), (17, SUBTLE),
                                            (32, TEXT), (17, SUBTLE))):
            lbl = ctk.CTkLabel(num, text="", text_color=colour, height=38,
                               font=self.F(size, bold=size > 20),
                               anchor="sw")
            lbl.pack(side="left", anchor="s",
                     padx=(6 if i == 2 else 0, 0),
                     pady=(0, 5 if size < 20 else 0))
            self._num.append(lbl)
        self.bar_hour = ctk.CTkProgressBar(play, width=10, height=2,
                                           corner_radius=1,
                                           progress_color=ACCENT,
                                           fg_color=HAIRLINE)
        self.bar_hour.pack(fill="x", pady=(8, 0))
        self.bar_hour.set(0)
        self.lbl_play_sub = ctk.CTkLabel(play, text="", text_color=MUTED,
                                         font=self.F(11), anchor="w",
                                         height=16)
        self.lbl_play_sub.pack(fill="x", pady=(4, 0))

        for col in (1, 3):
            ctk.CTkFrame(strip, width=1, height=1, fg_color=HAIRLINE,
                         corner_radius=0
                         ).grid(row=0, column=col, sticky="ns", pady=1)
        self.val_picks = self._counter(strip, 2, "LOADOUTS")
        self.val_joins = self._counter(strip, 4, "REJOINS")

        self.btn_run = GlassButton(body, self.toggle_run,
                                   font=self.F(14, semi=True),
                                   glyph_font=self.F(11, family=self._fam_sym),
                                   key_font=self.F(11, semi=True,
                                                   family="Consolas"))
        self.btn_run.pack(fill="x", pady=(16, 0))

    def _counter(self, parent, col, caption):
        f = ctk.CTkFrame(parent, fg_color="transparent")
        f.grid(row=0, column=col, sticky="nw", padx=16, pady=12)
        ctk.CTkLabel(f, text=caption, text_color=MUTED, height=14,
                     font=self.F(11, semi=True), anchor="w").pack(fill="x")
        v = ctk.CTkLabel(f, text="0", text_color=TEXT, height=30,
                         font=self.F(22, semi=True), anchor="w")
        v.pack(fill="x", pady=(6, 0))
        return v

    # ---- the weapon setup guide (shown instead of the status card) ----
    STEPS = (
        ("The Random tile", "Top-left of the weapon grid."),
        ("The Grenade Launcher",
         "Hover the Grenade Launcher button itself."),
        ("Your first loadout slot", "The first slot along the top."),
    )

    def _build_guide(self):
        self.guide = self._card(self.main)
        self._sheen(self.guide)
        body = ctk.CTkFrame(self.guide, fg_color="transparent")
        body.pack(fill="x", padx=18, pady=18)

        top = ctk.CTkFrame(body, fg_color="transparent")
        top.pack(fill="x")
        ctk.CTkLabel(top, text="Set up your weapons", text_color=TEXT,
                     font=self.F(18, semi=True), height=24).pack(side="left")
        ctk.CTkLabel(top, text="one time · about 30 seconds",
                     text_color=MUTED, font=self.F(11), height=24
                     ).pack(side="right")
        self.lbl_intro = ctk.CTkLabel(
            body, text=("Open the weapon picker in Rivals, hover each thing "
                        "below and press F8. Don't click."),
            text_color=SUBTLE, font=self.F(12), anchor="w", justify="left",
            wraplength=470, height=18)
        self.lbl_intro.pack(fill="x", pady=(4, 0))
        self._guide_body = body

        # built once and shown or hidden as things change - fixing Windows
        # scaling with the app open must make the box go away
        self._warn_box = ctk.CTkFrame(body, fg_color=AMBER_DIM,
                                      corner_radius=10)
        self.lbl_warn = ctk.CTkLabel(self._warn_box, text="", text_color=AMBER,
                                     font=self.F(12), anchor="w",
                                     justify="left", wraplength=440,
                                     height=18)
        self.lbl_warn.pack(fill="x", padx=12, pady=8)
        self._rewrap_on(self._warn_box, (self.lbl_warn,), indent=24)

        prog = ctk.CTkFrame(body, fg_color="transparent")
        prog.pack(fill="x", pady=(14, 0))
        self._guide_prog = prog
        self._paint_guide_warning()
        self._segs = []
        for i in range(3):
            prog.grid_columnconfigure(i, weight=1, uniform="p")
            seg = ctk.CTkFrame(prog, width=1, height=4, corner_radius=2,
                               fg_color=LINE)
            seg.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 3,
                                                         0 if i == 2 else 3))
            self._segs.append(seg)

        self._step_rows = []
        for i, (title, note) in enumerate(self.STEPS):
            row = ctk.CTkFrame(body, corner_radius=12, border_width=1,
                               fg_color="transparent", border_color=HAIRLINE)
            row.pack(fill="x", pady=(10 if i == 0 else 8, 0))
            inner = ctk.CTkFrame(row, fg_color="transparent")
            inner.pack(fill="x", padx=12, pady=10)
            # a real circle - a CTkLabel with rounded corners came out as a
            # pill once its text padding was added
            badge = tk.Canvas(inner, width=self._px(26), height=self._px(26),
                              bg=CARD, highlightthickness=0, bd=0)
            badge.pack(side="left", anchor="n")
            txt = ctk.CTkFrame(inner, fg_color="transparent")
            txt.pack(side="left", fill="x", expand=True, padx=(10, 0),
                     anchor="n", pady=(3, 0))
            t = ctk.CTkLabel(txt, text=title, text_color=TEXT, height=18,
                             font=self.F(13, semi=True), anchor="w")
            t.pack(fill="x")
            n = ctk.CTkLabel(txt, text=note, text_color=SUBTLE, height=16,
                             font=self.F(11), anchor="w", justify="left",
                             wraplength=230)
            n.pack(fill="x")
            pic = tk.Canvas(inner, width=self._px(170), height=self._px(92),
                            bg=CARD, highlightthickness=0, bd=0)
            self._draw_picker(pic, i, CARD)
            self._step_rows.append({"row": row, "inner": inner,
                                    "badge": badge, "title": t, "note": n,
                                    "pic": pic, "txt": txt})

        self.lbl_guide = ctk.CTkLabel(body, text="", text_color=SUBTLE,
                                      font=self.F(12), anchor="w",
                                      justify="left", wraplength=440,
                                      height=18)
        self.lbl_guide.pack(fill="x", pady=(12, 0))
        self.btn_cal = ctk.CTkButton(
            body, text="I'm on the weapon picker · start setup",
            height=46, corner_radius=12, fg_color=ACCENT,
            hover_color=ACCENT_SOFT, text_color=INK,
            text_color_disabled=SUBTLE, font=self.F(14, semi=True),
            command=self.calibrate)
        self.btn_cal.pack(fill="x", pady=(10, 0))
        self._rewrap_on(self._guide_body, (self.lbl_intro, self.lbl_guide))

    def _rewrap(self, width_px, labels, indent=0):
        w = width_px / max(0.5, ctk.ScalingTracker.get_widget_scaling(self))
        for lbl in labels:
            lbl.configure(wraplength=max(120, int(w - indent - 6)))

    def _rewrap_on(self, frame, labels, indent=0):
        """Wrap text to the width it really has. A fixed wraplength wider
        than the label clips the end off instead of wrapping - at the 500px
        minimum that cut "Don't click." down to "Do". Bound on the frame's
        canvas, never on a label, whose width follows its own wrapping."""
        frame.bind("<Configure>",
                   lambda e: self._rewrap(e.width, labels, indent), add="+")

    def _draw_picker(self, cv, step, bg):
        """A tiny weapon picker with the thing to hover outlined.

        A drawing, not a screenshot - it only has to show WHERE, and it cannot
        go out of date the way a picture of the real menu would.
        """
        cv.delete("all")
        cv.configure(bg=bg)
        p = self._px
        tile, bar, slot = "#22314a", "#2f4260", "#22314a"
        # the loadout slots along the top
        for i in range(4):
            x = p(8) + i * p(22)
            cv.create_rectangle(x, p(6), x + p(16), p(18), fill=slot,
                                outline="")
        # the weapon grid, each tile with its name bar under it
        for r in range(2):
            for c in range(5):
                x = p(8) + c * p(31)
                y = p(28) + r * p(32)
                cv.create_rectangle(x, y, x + p(25), y + p(20), fill=tile,
                                    outline="")
                cv.create_rectangle(x, y + p(23), x + p(25), y + p(27),
                                    fill=bar, outline="")
        if step == 0:                            # Random: top-left tile
            box = (p(6), p(26), p(35), p(50))
        elif step == 1:                          # the launcher's button
            x = p(8) + 2 * p(31)
            box = (x - p(2), p(26), x + p(27), p(28) + p(29))
        else:                                    # first loadout slot
            box = (p(6), p(4), p(26), p(20))
        cv.create_rectangle(*box, outline=ACCENT, width=p(2))
        bx, by = box[2] + p(4), box[1] - p(2)
        bx = min(bx, p(170) - p(16))
        by = max(by, p(2))
        cv.create_oval(bx, by, bx + p(14), by + p(14), fill=ACCENT,
                       outline="")
        cv.create_text(bx + p(7), by + p(7), text=str(step + 1), fill=INK,
                       font=self._tkfont(9, semi=True))

    # ---- weapons row (once set up) ----
    def _build_weapons(self):
        self.weap = self._card(self.main, corner_radius=14)
        inner = ctk.CTkFrame(self.weap, fg_color="transparent")
        inner.pack(fill="x", padx=14, pady=12)
        self.weap_tile = ctk.CTkLabel(inner, text="✓", width=30,
                                      height=30, corner_radius=9,
                                      fg_color=GREEN_DIM, text_color=GREEN,
                                      font=self.F(13, family=self._fam_sym))
        self.weap_tile.pack(side="left")
        txt = ctk.CTkFrame(inner, fg_color="transparent")
        txt.pack(side="left", fill="x", expand=True, padx=(12, 0))
        self.lbl_weap = ctk.CTkLabel(txt, text="Weapons ready", height=18,
                                     text_color=TEXT, anchor="w",
                                     font=self.F(13, semi=True))
        self.lbl_weap.pack(fill="x")
        self.lbl_cal = ctk.CTkLabel(txt, text="", text_color=SUBTLE, height=16,
                                    font=self.F(11), anchor="w")
        self.lbl_cal.pack(fill="x")
        self.btn_redo = ctk.CTkButton(inner, text="Redo setup", width=92,
                                      height=30, corner_radius=10,
                                      fg_color="transparent",
                                      hover_color=CARD_HI, text_color=SUBTLE,
                                      font=self.F(12), command=self.calibrate)
        self.btn_redo.pack(side="right")

    # ---- tabs ----
    def _build_tabs(self):
        self.tabbar = ctk.CTkFrame(self.main, fg_color="transparent")
        self.tabbar.pack(fill="x", pady=(20, 0))
        row = ctk.CTkFrame(self.tabbar, fg_color="transparent")
        row.pack(fill="x", padx=4)
        self._tab_labels = {}
        for key, text in (("activity", "Activity"), ("settings", "Settings")):
            col = ctk.CTkFrame(row, fg_color="transparent")
            col.pack(side="left", padx=(0, 22))
            lbl = ctk.CTkLabel(col, text=text, font=self.F(13, semi=True),
                               text_color=MUTED, height=20, cursor="hand2")
            lbl.pack()
            under = ctk.CTkFrame(col, width=1, height=2, corner_radius=1,
                                 fg_color="transparent")
            under.pack(fill="x", pady=(6, 0))
            lbl.bind("<Button-1>", lambda _e, k=key: self._show_tab(k))
            self._tab_labels[key] = (lbl, under)
        self.lnk_log = ctk.CTkLabel(row, text="Full log ›",
                                    text_color=MUTED, font=self.F(11),
                                    height=20, cursor="hand2")
        self.lnk_log.pack(side="right", anchor="n")
        self.lnk_log.bind("<Button-1>", lambda _e: self._toggle_full_log())
        ctk.CTkFrame(self.tabbar, height=1, fg_color=HAIRLINE,
                     corner_radius=0).pack(fill="x", padx=4)

    def _show_tab(self, key):
        self._tab = key
        for k, (lbl, under) in self._tab_labels.items():
            on = k == key
            lbl.configure(text_color=TEXT if on else MUTED)
            under.configure(fg_color=ACCENT if on else "transparent")
        if key == "activity":
            self.set_page.pack_forget()
            self.act_page.pack(fill="both", expand=True, pady=(12, 0))
            self.lnk_log.pack(side="right", anchor="n")
        else:
            self.act_page.pack_forget()
            self.lnk_log.pack_forget()
            self.set_page.pack(fill="both", expand=True, pady=(8, 0))

    # ---- activity: a readable feed, with the raw log one click away ----
    def _build_activity(self):
        self.act_page = ctk.CTkFrame(self.main, fg_color="transparent")
        # packed first, from the bottom, so a short window squeezes the feed
        # rather than pushing this line off the end
        self.lbl_foot = ctk.CTkLabel(self.act_page,
                                     text="Newest first · every detail "
                                          "is in Full log",
                                     text_color=MUTED, font=self.F(11),
                                     height=16)
        self.lbl_foot.pack(side="bottom", pady=(8, 0))
        box = ctk.CTkFrame(self.act_page, fg_color=PANEL, corner_radius=14,
                           border_width=1, border_color=LINE)
        box.pack(fill="both", expand=True)
        self._act_box = box

        self.feed = ctk.CTkTextbox(box, fg_color=PANEL, text_color=TEXT,
                                   border_width=0, wrap="none",
                                   font=self.F(13), activate_scrollbars=True,
                                   scrollbar_button_color=LINE,
                                   scrollbar_button_hover_color=MUTED)
        self.feed.pack(fill="both", expand=True, padx=8, pady=6)
        tb = self.feed._textbox
        tb.configure(spacing1=self._px(7), spacing3=self._px(7),
                     tabs=(self._px(46), self._px(68)), cursor="arrow")
        tb.tag_configure("ts", foreground=MUTED,
                         font=self._tkfont(11, family="Consolas"))
        for name, colour in (("g_GREEN", GREEN), ("g_ACCENT", ACCENT),
                             ("g_AMBER", AMBER), ("g_RED", RED),
                             ("g_SUBTLE", SUBTLE)):
            tb.tag_configure(name, foreground=colour,
                             font=self._tkfont(11, family=self._fam_sym))
        tb.tag_configure("title", foreground=TEXT, font=self._tkfont(13))
        tb.tag_configure("sub", foreground=SUBTLE, font=self._tkfont(11))
        tb.tag_configure("chip", foreground=ACCENT_SOFT,
                         font=self._tkfont(11, semi=True))
        self.feed.configure(state="disabled")

        # empty state, laid over the feed until the first event
        self.empty = ctk.CTkFrame(box, fg_color=PANEL)
        self._empty_gem = tk.Canvas(self.empty, width=self._px(40),
                                    height=self._px(40), bg=PANEL,
                                    highlightthickness=0, bd=0)
        draw_gem(self._empty_gem, 0, 0, self._px(40), dim=True)
        self._empty_gem.pack(pady=(0, 10))
        self._empty_title = ctk.CTkLabel(self.empty, text="Nothing yet",
                                         text_color=TEXT, height=18,
                                         font=self.F(13, semi=True))
        self._empty_title.pack()
        ctk.CTkLabel(self.empty, text="Press Start with Rivals open. Events "
                     "show up here.", text_color=SUBTLE, font=self.F(11),
                     height=16).pack(pady=(2, 0))
        self.empty.place(relx=0.5, rely=0.46, anchor="center")
        box.bind("<Configure>", self._fit_empty, add="+")

        # the raw log - every line, timestamped, for bug reports
        self.txt = ctk.CTkTextbox(box, fg_color=PANEL, text_color="#c3cfdd",
                                  border_width=0, wrap="word",
                                  font=ctk.CTkFont(family="Consolas", size=11),
                                  scrollbar_button_color=LINE,
                                  scrollbar_button_hover_color=MUTED)
        self.txt.tag_config("ts", foreground=MUTED)
        self.txt.tag_config("top", foreground="#d5e0ec")
        self.txt.tag_config("sub", foreground=SUBTLE)
        self.txt.configure(state="disabled")

    def _fit_empty(self, e):
        """In a short box (setup guide showing, small window) the gem would
        overlap the border - keep just the words."""
        small = e.height < self._px(150)
        shown = bool(self._empty_gem.winfo_manager())
        if small and shown:
            self._empty_gem.pack_forget()
        elif not small and not shown:
            self._empty_gem.pack(pady=(0, 10), before=self._empty_title)

    def _toggle_full_log(self):
        self._full_log = not self._full_log
        if self._full_log:
            self.feed.pack_forget()
            self.empty.place_forget()
            self.txt.pack(fill="both", expand=True, padx=8, pady=6)
            self.txt.see("end")
            self.lnk_log.configure(text="‹ Activity")
            self.lbl_foot.configure(text="Every line, oldest first · "
                                         "also saved to log.txt")
        else:
            self.txt.pack_forget()
            self.feed.pack(fill="both", expand=True, padx=8, pady=6)
            if not self._feed_rows:
                self.empty.place(relx=0.5, rely=0.46, anchor="center")
            self.lnk_log.configure(text="Full log ›")
            self.lbl_foot.configure(text="Newest first · every detail "
                                         "is in Full log")

    # ---- settings: grouped cards ----
    def _build_settings(self):
        self.set_page = ctk.CTkScrollableFrame(
            self.main, fg_color="transparent", scrollbar_button_color=LINE,
            scrollbar_button_hover_color=MUTED)
        sp = self.set_page

        self._group(sp, "WHILE IT RUNS")
        card = self._card(sp, corner_radius=14)
        card.pack(fill="x")
        self.sw_fullscreen = self._switch_row(
            card, "Keep Roblox fullscreen",
            "Presses F11 if Roblox ends up windowed.", first=True)
        self.sw_reconnect = self._switch_row(
            card, "Reconnect automatically",
            "Clicks Reconnect, or restarts Roblox.")
        self.sw_shots = self._switch_row(
            card, "Save screenshots",
            "Newest 40 of each, handy if a pick misses.")

        self._group(sp, "UPDATES")
        card = self._card(sp, corner_radius=14)
        card.pack(fill="x")
        self.sw_update = self._switch_row(
            card, "Check for updates",
            "Asks GitHub if there's a newer version. Nothing about you is sent.",
            first=True, on=self.settings.get("check_updates", True),
            command=self._update_switched)

        self._group(sp, "DETECTION")
        card = self._card(sp, corner_radius=14)
        card.pack(fill="x")
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=14, pady=12)
        r = ctk.CTkFrame(inner, fg_color="transparent")
        r.pack(fill="x")
        ctk.CTkLabel(r, text="Match sensitivity", text_color=TEXT,
                     font=self.F(13), height=20).pack(side="left")
        self.e_thresh = ctk.CTkEntry(r, width=56, height=24, corner_radius=6,
                                     border_width=1, border_color=KEYCAP_LINE,
                                     fg_color=PANEL, text_color=TEXT,
                                     justify="right",
                                     font=self.F(11, semi=True,
                                                 family="Consolas"))
        self.e_thresh.pack(side="right")
        self.e_thresh.insert(0, f"{self._saved_threshold():.2f}")
        self.e_thresh.bind("<Return>", self._entry_to_slider)
        self.e_thresh.bind("<FocusOut>", self._entry_to_slider)
        self.sld_thresh = ctk.CTkSlider(inner, from_=0.60, to=0.95,
                                        number_of_steps=35, height=16,
                                        progress_color=ACCENT,
                                        fg_color=LINE, button_color=TEXT,
                                        button_hover_color=ACCENT_SOFT,
                                        command=self._slider_to_entry)
        self.sld_thresh.pack(fill="x", pady=(10, 0))
        self._entry_to_slider()
        ctk.CTkLabel(inner, text="Leave it at 0.82 unless picks get skipped.",
                     text_color=SUBTLE, font=self.F(11), anchor="w",
                     height=16).pack(fill="x", pady=(4, 0))
        r = ctk.CTkFrame(inner, fg_color="transparent")
        r.pack(fill="x", pady=(10, 0))
        self.btn_watch = self._ghost(r, "Live test", self.toggle_watch)
        self.btn_watch.pack(side="left", anchor="n")
        well = ctk.CTkFrame(r, fg_color=INSET, corner_radius=8)
        well.pack(side="left", fill="x", expand=True, padx=(10, 0))
        self.lbl_score = ctk.CTkLabel(
            well, text="Open the weapon picker, then press Live test.",
            text_color=MUTED, justify="left", anchor="w", height=20,
            font=ctk.CTkFont(family="Consolas", size=11))
        self.lbl_score.pack(fill="x", padx=10, pady=5)

        self._group(sp, "WAY BACK INTO FREE FOR ALL")
        card = self._card(sp, corner_radius=14)
        card.pack(fill="x")
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=14, pady=11)
        ctk.CTkLabel(inner, text="✓", text_color=GREEN, width=18,
                     font=self.F(13, family=self._fam_sym)).pack(side="left")
        txt = ctk.CTkFrame(inner, fg_color="transparent")
        txt.pack(side="left", fill="x", expand=True, padx=(8, 0))
        self.lbl_ffa = ctk.CTkLabel(txt, text="Built in", text_color=TEXT,
                                    font=self.F(13), anchor="w", height=18)
        self.lbl_ffa.pack(fill="x")
        ctk.CTkLabel(txt, text="Only re-teach if the lobby changes.",
                     text_color=SUBTLE, font=self.F(11), anchor="w",
                     height=16).pack(fill="x")
        self.btn_ffa = self._ghost(inner, "Re-teach", self.teach_ffa)
        self.btn_ffa.pack(side="right")

        self._group(sp, "FILES")
        card = self._card(sp, corner_radius=14)
        card.pack(fill="x")
        inner = ctk.CTkFrame(card, fg_color="transparent", cursor="hand2")
        inner.pack(fill="x", padx=14, pady=11)
        txt = ctk.CTkFrame(inner, fg_color="transparent")
        txt.pack(side="left", fill="x", expand=True)
        a = ctk.CTkLabel(txt, text="Open data folder", text_color=TEXT,
                         font=self.F(13), anchor="w", height=18)
        a.pack(fill="x")
        b = ctk.CTkLabel(txt, text="Setup, log and screenshots",
                         text_color=SUBTLE, font=self.F(11), anchor="w",
                         height=16)
        b.pack(fill="x")
        c = ctk.CTkLabel(inner, text="›", text_color=SUBTLE,
                         font=self.F(16))
        c.pack(side="right")
        for w in (card, inner, txt, a, b, c):
            w.bind("<Button-1>", lambda _e: self.open_data())

        ctk.CTkLabel(sp, text=(f"GlassMacro {APP_VER} · made for "
                               f"1920×1080 · F8 starts and stops"),
                     text_color=MUTED, font=self.F(11), height=16
                     ).pack(pady=(18, 8))

    def _group(self, parent, title):
        ctk.CTkLabel(parent, text=title, text_color=MUTED, anchor="w",
                     font=self.F(11, semi=True), height=16
                     ).pack(fill="x", padx=4, pady=(16, 6))

    def _ghost(self, parent, text, command):
        return ctk.CTkButton(parent, text=text, width=84, height=30,
                             corner_radius=10, fg_color="transparent",
                             hover_color=CARD_HI, border_width=1,
                             border_color=LINE, text_color=TEXT,
                             font=self.F(12), command=command)

    def _switch_row(self, card, title, note, first=False, on=True,
                    command=None):
        if not first:
            ctk.CTkFrame(card, height=1, fg_color=HAIRLINE,
                         corner_radius=0).pack(fill="x", padx=1)
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=10)
        sw = ctk.CTkSwitch(row, text="", width=40, switch_width=34,
                           switch_height=18, progress_color=ACCENT,
                           fg_color=LINE, button_color=TEXT,
                           button_hover_color=ACCENT_SOFT, command=command)
        sw.pack(side="right")
        if on:
            sw.select()
        else:
            sw.deselect()
        txt = ctk.CTkFrame(row, fg_color="transparent")
        txt.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(txt, text=title, text_color=TEXT, font=self.F(13),
                     anchor="w", height=18).pack(fill="x")
        ctk.CTkLabel(txt, text=note, text_color=SUBTLE, font=self.F(11),
                     anchor="w", height=16).pack(fill="x")
        return sw

    def _slider_to_entry(self, value):
        if self._slider_guard:
            return
        self.e_thresh.delete(0, "end")
        self.e_thresh.insert(0, f"{float(value):.2f}")

    def _entry_to_slider(self, _e=None):
        try:
            v = float(self.e_thresh.get().strip())
        except ValueError:
            return
        self._slider_guard = True
        try:
            self.sld_thresh.set(max(0.60, min(0.95, v)))
        finally:
            self._slider_guard = False

    # ---- screen checks ----
    def _paint_res(self, sw, sh, pct):
        screen_ok = (sw, sh) == SUPPORTED_SCREEN
        scale_ok = pct == SUPPORTED_SCALING
        if screen_ok and scale_ok:
            text, colour = "1920\u00d71080", MUTED
        elif scale_ok:
            text, colour = (f"{sw}\u00d7{sh} \u00b7 made for "
                            f"1920\u00d71080"), AMBER
        elif screen_ok:
            text, colour = f"{pct}% scaling \u00b7 needs 100%", AMBER
        else:
            text, colour = (f"{sw}\u00d7{sh} at {pct}% \u00b7 needs "
                            f"1920\u00d71080 at 100%"), AMBER
        self.lbl_res.configure(text=text, text_color=colour)

    def _paint_guide_warning(self):
        problems = []
        if not self._screen_ok:
            sw, sh = screen_size()
            problems.append(f"This screen is {sw}\u00d7{sh}. GlassMacro only "
                            f"works on 1920\u00d71080 for now.")
        if self._scaling != SUPPORTED_SCALING:
            problems.append(f"Windows scaling is {self._scaling}%. Set it to "
                            f"100% first: Settings \u203a System \u203a "
                            f"Display \u203a Scale.")
        if problems:
            self.lbl_warn.configure(text="\n".join(problems))
            if not self._warn_box.winfo_manager():
                self._warn_box.pack(fill="x", pady=(12, 0),
                                    before=self._guide_prog)
        elif self._warn_box.winfo_manager():
            self._warn_box.pack_forget()

    def _recheck_display(self):
        """Every few seconds: did the screen size or Windows scaling change?
        Without this, a warning stayed up after the user had fixed it -
        and during setup there is no Start to press to re-check."""
        (sw, sh), pct = screen_size(), display_scaling()
        ok = (sw, sh) == SUPPORTED_SCREEN
        if ok == self._screen_ok and pct == self._scaling:
            return
        self._screen_ok = ok
        if ok and pct == SUPPORTED_SCALING:
            self._scaling = pct
            self._paint_res(sw, sh, pct)
            self._paint_guide_warning()
            self.log("screen check: 1920x1080 at 100% - all good now")
            if not self.running and self._state_title in (
                    "Set scaling to 100%", "Made for 1920\u00d71080"):
                self._show_cal()             # back to Ready
        else:
            self._warn_display()             # logs, and repaints the header
            self._paint_guide_warning()

    def _warn_display(self):
        """A heads-up for each thing about this screen GlassMacro can't
        handle. Runs at open and on every Start; _recheck_display keeps it
        current in between."""
        (sw, sh), pct = screen_size(), display_scaling()
        self._scaling = pct
        if (sw, sh) != SUPPORTED_SCREEN:
            self.log(f"heads up: this screen is {sw}x{sh} - GlassMacro is made "
                     f"for 1920x1080, so clicks may miss")
        if pct != SUPPORTED_SCALING:
            self.log(f"heads up: Windows scaling is {pct}% - GlassMacro needs "
                     f"100% (Settings > System > Display > Scale), so clicks "
                     f"may miss")
        try:
            self._screen_ok = (sw, sh) == SUPPORTED_SCREEN
            self._paint_res(sw, sh, pct)
            self._paint_guide_warning()
        except Exception:
            pass

    # ---- updates ----
    def _update_switched(self):
        self.settings["check_updates"] = bool(self.sw_update.get())
        save_settings(self.settings)

    def _update_worker(self):
        """Background thread: ask GitHub now and every UPDATE_EVERY seconds.
        Only touches the UI through _ui()."""
        time.sleep(4)                     # let the window settle first
        while not self._check_updates_once():
            time.sleep(UPDATE_EVERY)

    def _check_updates_once(self):
        """True once a newer release has been found and shown."""
        if not self.settings.get("check_updates", True):
            return False
        found = latest_release()
        if found and version_tuple(found[0]) > version_tuple(APP_VER):
            self._ui(lambda f=found: self._show_update(*f))
            return True                   # one notice is enough
        # one quiet line in Full log / log.txt, so "did it even check?" has
        # an answer - the feed ignores it
        note = (f"update check: up to date (latest on GitHub is {found[0]})"
                if found
                else "update check: couldn't reach GitHub - will try later")
        self._ui(lambda n=note: self.log(n))
        return False

    def _show_update(self, version, url):
        self._update_url = url
        self.lnk_update.configure(text=f"Update {version} available ›")
        self.lbl_res.pack_forget()
        self.lnk_update.pack(side="right")
        self.log(f"update available: GlassMacro {version}")

    def _open_update(self):
        try:
            os.startfile(self._update_url)
        except Exception as exc:
            self.log(f"could not open the release page: {exc}")

    def open_data(self):
        try:
            os.startfile(DATA_DIR)
        except Exception as exc:
            self.log(f"could not open the data folder: {exc}")

    # ------------------------------------------------------ live pieces --
    def set_state(self, title, detail="", colour=None):
        if title != self._state_title:
            self._state_title = title
            self._state_since = time.time()
        self.lbl_state.configure(text=title)
        self.lbl_detail.configure(text=detail)
        self._dot_colour = colour or MUTED
        self._paint_dot()

    def _status(self, text, colour):
        """RUNNING / IDLE / WATCHING. Only the dot shows it now - the old
        header pill said the same thing as the card, and they could briefly
        disagree."""
        self._run_mode = text
        if text != "RUNNING":
            self._dot_colour = colour if text == "WATCHING" else MUTED
        self._paint_dot()

    def _pulsing(self):
        return (self._run_mode in ("RUNNING", "WATCHING")
                and self._dot_colour in (GREEN, ACCENT, AMBER))

    def _paint_dot(self):
        try:
            self.dotc.itemconfigure(self._core, fill=self._dot_colour)
            if not self._pulsing():
                c = self._px(16) / 2
                self.dotc.coords(self._halo, c, c, c, c)
        except Exception:
            pass

    def _animate(self):
        """One timer for the breathing dot. Only moves existing canvas items,
        and idles when the window is minimised or nothing is running."""
        delay = 300
        try:
            if self._pulsing() and self.state() != "iconic":
                period = 2.4 if self._dot_colour == AMBER else 1.6
                self._pulse_t = (self._pulse_t + 0.06 / period) % 1.0
                t = self._pulse_t
                c = self._px(16) / 2
                rad = self._px(4) + self._px(4) * t
                self.dotc.coords(self._halo, c - rad, c - rad, c + rad,
                                 c + rad)
                self.dotc.itemconfigure(
                    self._halo,
                    fill=blend(blend(self._dot_colour, CARD, 0.45), CARD, t))
                delay = 60
        except Exception:
            pass
        self.after(delay, self._animate)

    def _show_playtime(self, secs, live=False):
        """The big number. None = never run; live = counting now; otherwise
        the last run, dimmed, so stopping does not wipe it to zero."""
        n = self._num
        if secs is None:
            self.lbl_play_cap.configure(text="PLAYTIME")
            for lbl, t in zip(n, ("—", "", "", "")):
                lbl.configure(text=t)
            n[0].configure(text_color=MUTED)
            # a CTkProgressBar at 0 still draws a nub; hide it in the track
            self.bar_hour.configure(progress_color=HAIRLINE)
            self.bar_hour.set(0)
            self.lbl_play_sub.configure(
                text="Starts counting when you press Start")
            return
        s = int(secs)
        if s >= 3600:
            parts = (str(s // 3600), "h", f"{s % 3600 // 60:02d}", "m")
        else:
            parts = (str(s // 60), "m", f"{s % 60:02d}", "s")
        colour = TEXT if live else SUBTLE
        for lbl, t in zip(n, parts):
            lbl.configure(text=t)
        n[0].configure(text_color=colour)
        n[2].configure(text_color=colour)
        self.lbl_play_cap.configure(text="PLAYTIME" if live else "LAST RUN")
        for v in (self.val_picks, self.val_joins):
            v.configure(text_color=colour)
        if live:
            self.bar_hour.configure(progress_color=ACCENT)
            self.bar_hour.set((s % 3600) / 3600.0)
            left = 60 - (s % 3600) // 60
            self.lbl_play_sub.configure(
                text=f"{left}m to {s // 3600 + 1}h")
        else:
            self.bar_hour.configure(progress_color=HAIRLINE)
            self.bar_hour.set(0)
            end = (self._last_run or {}).get("end", "")
            self.lbl_play_sub.configure(text=f"ended {end}" if end else "")

    def _tick(self):
        """Once a second, main thread: playtime, 'since', setup countdown."""
        try:
            now = time.time()
            self._ticks = getattr(self, "_ticks", 0) + 1
            if self._ticks % 5 == 0:
                try:
                    self._recheck_display()
                except Exception:
                    pass
            if self.running and self.session_start:
                self._run_t0 = self.session_start
                self._was_running = True
                self._show_playtime(now - self.session_start, live=True)
            elif self._was_running:
                # stopped - by F8, by Stop, or the worker ending on an error
                self._was_running = False
                self._last_run = {"secs": now - (self._run_t0 or now),
                                  "end": time.strftime("%H:%M")}
                self._show_playtime(self._last_run["secs"], live=False)
            if self.running and self._state_since:
                self.lbl_since.configure(text="since " + time.strftime(
                    "%H:%M", time.localtime(self._state_since)))
            else:
                self.lbl_since.configure(text="")
            if self._setup_active and self._guide["t0"]:
                # no countdown: the setup worker only checks its time limit
                # when a key arrives, so a clock reaching 0:00 would be a lie
                self._guide_status("Esc cancels · take your time",
                                   SUBTLE)
        except Exception:
            pass
        self.after(1000, self._tick)

    # A friendly reading of the log. Every path through the macro already logs
    # what it is doing, so the card and the feed follow the log instead of the
    # worker being re-plumbed - the tested behaviour stays exactly as it is.
    # First match wins, so the more specific phrases come first. "{n}" in a
    # detail is filled with the first number in the log line.
    STATUS_RULES = (
        ("stopped on an error", "Stopped on an error",
         "The full log says what happened.", "RED", None),
        ("just playing", "In a match", "Picked up where you were.",
         "GREEN", None),
        ("choosing loadout", "Picking a loadout",
         "Grenade Launcher, then Random.", "ACCENT", None),
        ("done, back to jumping", "In a match",
         "Jumping and firing until the next pick.", "GREEN", "picks"),
        ("in the hub - joining", "Joining Free For All",
         "Walking the menu from the hub.", "ACCENT", None),
        ("match started - stopping the join", "In a match",
         "A round started mid-join, so it stopped clicking the menu.",
         "GREEN", None),
        ("  in a match", "In a match",
         "Joined · waiting for the weapon picker.", "GREEN", "joins"),
        ("PAUSED", "Paused", "Click into Rivals and it carries on.",
         "AMBER", None),
        ("back in front", "Back in Rivals", "Carrying on.", "ACCENT", None),
        ("connection dialog", "Reconnecting",
         "Got disconnected · clicking Reconnect.", "AMBER", None),
        ("restarting Roblox", "Restarting Roblox",
         "Reconnect didn't work, so it's starting fresh.", "AMBER", None),
        ("Join prompt showing", "Between rounds",
         "Waiting for the next round to start.", "ACCENT", None),
        ("still spectating", "Joining the match",
         "Was stuck spectating · pressed Join.", "ACCENT", None),
        ("quiet for", "All quiet",
         "{n} min with nothing to do. The best case for playtime.",
         "GREEN", None),
        ("could not get into a match", "Couldn't get in",
         "Tried three times · it'll keep trying.", "RED", None),
        ("started - F8 stops it", "Starting", "Checking where you are.",
         "ACCENT", None),
        ("pressing F11 for fullscreen", "Going fullscreen",
         "Roblox was in a window · pressed F11.", "ACCENT", None),
        ("  fullscreen now", "Fullscreen", "Carrying on.", "GREEN", None),
        ("couldn't make Roblox fullscreen", "Roblox isn't fullscreen",
         "Press F11 in Roblox · clicks can miss in a window.",
         "AMBER", None),
        ("heads up: Windows scaling is", "Set scaling to 100%",
         "Windows is at {n}%. Set it to 100% in Settings \u203a System "
         "\u203a Display \u203a Scale - this clears by itself.", "AMBER",
         None),
        ("heads up: this screen is", "Made for 1920×1080",
         "This screen is different, so clicks may land in the wrong place.",
         "AMBER", None),
        ("calibrate first", "Set up your weapons first",
         "It only takes three hovers.", "AMBER", None),
        ("recalibrate - it now needs", "Redo setup",
         "It needs your first loadout slot too now.", "RED", None),
        ("calibration looks like the same", "Redo setup",
         "Random, then the Grenade Launcher, then the loadout slot.",
         "RED", None),
        ("calibration saved", "Weapons set up",
         "Open Rivals and press F8, or hit Start.", "GREEN", None),
        ("from the LOBBY", "Teaching the way back · 1 of 3",
         "In the hub: hover Play, press F8, then click it yourself.",
         "ACCENT", None),
        ("now hover FREE FOR ALL", "Teaching the way back · 2 of 3",
         "Scroll to Free For All, hover it, press F8, then click it.",
         "ACCENT", None),
        ("now hover PLAY on", "Teaching the way back · 3 of 3",
         "Hover Play on the Free For All screen and press F8.",
         "ACCENT", None),
        ("saved the way back", "Way back saved",
         "Open Rivals and press F8, or hit Start.", "GREEN", None),
        ("timed out - nothing saved", "Nothing saved",
         "Took too long between presses.", "MUTED", None),
        ("stop the macro first", "Stop it first",
         "Press Stop, then try that again.", "AMBER", None),
    )

    # The activity feed: (needle, key, glyph, colour, title, sub). A sub may
    # use {n} like above. Lines that match nothing stay out of the feed - they
    # are still in Full log and log.txt.
    FEED_RULES = (
        ("stopped on an error", "err", "×", "RED",
         "Stopped on an error", "see Full log"),
        ("just playing", "resume", "▶", "SUBTLE", "Already in a match",
         "picking up from here"),
        ("in the hub - joining", "hub", "➜", "ACCENT",
         "Joining Free For All", "from the hub"),
        ("match started - stopping the join", "back", "➜", "ACCENT",
         "Back in Free For All", "a round started mid-join"),
        ("  in a match", "back", "➜", "ACCENT", "Back in Free For All",
         ""),
        ("done, back to jumping", "pick", "◆", "ACCENT",
         "Picked a loadout", ""),
        ("NOT the launcher there", "miss", "!", "AMBER", "Skipped a pick",
         "couldn't see the Grenade Launcher"),
        ("quiet for", "quiet", "●", "GREEN", "All quiet",
         "{n} min with nothing to do"),
        ("connection dialog", "disc", "!", "AMBER", "Disconnected",
         "clicked Reconnect"),
        ("restarting Roblox", "restart", "!", "AMBER", "Restarting Roblox",
         "Reconnect didn't work"),
        ("could not get into a match", "noin", "×", "RED",
         "Couldn't get into a match", "will keep trying"),
        ("still spectating", "spec", "➜", "ACCENT",
         "Joined from spectate", ""),
        ("PAUSED", "pause", "❚❚", "AMBER", "Paused",
         "Rivals isn't in front"),
        ("back in front", "resume", "▶", "SUBTLE", "Resumed", ""),
        ("pressing F11 for fullscreen", "fs", "⤢", "ACCENT",
         "Back to fullscreen", "pressed F11"),
        ("couldn't make Roblox fullscreen", "nofs", "!", "AMBER",
         "Roblox isn't fullscreen", "press F11 in Roblox"),
        ("screen check: 1920x1080 at 100%", "screenok", "\u2713", "GREEN",
         "Screen settings look right", "1920\u00d71080 at 100%"),
        ("heads up: Windows scaling is", "scaling", "!", "AMBER",
         "Scaling isn't 100%", "set it to 100% in Windows display settings"),
        ("heads up: this screen is", "screen", "!", "AMBER",
         "Screen isn't 1920×1080", "clicks may miss"),
        ("NOTE: those two points are almost", "needsetup", "!", "AMBER",
         "Setup needs redoing", "the same spot was hovered twice"),
        ("update available:", "update", "↑", "ACCENT",
         "Update available", "the link is at the top"),
        ("calibration saved", "setup", "✓", "GREEN", "Weapons set up",
         ""),
        ("saved the way back", "way", "✓", "GREEN", "Way back saved",
         ""),
        ("calibrate first", "needsetup", "!", "AMBER", "Setup needed",
         "three hovers in the weapon picker"),
        ("recalibrate - it now needs", "needsetup", "!", "AMBER",
         "Setup needed", "redo it with the loadout slot"),
        ("calibration looks like the same", "needsetup", "!", "AMBER",
         "Setup needed", "one tile was hovered twice"),
        ("started - F8 stops it", "start", "▶", "SUBTLE", "Started", ""),
    )

    @staticmethod
    def _fill_n(template, text):
        if "{n}" not in template:
            return template
        import re
        m = re.search(r"\d+", text)
        return template.replace("{n}", m.group(0) if m else "a few")

    def _status_from_log(self, msg):
        text = str(msg)
        if text.startswith("started - F8 stops it"):
            # toggle_run sets session_start just before logging this. Taking
            # it here, not on the next 1s tick, keeps a run stopped within a
            # second from being reported with the PREVIOUS run's length.
            self._run_t0 = self.session_start or time.time()
            self._was_running = True
        if text == "stopped":
            secs = time.time() - self._run_t0 if self._run_t0 else 0
            self.set_state("Stopped", f"Ran {span(secs)} · "
                           f"{self.n_picks} loadouts · "
                           f"{self.n_joins} rejoins", MUTED)
            return
        if text == "cancelled":
            self.set_state("Cancelled", "Nothing changed.", MUTED)
            return
        for needle, title, detail, colour, counter in self.STATUS_RULES:
            if needle in text:
                self.set_state(title, self._fill_n(detail, text),
                               globals()[colour])
                if counter == "picks":
                    self.n_picks += 1
                    self.val_picks.configure(text=str(self.n_picks))
                elif counter == "joins":
                    self.n_joins += 1
                    self.val_joins.configure(text=str(self.n_joins))
                return

    def _feed_from_log(self, msg):
        text = str(msg)
        if text == "stopped":
            secs = time.time() - self._run_t0 if self._run_t0 else 0
            self._feed_add("stop", "■", "SUBTLE", "Stopped",
                           f"after {span(secs)}")
            return
        for needle, key, glyph, colour, title, sub in self.FEED_RULES:
            if needle in text:
                self._feed_add(key, glyph, colour, title,
                               self._fill_n(sub, text))
                return

    def _feed_add(self, key, glyph, colour, title, sub):
        """Newest on top. The same event twice in a row becomes one row with a
        count ('Picked a loadout  x6'), so hours of play read as a few lines."""
        tb = self.feed
        count = 1
        if self._feed_top and self._feed_top[0] == key and key not in (
                "start", "stop"):
            count = self._feed_top[1] + 1
            tb.configure(state="normal")
            tb._textbox.delete("1.0", "2.0")
            tb.configure(state="disabled")
            self._feed_rows -= 1
        self._feed_top = (key, count)
        t = tb._textbox
        tb.configure(state="normal")
        at = "1.0"
        parts = [(time.strftime("%H:%M") + "\t", "ts"),
                 (glyph + "\t", "g_" + colour), (title, "title")]
        if sub:
            parts.append(("   " + sub, "sub"))
        if count > 1:
            parts.append((f"   ×{count}", "chip"))
        parts.append(("\n", "title"))
        for chunk, tag in reversed(parts):
            t.insert(at, chunk, tag)
        self._feed_rows += 1
        if self._feed_rows > 300:
            t.delete("301.0", "end")
            self._feed_rows = 300
        tb.configure(state="disabled")
        t.see("1.0")
        if self._feed_rows == 1:
            self.empty.place_forget()

    # ---- the setup guide follows the setup's own log lines ----
    def _guide_from_log(self, msg):
        text = str(msg)
        g = self._guide
        if text.startswith("hover RANDOM"):
            self._setup_active = True
            g.update(step=1, done=set(), t0=time.time(), bad=0)
        elif text.startswith("  got RANDOM"):
            g["done"].add(1)
        elif text.startswith("hover GRENADE LAUNCHER"):
            g["step"] = 2
        elif text.startswith("  got GRENADE LAUNCHER"):
            g["done"].add(2)
        elif text.startswith("hover the FIRST loadout"):
            g["step"] = 3
        elif text.startswith("  got the FIRST loadout"):
            g["done"].add(3)
        elif text.startswith("NOTE: those two points are almost"):
            g.update(step=2, done={1}, t0=0.0, bad=2)
            self._guide_status("That was the same spot as Random · redo "
                               "it, and hover the Grenade Launcher.", RED)
        elif text == "calibration saved":
            g.update(step=0, done={1, 2, 3}, t0=0.0)
            self._setup_active = False
        elif self._setup_active and text in ("calibration cancelled",
                                             "timed out - nothing saved"):
            g.update(step=0, done=set(), t0=0.0)
            self._setup_active = False
            self._guide_status(
                "Cancelled · nothing changed." if "cancel" in text else
                "Took too long between presses · nothing saved. Try "
                "again whenever.", SUBTLE)
        elif self._setup_active and text.startswith("calibration failed"):
            g.update(step=0, done=set(), t0=0.0)
            self._setup_active = False
            self._guide_status("Something went wrong · nothing saved. "
                               "Full log has the details.", RED)
        else:
            return
        self._paint_guide()
        self._refresh_layout()

    def _draw_badge(self, cv, text, fill, colour, bg):
        cv.delete("all")
        cv.configure(bg=bg)
        d = self._px(26)
        cv.create_oval(1, 1, d - 1, d - 1, fill=fill, outline="")
        cv.create_text(d / 2, d / 2, text=text, fill=colour,
                       font=self._tkfont(12, semi=True,
                                         family=self._fam_sym
                                         if text == "✓" else None))

    def _guide_status(self, text, colour):
        self.lbl_guide.configure(text=text, text_color=colour)

    def _paint_guide(self):
        g = self._guide
        for i, seg in enumerate(self._segs, start=1):
            seg.configure(fg_color=GREEN if i in g["done"] else
                          ACCENT if i == g["step"] else LINE)
        # not started yet: still point at step 1, so the picture shows what
        # to hover first
        current = g["step"] or (1 if not g["done"] else 0)
        for i, row in enumerate(self._step_rows, start=1):
            is_cur = i == current and i not in g["done"]
            bad = g.get("bad") == i
            border = RED if bad else (ACCENT if is_cur else HAIRLINE)
            fill = CARD_HI if is_cur else "transparent"
            row["row"].configure(border_color=border, fg_color=fill)
            bg = CARD_HI if is_cur else CARD
            if i in g["done"]:
                self._draw_badge(row["badge"], "✓", GREEN_DIM, GREEN, bg)
            else:
                self._draw_badge(row["badge"], str(i),
                                 ACCENT if is_cur else CARD_HI,
                                 INK if is_cur else SUBTLE, bg)
            if is_cur:
                self._draw_picker(row["pic"], i - 1, CARD_HI)
                row["pic"].pack(side="right", padx=(8, 0))
            else:
                row["pic"].pack_forget()
        if self._setup_active:
            self.btn_cal.configure(state="disabled",
                                   text="Hover it and press F8",
                                   fg_color=CARD_HI)
        else:
            self.btn_cal.configure(state="normal",
                                   text="I'm on the weapon picker · "
                                        "start setup", fg_color=ACCENT)

    def _refresh_layout(self):
        """The guide takes the status card's place until setup is done."""
        want = self._setup_active or not self._ready()
        if want == self._guide_visible:
            return
        self._guide_visible = want
        if want:
            self.hero.pack_forget()
            self.weap.pack_forget()
            self.guide.pack(fill="x", before=self.tabbar)
        else:
            self.guide.pack_forget()
            self.hero.pack(fill="x", before=self.tabbar)
            self.weap.pack(fill="x", pady=(12, 0), before=self.tabbar)

    def log(self, msg):
        stamp = time.strftime("%H:%M:%S")
        try:
            with io.open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(time.strftime("%Y-%m-%d %H:%M:%S") + "  " + str(msg)
                         + chr(10))
        except Exception:
            pass                      # never let logging break the macro
        try:
            self.txt.configure(state="normal")
            self.txt.insert("end", f"{stamp}  ", "ts")
            sub = str(msg).startswith("  ")
            self.txt.insert("end", f"{msg}\n", "sub" if sub else "top")
            # A day-long run logs tens of thousands of lines, and a Text widget
            # that size gets slow. The full history is in log.txt anyway.
            lines = int(self.txt.index("end-1c").split(".")[0])
            if lines > 2000:
                self.txt.delete("1.0", f"{lines - 1500}.0")
            if self._full_log:
                self.txt.see("end")
            self.txt.configure(state="disabled")
        except Exception:
            pass
        for fn in (self._status_from_log, self._feed_from_log,
                   self._guide_from_log):
            try:
                fn(msg)
            except Exception:
                pass                  # the UI is cosmetic; never let it break

    def _saved_randoms(self):
        if self.cal and "randoms" in self.cal:
            return self.cal["randoms"]
        return DEFAULT_RANDOMS

    def randoms(self):
        return self._saved_randoms()

    def place_id(self):
        return str((self.cal or {}).get("place_id") or RIVALS_PLACE)

    def _saved_scroll(self):
        if self.cal and "ffa_scroll_secs" in self.cal:
            return self.cal["ffa_scroll_secs"]
        return DEFAULT_SCROLL_SECS

    def ffa_scroll(self):
        """Seconds of scrolling down, to get to the bottom of the list."""
        return self._saved_scroll()

    def _saved_gl_nudge(self):
        if self.cal and "gl_nudge" in self.cal:
            return self.cal["gl_nudge"]
        return list(DEFAULT_GL_NUDGE)

    def gl_nudge(self):
        return tuple(self._saved_gl_nudge())

    def _saved_nudge(self):
        if self.cal and "nudge3" in self.cal:
            return self.cal["nudge3"]
        return DEFAULT_NUDGE3

    def nudge3(self):
        return self._saved_nudge()

    def _saved_threshold(self):
        if self.cal and "threshold" in self.cal:
            return self.cal["threshold"]
        return DEFAULT_THRESHOLD

    def threshold(self):
        try:
            return max(0.1, min(0.99, float(self.e_thresh.get().strip())))
        except ValueError:
            return self._saved_threshold()

    def _ready(self):
        """Calibrated well enough that Start will actually run."""
        if not self.cal or not self.cal.get("tab_offset"):
            return False
        dx, dy = self.cal["gl_offset"]
        return max(abs(dx), abs(dy)) >= 40

    def _paint_run(self):
        """One place that decides how the big button looks."""
        b = self.btn_run
        if self.running:
            # neutral, not a red slab - a 3-hour run should not look like an
            # error the whole time
            b.configure(text="Stop", glyph="■", fg_color=CARD_HI,
                        hover_color=RED_DIM, text_color=TEXT, glyph_color=RED,
                        border_color=LINE, key_fg=PANEL,
                        key_border=KEYCAP_LINE, key_text=SUBTLE)
        elif self._ready():
            b.configure(text="Start", glyph="▶", fg_color=ACCENT,
                        hover_color=ACCENT_SOFT, text_color=INK,
                        glyph_color=INK, border_color=ACCENT,
                        key_fg="#4ab8ec", key_border="#2b93d1", key_text=INK)
        else:
            b.configure(text="Set up weapons first", glyph="!",
                        fg_color=CARD_HI, hover_color=LINE, text_color=MUTED,
                        glyph_color=AMBER, border_color=LINE, key_fg=PANEL,
                        key_border=KEYCAP_LINE, key_text=MUTED)

    def _show_cal(self):
        if not self.calibrating:
            self._setup_active = False       # the setup worker has finished
        if self._ready():
            made = (self.cal or {}).get("made", "")
            try:
                when = time.strftime("%b %d", time.strptime(made[:10],
                                                            "%Y-%m-%d"))
                when = when.replace(" 0", " ")
            except Exception:
                when = ""
            self.lbl_weap.configure(text="Weapons ready")
            self.lbl_cal.configure(
                text=f"Grenade Launcher + {self._saved_randoms()} Random"
                     + (f" · set up {when}" if when else ""))
            self.weap_tile.configure(text="✓", fg_color=GREEN_DIM,
                                     text_color=GREEN)
        self._paint_run()
        self._paint_guide()
        self._refresh_layout()
        if not self.running and self._ready() and not self._setup_active:
            self.set_state("Ready", "Open Rivals and press F8, or hit Start.",
                           MUTED)

    # ----------------------------------------------------------- actions --
    def _hotkey(self):
        # F8 means "capture a point" while calibrating and "start/stop"
        # otherwise; they never overlap.
        if self.calibrating:
            return
        self._ui(self.toggle_run)

    def calibrate(self):
        if self.calibrating:
            return
        if self.running or self.watching:
            self.log("stop the macro first")
            return
        self.calibrating = True
        self.btn_cal.configure(text="Waiting for F8...", state="disabled")
        self.log("hover RANDOM and press F8")
        threading.Thread(target=self._calibrate_worker, daemon=True).start()

    def _calibrate_worker(self):
        spots = []
        WANT = ("RANDOM", "GRENADE LAUNCHER", "the FIRST loadout slot at the top")
        deadline = time.time() + 180
        try:
            while len(spots) < 3 and time.time() < deadline:
                ev = keyboard.read_event(suppress=False)
                if ev.event_type != "down":
                    continue
                if ev.name == "esc":
                    self._ui(lambda: self.log("calibration cancelled"))
                    return
                if ev.name != "f8":
                    continue
                spots.append(cursor_pos())
                n = len(spots)
                self._ui(lambda n=n, p=spots[-1]: self.log(
                    f"  got {WANT[n - 1]} at {p}"))
                if n < len(WANT):
                    self._ui(lambda n=n: self.log(f"hover {WANT[n]} and press F8"))
                time.sleep(0.3)

            if len(spots) < 3:
                self._ui(lambda: self.log("timed out - nothing saved"))
                return

            (rx, ry), (gx, gy), (tx, ty) = spots
            shot = grab_gray()
            h, w = shot.shape
            x0, y0 = max(0, rx - GRAB // 2), max(0, ry - GRAB // 2)
            x1, y1 = min(w, x0 + GRAB), min(h, y0 + GRAB)
            cv2.imwrite(TEMPLATE_PATH, shot[y0:y1, x0:x1])
            gx0, gy0 = max(0, gx - GRAB // 2), max(0, gy - GRAB // 2)
            cv2.imwrite(GL_TEMPLATE_PATH,
                        shot[gy0:min(h, gy0 + GRAB), gx0:min(w, gx0 + GRAB)])
            cal = {
                "screen": [w, h],
                "anchor_offset": [rx - x0, ry - y0],
                # constant in pixels however far the grid slides
                "gl_offset": [gx - rx, gy - ry],
                # the primary loadout slot, clicked to get back to the weapons
                # tab. Random appears in EVERY tab, so a perfect Random match
                # says nothing about which tab is showing - and the macro sat
                # on the melee tab for minutes refusing to click, correctly.
                "tab_offset": [tx - rx, ty - ry],
                "threshold": self.threshold(),
                "made": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            save_cal(cal)
            self.cal, self.tile = load_cal()
            self._ui(self._show_cal)
            self._ui(lambda: self.log("calibration saved"))
            dx, dy = cal["gl_offset"]
            if max(abs(dx), abs(dy)) < 40:
                self._ui(lambda: self.log(
                    "NOTE: those two points are almost the same spot - looks "
                    "like the same tile was hovered twice. Worth redoing."))
            else:
                self._ui(lambda x=dx, y=dy: self.log(
                    f"  launcher is {x:+d}, {y:+d} from Random"))
        except Exception as exc:
            self._ui(lambda e=str(exc): self.log(f"calibration failed: {e}"))
        finally:
            self.calibrating = False
            self._ui(lambda: self.btn_cal.configure(state="normal"))
            self._ui(self._show_cal)

    def teach_ffa(self):
        """Learn the four spots that get from the lobby back into FFA.

        Reconnecting drops you in the lobby, not the match, so the macro has to
        walk the menu itself. Points are read from the CURSOR, so the menu can
        be navigated by hand between presses - a click would advance it before
        the position was recorded.
        """
        if self.calibrating or self.running:
            self.log("stop the macro first")
            return
        self.calibrating = True
        self.btn_ffa.configure(text="Waiting for F8...", state="disabled")
        self.log("from the LOBBY: hover PLAY and press F8 (then click it "
                 "yourself and carry on)")
        threading.Thread(target=self._teach_ffa_worker, daemon=True).start()

    def _teach_ffa_worker(self):
        WANT = ("PLAY in the lobby",
                "FREE FOR ALL (scroll down to it first)",
                "PLAY on the Free For All screen")
        spots = []
        deadline = time.time() + 300
        try:
            while len(spots) < 3 and time.time() < deadline:
                ev = keyboard.read_event(suppress=False)
                if ev.event_type != "down":
                    continue
                if ev.name == "esc":
                    self._ui(lambda: self.log("cancelled"))
                    return
                if ev.name != "f8":
                    continue
                spots.append(cursor_pos())
                k = len(spots)
                self._ui(lambda k=k, p=spots[-1]: self.log(
                    f"  got {WANT[k - 1]} at {p}"))
                if k < len(WANT):
                    self._ui(lambda k=k: self.log(f"now hover {WANT[k]}, F8"))
                time.sleep(0.3)

            if len(spots) < 3:
                self._ui(lambda: self.log("timed out - nothing saved"))
                return
            if self.cal is None:
                self._ui(lambda: self.log("calibrate the weapon picker first"))
                return
            self.cal["ffa_path"] = [list(p) for p in spots]
            self.cal["ffa_scroll_secs"] = self.ffa_scroll()
            save_cal(self.cal)
            self._ui(lambda: self.log("saved the way back into FFA"))
            self._ui(self._show_ffa)
        except Exception as exc:
            self._ui(lambda e=str(exc): self.log(f"failed: {e}"))
        finally:
            self.calibrating = False
            self._ui(lambda: self.btn_ffa.configure(
                text="Re-teach", state="normal"))

    def _show_ffa(self):
        if not hasattr(self, "lbl_ffa"):
            return
        taught = bool((self.cal or {}).get("ffa_path"))
        self.lbl_ffa.configure(
            text="Taught on this PC" if taught else "Built in",
            text_color=TEXT)

    # ---- fullscreen. Worker thread only, like everything that sends input.
    def _fresh_fs(self):
        return {"hwnd": 0, "since": 0.0, "tries": 0, "pressed": False,
                "gave_up": False}

    def _fullscreen_tick(self, log):
        """Press F11 if Roblox has sat in a window a while. Never blocks.

        True when Roblox is fullscreen (or the switch is off), False while it
        is windowed, None when Roblox is not in front or cannot be read.
        """
        if not self.sw_fullscreen.get():
            return True
        fs = self._fs
        h = roblox_hwnd()
        if not h or not focused():
            # the patience is 3s of watching it IN FRONT, so time spent
            # alt-tabbed away does not count towards it
            fs["since"] = 0.0
            return None
        if h != fs["hwnd"]:                  # a new Roblox gets a fresh budget
            fs.update(self._fresh_fs(), hwnd=h)
        full = roblox_fullscreen(h)
        if full is None:
            fs["since"] = 0.0
            return None
        if full:
            if fs["pressed"]:
                log("  fullscreen now")
                # F11 demonstrably works on this window, so if it ever drops
                # back to windowed it is safe to fix again
                # - and that includes waiting properly again, so a give-up
                # from earlier must not linger
                fs.update(pressed=False, tries=0, gave_up=False,
                          just_switched=True)
            fs["since"] = 0.0
            return True
        now = time.time()
        if not fs["since"]:
            fs["since"] = now
            return False
        if now - fs["since"] < FULLSCREEN_PATIENCE:
            return False
        if fs["tries"] >= FULLSCREEN_TRIES:
            if not fs["gave_up"]:
                fs["gave_up"] = True
                log("couldn't make Roblox fullscreen - press F11 in Roblox "
                    "yourself")
            return False
        fs["tries"] += 1
        fs["pressed"] = True
        fs["since"] = 0.0                    # patience starts over every press
        log("Roblox is windowed - pressing F11 for fullscreen")
        tap_key(SCAN_F11)
        return False

    def _make_fullscreen(self, log, wait=10.0):
        """Block until Roblox is fullscreen, it gave up, or `wait` runs out.

        For the places that are about to click by screen position - the join
        path above all - where a window would put every click off by the
        height of a title bar.
        """
        end = time.time() + wait
        while self.running and time.time() < end:
            state = self._fullscreen_tick(log)
            if state is True and self._fs.pop("just_switched", False):
                time.sleep(FULLSCREEN_SETTLE)    # menus re-lay out at the new size
            if state is not False or self._fs["gave_up"]:
                return
            time.sleep(0.5)

    def rejoin_ffa(self, log):
        """Walk the lobby menu into a Free For All, and check it actually took.

        Walking the menu is not the same as being in a match - it can land in
        spectate, showing a "Join this game!" prompt and no weapon picker. So
        each attempt ends by watching for the picker, and walks the menu again
        if it never turns up.
        """
        path = (self.cal or {}).get("ffa_path") or default_ffa_path()
        play, ffa, go = path

        def picker_up():
            """The match started - every remaining click would hit a weapon."""
            try:
                _sc, _p = find_random(self.cal, self.tile, self.threshold())
                return _p is not None
            except Exception:
                return False

        for attempt in range(1, REJOIN_TRIES + 1):
            if not self.running or not focused():
                return False
            self._make_fullscreen(log)
            if not self.running or not focused():
                return False
            if attempt > 1:
                log(f"  join did not take - trying again ({attempt}/{REJOIN_TRIES})")

            if picker_up():
                log("  match started - stopping the join")
                return True
            log("  clicking Play")
            if not click(play[0], play[1], settle=1.6):
                return False
            if picker_up():
                log("  match started - stopping the join")
                return True
            secs = self.ffa_scroll()
            log(f"  scrolling to the bottom ({secs:.0f}s)")
            scroll_down_for(ffa[0], ffa[1], secs)
            time.sleep(0.8)
            if picker_up():
                log("  match started - stopping the join")
                return True
            log(f"  choosing Free For All ({FFA_CLICK_UP}px higher)")
            if not click(ffa[0], ffa[1] - FFA_CLICK_UP, settle=1.6):
                return False
            if picker_up():
                log("  match started - stopping the join")
                return True
            log(f"  clicking Play to join ({GO_CLICK_UP}px higher)")
            if not click(go[0], go[1] - GO_CLICK_UP, settle=1.2):
                return False

            # did it actually land in a match?
            log(f"  waiting {REJOIN_CONFIRM:.0f}s for the weapon picker")
            end = time.time() + REJOIN_CONFIRM
            while time.time() < end:
                if not self.running or not focused():
                    return False
                try:
                    _sc, _p = find_random(self.cal, self.tile, self.threshold())
                except Exception:
                    _p = None
                if _p:
                    log("  in a match")
                    return True
                jb = find_join_button()
                if jb:
                    log("  landed in spectate - clicking Join this game")
                    click(jb[0], jb[1], settle=1.5)
                    continue
                # jump only. At this point the loadout has not been chosen,
                # so there is no weapon to fire - and if the join did not take
                # we may still be on a menu, where a click presses something.
                tap_space()
                time.sleep(0.35)

        log("  could not get into a match after "
            f"{REJOIN_TRIES} tries")
        return False

    def toggle_watch(self):
        if self.watching:
            self.watching = False
            self.btn_watch.configure(text="Live test")
            self._status("IDLE", SUBTLE)
            return
        if not self.cal:
            self.log("calibrate first")
            return
        if self.running:
            self.log("stop the macro first")
            return
        self.watching = True
        self.btn_watch.configure(text="Stop test")
        self._status("WATCHING", AMBER)
        threading.Thread(target=self._watch_worker, daemon=True).start()

    def _watch_worker(self):
        lo, hi = 1.0, 0.0
        while self.watching:
            try:
                score, pos = find_random(self.cal, self.tile, self.threshold())
            except Exception as exc:
                self._ui(lambda e=str(exc): self.log(f"watch failed: {e}"))
                break
            lo, hi = min(lo, score), max(hi, score)
            if pos:
                gx = pos[0] + self.cal["gl_offset"][0]
                gy = pos[1] + self.cal["gl_offset"][1]
                text = (f"picker OPEN    match {score:.2f}\n"
                        f"  Random ({pos[0]}, {pos[1]})\n"
                        f"  Launcher ({gx}, {gy})\n"
                        f"  seen {lo:.2f} - {hi:.2f}")
            else:
                text = (f"picker closed  best {score:.2f}\n"
                        f"  seen {lo:.2f} - {hi:.2f}")
            self._ui(lambda t=text: self.lbl_score.configure(text=t))
            time.sleep(0.5)
        self.watching = False
        self._ui(lambda: self.btn_watch.configure(text="Live test"))

    def toggle_run(self):
        if self.running:
            self.running = False
            self._paint_run()
            self.session_start = None
            self._status("IDLE", MUTED)
            self.log("stopped")
            return
        if not self.cal:
            self.log("calibrate first")
            return
        if not self.cal.get("tab_offset"):
            self.log("recalibrate - it now needs the loadout slot too, so it "
                     "can get back to the weapons tab on its own")
            return
        dx, dy = self.cal["gl_offset"]
        if max(abs(dx), abs(dy)) < 40:
            self.log("calibration looks like the same tile twice - recalibrate, "
                     "Random first then Grenade Launcher.")
            return
        if self.watching:
            self.toggle_watch()
        self._fs = self._fresh_fs()
        self._warn_display()
        self.running = True
        self._paint_run()
        self.session_start = time.time()
        self.n_picks = self.n_joins = 0
        self.val_picks.configure(text="0")
        self.val_joins.configure(text="0")
        self._status("RUNNING", GREEN)
        self.log("started - F8 stops it")
        threading.Thread(target=self._run_worker, daemon=True).start()

    def _run_worker(self):
        picked_at = 0.0
        fails = 0
        warned = False
        beat = 0.0
        join_since = 0.0
        last_picker = 0.0
        last_known = time.time()    # last time ANY detector recognised the screen
        last_stall_shot = 0.0
        last_join_shot = 0.0

        # Starting from the lobby is the normal case, so it joins by itself.
        # But "the picker is not up" does NOT mean "not in a match" - mid-round
        # it is not up either, and joining then would fire menu clicks into the
        # game. So WATCH for a while: in FFA you die often enough that the
        # picker shows up quickly, and if it never does, this is the lobby.
        for _ in range(6):                      # let the window settle
            if not self.running:
                return
            if focused():
                break
            time.sleep(0.5)

        wlog = lambda m: self._ui(lambda mm=m: self.log(mm))
        if self.running and focused():
            self._make_fullscreen(wlog)
        if self.running and focused():
            if in_lobby():
                self._ui(lambda: self.log("in the hub - joining FFA"))
                self.rejoin_ffa(
                    lambda m: self._ui(lambda mm=m: self.log(mm)))
            else:
                self._ui(lambda: self.log(
                    "not in the hub - already in a match or an intermission, "
                    "just playing"))
        while self.running:
            if not focused():
                if not warned:
                    self._ui(lambda: self.log(
                        "PAUSED - Roblox is not the focused window"))
                    warned = True
                    beat = time.time()
                elif time.time() - beat > HEARTBEAT:
                    beat = time.time()
                    self._ui(lambda: self.log("still paused - Roblox not in front"))
                time.sleep(0.5)
                continue
            if warned:
                self._ui(lambda: self.log("Roblox is back in front - resuming"))
            warned = False

            # Windowed, everything positional is off by a title bar - so hold
            # still for the few seconds it takes to fix. Once it has given up,
            # carry on anyway: a macro that stops dead helps nobody.
            if (self._fullscreen_tick(wlog) is False
                    and not self._fs["gave_up"]):
                time.sleep(0.5)
                continue
            if self._fs.pop("just_switched", False):
                time.sleep(FULLSCREEN_SETTLE)    # menus re-lay out at the new size
                continue

            if time.time() - beat > HEARTBEAT:
                beat = time.time()
                try:
                    sc, _p = find_random(self.cal, self.tile, self.threshold())
                    where = "picker up" if _p else "in game / no picker"
                    self._ui(lambda s=sc, w=where: self.log(
                        f"still running - {w} (match {s:.2f})"))
                except Exception:
                    self._ui(lambda: self.log("still running"))

            # Checked before the quiet window: seeing the hub is a certain
            # answer, not a guess, so there is nothing to wait for. The quiet
            # window exists for the screens in between rounds, which this is
            # not.
            if in_lobby():
                last_known = time.time()
                self._ui(lambda: self.log("in the hub - joining FFA"))
                self.rejoin_ffa(
                    lambda m: self._ui(lambda mm=m: self.log(mm)))
                picked_at = 0.0
                join_since = 0.0
                last_picker = 0.0
                continue

            # Quiet only suppresses the RISKY actions - clicking dialogs and
            # join prompts during the screens between rounds. It must not take
            # over the loop: it used to return early here, which meant that for
            # the whole minute after a loadout was picked - i.e. while actually
            # playing - the macro jumped but never fired.
            quiet = (last_picker and
                     time.time() - last_picker < POST_MATCH_QUIET)

            if not quiet and self.sw_reconnect.get():
                rc = find_reconnect()
                if rc:
                    shot = (save_event_shot("dialog", rc)
                            if self.sw_shots.get() else None)
                    self._ui(lambda r=rc, f=shot: self.log(
                        f"connection dialog - clicking the right-hand button "
                        f"at ({r[0]:.0f}, {r[1]:.0f})"
                        + (f" - saved {f}" if f else "")))
                    click(rc[0], rc[1], settle=1.0)

                    # Reconnect works; Retry does not. Rather than tell the two
                    # dialogs apart, see whether the click helped - if a dialog
                    # is still up, the click was never going to work.
                    time.sleep(6.0)
                    if find_reconnect():
                        shot = (save_event_shot("stuck")
                                if self.sw_shots.get() else None)
                        self._ui(lambda f=shot: self.log(
                            "  still stuck - restarting Roblox instead"
                            + (f" - saved {f}" if f else "")))
                        relaunch_roblox(
                            self.place_id(),
                            lambda m: self._ui(lambda mm=m: self.log("  " + mm)))
                        for _ in range(int(RELAUNCH_WAIT * 2)):
                            if not self.running:
                                break
                            time.sleep(0.5)
                        if self.running and focused():
                            self.rejoin_ffa(
                                lambda m: self._ui(lambda mm=m: self.log(mm)))
                        picked_at = 0.0
                        fails = 0
                        continue
                    # rejoining takes a while; checking for the picker during
                    # the load would just burn screenshots
                    self._ui(lambda: self.log("  rejoining, waiting 25s"))
                    for _ in range(50):
                        if not self.running:
                            break
                        time.sleep(0.5)
                    # reconnecting lands in the LOBBY, not the match
                    if self.running and focused():
                        self.rejoin_ffa(
                            lambda m: self._ui(lambda mm=m: self.log(mm)))
                    picked_at = 0.0
                    fails = 0
                    continue

            jb = None if quiet else find_join_button()
            if jb:
                last_known = time.time()
                if not join_since:
                    join_since = time.time()
                    shot = None
                    if (self.sw_shots.get()
                            and time.time() - last_join_shot > 120):
                        last_join_shot = time.time()
                        shot = save_event_shot("join", jb)
                    self._ui(lambda f=shot: self.log(
                        "Join prompt showing - waiting to see if the next "
                        "round starts on its own"
                        + (f" - saved {f}" if f else "")))
                elif time.time() - join_since > JOIN_BUTTON_PATIENCE:
                    shot = (save_event_shot("joinclick", jb)
                            if self.sw_shots.get() else None)
                    self._ui(lambda j=jb, f=shot: self.log(
                        f"still spectating - clicking Join this game at "
                        f"({j[0]:.0f}, {j[1]:.0f})"
                        + (f" - saved {f}" if f else "")))
                    click(jb[0], jb[1], settle=1.5)
                    join_since = 0.0
                    picked_at = 0.0
                    continue
                tap_space()
                time.sleep(0.35)
                continue
            join_since = 0.0

            stalled = time.time() - last_known
            if (stalled > STALL_AFTER
                    and time.time() - last_stall_shot > STALL_EVERY):
                last_stall_shot = time.time()
                try:
                    os.makedirs(STALL_DIR, exist_ok=True)
                    img = np.array(ImageGrab.grab())[:, :, ::-1]
                    nm = time.strftime("%m%d_%H%M%S") + "_stall.png"
                    cv2.imwrite(os.path.join(STALL_DIR, nm), img)
                    trim_folder(STALL_DIR, KEEP_STALLS)
                    self._ui(lambda m=int(stalled / 60), f=nm: self.log(
                        f"quiet for {m} min (not died, nothing to do - "
                        f"still playing) - saved {f}"))
                except Exception as exc:
                    self._ui(lambda e=str(exc): self.log(
                        f"could not save a stall shot: {e}"))

            try:
                score, pos = find_random(self.cal, self.tile, self.threshold())
            except Exception as exc:
                self._ui(lambda e=str(exc): self.log(f"stopped on an error: {e}"))
                break

            if pos:
                last_picker = last_known = time.time()
                # Only pick once per appearance - re-picking every loop would
                # fight the menu while it animates away.
                if time.time() - picked_at > REPICK_LOCKOUT:
                    self._ui(lambda s=score: self.log(
                        f"picker up (match {s:.2f}) - choosing loadout"))
                    if self._pick(pos):
                        picked_at = time.time()
                        fails = 0
                        self._ui(lambda: self.log("  done, back to jumping"))
                    else:
                        # back off rather than retrying flat out. The old code
                        # logged hundreds of identical failures a minute and
                        # hammered the screen grab for nothing.
                        fails += 1
                        wait = min(30.0, 2.0 * fails)
                        if fails in (1, 3, 6) or fails % 15 == 0:
                            self._ui(lambda f=fails, w=wait: self.log(
                                f"  failed {f}x - waiting {w:.0f}s before trying again"))
                        time.sleep(wait)
                else:
                    time.sleep(0.4)
            else:
                # The picker is gone, so whatever was chosen is locked in. Clear
                # the lockout: if it opens again - respawn, new round, or dying
                # mid-choice - that is a fresh loadout and should be picked at
                # once rather than waiting out a timer from the last one.
                picked_at = 0.0
                fails = 0
                tap_space()
                # only ever fires with the picker CLOSED, so a stray click can
                # never land in the weapon menu
                # firing while jumping is always on now - it is what makes
                # the launcher do anything, and there was no reason to turn it
                # off. Only ever fires with the picker CLOSED.
                click_here()
                time.sleep(0.35)

        self.running = False
        self._ui(self._paint_run)
        self.session_start = None
        self._ui(lambda: self._status("IDLE", SUBTLE))

    def _snap(self, pos, gx, gy, tag):
        """Save what the screen looked like, with the click marked."""
        try:
            os.makedirs(SHOT_DIR, exist_ok=True)
            img = np.array(ImageGrab.grab())[:, :, ::-1].copy()   # RGB -> BGR
            # where Random was matched
            cv2.circle(img, (int(pos[0]), int(pos[1])), 26, (0, 255, 0), 3)
            cv2.putText(img, "RANDOM", (int(pos[0]) - 40, int(pos[1]) - 34),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
            # where it is about to click
            cv2.circle(img, (int(gx), int(gy)), 26, (0, 0, 255), 3)
            cv2.line(img, (int(gx) - 34, int(gy)), (int(gx) + 34, int(gy)),
                     (0, 0, 255), 2)
            cv2.line(img, (int(gx), int(gy) - 34), (int(gx), int(gy) + 34),
                     (0, 0, 255), 2)
            cv2.putText(img, "CLICK", (int(gx) - 34, int(gy) + 54),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
            name = time.strftime("%H%M%S") + "_" + tag + ".png"
            cv2.imwrite(os.path.join(SHOT_DIR, name), img)
            trim_folder(SHOT_DIR, KEEP_PICKS)
            self._ui(lambda nm=name: self.log(f"  saved {nm}"))
        except Exception as exc:
            self._ui(lambda e=str(exc): self.log(f"  could not save shot: {e}"))

    def _pick(self, pos):
        gx = pos[0] + self.cal["gl_offset"][0]
        gy = pos[1] + self.cal["gl_offset"][1]
        time.sleep(SETTLE)

        if self.cal.get("_gl_tile") is None:
            self._ui(lambda: self.log(
                "  cannot check the launcher - recalibrate to enable that"))
        else:
            score, real = verify_launcher(self.cal, (gx, gy), self.threshold())
            if real is None:
                # not where it was taught - the roster may have changed, so
                # look across the whole grid before giving up on the tab
                gscore, greal = find_launcher_in_grid(
                    self.cal, pos, self.threshold())
                if greal:
                    self._ui(lambda g=greal, s=gscore: self.log(
                        f"  launcher has MOVED - found at {g} (match {s:.2f})"))
                    score, real = gscore, greal
            if real is None and self.cal.get("tab_offset"):
                # wrong TAB, not a wrong position: Random looks identical in all
                # four, so click the primary slot and look again
                tx = pos[0] + self.cal["tab_offset"][0]
                ty = pos[1] + self.cal["tab_offset"][1]
                self._ui(lambda s=score: self.log(
                    f"  not the weapons tab (match {s:.2f}) - switching to it"))
                click(tx, ty, settle=0.7)
                score, real = verify_launcher(self.cal, (gx, gy),
                                              self.threshold())
            if real is None:
                self._ui(lambda s=score: self.log(
                    f"  NOT the launcher there (match {s:.2f}) - not clicking"))
                return False
            if real != (gx, gy):
                self._ui(lambda r=real, s=score: self.log(
                    f"  launcher confirmed, corrected to {r} (match {s:.2f})"))
            gx, gy = real

        nx, ny = self.gl_nudge()
        gx, gy = gx + nx, gy + ny
        if self.sw_shots.get():
            self._snap(pos, gx, gy, "launcher")
        self._ui(lambda: self.log(
            f"  grenade launcher ({gx}, {gy})"
            + (f"  [nudged {nx:+d},{ny:+d}]" if (nx or ny) else "")))
        if not click(gx, gy, settle=BETWEEN_CLICKS):
            self._ui(lambda: self.log("  CURSOR WOULD NOT MOVE - nothing clicked"))
            return False
        n = self.randoms()
        for i in range(n):
            if not self.running or not focused():
                self._ui(lambda: self.log("  interrupted part way through"))
                return False
            # the third tab's Random does not line up with the others, so
            # that one click gets nudged up by a measured amount
            dy = -self.nudge3() if i == 2 else 0
            self._ui(lambda i=i, n=n, dy=dy: self.log(
                f"  random {i + 1}/{n}" + (f"  (up {-dy}px)" if dy else "")))
            if not click(pos[0], pos[1] + dy, settle=BETWEEN_CLICKS):
                self._ui(lambda: self.log("  cursor would not move"))
                return False
        return True

    def _close(self):
        self.running = self.watching = False
        # remember a tuned threshold, so it does not have to be set every run
        if self.cal:
            try:
                self.cal["threshold"] = self.threshold()
                self.cal["randoms"] = self.randoms()
                self.cal["nudge3"] = self.nudge3()
                self.cal["gl_nudge"] = list(self.gl_nudge())
                self.cal["ffa_scroll_secs"] = self.ffa_scroll()
                self.cal["place_id"] = self.place_id()
                save_cal(self.cal)
            except Exception:
                pass
        self.destroy()


if __name__ == "__main__":
    GlassMacro().mainloop()
