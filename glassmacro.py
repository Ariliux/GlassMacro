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

APP_NAME, APP_VER = "GlassMacro", "1.0.9"

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

    GLASSMACRO_TEST_SCALING=150 in the environment pretends, so the built app
    can be checked end to end without touching anyone's display settings.

    The main screen, not wherever this window sits: that is the screen every
    screenshot here is taken of. Needs DPI awareness (CustomTkinter turns it
    on), or Windows answers 100% whatever the truth is.
    """
    fake = os.environ.get("GLASSMACRO_TEST_SCALING", "")
    if fake.isdigit():
        return int(fake)
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


# Roblox gone for this long in the middle of a run = it closed or crashed.
# Without this the macro sat on "Paused" forever - fatal for an overnight run.
ROBLOX_GONE_AFTER = 45.0
ROBLOX_REOPENS_PER_HOUR = 3       # never a crash-and-relaunch loop
# Running with no window this long = stuck (crash box, never-exited client).
ROBLOX_STUCK_AFTER = 90.0


def keep_awake(on):
    """While a run is going, stop Windows going to sleep - a sleeping PC ends
    an AFK session for good. Only the PC; the screen may still turn off.
    Per thread, so call it from the Tk thread."""
    try:
        flags = 0x80000000 | (0x00000001 if on else 0)  # CONTINUOUS | SYSTEM
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))
    except Exception:
        return False


LOG_KEEP_BYTES = 5 * 1024 * 1024


def rotate_log(path=None, limit=LOG_KEEP_BYTES):
    """At start-up, a log over `limit` becomes log.old.txt (replacing the
    previous one) - day-long runs grew it without end. True if rotated."""
    path = path or LOG_PATH
    try:
        if os.path.getsize(path) > limit:
            os.replace(path, os.path.splitext(path)[0] + ".old.txt")
            return True
    except OSError:
        pass
    return False


SINGLE_INSTANCE_MUTEX = "Local\\GlassMacro.SingleInstance"


def claim_single_instance():
    """True if no other GlassMacro is running. Two copies would both press
    keys and click in the same game; a second start just brings the open
    one to the front instead."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                 wintypes.LPCWSTR]
    handle = k32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX)
    # 183 = already exists. 5 = access denied, which is what a normal copy
    # gets when the first one was started with "Run as administrator".
    if ctypes.get_last_error() in (183, 5) or not handle:
        try:
            u = ctypes.windll.user32
            h = u.FindWindowW("TkTopLevel", APP_NAME) or 0
            if h:
                u.ShowWindow(h, 9)                  # SW_RESTORE
                u.SetForegroundWindow(h)
        except Exception:
            pass
        return False
    globals()["_instance_mutex"] = handle        # held until the app exits
    return True


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
# 1.1 sidebar: the selected page's row, and a row under the mouse
NAV_ACTIVE, NAV_HOVER = "#142432", "#101b28"


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
# By the repo's permanent number, not its name: if the account were ever
# renamed or deleted, someone could register "Ariliux" again and publish a
# fake GlassMacro - the number can never be re-registered.
REPO_ID = 1398827524
LATEST_API = f"https://api.github.com/repositories/{REPO_ID}/releases/latest"
UPDATE_EVERY = 12 * 3600       # runs last for days, so check again now and then
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")


def version_tuple(v):
    """'v1.0.3' -> (1, 0, 3); anything without a number -> ().

    Only the first three numbers count, so a tag like 'v1.0.4-x64' can't look
    newer than 1.0.4 itself."""
    return tuple(int(n) for n in re.findall(r"\d+", str(v))[:3])


ASSET_PREFIX = f"https://github.com/{REPO}/releases/download/"
UPDATE_DIR = os.path.join(DATA_DIR, "update")


def _http(req, timeout):
    """The one place a request leaves the app. urlopen is looked up on every
    call, so a test that fakes urllib.request.urlopen still catches it."""
    import urllib.request
    return urllib.request.urlopen(req, timeout=timeout)


def release_info(timeout=6.0):
    """The newest published release, or None. A dict with:
    version, page (release page), zip (download url or None), sha256.

    Never raises - no network, a GitHub outage or a rate limit all just mean
    "no news", and the app carries on exactly as before.
    """
    import urllib.request
    req = urllib.request.Request(LATEST_API, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{APP_NAME}/{APP_VER}"})
    try:
        with _http(req, timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        tag = str(data.get("tag_name") or "")
        if data.get("draft") or data.get("prerelease") or not version_tuple(tag):
            return None
        url = str(data.get("html_url") or "")
        if not url.startswith(f"https://github.com/{REPO}/"):
            url = RELEASES_URL           # only ever open our own release page
        info = {"version": tag.lstrip("vV"), "page": url, "zip": None,
                "sha256": None}
        # the app zip, but only from THIS repo's releases and only with the
        # fingerprint GitHub publishes for it - no fingerprint, no auto-update
        for asset in data.get("assets") or []:
            name = str(asset.get("name") or "")
            link = str(asset.get("browser_download_url") or "")
            digest = str(asset.get("digest") or "")
            if (re.fullmatch(r"GlassMacro-v[\d.]+\.zip", name)
                    and link.startswith(ASSET_PREFIX)
                    and re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest)):
                info["zip"], info["sha256"] = link, digest[7:].lower()
                break
        return info
    except Exception:
        return None


def latest_release(timeout=6.0):
    """(version, page url) of the newest published release, or None."""
    info = release_info(timeout)
    return (info["version"], info["page"]) if info else None


def download_update(url, sha256, dest, progress=None, timeout=30):
    """Download url to dest, fingerprinting it on the way. True only if the
    SHA-256 matches - anything else leaves no file behind."""
    import hashlib
    import urllib.request
    tmp = dest + ".part"
    h = hashlib.sha256()
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": f"{APP_NAME}/{APP_VER}"})
        with _http(req, timeout) as r, \
                open(tmp, "wb") as out:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        if h.hexdigest() != str(sha256).lower():
            raise ValueError("fingerprint mismatch")
        os.replace(tmp, dest)
        return True
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def stage_update(zip_path, stage_dir):
    """Unpack the new version into stage_dir. Path of its GlassMacro.exe, or
    None if the zip is not a GlassMacro folder build."""
    import zipfile
    shutil.rmtree(stage_dir, ignore_errors=True)
    os.makedirs(stage_dir, exist_ok=True)
    root = os.path.realpath(stage_dir)
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():           # nothing may land outside
            dest = os.path.realpath(os.path.join(root, name))
            if not dest.startswith(root + os.sep):
                return None
        z.extractall(root)
    for folder in (os.path.join(root, "GlassMacro"), root):
        exe = os.path.join(folder, "GlassMacro.exe")
        if os.path.isfile(exe) and os.path.isdir(os.path.join(folder,
                                                              "_internal")):
            # every file and its size, so the swap can refuse to install a
            # staging copy that lost files after unpacking
            sizes = {}
            for base, _dirs, files in os.walk(folder):
                for f in files:
                    full = os.path.join(base, f)
                    sizes[os.path.relpath(full, folder)] = os.path.getsize(full)
            with open(os.path.join(root, "manifest.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(sizes, fh)
            return exe
    return None


def _wait_for_exit(pid, seconds=30):
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(0x00100000, False, int(pid))   # SYNCHRONIZE
    if handle:
        k32.WaitForSingleObject(handle, int(seconds * 1000))
        k32.CloseHandle(handle)


def swap_in_update(src_dir, dst_dir, manifest=None, tries=60):
    """Put the new GlassMacro.exe and _internal from src_dir into dst_dir.

    The slow part - copying 160 MB - goes into _internal.new and
    GlassMacro.exe.new NEXT TO the install, which stays complete and
    runnable the whole time. Only once the old exe is free do three quick
    renames switch over, and if any of them fails they are undone. A first
    version moved the old files aside before copying, and anything that
    went wrong mid-copy (someone reopening GlassMacro, antivirus holding a
    file) could leave an install that no longer started.

    True when switched; False when nothing changed. Nothing else in dst_dir
    is ever touched.
    """
    internal = os.path.join(dst_dir, "_internal")
    new_int, old_int = internal + ".new", internal + ".old"
    exe = os.path.join(dst_dir, "GlassMacro.exe")
    new_exe = exe + ".new"

    def drop_new():
        shutil.rmtree(new_int, ignore_errors=True)
        try:
            os.remove(new_exe)
        except OSError:
            pass

    for leftover in (new_int, old_int):
        shutil.rmtree(leftover, ignore_errors=True)
    # 1. copy, and check the copy against the list made at unpacking
    try:
        shutil.copytree(os.path.join(src_dir, "_internal"), new_int)
        shutil.copy2(os.path.join(src_dir, "GlassMacro.exe"), new_exe)
        for rel, size in (manifest or {}).items():
            here = (new_exe if rel == "GlassMacro.exe" else
                    os.path.join(dst_dir, rel.replace("_internal",
                                                      "_internal.new", 1)))
            if not os.path.isfile(here) or os.path.getsize(here) != size:
                raise RuntimeError(f"the new files are incomplete ({rel})")
    except Exception:
        drop_new()
        raise
    # 2. wait until the old exe is free (a onefile launcher lingers a moment)
    for _ in range(tries):
        try:
            if os.path.exists(exe):
                with open(exe, "r+b"):
                    pass
            break
        except OSError:
            time.sleep(0.5)
    else:
        drop_new()
        return False
    # 3. switch with renames; undo them if any fails
    had_old = os.path.isdir(internal)
    try:
        if had_old:
            os.rename(internal, old_int)     # fails if anything runs from it
    except OSError:
        drop_new()
        return False
    moved_in = False
    try:
        os.rename(new_int, internal)
        moved_in = True
        os.replace(new_exe, exe)
    except Exception:
        if moved_in:
            os.rename(internal, new_int)
        if had_old:
            os.rename(old_int, internal)
        drop_new()
        raise
    shutil.rmtree(old_int, ignore_errors=True)
    return True


def finish_update(dst_dir, old_pid, old_version):
    """Runs in the NEW version, started from the staging folder with
    --finish-update: wait for the old app to close, swap the files, start
    the updated app. No window of its own."""
    def note(line):
        try:
            with io.open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(time.strftime("%Y-%m-%d %H:%M:%S") + "  " + line
                         + chr(10))
        except Exception:
            pass
    _wait_for_exit(old_pid)
    src_dir = os.path.dirname(os.path.abspath(sys.executable))
    manifest = None
    for folder in (os.path.dirname(src_dir), src_dir):
        try:
            with open(os.path.join(folder, "manifest.json"),
                      encoding="utf-8") as fh:
                manifest = json.load(fh)
            break
        except (OSError, ValueError):
            pass
    if manifest is None:
        note("update failed: the list of new files is missing")
        ok = False
    else:
        try:
            ok = swap_in_update(src_dir, dst_dir, manifest)
            if not ok:
                note("update skipped: GlassMacro was still open - nothing "
                     "changed")
        except Exception as exc:
            note(f"update failed while copying: {exc} - nothing changed")
            ok = False
    if ok:
        try:
            os.makedirs(UPDATE_DIR, exist_ok=True)
            with open(os.path.join(UPDATE_DIR, "updated.txt"), "w",
                      encoding="utf-8") as fh:
                fh.write(old_version)
        except Exception:
            pass
        note(f"update installed: {old_version} -> {APP_VER}")
    exe = os.path.join(dst_dir, "GlassMacro.exe")
    if not ok:
        # if the old copy is open again (someone double-clicked it), leave it
        # be rather than starting a second one
        try:
            with open(exe, "r+b"):
                pass
        except OSError:
            return
    try:
        os.startfile(exe)
    except Exception as exc:
        note(f"could not reopen GlassMacro: {exc}")


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


# ------------------------------------------------------ Discord alerts ---
# Opt-in only: nothing is sent until the user turns alerts on AND pastes their
# own webhook link. A webhook link works like a password for one channel, so
# it is never logged and is masked anywhere it is shown. Only a real Start can
# send anything (see GlassMacro._hook), and GLASSMACRO_NO_SEND blocks every
# post outright - the tests and the screenshot tool always set it.
HOOK_RE = (r"https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/"
           r"(?:v\d{1,2}/)?webhooks/(\d{17,20})/([A-Za-z0-9_-]{60,100})/?")
HOOK_UA = f"{APP_NAME}/{APP_VER} (+https://github.com/{REPO})"
HOOK_TIMEOUT = 10
HOOK_BATCH_WAIT = 2.0          # gather events this long into one message
HOOK_MAX_EMBEDS = 10           # Discord's limits for one message
HOOK_MAX_CHARS = 6000
HOOK_GAP = 2.5                 # at most one post this often
HOOK_BACKOFF = (2, 5, 15, 30, 60)
HOOK_429_WAITS = 3
HOOK_ACCENT, HOOK_GREEN, HOOK_AMBER, HOOK_RED, HOOK_MUTED = (
    0x5ECBFF, 0x4ADE80, 0xFBBF24, 0xF87171, 0x56687E)
HOOK_COLOURS = {"start": HOOK_ACCENT, "hourly": HOOK_GREEN,
                "updated": HOOK_GREEN, "reopen": HOOK_AMBER,
                "restart": HOOK_AMBER, "reconnect": HOOK_AMBER,
                "paused10": HOOK_AMBER, "error": HOOK_RED,
                "gave_up": HOOK_RED, "no_join": HOOK_RED}
# when the queue is full the least important go first
PRI_PERIODIC, PRI_MILESTONE, PRI_NORMAL = 0, 1, 2


def webhook_defaults():
    return {"enabled": False, "url": "", "user_id": "",
            "events": {"start_stop": True, "hourly": True, "error": True,
                       "stuck": True, "paused": True, "recover": False,
                       "updated": False},
            "mention": {"error": True, "stuck": True, "paused": True}}


def _hook_parts(url):
    m = re.fullmatch(HOOK_RE, str(url or "").strip())
    return m.groups() if m else None


def normalize_hook(url):
    """The canonical https://discord.com/api/webhooks/<id>/<token>, or None
    for anything that is not a Discord webhook link."""
    parts = _hook_parts(url)
    if not parts:
        return None
    return f"https://discord.com/api/webhooks/{parts[0]}/{parts[1]}"


def mask_hook(url):
    """'discord.com/…/webhooks/1234…5678/••••' - safe to show; '' if invalid."""
    parts = _hook_parts(url)
    if not parts:
        return ""
    wid = parts[0]
    return f"discord.com/…/webhooks/{wid[:4]}…{wid[-4:]}/••••"


def scrub(text, url=None):
    """Text that is safe to send: no webhook token, no paths, no PC or user
    name, at most 300 characters."""
    s = str(text or "")
    parts = _hook_parts(url)
    if parts:
        s = s.replace(parts[1], "••••")
    s = re.sub(r"https?://\S*webhooks/\S+", "[webhook link]", s)
    s = re.sub(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s'\"<>|]*", "[path]", s)
    s = re.sub(r"\\\\[^\s'\"<>|]+", "[path]", s)
    for name in {os.environ.get("USERNAME"), os.environ.get("COMPUTERNAME")}:
        if name and len(name) >= 3:
            s = re.sub(re.escape(name), "[name]", s, flags=re.I)
    return s if len(s) <= 300 else s[:297] + "..."


def build_embed(kind, title, desc, colour_int=None, fields=(), footer=""):
    """One Discord embed. The footer is only ever 'GlassMacro <ver> · run #N'
    - never the PC name, the user name or a path."""
    embed = {"title": str(title)[:256],
             "description": str(desc or "")[:4096],
             "color": int(colour_int if colour_int is not None
                          else HOOK_COLOURS.get(kind, HOOK_MUTED)),
             "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if fields:
        embed["fields"] = [{"name": str(n)[:256], "value": str(v)[:1024] or "-",
                            "inline": True} for n, v in list(fields)[:25]]
    if footer:
        embed["footer"] = {"text": str(footer)[:2048]}
    return embed


def build_payload(embeds, mention_ids=()):
    """The message. Mentions are opt-in per user id; parse is always empty,
    so text in an embed can never ping @everyone or a role."""
    ids = [i for i in dict.fromkeys(str(m) for m in mention_ids if m)
           if re.fullmatch(r"\d{17,20}", i)]
    allowed = {"parse": []}
    if ids:
        allowed["users"] = ids
    return {"username": APP_NAME,
            "content": " ".join(f"<@{i}>" for i in ids),
            "allowed_mentions": allowed,
            "embeds": list(embeds)}


def embed_chars(embed):
    """What Discord counts toward its 6000-character limit."""
    n = len(embed.get("title", "")) + len(embed.get("description", ""))
    n += len((embed.get("footer") or {}).get("text", ""))
    for f in embed.get("fields") or []:
        n += len(f.get("name", "")) + len(f.get("value", ""))
    return n


class WebhookSender:
    """Posts alerts on its own daemon thread, started on the first enqueue.

    enqueue() never blocks. Events are batched (up to 2 s, 10 embeds, 6000
    characters a post), posts are paced, rate limits and outages are waited
    out, and a link Discord rejects stops everything for the session. Only
    state changes are logged, as fixed 'webhook:' lines that never contain
    the link or any word the log readers react to.
    """

    def __init__(self, url, *, transport=None, sleep=time.sleep, log=None):
        self.url = normalize_hook(url) or ""
        self._transport = transport      # None = _http, looked up per post
        self._sleep = sleep
        self._log = log
        self._q = queue.Queue(30)
        self._lock = threading.Lock()
        self._thread = None
        self._pending = 0
        self._hurry = False
        self._last_post = None
        self._failing = False
        self._retry = 1.0
        self.dead = False
        self.dropped = 0
        self.sent = 0

    def _say(self, line):
        if self._log:
            try:
                self._log(line)
            except Exception:
                pass

    def _done(self, n):
        with self._lock:
            self._pending = max(0, self._pending - n)

    # ---- queue ----
    def enqueue(self, event, priority=PRI_NORMAL):
        """Queue {"embed": ..., "mention": user id or None}. Never blocks;
        False if it was dropped."""
        if self.dead or not self.url:
            return False
        item = (int(priority), event)
        try:
            with self._lock:
                self._q.put_nowait(item)
                self._pending += 1
        except queue.Full:
            q = self._q
            with self._lock, q.mutex:
                dq = q.queue
                if len(dq) < q.maxsize:          # the sender took one meanwhile
                    dq.append(item)
                    q.unfinished_tasks += 1
                    q.not_empty.notify()
                    self._pending += 1
                else:
                    low = min(range(len(dq)), key=lambda i: dq[i][0])
                    self.dropped += 1
                    if dq[low][0] > item[0]:
                        return False             # the new one matters least
                    del dq[low]
                    dq.append(item)
        self._start()
        return True

    def _start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            t = threading.Thread(target=self._loop, daemon=True,
                                 name="GlassMacro webhook")
            self._thread = t
        t.start()

    def _loop(self):
        while not self.dead:
            try:
                first = self._q.get(timeout=60)
            except queue.Empty:
                continue
            try:
                self._run_batch(self._gather(first, HOOK_BATCH_WAIT))
            except Exception:
                pass
        self._discard()

    def _gather(self, first, wait):
        batch = [first[1]]
        end = time.monotonic() + wait
        while len(batch) < HOOK_MAX_EMBEDS:
            left = 0 if self._hurry else end - time.monotonic()
            try:
                item = (self._q.get(timeout=min(left, 0.1)) if left > 0
                        else self._q.get_nowait())
            except queue.Empty:
                if left > 0:
                    continue
                break
            batch.append(item[1])
        return batch

    def _discard(self):
        n = 0
        while True:
            try:
                self._q.get_nowait()
                n += 1
            except queue.Empty:
                break
        self._done(n)

    def pump(self):
        """Send everything queued right now on the calling thread, without
        the batching wait. Returns the statuses (tests use it)."""
        out = []
        while True:
            try:
                first = self._q.get_nowait()
            except queue.Empty:
                return out
            out += self._run_batch(self._gather(first, 0))

    @staticmethod
    def _groups(events):
        group, chars = [], 0
        for ev in events:
            c = embed_chars(ev["embed"])
            if group and (len(group) >= HOOK_MAX_EMBEDS
                          or chars + c > HOOK_MAX_CHARS):
                yield group
                group, chars = [], 0
            group.append(ev)
            chars += c
        if group:
            yield group

    def _run_batch(self, events):
        out = []
        try:
            for group in self._groups(events):
                if self.dead:
                    out.append("dead")
                    break
                out.append(self._send(build_payload(
                    [e["embed"] for e in group],
                    [e.get("mention") for e in group])))
        finally:
            self._done(len(events))
        return out

    # ---- posting ----
    def _pace(self):
        if self._last_post is None or self._hurry:
            return
        gap = HOOK_GAP - (time.monotonic() - self._last_post)
        if gap > 0:
            self._sleep(gap)

    def _send(self, payload):
        """'ok', 'blocked', 'dropped' or 'dead'."""
        waits = backoffs = 0
        while not self.dead:
            self._pace()
            code = self._post(payload)
            if code == "blocked":
                return "blocked"
            self._last_post = time.monotonic()
            if code in (200, 204):
                self.sent += 1
                if self._failing:
                    self._failing = False
                    self._say("webhook: reaching Discord again")
                return "ok"
            if code == 429:
                waits += 1
                if waits > HOOK_429_WAITS:
                    return "dropped"
                self._sleep(self._retry + 0.25)
                continue
            if code in (401, 403, 404):
                self.dead = True
                self._say(f"webhook: Discord says the link no longer works "
                          f"({code}) - check the Discord page")
                return "dead"
            if 400 <= code < 500:
                return "dropped"              # a bad message: never resent
            # 5xx, no network, a timeout
            if not self._failing:
                self._failing = True
                self._say("webhook: couldn't reach Discord - will retry")
            if backoffs >= len(HOOK_BACKOFF) or self._hurry:
                return "dropped"
            self._sleep(HOOK_BACKOFF[backoffs])
            backoffs += 1
        return "dead"

    def _post(self, payload):
        if os.environ.get("GLASSMACRO_NO_SEND"):
            return "blocked"
        import urllib.error
        import urllib.request
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"User-Agent": HOOK_UA,
                     "Content-Type": "application/json"})
        send = self._transport or _http
        try:
            r = send(req, HOOK_TIMEOUT)
            try:
                code = getattr(r, "status", None) or r.getcode()
            finally:
                try:
                    r.close()
                except Exception:
                    pass
            return int(code)
        except urllib.error.HTTPError as e:
            self._retry = 1.0
            if e.code == 429:
                self._retry = self._retry_after(e)
            try:
                e.close()
            except Exception:
                pass
            return int(e.code)
        except Exception:
            return 0

    @staticmethod
    def _retry_after(e):
        secs = None
        try:
            secs = float(json.loads(e.read().decode("utf-8"))["retry_after"])
        except Exception:
            try:
                secs = float(e.headers.get("Retry-After"))
            except Exception:
                pass
        return min(60.0, max(0.0, secs)) if secs is not None else 1.0

    def post_now(self, payload):
        """One message right now on the calling thread - the Discord page's
        Send test. Still blocked by GLASSMACRO_NO_SEND."""
        status = self._send(payload)
        if status == "ok":
            self._say("webhook: test message sent")
        return status

    def flush(self, timeout=2.0):
        """Give queued alerts up to `timeout` seconds to go out (on close)."""
        self._hurry = True
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self._lock:
                left = self._pending
            if left <= 0 or self.dead:
                return True
            if self._thread is None or not self._thread.is_alive():
                break
            time.sleep(0.05)
        with self._lock:
            return self._pending <= 0


# ------------------------------------------------------- lifetime stats ---
# stats.json: totals across every run. Written whole (tmp, fsync, swap), with
# the previous copy kept as stats.json.bak; a damaged file is set aside, never
# deleted. Time is added a clamped second at a time, so a clock that jumps
# can't add hours.
STATS_PATH = os.path.join(DATA_DIR, "stats.json")
STATS_KEEP_DAYS = 60
STATS_COUNTERS = ("runs", "loadouts", "rejoins", "reconnects",
                  "roblox_reopens", "roblox_restarts", "reopen_giveups",
                  "errors", "join_failures", "quiet_stretches",
                  "skipped_picks", "unclean_ends")


def new_stats():
    st = {"v": 1, "total_secs": 0, "runs": 0, "longest_secs": 0,
          "longest_on": ""}
    for k in STATS_COUNTERS:
        st.setdefault(k, 0)
    st.update({"first_run": "", "last_run": "", "days": {}, "current": None})
    return st


def _num(v):
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and v == v and 0 <= v < 1e12)


def _clean_stats(data):
    """A loaded file in the v1 shape; anything missing or the wrong type is
    the default."""
    st = new_stats()
    for k in ("total_secs", "longest_secs") + STATS_COUNTERS:
        if _num(data.get(k)):
            st[k] = int(data[k]) if k in STATS_COUNTERS else data[k]
    for k in ("longest_on", "first_run", "last_run"):
        if isinstance(data.get(k), str):
            st[k] = data[k]
    days = data.get("days")
    if isinstance(days, dict):
        st["days"] = {d: v for d, v in days.items()
                      if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(d)) and _num(v)}
    cur = data.get("current")
    if cur is not None:
        cur = cur if isinstance(cur, dict) else {}
        st["current"] = {"start": str(cur.get("start") or ""),
                         "secs": cur["secs"] if _num(cur.get("secs")) else 0,
                         "flushed_at": cur["flushed_at"]
                         if _num(cur.get("flushed_at")) else 0}
    return st


def save_stats(data, path=None):
    path = path or STATS_PATH
    try:
        days = data.get("days")
        if isinstance(days, dict) and len(days) > STATS_KEEP_DAYS:
            for k in sorted(days)[:-STATS_KEEP_DAYS]:
                del days[k]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        if os.path.exists(path):
            try:
                shutil.copyfile(path, path + ".bak")
            except OSError:
                pass
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def load_stats(path=None):
    """stats.json, else stats.json.bak, else a fresh start. A damaged
    stats.json is renamed to stats.json.corrupt-<time> so it can't overwrite
    the good .bak. A run that never ended cleanly (crash, power cut) still
    counts toward the longest run."""
    path = path or STATS_PATH
    data, corrupt = None, False
    for p in (path, path + ".bak"):
        try:
            with open(p, encoding="utf-8") as fh:
                raw = json.load(fh)
            if not isinstance(raw, dict):
                raise ValueError("not a stats file")
            data = raw
            break
        except FileNotFoundError:
            continue
        except Exception:
            if p == path:
                corrupt = True
    if corrupt:
        try:
            os.replace(path, f"{path}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}")
        except OSError:
            pass
    st = _clean_stats(data) if data is not None else new_stats()
    cur = st["current"]
    if cur is not None:
        if cur["secs"] > st["longest_secs"]:
            st["longest_secs"] = cur["secs"]
            st["longest_on"] = cur["start"][:10]
        st["unclean_ends"] += 1
        st["current"] = None
        save_stats(st, path)
    return st


def stats_add_time(st, start, end):
    """Add [start, end) to the total and to each local day it covers, split
    at midnight."""
    if not end > start:
        return
    st["total_secs"] = st.get("total_secs", 0) + (end - start)
    days = st.setdefault("days", {})
    t = start
    while t < end:
        lt = time.localtime(t)
        nxt = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1,
                           0, 0, 0, 0, 0, -1))
        stop = min(end, nxt) if nxt > t else end
        key = time.strftime("%Y-%m-%d", lt)
        days[key] = days.get(key, 0) + (stop - t)
        t = stop



# ------------------------------------------------------ warning pop-up ---
# Windows' own warning pop-up - the task dialog: yellow triangle, a bold
# headline, the details, and real buttons - with the Windows 11 "Foreground"
# sound. A corner notification was tried first and was too easy to ignore.
#
# The sound is played here, once. The dialog gets the warning icon as a plain
# icon handle rather than "the warning icon", so Windows has no event to play
# its own sound for - on Windows 11 that would be the softer "Background" one,
# and the two would play on top of each other.
class _TDBUTTON(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("nButtonID", ctypes.c_int), ("pszButtonText", wintypes.LPCWSTR)]


class _TASKDIALOGCONFIG(ctypes.Structure):
    _pack_ = 1                     # commctrl.h declares it inside pshpack1
    _fields_ = [("cbSize", wintypes.UINT), ("hwndParent", wintypes.HWND),
                ("hInstance", wintypes.HINSTANCE), ("dwFlags", ctypes.c_int),
                ("dwCommonButtons", ctypes.c_int),
                ("pszWindowTitle", wintypes.LPCWSTR),
                ("hMainIcon", wintypes.HICON),
                ("pszMainInstruction", wintypes.LPCWSTR),
                ("pszContent", wintypes.LPCWSTR), ("cButtons", wintypes.UINT),
                ("pButtons", ctypes.POINTER(_TDBUTTON)),
                ("nDefaultButton", ctypes.c_int),
                ("cRadioButtons", wintypes.UINT),
                ("pRadioButtons", ctypes.POINTER(_TDBUTTON)),
                ("nDefaultRadioButton", ctypes.c_int),
                ("pszVerificationText", wintypes.LPCWSTR),
                ("pszExpandedInformation", wintypes.LPCWSTR),
                ("pszExpandedControlText", wintypes.LPCWSTR),
                ("pszCollapsedControlText", wintypes.LPCWSTR),
                ("hFooterIcon", wintypes.HICON),
                ("pszFooter", wintypes.LPCWSTR),
                ("pfCallback", ctypes.c_void_p),
                ("lpCallbackData", ctypes.c_void_p),
                ("cxWidth", wintypes.UINT)]


class _ACTCTXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.ULONG), ("dwFlags", wintypes.DWORD),
                ("lpSource", wintypes.LPCWSTR),
                ("wProcessorArchitecture", wintypes.USHORT),
                ("wLangId", wintypes.USHORT),
                ("lpAssemblyDirectory", wintypes.LPCWSTR),
                ("lpResourceName", wintypes.LPCWSTR),
                ("lpApplicationName", wintypes.LPCWSTR),
                ("hModule", wintypes.HMODULE)]


_TDF_USE_HICON_MAIN, _TDF_ALLOW_CANCEL = 0x0002, 0x0008
_TDCBF_OK = 0x0001
_ID_SETTINGS = 100
NOTIFY_GAP = 60.0            # never more than one warning a minute
FOREGROUND_WAV = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                              "Media", "Windows Foreground.wav")


def _task_dialog_function():
    """TaskDialogIndirect from Common Controls 6, or None.

    Version 6 only loads inside an "activation context". The built exe's
    manifest asks for it; plain python.exe does not, so borrow the one
    Windows ships inside shell32.dll (resource 124) for the call.
    """
    k32 = ctypes.WinDLL("kernel32")
    k32.CreateActCtxW.restype = wintypes.HANDLE
    k32.CreateActCtxW.argtypes = [ctypes.POINTER(_ACTCTXW)]
    k32.ActivateActCtx.argtypes = [wintypes.HANDLE,
                                   ctypes.POINTER(ctypes.c_size_t)]
    system = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                          "System32")
    ctx = _ACTCTXW()
    ctx.cbSize = ctypes.sizeof(_ACTCTXW)
    ctx.dwFlags = 0x004 | 0x008     # ASSEMBLY_DIRECTORY_VALID | RESOURCE_NAME
    ctx.lpSource = os.path.join(system, "shell32.dll")
    ctx.lpAssemblyDirectory = system
    ctx.lpResourceName = ctypes.cast(ctypes.c_void_p(124), wintypes.LPCWSTR)
    handle = k32.CreateActCtxW(ctypes.byref(ctx))
    if handle and handle != wintypes.HANDLE(-1).value:
        cookie = ctypes.c_size_t()
        k32.ActivateActCtx(handle, ctypes.byref(cookie))
    try:
        fn = ctypes.WinDLL("comctl32").TaskDialogIndirect
    except (OSError, AttributeError):
        return None
    fn.restype = ctypes.c_long
    fn.argtypes = [ctypes.POINTER(_TASKDIALOGCONFIG),
                   ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
                   ctypes.POINTER(ctypes.c_int)]
    return fn


def play_foreground_sound():
    """The Windows 11 "Foreground" sound, once, without waiting for it."""
    try:
        import winsound
        if os.path.exists(FOREGROUND_WAV):
            winsound.PlaySound(FOREGROUND_WAV, winsound.SND_FILENAME
                               | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
            return
    except Exception:
        pass
    try:
        ctypes.windll.user32.MessageBeep(0x10)  # Critical Stop = Foreground
    except Exception:
        pass


_TDCALLBACK = ctypes.WINFUNCTYPE(ctypes.c_long, wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM,
                                 ctypes.c_ssize_t)


def _gem_icon(size):
    """GlassMacro's own gem, as a Windows icon handle of the given size."""
    try:
        u = ctypes.WinDLL("user32")
        u.LoadImageW.restype = wintypes.HANDLE
        u.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR,
                                 wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                 wintypes.UINT]
        # IMAGE_ICON, LR_LOADFROMFILE
        return u.LoadImageW(None, resource("glassmacro.ico"), 1, size, size,
                            0x10)
    except Exception:
        return None


def ask_to_update(new_version=None, notes=None):
    """The update pop-up: a plain Windows box with GlassMacro's gem -
    "v1.0.8 is available. Do you want to update?" - Yes / No. Blocks
    until answered, so call it on its own thread. 'yes', 'no', or None."""
    fn = _task_dialog_function()
    play_foreground_sound()
    text = (f"v{new_version} is available.\nDo you want to update?"
            if new_version else
            "An update is available.\nDo you want to update?")
    if fn is None:                               # very old Windows: MessageBox
        got = ctypes.windll.user32.MessageBoxW(
            None, text, "GlassMacro", 0x40 | 0x4 | 0x40000 | 0x10000)
        return "yes" if got == 6 else "no"
    big, small = _gem_icon(32), _gem_icon(16)
    u = ctypes.WinDLL("user32")
    u.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                               wintypes.LPARAM]

    def on_event(hwnd, msg, wparam, lparam, data):
        if msg == 0 and big:                     # TDN_CREATED: title bar gem
            u.SendMessageW(hwnd, 0x80, 1, big)    # WM_SETICON, ICON_BIG
            u.SendMessageW(hwnd, 0x80, 0, small or big)
        return 0
    callback = _TDCALLBACK(on_event)
    cfg = _TASKDIALOGCONFIG()
    cfg.cbSize = ctypes.sizeof(_TASKDIALOGCONFIG)
    cfg.dwFlags = 0x0002 | 0x0008                # USE_HICON_MAIN | ALLOW_CANCEL
    cfg.dwCommonButtons = 0x2 | 0x4              # Yes | No
    cfg.pszWindowTitle = "GlassMacro"
    cfg.hMainIcon = big or ctypes.windll.user32.LoadIconW(
        None, ctypes.cast(ctypes.c_void_p(32516), wintypes.LPCWSTR))
    cfg.pszContent = text                        # no headline: a plain box
    cfg.nDefaultButton = 6                       # Yes
    cfg.pfCallback = ctypes.cast(callback, ctypes.c_void_p).value
    pressed = ctypes.c_int(0)
    if fn(ctypes.byref(cfg), ctypes.byref(pressed), None, None) != 0:
        return None
    return "yes" if pressed.value == 6 else "no"


class WarningPopup:
    """Shows Windows' warning pop-up. show() blocks until it is closed, so
    run it on its own thread - never on the Tk thread."""

    def __init__(self):
        self.last = 0.0
        self.open = False

    def show(self, title, headline, details, settings_button=True,
             yes_no=False):
        """'settings', 'ok', 'yes', 'no', or None if nothing could be shown.
        yes_no: an information pop-up with Yes / No instead of a warning."""
        self.open, self.last = True, time.time()
        try:
            fn = _task_dialog_function()
            icon = ctypes.windll.user32.LoadIconW(
                None, ctypes.cast(ctypes.c_void_p(32516 if yes_no else 32515),
                                  wintypes.LPCWSTR))
            play_foreground_sound()
            if fn is None:                       # very old Windows: MessageBox
                flags = (0x40 | 0x4 if yes_no else 0x30) | 0x40000 | 0x10000
                got = ctypes.windll.user32.MessageBoxW(
                    None, f"{headline}\n\n{details}", title, flags)
                return ("yes" if got == 6 else "no") if yes_no else "ok"
            buttons = (_TDBUTTON * 1)(_TDBUTTON(_ID_SETTINGS,
                                                "Open display settings"))
            cfg = _TASKDIALOGCONFIG()
            cfg.cbSize = ctypes.sizeof(_TASKDIALOGCONFIG)
            cfg.dwFlags = _TDF_USE_HICON_MAIN | _TDF_ALLOW_CANCEL
            cfg.dwCommonButtons = 0x2 | 0x4 if yes_no else _TDCBF_OK
            cfg.pszWindowTitle = title
            cfg.hMainIcon = icon
            cfg.pszMainInstruction = headline
            cfg.pszContent = details
            if settings_button and not yes_no:
                cfg.cButtons = 1
                cfg.pButtons = buttons
            cfg.nDefaultButton = (6 if yes_no else
                                  _ID_SETTINGS if settings_button else 1)
            pressed = ctypes.c_int(0)
            if fn(ctypes.byref(cfg), ctypes.byref(pressed), None, None) != 0:
                return None
            if yes_no:
                return "yes" if pressed.value == 6 else "no"
            return "settings" if pressed.value == _ID_SETTINGS else "ok"
        finally:
            self.open = False


class GlassButton(ctk.CTkFrame):
    """The big Start / Stop button: a glyph, a word and an F8 keycap.

    A CTkButton holds one string, and 'Start    F8' spaced out by hand read
    like a placeholder. This is a frame that behaves like a button. Its
    configure() takes the options the app sets and passes anything else (the
    bg_color a parent frame pushes down) straight through.
    """

    def __init__(self, master, command, font, glyph_font, key_font,
                 height=46, corner_radius=12, width=None):
        size = {"width": width} if width else {}
        super().__init__(master, height=height, corner_radius=corner_radius,
                         border_width=1, fg_color=ACCENT, border_color=ACCENT,
                         **size)
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
        # a plain file read - before the window is sized, so a saved window
        # size (later) can be used
        self.settings = load_settings()
        self._settings_defaults()
        self._fit_to_screen()
        self.configure(fg_color=BG)
        self._set_icon()
        self.after(50, self._style_titlebar)

        # session stats shown on the status card
        self.session_start = None
        self.n_picks = 0
        self.n_joins = 0
        self.n_recov = 0                    # reopens, restarts, reconnects
        self._fs = self._fresh_fs()

        self.cal, self.tile = load_cal()
        self.running = False
        self.watching = False
        self.calibrating = False
        self._q = queue.Queue()

        self._build()
        self._drain()
        threading.Thread(target=self._update_worker, daemon=True).start()
        keyboard.add_hotkey("f8", self._hotkey)
        self.log(f"--- {APP_NAME} v{APP_VER} opened ---")
        self._after_update()
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

    def _settings_defaults(self):
        """Fill in missing settings with their defaults. Only missing keys:
        nothing the user chose is changed, and nothing is saved here."""
        def fill(dst, src):
            for k, v in src.items():
                if isinstance(v, dict):
                    if not isinstance(dst.get(k), dict):
                        dst[k] = {}
                    fill(dst[k], v)
                elif k not in dst:
                    dst[k] = v
        if not isinstance(self.settings, dict):
            self.settings = {}
        fill(self.settings, {"webhook": webhook_defaults()})

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
                              (35, ref(PANEL)),          # caption
                              (36, ref(SUBTLE)),         # caption text
                              (34, ref(LINE))):          # border
                dwm(hwnd, attr, ctypes.byref(val), ctypes.sizeof(val))
        except Exception:
            pass

    def _fit_to_screen(self, want_w=985, want_h=550):
        """985x550 where it fits; smaller where it doesn't.

        A 1080p laptop at the usual 150% scaling has only about 688 logical
        pixels above the taskbar, so the size is capped by the work area.
        CustomTkinter scales the size but not the position, so the position
        is worked out in real pixels.
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
        width = min(want_w, int(w / s) - 16)
        height = min(want_h, int(h / s) - 48)  # title bar and a little air
        # a 220px sidebar plus the Home page needs about 800 across
        self.minsize(800, 500)
        x = left + max(0, (w - round(width * s)) // 2)
        y = top + max(0, (h - round((height + 32) * s)) // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")

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
        # sidebar icons: Windows 11's icon font, Windows 10's, or plain
        # Segoe UI Symbol glyphs when neither is there
        if "Segoe Fluent Icons" in fams:
            self._fam_icon = "Segoe Fluent Icons"
        elif "Segoe MDL2 Assets" in fams:
            self._fam_icon = "Segoe MDL2 Assets"
        else:
            self._fam_icon = None
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
        self._feed_items = []               # newest first, merged like the feed
        self._feed_filter = "All"           # never saved: always starts at All
        self._log_follow = True
        self._log_query, self._log_mode = "", "All"
        self._log_shown = 0
        self._way = {"step": 0, "done": set()}
        self._tab = "activity"
        self._full_log = False
        self._slider_guard = False
        # lifetime stats and Discord alerts - only a real Start sets _live_run
        self._live_run = False
        self._worker = None
        self._hour_sent = None
        self._paused_since = None
        self._paused_sent = False
        self._stats = load_stats()
        self._sender = None
        self._flushed_at = 0
        self._flushed_picks = 0
        self._flushed_joins = 0
        self._stats_saved_at = 0
        self._run_seq = 0
        self._end_reason, self._end_detail = "stopped", ""
        self._updated_note = None

        sw, sh = screen_size()
        self._screen_ok = (sw, sh) == SUPPORTED_SCREEN
        self._scaling = display_scaling()

        # ---- the window: sidebar | 1px line | header / hairline / pages ----
        self.grid_columnconfigure(2, weight=1)
        self.grid_rowconfigure(2, weight=1)
        self._build_sidebar(sw, sh)
        ctk.CTkFrame(self, width=1, height=1, fg_color=LINE, corner_radius=0
                     ).grid(row=0, column=1, rowspan=3, sticky="ns")
        self._build_header()
        tk.Canvas(self, width=1, height=self._px(2), bg=HAIRLINE,
                  highlightthickness=0, bd=0).grid(row=1, column=2,
                                                   sticky="ew")
        # every page is built once and stays alive; switching only raises
        # one over the others (workers read switches and the threshold entry
        # from these widgets, so none may ever be destroyed)
        self.stack = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        self.stack.grid(row=2, column=2, sticky="nsew")
        self.stack.grid_rowconfigure(0, weight=1)
        self.stack.grid_columnconfigure(0, weight=1)
        self._pages = {}
        for key in self.PAGE_INFO:
            page = ctk.CTkFrame(self.stack, fg_color=BG, corner_radius=0)
            page.grid(row=0, column=0, sticky="nsew")
            self._pages[key] = page
        self._page = None

        self._build_home(self._pages["home"])
        self._build_weapons_page(self._pages["weapons"])
        self._build_activity(self._pages["activity"])
        self._build_log(self._pages["log"])
        self._build_wayback(self._pages["wayback"])
        self._build_settings(self._pages["settings"])
        self._build_stats(self._pages["stats"])
        self._build_discord(self._pages["discord"])
        self._build_about(self._pages["about"])

        self._show_page("home" if self._ready() else "weapons")
        self._show_cal()
        self._show_ffa()
        self._show_playtime(None)
        self._tick()
        self._animate()

    # ---- the window shell: sidebar, header, pages ----
    # key: (title, header subtitle, sidebar group, icon-font glyph, fallback)
    PAGE_INFO = {
        "home": ("Home", "What it's doing right now",
                 "RUN", "\ue80f", "⌂"),
        "activity": ("Activity", "What happened, newest first",
                     "RUN", "\ue81c", "◷"),
        "stats": ("Stats", "Lifetime playtime and runs",
                  "RUN", "\ue9d2", "▤"),
        "log": ("Log", "Every line it writes, for bug reports",
                "RUN", "\ue9f9", "≡"),
        "weapons": ("Weapons", "Where it clicks in the weapon picker",
                    "SETUP", "\uf272", "◎"),
        "wayback": ("Way back", "How it gets back into Free For All",
                    "SETUP", "\ue7a7", "↺"),
        "discord": ("Discord", "Alerts in your own Discord channel",
                    "ALERTS", "\uea8f", "✉"),
        "settings": ("Settings", "How it behaves while it runs",
                     "APP", "\ue713", "⚙"),
        "about": ("About", f"{APP_NAME} {APP_VER}",
                  "APP", "\ue946", "ⓘ"),
    }

    def _build_sidebar(self, sw, sh):
        side = ctk.CTkFrame(self, width=220, fg_color=PANEL, corner_radius=0)
        side.grid(row=0, column=0, rowspan=3, sticky="ns")
        side.grid_propagate(False)
        side.pack_propagate(False)
        self.sidebar = side

        # brand: gem, wordmark, version
        brand = ctk.CTkFrame(side, fg_color="transparent", height=52)
        brand.pack(fill="x", padx=20)
        brand.pack_propagate(False)
        gem = tk.Canvas(brand, width=self._px(20), height=self._px(20),
                        bg=PANEL, highlightthickness=0, bd=0)
        draw_gem(gem, 0, 0, self._px(20))
        gem.pack(side="left")
        ctk.CTkLabel(brand, text="Glass", text_color=ACCENT,
                     font=self.F(15, semi=True)).pack(side="left",
                                                      padx=(8, 0))
        ctk.CTkLabel(brand, text="Macro", text_color=TEXT,
                     font=self.F(15, semi=True)).pack(side="left")
        ctk.CTkLabel(brand, text=APP_VER, text_color=MUTED,
                     font=self.F(11)).pack(side="left", padx=(6, 0),
                                           pady=(3, 0))

        # footer, packed before the nav so a short window squeezes the nav
        # and never pushes the screen line or the update link off the end
        foot = ctk.CTkFrame(side, fg_color="transparent", height=44)
        foot.pack(side="bottom", fill="x")
        foot.pack_propagate(False)
        ctk.CTkFrame(foot, width=1, height=1, fg_color=HAIRLINE,
                     corner_radius=0).pack(side="top", fill="x", padx=14)
        self._side_foot = foot
        self.lbl_res = ctk.CTkLabel(foot, font=self.F(11), text="",
                                    anchor="w", justify="left",
                                    wraplength=184, height=16)
        self._paint_res(sw, sh, self._scaling)
        self.lbl_res.pack(side="left", padx=20)
        self.lnk_update = ctk.CTkLabel(foot, text="", text_color=ACCENT,
                                       font=self.F(11, semi=True),
                                       cursor="hand2")
        self._update_url = RELEASES_URL
        self.lnk_update.bind("<Button-1>", lambda _e: self._open_update())

        nav = ctk.CTkFrame(side, fg_color="transparent")
        nav.pack(fill="both", expand=True)
        self._nav = {}
        group = None
        icon_font = self.F(14, family=self._fam_icon or self._fam_sym)
        for key, (title, _sub, grp, glyph, alt) in self.PAGE_INFO.items():
            if grp != group:
                ctk.CTkLabel(nav, text=grp, text_color=MUTED, anchor="w",
                             font=self.F(10, semi=True), height=14
                             ).pack(fill="x", padx=22,
                                    pady=(0 if group is None else 8, 4))
                group = grp
            row = ctk.CTkFrame(nav, height=32, corner_radius=8,
                               fg_color="transparent")
            row.pack(fill="x", padx=10, pady=1)
            row.pack_propagate(False)
            icon = ctk.CTkLabel(row, text=glyph if self._fam_icon else alt,
                                width=20, height=20, text_color=SUBTLE,
                                font=icon_font)
            icon.pack(side="left", padx=(12, 0))
            lbl = ctk.CTkLabel(row, text=title, text_color=SUBTLE,
                               font=self.F(13), anchor="w", height=20)
            lbl.pack(side="left", padx=(10, 0))
            for w in (row, icon, lbl):
                w.bind("<Enter>", lambda _e, k=key: self._nav_hover(k, True),
                       add="+")
                w.bind("<Leave>", lambda _e, k=key: self._nav_hover(k, False),
                       add="+")
                w.bind("<ButtonRelease-1>",
                       lambda _e, k=key: self._nav_click(k), add="+")
                try:
                    w.configure(cursor="hand2")
                except Exception:
                    pass
            self._nav[key] = {"row": row, "icon": icon, "label": lbl,
                              "hover": False, "painted": None}
        # the 3x16 bar beside the selected row. Plain Tk: place() will not
        # size a CustomTkinter widget
        self._nav_ind = tk.Frame(nav, bg=ACCENT, bd=0, highlightthickness=0)

    def _nav_inside(self, key):
        row = self._nav[key]["row"]
        x, y = self.winfo_pointerxy()
        rx, ry = row.winfo_rootx(), row.winfo_rooty()
        return (rx <= x < rx + row.winfo_width()
                and ry <= y < ry + row.winfo_height())

    def _nav_hover(self, key, on):
        # moving from the row onto its own label fires Leave too
        if not on and self._nav_inside(key):
            return
        self._nav[key]["hover"] = on
        self._paint_nav()

    def _nav_click(self, key):
        if self._nav_inside(key):
            self._show_page(key)

    def _paint_nav(self):
        for key, n in self._nav.items():
            on = key == self._page
            fill = (NAV_ACTIVE if on else
                    NAV_HOVER if n["hover"] else "transparent")
            if n["painted"] == (on, fill):
                continue
            n["painted"] = (on, fill)
            n["row"].configure(fg_color=fill)
            n["icon"].configure(text_color=ACCENT if on else SUBTLE)
            n["label"].configure(text_color=TEXT if on else SUBTLE,
                                 font=self.F(13, semi=on))
        if self._page in self._nav:
            self._nav_ind.place(in_=self._nav[self._page]["row"], x=0,
                                rely=0.5, anchor="w", width=self._px(3),
                                height=self._px(16))
            self._nav_ind.lift()

    def _build_header(self):
        head = ctk.CTkFrame(self, height=56, fg_color=PANEL, corner_radius=0)
        head.grid(row=0, column=2, sticky="ew")
        head.grid_propagate(False)
        head.pack_propagate(False)
        self.header = head
        titles = ctk.CTkFrame(head, fg_color="transparent")
        titles.pack(side="left", padx=(24, 0))
        self.lbl_title = ctk.CTkLabel(titles, text="", text_color=TEXT,
                                      font=self.F(16, semi=True), anchor="w",
                                      height=22)
        self.lbl_title.pack(fill="x")
        self.lbl_sub = ctk.CTkLabel(titles, text="", text_color=MUTED,
                                    font=self.F(11), anchor="w", height=16)
        self.lbl_sub.pack(fill="x")

        # right to left: Start/Stop, the pin, the state pill
        self.btn_run = GlassButton(head, self._run_clicked,
                                   font=self.F(13, semi=True),
                                   glyph_font=self.F(10, family=self._fam_sym),
                                   key_font=self.F(10, semi=True,
                                                   family="Consolas"),
                                   height=36, corner_radius=10, width=156)
        self.btn_run.pack(side="right", padx=(0, 16))
        self.btn_pin = ctk.CTkButton(
            head, text="", width=34, height=34, corner_radius=10,
            fg_color="transparent", hover_color=CARD_HI, border_width=1,
            border_color=LINE, text_color=SUBTLE,
            font=self.F(13, family=self._fam_icon or self._fam_sym),
            command=self._toggle_pin)
        self.btn_pin.pack(side="right", padx=(0, 10))
        self.pill = ctk.CTkFrame(head, height=26, corner_radius=13,
                                 border_width=1, fg_color=PANEL,
                                 border_color=LINE)
        self.pill.pack(side="right", padx=(0, 10))
        d, r = self._px(10), self._px(4)
        self.pill_dot = tk.Canvas(self.pill, width=d, height=d, bg=PANEL,
                                  highlightthickness=0, bd=0)
        self.pill_dot.pack(side="left", padx=(10, 5))
        self._pill_core = self.pill_dot.create_oval(d / 2 - r, d / 2 - r,
                                                    d / 2 + r, d / 2 + r,
                                                    fill=MUTED, outline="")
        self.lbl_pill = ctk.CTkLabel(self.pill, text="Idle",
                                     text_color=SUBTLE, height=18,
                                     font=self.F(11, semi=True))
        self.lbl_pill.pack(side="left", padx=(0, 12), pady=4)
        self._pill_painted = None
        self._paint_pin()
        if self.settings.get("on_top"):
            self._apply_topmost()

    def _show_page(self, key):
        page = self._pages.get(key)
        if page is None:
            return
        self._page = key
        self._full_log = key == "log"      # log() follows the end only here
        if key in ("activity", "settings"):
            self._tab = key
        page.lift()
        title, sub = self.PAGE_INFO[key][:2]
        self.lbl_title.configure(text=title)
        self.lbl_sub.configure(text=sub)
        self._paint_nav()
        if key == "log" and self._log_follow:
            try:
                self.txt.see("end")
            except Exception:
                pass
        elif key == "stats":
            self._paint_stats()
            self._draw_days(True)

    def _show_tab(self, key):
        """The 1.0 tab names, still used by render_states.py."""
        self._show_page("settings" if key == "settings" else "activity")
        self._tab = key

    def _paint_pill(self):
        """Idle / Running / Paused... worked out fresh from the state the
        card already shows, so the two can never disagree."""
        try:
            if getattr(self, "_updating", False):
                text, colour = "Updating", ACCENT
            elif self.calibrating:
                text, colour = "Setting up · F8", ACCENT
            elif self.watching:
                text, colour = "Live test", ACCENT
            elif self._run_mode == "RUNNING" and self._state_title == "Paused":
                text, colour = "Paused", AMBER
            elif self._run_mode == "RUNNING" and self._dot_colour in (AMBER,
                                                                      RED):
                text, colour = "Recovering", AMBER
            elif self._run_mode == "RUNNING":
                text = ("Running · " + span(time.time()
                                                 - self.session_start)
                        if self.session_start else "Running")
                colour = GREEN
            elif not self._ready():
                text, colour = "Setup needed", AMBER
            else:
                text, colour = "Idle", None
            if colour is None:
                fill, border, dot, fg = PANEL, LINE, MUTED, SUBTLE
            else:
                fill, border = blend(PANEL, colour, 0.10), blend(PANEL, colour,
                                                                0.40)
                dot, fg = colour, colour
            if self._pill_painted == (text, colour):
                return
            self._pill_painted = (text, colour)
            self.pill.configure(fg_color=fill, border_color=border)
            self.pill_dot.configure(bg=fill)
            self.pill_dot.itemconfigure(self._pill_core, fill=dot)
            self.lbl_pill.configure(text=text, text_color=fg)
        except Exception:
            pass

    def _toggle_pin(self):
        self.settings["on_top"] = not self.settings.get("on_top", False)
        save_settings(self.settings)
        self._apply_topmost()
        self._paint_pin()

    def _apply_topmost(self):
        """Keep on top only while idle: over fullscreen Rivals the macro's
        clicks would land on this window and pause the run, and setup /
        Live test read the screen, which would see this window instead."""
        try:
            self.attributes("-topmost", bool(
                self.settings.get("on_top")
                and not (self.running or self.calibrating or self.watching)))
        except Exception:
            pass

    def _drop_pin(self):
        """Get out of Rivals' way for a run, setup or Live test. -topmost 0
        alone leaves the window above every normal window (Tk uses
        HWND_NOTOPMOST), so a pinned window is also pushed to the bottom -
        lower() uses HWND_BOTTOM and never activates it."""
        self._apply_topmost()
        if self.settings.get("on_top"):
            try:
                self.lower()
            except Exception:
                pass

    def _paint_pin(self):
        on = bool(self.settings.get("on_top"))
        if self._fam_icon:
            glyph = "\ue840" if on else "\ue718"
        else:
            glyph = "▲" if on else "△"
        self.btn_pin.configure(text=glyph,
                               text_color=ACCENT if on else SUBTLE,
                               border_color=ACCENT if on else LINE,
                               fg_color=ACCENT_DIM if on else "transparent")
        sw = getattr(self, "sw_ontop", None)     # Settings mirrors the pin
        if sw is not None and bool(sw.get()) != on:
            sw.select() if on else sw.deselect()

    def _run_clicked(self):
        """The header button. F8 calls toggle_run directly."""
        if self.calibrating and not self.running:
            return              # F8 is ignored during setup too (_hotkey)
        self.toggle_run()
        if not self._ready():
            self._show_page("weapons")

    # ---- pages ----
    def _scroll_page(self, page):
        """A scrolling page: a CTkScrollableFrame INSIDE the plain page
        frame (raising a scrollable frame itself does not work)."""
        sf = ctk.CTkScrollableFrame(page, fg_color="transparent",
                                    scrollbar_button_color=LINE,
                                    scrollbar_button_hover_color=MUTED)
        sf.pack(fill="both", expand=True, padx=(14, 4), pady=(0, 8))
        return sf

    def _build_home(self, page):
        """Fixed size, no scrolling: the status card, four tiles, then recent
        activity beside the setup summary."""
        body = ctk.CTkFrame(page, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=20, pady=16)
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(2, weight=1)
        self._home = body
        self._build_hero(body)
        self._build_tiles(body)
        self._build_home_low(body)
        # until setup is done Home points at the Weapons page instead
        self.cta = self._card(body)
        self._sheen(self.cta)
        inner = ctk.CTkFrame(self.cta, fg_color="transparent")
        inner.pack(fill="x", padx=18, pady=18)
        ctk.CTkLabel(inner, text="Set up your weapons", text_color=TEXT,
                     font=self.F(18, semi=True), height=24, anchor="w"
                     ).pack(fill="x")
        cta_text = ctk.CTkLabel(
            inner, text=("Three hovers in the weapon picker, about 30 "
                         "seconds. Start works once that's done."),
            text_color=SUBTLE, font=self.F(12), anchor="w", justify="left",
            wraplength=600, height=18)
        cta_text.pack(fill="x", pady=(4, 0))
        self._rewrap_on(inner, (cta_text,))
        ctk.CTkButton(inner, text="Go to Weapons  ›", width=180,
                      height=40, corner_radius=12, fg_color=ACCENT,
                      hover_color=ACCENT_SOFT, text_color=INK,
                      font=self.F(13, semi=True),
                      command=lambda: self._show_page("weapons")
                      ).pack(anchor="w", pady=(14, 0))

    def _build_weapons_page(self, page):
        """Card A: the setup guide while setting up (or not set up yet), a
        summary with Redo once it's done. Card B: DETECTION."""
        sf = self._scroll_page(page)
        self._weap_scroll = sf
        self._build_weapons_ready(sf)
        self._build_guide(sf)
        self._build_detection(sf)

    def _build_log(self, page):
        wrap = ctk.CTkFrame(page, fg_color="transparent")
        wrap.pack(fill="both", expand=True, padx=20, pady=16)
        # packed first, from the bottom, so a short window squeezes the text
        foot = ctk.CTkFrame(wrap, fg_color="transparent")
        foot.pack(side="bottom", fill="x", pady=(10, 0))
        self.lbl_log_count = ctk.CTkLabel(foot, text="", text_color=MUTED,
                                          font=self.F(11), height=16)
        self.lbl_log_count.pack(side="left")
        self.btn_log_open = self._ghost(foot, "Open log.txt", self._open_log)
        self.btn_log_open.configure(width=104, height=28)
        self.btn_log_open.pack(side="right")
        self.btn_log_copy = self._ghost(foot, "Copy", self._copy_log)
        self.btn_log_copy.configure(width=72, height=28)
        self.btn_log_copy.pack(side="right", padx=(0, 8))
        self.sw_follow = ctk.CTkSwitch(
            foot, text="Follow", width=40, switch_width=30, switch_height=16,
            progress_color=ACCENT, fg_color=LINE, button_color=TEXT,
            button_hover_color=ACCENT_SOFT, text_color=SUBTLE,
            font=self.F(12), command=self._follow_switched)
        self.sw_follow.select()
        self.sw_follow.pack(side="right", padx=(0, 14))

        bar = ctk.CTkFrame(wrap, fg_color="transparent")
        bar.pack(fill="x", pady=(0, 10))
        self.seg_log = self._segmented(bar, ["All", "Problems"],
                                       self._set_log_mode)
        self.seg_log.pack(side="right", padx=(10, 0))
        self.e_log = ctk.CTkEntry(bar, height=28, corner_radius=8,
                                  border_width=1, border_color=LINE,
                                  fg_color=PANEL, text_color=TEXT,
                                  placeholder_text="Filter lines…",
                                  placeholder_text_color=MUTED,
                                  font=self.F(12))
        self.e_log.pack(side="left", fill="x", expand=True)
        self.e_log.bind("<KeyRelease>", self._log_query_changed, add="+")

        box = ctk.CTkFrame(wrap, fg_color=PANEL, corner_radius=14,
                           border_width=1, border_color=LINE)
        box.pack(fill="both", expand=True)
        # the raw log - every line, timestamped, for bug reports
        self.txt = ctk.CTkTextbox(box, fg_color=PANEL, text_color="#c3cfdd",
                                  border_width=0, wrap="word",
                                  font=ctk.CTkFont(family="Consolas", size=11),
                                  scrollbar_button_color=LINE,
                                  scrollbar_button_hover_color=MUTED)
        self.txt.tag_config("ts", foreground=MUTED)
        self.txt.tag_config("top", foreground="#d5e0ec")
        self.txt.tag_config("sub", foreground=SUBTLE)
        # made after the tags above, so they win over them
        tb = self.txt._textbox
        tb.tag_configure("warn", foreground=AMBER)
        tb.tag_configure("err", foreground=RED)
        tb.tag_configure("hit", background=ACCENT_DIM, foreground=ACCENT_SOFT)
        tb.tag_configure("hide", elide=True)
        self.txt.configure(state="disabled")
        self.txt.pack(fill="both", expand=True, padx=8, pady=6)
        self._paint_log_count()

    # What the Log page colours (and its Problems view keeps): whatever the
    # feed and the card already show red or amber, plus a few plain words.
    # Read from the rule tables, never copied - a copy of a needle here would
    # let test_ui_rules find it even after the real log line was reworded.
    LOG_ERR_WORDS = ("failed", "Discord says")
    LOG_WARN_WORDS = ("couldn't", "could not", "NOTE:", "timed out",
                      "cancelled", "heads up", "not reopening")

    def _log_class(self, text):
        for rows in (self.FEED_RULES, self.STATUS_RULES):
            for row in rows:
                if row[0] in text:
                    if row[3] == "RED":
                        return "err"
                    if row[3] == "AMBER":
                        return "warn"
                    break
        if any(w in text for w in self.LOG_ERR_WORDS):
            return "err"
        if any(w in text for w in self.LOG_WARN_WORDS):
            return "warn"
        return None

    def _log_visible(self, text):
        if self._log_mode == "Problems" and not self._log_class(text):
            return False
        q = self._log_query
        return not q or q in text.lower()

    def _log_refilter(self):
        """Hide the lines the filter leaves out and mark what matched."""
        try:
            tb = self.txt._textbox
            tb.tag_remove("hide", "1.0", "end")
            tb.tag_remove("hit", "1.0", "end")
            last = int(tb.index("end-1c").split(".")[0])
            q = self._log_query
            shown = 0
            for n in range(1, last):
                line = tb.get(f"{n}.0", f"{n}.end")
                if not self._log_visible(line[10:]):
                    tb.tag_add("hide", f"{n}.0", f"{n + 1}.0")
                    continue
                shown += 1
                if q:
                    low, at = line.lower(), 10
                    while True:
                        at = low.find(q, at)
                        if at < 0:
                            break
                        tb.tag_add("hit", f"{n}.{at}", f"{n}.{at + len(q)}")
                        at += len(q)
            self._log_shown = shown
            if self._page == "log" and self._log_follow:
                tb.see("end")
        except Exception:
            pass
        self._paint_log_count()

    def _paint_log_count(self):
        try:
            total = int(self.txt._textbox.index("end-1c").split(".")[0]) - 1
            if not self._log_query and self._log_mode == "All":
                self._log_shown = total
                text = f"{total} line{'s' if total != 1 else ''}"
            else:
                text = f"{self._log_shown} of {total} lines"
            self.lbl_log_count.configure(text=text)
        except Exception:
            pass

    def _log_query_changed(self, _e=None):
        q = self.e_log.get().strip().lower()
        if q == self._log_query:
            return
        self._log_query = q
        # typing fast refilters once, after the last key
        if getattr(self, "_log_after", None):
            self.after_cancel(self._log_after)
        self._log_after = self.after(150, self._log_refilter)

    def _set_log_mode(self, value):
        if value != self._log_mode:
            self._log_mode = value
            self._log_refilter()

    def _follow_switched(self):
        self._log_follow = bool(self.sw_follow.get())
        if self._log_follow:
            self.txt.see("end")

    def _copy_log(self):
        """The lines showing now, to the clipboard."""
        try:
            tb = self.txt._textbox
            last = int(tb.index("end-1c").split(".")[0])
            lines = [tb.get(f"{n}.0", f"{n}.end") for n in range(1, last)
                     if "hide" not in tb.tag_names(f"{n}.0")]
            self.clipboard_clear()
            self.clipboard_append("\n".join(lines))
            self.btn_log_copy.configure(text="Copied")
            self.after(1500, lambda: self.btn_log_copy.configure(text="Copy"))
        except Exception as exc:
            self.log(f"could not copy the log: {exc}")

    def _open_log(self):
        try:
            os.startfile(LOG_PATH)
        except Exception as exc:
            self.log(f"could not open log.txt: {exc}")

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

    def _build_hero(self, parent):
        """The status card, 148 high: what it's doing on the left, the
        playtime well on the right."""
        self.hero = self._card(parent, height=148)
        self.hero.grid_propagate(False)
        self.hero.pack_propagate(False)
        self._sheen(self.hero)
        self.hero.grid_columnconfigure(0, weight=3, uniform="h")
        self.hero.grid_columnconfigure(1, weight=2, uniform="h")
        self.hero.grid_rowconfigure(0, weight=1)

        left = ctk.CTkFrame(self.hero, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(18, 8), pady=(18, 14))
        top = ctk.CTkFrame(left, fg_color="transparent")
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
                                      font=self.F(20, semi=True), anchor="w",
                                      height=28)
        self.lbl_state.pack(side="left")
        self.lbl_since = ctk.CTkLabel(left, text="", text_color=MUTED,
                                      font=self.F(11), height=16, anchor="w")
        self.lbl_since.pack(side="bottom", fill="x", padx=(26, 0))
        self.lbl_detail = ctk.CTkLabel(left, text="", text_color=SUBTLE,
                                       font=self.F(12), anchor="nw",
                                       justify="left", wraplength=380,
                                       height=18)
        self.lbl_detail.pack(fill="x", padx=(26, 0), pady=(4, 0))
        self._rewrap_on(left, (self.lbl_detail,), indent=26)

        well = ctk.CTkFrame(self.hero, fg_color=INSET, corner_radius=12,
                            border_width=1, border_color=HAIRLINE)
        well.grid(row=0, column=1, sticky="nsew", padx=(8, 14), pady=14)
        play = ctk.CTkFrame(well, fg_color="transparent")
        play.pack(fill="both", expand=True, padx=16, pady=(12, 10))
        self.lbl_play_cap = ctk.CTkLabel(play, text="PLAYTIME",
                                         text_color=MUTED, height=14,
                                         font=self.F(11, semi=True),
                                         anchor="w")
        self.lbl_play_cap.pack(fill="x")
        num = ctk.CTkFrame(play, fg_color="transparent")
        num.pack(fill="x", pady=(2, 0))
        self._num = []
        for i, (size, colour) in enumerate(((36, TEXT), (18, SUBTLE),
                                            (36, TEXT), (18, SUBTLE))):
            lbl = ctk.CTkLabel(num, text="", text_color=colour, height=44,
                               font=self.F(size, bold=size > 20),
                               anchor="sw")
            lbl.pack(side="left", anchor="s",
                     padx=(6 if i == 2 else 0, 0),
                     pady=(0, 6 if size < 20 else 0))
            self._num.append(lbl)
        self.bar_hour = ctk.CTkProgressBar(play, width=10, height=2,
                                           corner_radius=1,
                                           progress_color=ACCENT,
                                           fg_color=HAIRLINE)
        self.bar_hour.pack(fill="x", pady=(6, 0))
        self.bar_hour.set(0)
        self.lbl_play_sub = ctk.CTkLabel(play, text="", text_color=MUTED,
                                         font=self.F(11), anchor="w",
                                         height=16)
        self.lbl_play_sub.pack(fill="x", pady=(4, 0))

    def _build_tiles(self, parent):
        """LOADOUTS / REJOINS / RECOVERIES for this run, TODAY's playtime."""
        row = ctk.CTkFrame(parent, fg_color="transparent", height=76)
        row.grid_propagate(False)
        row.grid_rowconfigure(0, weight=1)
        self.tiles = row
        for i in range(4):
            row.grid_columnconfigure(i, weight=1, uniform="t")
        self.val_picks = self._tile(row, 0, "LOADOUTS")
        self.val_joins = self._tile(row, 1, "REJOINS")
        self.val_recov = self._tile(row, 2, "RECOVERIES")
        self.val_today = self._tile(row, 3, "TODAY")
        self.val_today.configure(text=self._today_text())

    def _tile(self, parent, col, caption, value="0", size=22, span_=1,
              row=0, height=1, sub=None, bold=False):
        """A number tile in a 4-column grid. With sub, the value label gets
        a .sub label under it."""
        card = self._card(parent, corner_radius=14, height=height)
        card.grid(row=row, column=col, columnspan=span_, sticky="nsew",
                  padx=(0 if col == 0 else 6, 0 if col + span_ >= 4 else 6),
                  pady=(0 if row == 0 else 12, 0))
        card.pack_propagate(False)
        ctk.CTkLabel(card, text=caption, text_color=MUTED, height=14,
                     font=self.F(11, semi=True), anchor="w"
                     ).pack(fill="x", padx=16, pady=(13, 0))
        v = ctk.CTkLabel(card, text=value, text_color=TEXT,
                         height=max(30, size + 10), anchor="w",
                         font=self.F(size, semi=not bold, bold=bold))
        v.pack(fill="x", padx=16, pady=(4 if size < 30 else 0, 0))
        if sub is not None:
            v.sub = ctk.CTkLabel(card, text=sub, text_color=MUTED, height=14,
                                 font=self.F(11), anchor="w")
            v.sub.pack(fill="x", padx=16)
        return v

    def _today_text(self):
        """Today's playtime: what stats.json holds plus the second not yet
        added by the live tick."""
        try:
            secs = self._stats.get("days", {}).get(
                time.strftime("%Y-%m-%d"), 0)
            if self._live_run:
                secs += min(5.0, max(0.0, time.time() - self._flushed_at))
            return span(secs) if secs >= 60 else "0m"
        except Exception:
            return "0m"

    def _build_home_low(self, parent):
        """Recent activity (from the feed) beside the setup summary."""
        low = ctk.CTkFrame(parent, fg_color="transparent")
        low.grid_columnconfigure(0, weight=3, uniform="b")
        low.grid_columnconfigure(1, weight=2, uniform="b")
        low.grid_rowconfigure(0, weight=1)
        self.home_low = low

        recent = self._card(low, corner_radius=14, height=1)
        recent.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        recent.pack_propagate(False)
        head = ctk.CTkFrame(recent, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(13, 6))
        ctk.CTkLabel(head, text="RECENT ACTIVITY", text_color=MUTED,
                     height=14, font=self.F(11, semi=True)).pack(side="left")
        more = ctk.CTkLabel(head, text="See all ›", text_color=ACCENT,
                            height=14, font=self.F(11, semi=True),
                            cursor="hand2")
        more.pack(side="right")
        more.bind("<Button-1>", lambda _e: self._show_page("activity"))
        self._recent_rows = []
        for _ in range(6):
            r = ctk.CTkFrame(recent, fg_color="transparent", height=26)
            r.pack(fill="x", padx=16)
            r.pack_propagate(False)
            ts = ctk.CTkLabel(r, text="", text_color=MUTED, width=40,
                              anchor="w", font=self.F(11, family="Consolas"))
            ts.pack(side="left")
            gl = ctk.CTkLabel(r, text="", width=20, anchor="w",
                              font=self.F(11, family=self._fam_sym))
            gl.pack(side="left", padx=(4, 4))
            ti = ctk.CTkLabel(r, text="", text_color=TEXT, anchor="w",
                              font=self.F(12))
            ti.pack(side="left")
            ch = ctk.CTkLabel(r, text="", text_color=ACCENT_SOFT, anchor="w",
                              font=self.F(11, semi=True))
            ch.pack(side="left", padx=(8, 0))
            self._recent_rows.append((r, ts, gl, ti, ch))

        # setup at a glance; each row opens its page
        setup = self._card(low, corner_radius=14, height=1)
        setup.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        setup.pack_propagate(False)
        self.weap = setup
        ctk.CTkLabel(setup, text="SETUP", text_color=MUTED, height=14,
                     anchor="w", font=self.F(11, semi=True)
                     ).pack(fill="x", padx=16, pady=(13, 4))
        r1, self.weap_tile, self.lbl_weap, self.lbl_cal = self._setup_row(
            setup, "✓", GREEN_DIM, GREEN, "Weapons ready", "", "weapons")
        r2, self._ffa_tile, _t, self.lbl_ffa_home = self._setup_row(
            setup, "↺", ACCENT_DIM, ACCENT, "Way back", "Built in",
            "wayback")
        self._rewrap_on(setup, (self.lbl_cal, self.lbl_ffa_home), indent=74)
        self._paint_recent()

    def _setup_row(self, card, glyph, fill, colour, title, note, page):
        row = ctk.CTkFrame(card, fg_color="transparent", cursor="hand2")
        row.pack(fill="x", padx=16, pady=(6, 0))
        tile = ctk.CTkLabel(row, text=glyph, width=30, height=30,
                            corner_radius=9, fg_color=fill, text_color=colour,
                            font=self.F(13, family=self._fam_sym))
        tile.pack(side="left", anchor="n")
        txt = ctk.CTkFrame(row, fg_color="transparent")
        txt.pack(side="left", fill="x", expand=True, padx=(12, 0))
        t = ctk.CTkLabel(txt, text=title, height=18, text_color=TEXT,
                         anchor="w", font=self.F(13, semi=True))
        t.pack(fill="x")
        n = ctk.CTkLabel(txt, text=note, text_color=SUBTLE, height=16,
                         font=self.F(11), anchor="w", justify="left",
                         wraplength=200)
        n.pack(fill="x")
        for w in (row, tile, txt, t, n):
            w.bind("<Button-1>", lambda _e, p=page: self._show_page(p),
                   add="+")
        return row, tile, t, n

    def _paint_recent(self):
        """Home's recent activity: the newest few feed rows, unfiltered."""
        try:
            items = self._feed_items[:len(self._recent_rows)]
            r, ts, gl, ti, ch = self._recent_rows[0]
            if not items and ts.winfo_manager():       # the empty line
                ts.pack_forget()
                gl.pack_forget()
            elif items and not ts.winfo_manager():
                ts.pack(side="left", before=ti)
                gl.pack(side="left", padx=(4, 4), before=ti)
            for i, (r, ts, gl, ti, ch) in enumerate(self._recent_rows):
                it = items[i] if i < len(items) else None
                if it:
                    ts.configure(text=it["ts"])
                    gl.configure(text=it["glyph"],
                                 text_color=globals()[it["colour"]])
                    ti.configure(text=it["title"], text_color=TEXT)
                    ch.configure(text=f"×{it['count']}"
                                 if it["count"] > 1 else "")
                else:
                    ts.configure(text="")
                    gl.configure(text="")
                    ch.configure(text="")
                    ti.configure(text="Nothing yet · press Start with "
                                      "Rivals open." if i == 0 and not items
                                 else "", text_color=MUTED)
        except Exception:
            pass

    # ---- the weapon setup guide (shown instead of the status card) ----
    STEPS = (
        ("The Random tile", "Top-left of the weapon grid."),
        ("The Grenade Launcher",
         "Hover the Grenade Launcher button itself."),
        ("Your first loadout slot", "The first slot along the top."),
    )

    def _build_guide(self, parent):
        self.guide = self._card(parent)
        self._sheen(self.guide)
        wrap = ctk.CTkFrame(self.guide, fg_color="transparent")
        wrap.pack(fill="x", padx=18, pady=18)
        # a big picture of the current step on the right when there's room;
        # in a narrow window each step row shows its own small one instead
        self._guide_wide = True
        self._preview = ctk.CTkFrame(wrap, fg_color=INSET, corner_radius=12,
                                     border_width=1, border_color=HAIRLINE)
        pv = ctk.CTkFrame(self._preview, fg_color="transparent")
        pv.pack(fill="both", expand=True, padx=14, pady=14)
        self.lbl_preview_cap = ctk.CTkLabel(pv, text="STEP 1 OF 3",
                                            text_color=MUTED, height=14,
                                            anchor="w",
                                            font=self.F(10, semi=True))
        self.lbl_preview_cap.pack(fill="x")
        k = self.PREVIEW_K
        self.pic_preview = tk.Canvas(pv, width=self._px(170 * k),
                                     height=self._px(92 * k), bg=INSET,
                                     highlightthickness=0, bd=0)
        self.pic_preview.pack(pady=(10, 0))
        self.lbl_preview = ctk.CTkLabel(pv, text="", text_color=TEXT,
                                        height=18, anchor="w",
                                        font=self.F(13, semi=True))
        self.lbl_preview.pack(fill="x", pady=(10, 0))
        ctk.CTkLabel(pv, text="Hover it, press F8. Don't click.",
                     text_color=SUBTLE, height=16, anchor="w",
                     font=self.F(11)).pack(fill="x")
        self._preview.pack(side="right", fill="y", padx=(16, 0))
        body = ctk.CTkFrame(wrap, fg_color="transparent")
        body.pack(side="left", fill="both", expand=True)
        wrap.bind("<Configure>", self._guide_width, add="+")

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

    PREVIEW_K = 1.35                # the guide's big picture, vs a row's

    def _guide_width(self, e):
        """Side picture at 600+ of inner width, per-row pictures under."""
        w = e.width / max(0.5, ctk.ScalingTracker.get_widget_scaling(self))
        wide = w >= 600
        if wide != self._guide_wide:
            self._guide_wide = wide
            self._paint_guide()

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

    def _draw_picker(self, cv, step, bg, k=1.0):
        """A tiny weapon picker with the thing to hover outlined, k times the
        170x92 row size.

        A drawing, not a screenshot - it only has to show WHERE, and it cannot
        go out of date the way a picture of the real menu would.
        """
        cv.delete("all")
        cv.configure(bg=bg)

        def p(n):
            return self._px(n * k)
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
                       font=self._tkfont(round(9 * k), semi=True))

    # ---- the Weapons page once set up: what's saved, and Redo ----
    def _build_weapons_ready(self, parent):
        self.weap_ready = self._card(parent)
        self._sheen(self.weap_ready)
        inner = ctk.CTkFrame(self.weap_ready, fg_color="transparent")
        inner.pack(fill="x", padx=18, pady=16)
        # Home's setup card has weap_tile / lbl_weap / lbl_cal; these are
        # this page's own copies, painted by _show_cal alongside them
        self._wp_tile = ctk.CTkLabel(inner, text="✓", width=40, height=40,
                                     corner_radius=12, fg_color=GREEN_DIM,
                                     text_color=GREEN,
                                     font=self.F(16, family=self._fam_sym))
        self._wp_tile.pack(side="left")
        txt = ctk.CTkFrame(inner, fg_color="transparent")
        txt.pack(side="left", fill="x", expand=True, padx=(14, 0))
        self._wp_title = ctk.CTkLabel(txt, text="Weapons ready", height=22,
                                      text_color=TEXT, anchor="w",
                                      font=self.F(15, semi=True))
        self._wp_title.pack(fill="x")
        self._wp_cal = ctk.CTkLabel(txt, text="", text_color=SUBTLE,
                                    height=16, font=self.F(12), anchor="w",
                                    justify="left", wraplength=360)
        self._wp_cal.pack(fill="x")
        self.btn_redo = ctk.CTkButton(inner, text="Redo setup", width=104,
                                      height=34, corner_radius=10,
                                      fg_color="transparent", border_width=1,
                                      border_color=LINE,
                                      hover_color=CARD_HI, text_color=TEXT,
                                      font=self.F(12), command=self.calibrate)
        self.btn_redo.pack(side="right")
        self._rewrap_on(inner, (self._wp_cal,), indent=180)

    # ---- activity: a readable feed, with the raw log one click away ----
    def _build_activity(self, page):
        self.act_page = ctk.CTkFrame(page, fg_color="transparent")
        self.act_page.pack(fill="both", expand=True, padx=20, pady=16)
        # packed first, from the bottom, so a short window squeezes the feed
        # rather than pushing this line off the end
        self.lbl_foot = ctk.CTkLabel(self.act_page,
                                     text="Newest first · every detail "
                                          "is on the Log page",
                                     text_color=MUTED, font=self.F(11),
                                     height=16)
        self.lbl_foot.pack(side="bottom", pady=(8, 0))
        bar = ctk.CTkFrame(self.act_page, fg_color="transparent")
        bar.pack(fill="x", pady=(0, 10))
        self.seg_feed = self._segmented(bar, list(self.FEED_FILTERS),
                                        self._set_feed_filter)
        self.seg_feed.pack(side="left")
        self.lbl_feed_count = ctk.CTkLabel(bar, text="", text_color=MUTED,
                                           font=self.F(11), height=16)
        self.lbl_feed_count.pack(side="right")
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
        self._empty_sub = ctk.CTkLabel(
            self.empty, text="Press Start with Rivals open. Events show up "
            "here.", text_color=SUBTLE, font=self.F(11), height=16)
        self._empty_sub.pack(pady=(2, 0))
        self.empty.place(relx=0.5, rely=0.46, anchor="center")
        box.bind("<Configure>", self._fit_empty, add="+")

    def _fit_empty(self, e):
        """In a short box (setup guide showing, small window) the gem would
        overlap the border - keep just the words."""
        small = e.height < self._px(150)
        shown = bool(self._empty_gem.winfo_manager())
        if small and shown:
            self._empty_gem.pack_forget()
        elif not small and not shown:
            self._empty_gem.pack(pady=(0, 10), before=self._empty_title)

    # Activity filters, by FEED_RULES key. Never saved: tests and people both
    # expect the newest row of everything on top when the app opens.
    FEED_FILTERS = {
        "All": None,
        "Loadouts": ("pick", "miss"),
        "Problems": ("err", "reopen", "reopenstop", "noin", "disc", "restart",
                     "nofs", "scaling", "screen", "needsetup", "updfail",
                     "pause"),
        "Setup": ("setup", "way", "needsetup", "screenok", "update",
                  "updated"),
    }

    def _segmented(self, parent, values, command):
        seg = ctk.CTkSegmentedButton(
            parent, values=values, command=command, height=28,
            corner_radius=8, font=self.F(12), fg_color=PANEL,
            selected_color=CARD_HI, selected_hover_color=CARD_HI,
            unselected_color=PANEL, unselected_hover_color=NAV_HOVER,
            text_color=TEXT, text_color_disabled=MUTED)
        seg.set(values[0])
        return seg

    def _set_feed_filter(self, value):
        if value not in self.FEED_FILTERS or value == self._feed_filter:
            return
        self._feed_filter = value
        self._feed_rebuild()

    def _feed_line(self, it):
        """One feed row as (text, tag) chunks - the same as _feed_add's."""
        parts = [(it["ts"] + "\t", "ts"),
                 (it["glyph"] + "\t", "g_" + it["colour"]),
                 (it["title"], "title")]
        if it["sub"]:
            parts.append(("   " + it["sub"], "sub"))
        if it["count"] > 1:
            parts.append((f"   ×{it['count']}", "chip"))
        parts.append(("\n", "title"))
        return parts

    def _feed_rebuild(self):
        """Redraw the feed from _feed_items for the current filter. The
        'All' view comes out exactly as _feed_add would have built it."""
        keys = self.FEED_FILTERS.get(self._feed_filter)
        tb = self.feed
        t = tb._textbox
        tb.configure(state="normal")
        t.delete("1.0", "end")
        for it in self._feed_items:
            if keys is None or it["key"] in keys:
                for chunk, tag in self._feed_line(it):
                    t.insert("end", chunk, tag)
        tb.configure(state="disabled")
        t.see("1.0")
        if keys is None:
            self._feed_rows = len(self._feed_items)
        self._paint_feed_empty()

    def _paint_feed_empty(self):
        """The 'Nothing yet' overlay whenever the view has no rows."""
        try:
            rows = int(self.feed._textbox.index("end-1c").split(".")[0]) - 1
            keys = self.FEED_FILTERS.get(self._feed_filter)
            n = len(self._feed_items) if keys is None else rows
            if keys is None:
                self.lbl_feed_count.configure(
                    text=f"{n} event{'s' if n != 1 else ''}" if n else "")
            else:
                self.lbl_feed_count.configure(
                    text=f"{rows} of {len(self._feed_items)}")
            if rows > 0:
                self.empty.place_forget()
                return
            if keys is None:
                self._empty_title.configure(text="Nothing yet")
                self._empty_sub.configure(text="Press Start with Rivals open. "
                                               "Events show up here.")
            else:
                self._empty_title.configure(text="Nothing here")
                self._empty_sub.configure(
                    text=f"No {self._feed_filter.lower()} events so far.")
            self.empty.place(relx=0.5, rely=0.46, anchor="center")
        except Exception:
            pass

    def _toggle_full_log(self):
        """1.0's 'Full log' link, still used by render_states.py: Log and
        Activity swap."""
        self._show_page("activity" if self._page == "log" else "log")

    # ---- settings: grouped cards ----
    def _build_settings(self, page):
        self.set_page = self._scroll_page(page)
        sp = self.set_page

        # saved since 1.1 - they used to reset to On at every launch
        self._group(sp, "WHILE IT RUNS")
        card = self._card(sp, corner_radius=14)
        card.pack(fill="x")
        self.sw_fullscreen = self._switch_row(
            card, "Keep Roblox fullscreen",
            "Presses F11 if Roblox ends up windowed.", first=True,
            on=self.settings.get("keep_fullscreen", True),
            command=lambda: self._switch_saved("keep_fullscreen",
                                               "sw_fullscreen"))
        self.sw_reconnect = self._switch_row(
            card, "Reconnect automatically",
            "Clicks Reconnect, or restarts Roblox.",
            on=self.settings.get("auto_reconnect", True),
            command=lambda: self._switch_saved("auto_reconnect",
                                               "sw_reconnect"))
        self.sw_shots = self._switch_row(
            card, "Save screenshots",
            "Newest 40 of each, handy if a pick misses.",
            on=self.settings.get("save_shots", True),
            command=lambda: self._switch_saved("save_shots", "sw_shots"))

        self._group(sp, "WINDOW")
        card = self._card(sp, corner_radius=14)
        card.pack(fill="x")
        self.sw_ontop = self._switch_row(
            card, "Keep on top while idle",
            "Same as the pin. Steps back while a run, setup or test is on.",
            first=True, on=bool(self.settings.get("on_top")),
            command=self._ontop_switched)
        self.sw_motion = self._switch_row(
            card, "Animations", "The shimmer and fades while it runs.",
            on=self.settings.get("motion", True),
            command=lambda: self._switch_saved("motion", "sw_motion"))
        self.sw_remember = self._switch_row(
            card, "Remember window and page",
            "Opens at the same size, place and page.",
            on=self.settings.get("remember", True),
            command=lambda: self._switch_saved("remember", "sw_remember"))

        self._group(sp, "MORE")
        card = self._card(sp, corner_radius=14)
        card.pack(fill="x")
        for i, (key, note) in enumerate((
                ("weapons", "Redo setup, match sensitivity, Live test"),
                ("wayback", "How it gets back into Free For All"),
                ("discord", "Alerts in your own Discord channel"),
                ("about", "Version, updates and shortcuts"))):
            self._link_row(card, self.PAGE_INFO[key][0], note,
                           lambda k=key: self._show_page(k), first=i == 0)

        self._build_files(sp)

    def _switch_saved(self, key, name):
        self.settings[key] = bool(getattr(self, name).get())
        save_settings(self.settings)

    def _ontop_switched(self):
        if bool(self.sw_ontop.get()) != bool(self.settings.get("on_top")):
            self._toggle_pin()

    def _link_row(self, card, title, note, command, first=False):
        """A row that opens something: title, note and a chevron."""
        if not first:
            ctk.CTkFrame(card, height=1, fg_color=HAIRLINE,
                         corner_radius=0).pack(fill="x", padx=1)
        row = ctk.CTkFrame(card, fg_color="transparent", corner_radius=12,
                           cursor="hand2")
        row.pack(fill="x", padx=2, pady=2)
        inner = ctk.CTkFrame(row, fg_color="transparent")
        inner.pack(fill="x", padx=12, pady=8)
        txt = ctk.CTkFrame(inner, fg_color="transparent")
        txt.pack(side="left", fill="x", expand=True)
        a = ctk.CTkLabel(txt, text=title, text_color=TEXT, font=self.F(13),
                         anchor="w", height=18)
        a.pack(fill="x")
        b = ctk.CTkLabel(txt, text=note, text_color=SUBTLE, font=self.F(11),
                         anchor="w", height=16)
        b.pack(fill="x")
        c = ctk.CTkLabel(inner, text="›", text_color=SUBTLE, font=self.F(16))
        c.pack(side="right")
        for w in (row, inner, txt, a, b, c):
            w.bind("<Button-1>", lambda _e: command(), add="+")
            w.bind("<Enter>", lambda _e: row.configure(fg_color=CARD_HI),
                   add="+")
            w.bind("<Leave>", lambda _e: self._link_leave(row), add="+")
        return row

    def _link_leave(self, row):
        x, y = self.winfo_pointerxy()
        rx, ry = row.winfo_rootx(), row.winfo_rooty()
        if not (rx <= x < rx + row.winfo_width()
                and ry <= y < ry + row.winfo_height()):
            row.configure(fg_color="transparent")

    def _build_detection(self, sp):
        self._det_group = self._group(sp, "DETECTION")
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

    def _build_wayback(self, page):
        sp = self._scroll_page(page)
        self._group(sp, "WAY BACK INTO FREE FOR ALL")
        card = self._card(sp, corner_radius=14)
        card.pack(fill="x")
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=14, pady=12)
        ctk.CTkLabel(inner, text="↺", width=34, height=34, corner_radius=10,
                     fg_color=ACCENT_DIM, text_color=ACCENT,
                     font=self.F(15, family=self._fam_sym)).pack(side="left")
        txt = ctk.CTkFrame(inner, fg_color="transparent")
        txt.pack(side="left", fill="x", expand=True, padx=(12, 0))
        self.lbl_ffa = ctk.CTkLabel(txt, text="Built in", text_color=TEXT,
                                    font=self.F(13, semi=True), anchor="w",
                                    height=18)
        self.lbl_ffa.pack(fill="x")
        ctk.CTkLabel(txt, text="Used when a reconnect or a Roblox restart "
                               "drops you in the lobby. Only re-teach if the "
                               "lobby changes.",
                     text_color=SUBTLE, font=self.F(11), anchor="w",
                     justify="left", height=16, wraplength=420
                     ).pack(fill="x")
        self.btn_ffa = self._ghost(inner, "Re-teach", self.teach_ffa)
        self.btn_ffa.pack(side="right", padx=(10, 0))
        self._rewrap_on(txt, (txt.winfo_children()[-1],))

        self._group(sp, "HOW IT'S TAUGHT")
        card = self._card(sp, corner_radius=14)
        card.pack(fill="x")
        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(fill="x", padx=14, pady=(12, 14))
        self._way_rows = []
        for i, (title, note) in enumerate(self.WAY_STEPS):
            row = ctk.CTkFrame(body, corner_radius=12, border_width=1,
                               fg_color="transparent", border_color=HAIRLINE)
            row.pack(fill="x", pady=(0 if i == 0 else 8, 0))
            inn = ctk.CTkFrame(row, fg_color="transparent")
            inn.pack(fill="x", padx=12, pady=10)
            badge = tk.Canvas(inn, width=self._px(26), height=self._px(26),
                              bg=CARD, highlightthickness=0, bd=0)
            badge.pack(side="left", anchor="n")
            tx = ctk.CTkFrame(inn, fg_color="transparent")
            tx.pack(side="left", fill="x", expand=True, padx=(10, 0),
                    pady=(3, 0))
            ctk.CTkLabel(tx, text=title, text_color=TEXT, height=18,
                         font=self.F(13, semi=True), anchor="w").pack(fill="x")
            ctk.CTkLabel(tx, text=note, text_color=SUBTLE, height=16,
                         font=self.F(11), anchor="w").pack(fill="x")
            self._way_rows.append({"row": row, "badge": badge})
        self.lbl_way = ctk.CTkLabel(body, text="Press Re-teach in the "
                                    "Rivals lobby. Esc cancels.",
                                    text_color=MUTED, font=self.F(11),
                                    anchor="w", height=16)
        self.lbl_way.pack(fill="x", pady=(10, 0))

        link = ctk.CTkLabel(sp, text="Reconnect and fullscreen switches "
                                     "are on the Settings page ›",
                            text_color=ACCENT, font=self.F(11, semi=True),
                            height=16, cursor="hand2")
        link.pack(pady=(16, 8))
        link.bind("<Button-1>", lambda _e: self._show_page("settings"))
        self._paint_way()

    WAY_STEPS = (
        ("Play, in the lobby", "Hover it, press F8, then click it yourself."),
        ("Free For All", "Scroll down to it, hover it, press F8, click it."),
        ("Play, on the Free For All screen", "Hover it and press F8."),
    )

    # the card's titles for the teach flow's lines -> step (4 = saved, 0 =
    # reset). Matched through STATUS_RULES, so the needles live in one place
    WAY_TITLES = {"Teaching the way back · 1 of 3": 1,
                  "Teaching the way back · 2 of 3": 2,
                  "Teaching the way back · 3 of 3": 3,
                  "Way back saved": 4, "Nothing saved": 0}

    def _way_from_log(self, msg):
        """The Way back steps follow the teach flow's own log lines."""
        text = str(msg)
        step = 0 if text == "cancelled" else None
        if step is None:
            for row in self.STATUS_RULES:
                if row[0] in text:
                    step = self.WAY_TITLES.get(row[1])
                    break
        if step is None:
            return
        w = self._way
        if step == 0:
            if not w["step"]:
                return              # a weapon setup timing out, not this
            w.update(step=0, done=set())
        elif step == 4:
            w.update(step=0, done={1, 2, 3})
        else:
            w.update(step=step, done=set(range(1, step)))
        self._paint_way()

    def _paint_way(self):
        w = self._way
        for i, r in enumerate(self._way_rows, start=1):
            cur = i == w["step"]
            r["row"].configure(border_color=ACCENT if cur else HAIRLINE,
                               fg_color=CARD_HI if cur else "transparent")
            bg = CARD_HI if cur else CARD
            if i in w["done"]:
                self._draw_badge(r["badge"], "✓", GREEN_DIM, GREEN, bg)
            else:
                self._draw_badge(r["badge"], str(i),
                                 ACCENT if cur else CARD_HI,
                                 INK if cur else SUBTLE, bg)
        self.lbl_way.configure(
            text=("Hover it and press F8 · Esc cancels" if w["step"] else
                  "Saved · it'll use this from the next reconnect"
                  if w["done"] == {1, 2, 3} else
                  "Press Re-teach in the Rivals lobby. Esc cancels."),
            text_color=ACCENT if w["step"] else
            GREEN if w["done"] == {1, 2, 3} else MUTED)

    def _build_files(self, sp):
        self._group(sp, "FILES")
        card = self._card(sp, corner_radius=14)
        card.pack(fill="x")
        self._link_row(card, "Open data folder",
                       "Setup, log, stats and screenshots", self.open_data,
                       first=True)
        self._link_row(card, "Open log.txt",
                       "Every line it has written, for bug reports",
                       self._open_log)

        ctk.CTkLabel(sp, text=(f"GlassMacro {APP_VER} · made for "
                               f"1920×1080 · F8 starts and stops"),
                     text_color=MUTED, font=self.F(11), height=16
                     ).pack(pady=(18, 8))

    # ---- Stats: lifetime totals from stats.json ----
    def _build_stats(self, page):
        sp = self._scroll_page(page)
        self._stats_sp = sp
        # nothing counted yet
        self._stats_empty = self._card(sp)
        self._sheen(self._stats_empty)
        inner = ctk.CTkFrame(self._stats_empty, fg_color="transparent")
        inner.pack(fill="x", padx=18, pady=28)
        gem = tk.Canvas(inner, width=self._px(40), height=self._px(40),
                        bg=CARD, highlightthickness=0, bd=0)
        draw_gem(gem, 0, 0, self._px(40), dim=True)
        gem.pack()
        ctk.CTkLabel(inner, text="No runs yet", text_color=TEXT, height=22,
                     font=self.F(15, semi=True)).pack(pady=(10, 0))
        ctk.CTkLabel(inner, text="Playtime, loadouts and rejoins add up here "
                                 "from your first run.",
                     text_color=SUBTLE, font=self.F(12), height=18
                     ).pack(pady=(2, 0))

        body = ctk.CTkFrame(sp, fg_color="transparent")
        self._stats_body = body
        grid = ctk.CTkFrame(body, fg_color="transparent")
        grid.pack(fill="x", pady=(16, 0))
        for i in range(4):
            grid.grid_columnconfigure(i, weight=1, uniform="s")
        self._st = {}
        self._st["total"] = self._tile(grid, 0, "TOTAL PLAYTIME", "0m",
                                       size=32, span_=2, height=96, sub="",
                                       bold=True)
        self._st["longest"] = self._tile(grid, 2, "LONGEST RUN", "0m",
                                         height=96, sub="")
        self._st["runs"] = self._tile(grid, 3, "RUNS", "0", height=96,
                                      sub="")
        for col, (key, cap) in enumerate((("loadouts", "LOADOUTS"),
                                          ("rejoins", "REJOINS"),
                                          ("recov", "RECOVERIES"),
                                          ("avg", "AVERAGE RUN"))):
            self._st[key] = self._tile(grid, col, cap, "0", row=1,
                                       height=76)

        self._group(body, "LAST 14 DAYS")
        chart = self._card(body, corner_radius=14)
        chart.pack(fill="x")
        self.cv_days = tk.Canvas(chart, height=self._px(150), bg=CARD,
                                 highlightthickness=0, bd=0)
        self.cv_days.pack(fill="x", padx=14, pady=12)
        self.cv_days.bind("<Configure>", lambda _e: self._draw_days(True),
                          add="+")
        self._days_drawn = None

        foot = ctk.CTkFrame(body, fg_color="transparent")
        foot.pack(fill="x", pady=(14, 8))
        self.lbl_stats_since = ctk.CTkLabel(foot, text="", text_color=MUTED,
                                            font=self.F(11), height=16)
        self.lbl_stats_since.pack(side="left", padx=4)
        self.btn_reset_stats = self._ghost(foot, "Reset stats",
                                           self._reset_stats_click)
        self.btn_reset_stats.configure(width=104, text_color=SUBTLE)
        self.btn_reset_stats.pack(side="right")
        self._reset_armed = None
        self._stats_shown = None
        self._paint_stats()

    def _stats_now(self):
        """Stored totals plus the part of a live run not yet added."""
        st = self._stats
        live = self._live_run
        now = time.time()
        extra = min(5.0, max(0.0, now - self._flushed_at)) if live else 0
        cur = (st.get("current") or {}) if live else {}
        v = {"total": st.get("total_secs", 0) + extra,
             "runs": st.get("runs", 0),
             "longest": max(st.get("longest_secs", 0),
                            cur.get("secs", 0) + extra if cur else 0),
             "loadouts": st.get("loadouts", 0) + (
                 max(0, self.n_picks - self._flushed_picks) if live else 0),
             "rejoins": st.get("rejoins", 0) + (
                 max(0, self.n_joins - self._flushed_joins) if live else 0),
             "recov": (st.get("roblox_reopens", 0)
                       + st.get("roblox_restarts", 0)
                       + st.get("reconnects", 0))}
        v["avg"] = v["total"] / v["runs"] if v["runs"] else 0
        return v

    def _paint_stats(self):
        try:
            v = self._stats_now()
            empty = v["runs"] == 0
            if empty != self._stats_shown:
                self._stats_shown = empty
                if empty:
                    self._stats_body.pack_forget()
                    self._stats_empty.pack(fill="x", pady=(16, 0))
                else:
                    self._stats_empty.pack_forget()
                    self._stats_body.pack(fill="x")
            if empty:
                return
            st, t = self._stats, self._st
            t["total"].configure(text=span(v["total"]) if v["total"] >= 60
                                 else "0m")
            first = str(st.get("first_run", ""))[:10]
            t["total"].sub.configure(text=f"since {self._day(first)}"
                                     if first else "")
            t["longest"].configure(text=span(v["longest"]))
            on = st.get("longest_on", "")
            t["longest"].sub.configure(text=f"on {self._day(on)}" if on
                                       else "")
            t["runs"].configure(text=str(v["runs"]))
            unclean = st.get("unclean_ends", 0)
            t["runs"].sub.configure(text=f"{unclean} ended unexpectedly"
                                    if unclean else "")
            for k in ("loadouts", "rejoins", "recov"):
                t[k].configure(text=f"{int(v[k]):,}")
            t["avg"].configure(text=span(v["avg"]))
            last = str(st.get("last_run", ""))[:10]
            self.lbl_stats_since.configure(
                text=f"Last run {self._day(last)} · saved in stats.json"
                if last else "Saved in stats.json")
            self._draw_days()
        except Exception:
            pass

    @staticmethod
    def _day(iso):
        try:
            return time.strftime("%b %d", time.strptime(iso[:10], "%Y-%m-%d")
                                 ).replace(" 0", " ")
        except Exception:
            return iso

    def _draw_days(self, force=False):
        """The 14-day bars. Redrawn only when the size or a value changed."""
        import datetime
        cv = self.cv_days
        try:
            w, h = cv.winfo_width(), cv.winfo_height()
        except Exception:
            return
        if w < 50:
            return
        today = datetime.date.today()
        days = [today - datetime.timedelta(days=13 - i) for i in range(14)]
        stored = self._stats.get("days", {})
        vals = [stored.get(d.isoformat(), 0) for d in days]
        if self._live_run:
            vals[-1] += min(5.0, max(0.0, time.time() - self._flushed_at))
        sig = (w, h, tuple(int(x // 60) for x in vals))
        if sig == self._days_drawn and not force:
            return
        self._days_drawn = sig
        p = self._px
        cv.delete("all")
        top, bottom = p(22), p(22)
        base = h - bottom
        slot = w / 14.0
        bar = max(p(6), min(p(30), slot * 0.56))
        peak = max(max(vals), 3600)
        cv.create_line(0, base + 0.5, w, base + 0.5, fill=HAIRLINE)
        best = max(range(14), key=lambda i: vals[i])
        for i, (d, v) in enumerate(zip(days, vals)):
            cx = slot * i + slot / 2
            is_today = i == 13
            if v >= 60:
                bh = max(p(3), (base - top) * v / peak)
                colour = ACCENT if is_today else blend(ACCENT_DEEP, ACCENT,
                                                       0.25)
                cv.create_rectangle(cx - bar / 2, base - bh, cx + bar / 2,
                                    base, fill=colour, outline="")
                if is_today or i == best:
                    cv.create_text(cx, base - bh - p(9), text=span(v),
                                   fill=TEXT if is_today else SUBTLE,
                                   font=self._tkfont(10, semi=True))
            else:
                cv.create_rectangle(cx - bar / 2, base - p(2), cx + bar / 2,
                                    base, fill=LINE, outline="")
            cv.create_text(cx, base + p(11),
                           text="Today" if is_today and slot >= p(40)
                           else str(d.day),
                           fill=ACCENT if is_today else MUTED,
                           font=self._tkfont(10, semi=is_today))

    def _reset_stats_click(self):
        """Two clicks. The old file is renamed, never deleted."""
        if self.running or self._live_run:
            self.btn_reset_stats.configure(text="Stop the run first")
            self.after(2500, self._disarm_reset)
            return
        if not self._reset_armed:
            self._reset_armed = self.after(4000, self._disarm_reset)
            self.btn_reset_stats.configure(text="Click again to reset",
                                           width=150, text_color=RED,
                                           border_color=RED)
            return
        self.after_cancel(self._reset_armed)
        self._reset_armed = None
        stamp = time.strftime("%Y%m%d-%H%M%S")
        kept = f"stats.json.reset-{stamp}"
        try:
            if os.path.exists(STATS_PATH):
                os.replace(STATS_PATH, os.path.join(DATA_DIR, kept))
            if os.path.exists(STATS_PATH + ".bak"):
                os.replace(STATS_PATH + ".bak",
                           os.path.join(DATA_DIR, kept + ".bak"))
        except OSError as exc:
            self.log(f"could not reset the stats: {exc}")
            self._disarm_reset()
            return
        self._stats = new_stats()
        save_stats(self._stats)
        self.log(f"stats reset - the old numbers are kept in {kept}")
        self._disarm_reset()
        self._paint_stats()
        self.val_today.configure(text=self._today_text())

    def _disarm_reset(self):
        if self._reset_armed:
            try:
                self.after_cancel(self._reset_armed)
            except Exception:
                pass
        self._reset_armed = None
        self.btn_reset_stats.configure(text="Reset stats", width=104,
                                       text_color=SUBTLE, border_color=LINE)

    # ---- Discord: opt-in alerts to the user's own channel ----
    HOOK_EVENTS = (
        ("start_stop", "Run starts and stops", None),
        ("hourly", "Every full hour of playtime", None),
        ("error", "Stopped by an error", "error"),
        ("stuck", "Stuck: Roblox keeps closing, or can't get into a match",
         "stuck"),
        ("paused", "Paused for 10 minutes or more", "paused"),
        ("recover", "Recoveries: reopened, restarted or reconnected", None),
        ("updated", "Installed an update", None),
    )

    def _build_discord(self, page):
        sp = self._scroll_page(page)
        wh = self.settings.get("webhook")
        wh = wh if isinstance(wh, dict) else webhook_defaults()

        self._group(sp, "WEBHOOK")
        card = self._card(sp, corner_radius=14)
        card.pack(fill="x")
        self.sw_hook = self._switch_row(
            card, "Send alerts to Discord",
            "Off until a webhook link is saved below.", first=True,
            on=False, command=self._hook_switched)
        ctk.CTkFrame(card, height=1, fg_color=HAIRLINE,
                     corner_radius=0).pack(fill="x", padx=1)
        box = ctk.CTkFrame(card, fg_color="transparent")
        box.pack(fill="x", padx=14, pady=(10, 12))
        ctk.CTkLabel(box, text="Webhook link", text_color=TEXT,
                     font=self.F(13), anchor="w", height=18).pack(fill="x")
        row = ctk.CTkFrame(box, fg_color="transparent")
        row.pack(fill="x", pady=(6, 0))
        self.e_hook = ctk.CTkEntry(
            row, height=32, corner_radius=8, border_width=1,
            border_color=KEYCAP_LINE, fg_color=PANEL, text_color=TEXT,
            show="•", placeholder_text="https://discord.com/api/webhooks/…",
            placeholder_text_color=MUTED,
            font=self.F(11, family="Consolas"))
        self.e_hook.pack(side="left", fill="x", expand=True)
        self.e_hook.bind("<KeyRelease>", lambda _e: self._hook_validate(),
                         add="+")
        self.btn_hook_b = self._ghost(row, "Paste", self._hook_paste)
        self.btn_hook_b.configure(width=70, height=32)
        self.btn_hook_b.pack(side="right", padx=(8, 0))
        self.btn_hook_a = self._ghost(row, "Save", self._hook_save)
        self.btn_hook_a.configure(width=70, height=32)
        self.btn_hook_a.pack(side="right", padx=(8, 0))
        self.btn_eye = ctk.CTkButton(
            row, text="" if self._fam_icon else "👁", width=32,
            height=32, corner_radius=8, fg_color="transparent",
            hover_color=CARD_HI, border_width=1, border_color=LINE,
            text_color=SUBTLE,
            font=self.F(13, family=self._fam_icon or self._fam_sym),
            command=self._hook_eye)
        self.btn_eye.pack(side="right", padx=(8, 0))
        self.lbl_hook = ctk.CTkLabel(box, text="", text_color=MUTED,
                                     font=self.F(11), anchor="w",
                                     justify="left", height=16,
                                     wraplength=520)
        self.lbl_hook.pack(fill="x", pady=(6, 0))
        self._rewrap_on(box, (self.lbl_hook,))
        r = ctk.CTkFrame(box, fg_color="transparent")
        r.pack(fill="x", pady=(10, 0))
        self.btn_hook_test = self._ghost(r, "Send test", self._hook_test)
        self.btn_hook_test.pack(side="left")
        self.lbl_hook_test = ctk.CTkLabel(r, text="", text_color=MUTED,
                                          font=self.F(11), anchor="w",
                                          height=16)
        self.lbl_hook_test.pack(side="left", padx=(10, 0))
        ctk.CTkLabel(box, text="Anyone with this link can post in that "
                               "channel. Keep it to yourself.",
                     text_color=AMBER, font=self.F(11), anchor="w",
                     height=16).pack(fill="x", pady=(10, 0))

        self._group(sp, "SEND AN ALERT WHEN")
        card = self._card(sp, corner_radius=14)
        card.pack(fill="x")
        ev = wh.get("events") if isinstance(wh.get("events"), dict) else {}
        me = wh.get("mention") if isinstance(wh.get("mention"), dict) else {}
        self.chk_hook, self.chk_mention = {}, {}
        for i, (key, text, mention) in enumerate(self.HOOK_EVENTS):
            if i:
                ctk.CTkFrame(card, height=1, fg_color=HAIRLINE,
                             corner_radius=0).pack(fill="x", padx=1)
            row = ctk.CTkFrame(card, fg_color="transparent")
            row.pack(fill="x", padx=14, pady=7)
            chk = self._check(row, text, bool(ev.get(key)),
                              self._hook_options_changed)
            chk.pack(side="left")
            self.chk_hook[key] = chk
            if mention:
                m = self._check(row, "@ me", bool(me.get(mention)),
                                self._hook_options_changed, small=True)
                m.pack(side="right")
                self.chk_mention[mention] = m

        self._group(sp, "MENTIONS")
        card = self._card(sp, corner_radius=14)
        card.pack(fill="x")
        box = ctk.CTkFrame(card, fg_color="transparent")
        box.pack(fill="x", padx=14, pady=12)
        row = ctk.CTkFrame(box, fg_color="transparent")
        row.pack(fill="x")
        txt = ctk.CTkFrame(row, fg_color="transparent")
        txt.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(txt, text="Your Discord user ID", text_color=TEXT,
                     font=self.F(13), anchor="w", height=18).pack(fill="x")
        ctk.CTkLabel(txt, text="Only for the alerts ticked '@ me'. Discord: "
                               "Settings › Advanced › Developer Mode, then "
                               "right-click your name › Copy User ID.",
                     text_color=SUBTLE, font=self.F(11), anchor="w",
                     justify="left", height=16, wraplength=380
                     ).pack(fill="x")
        self._rewrap_on(txt, (txt.winfo_children()[-1],))
        self.e_uid = ctk.CTkEntry(row, width=190, height=30, corner_radius=8,
                                  border_width=1, border_color=KEYCAP_LINE,
                                  fg_color=PANEL, text_color=TEXT,
                                  placeholder_text="e.g. 123456789012345678",
                                  placeholder_text_color=MUTED,
                                  font=self.F(11, family="Consolas"))
        self.e_uid.pack(side="right", padx=(10, 0), anchor="n")
        if wh.get("user_id"):
            self.e_uid.insert(0, str(wh.get("user_id")))
        self.e_uid.bind("<KeyRelease>", lambda _e: self._uid_changed(),
                        add="+")
        self.e_uid.bind("<FocusOut>", lambda _e: self._uid_changed(),
                        add="+")
        self.lbl_uid = ctk.CTkLabel(box, text="", text_color=MUTED,
                                    font=self.F(11), anchor="w", height=16)
        self.lbl_uid.pack(fill="x", pady=(6, 0))

        self._group(sp, "WHAT AN ALERT LOOKS LIKE")
        self._build_hook_preview(sp)
        ctk.CTkLabel(sp, text="Sent only during a real run, at most one "
                              "message every few seconds. No screenshots, "
                              "PC name or file paths are ever sent.",
                     text_color=MUTED, font=self.F(11), height=16
                     ).pack(pady=(14, 8))
        self._hook_reveal = False
        self._hook_test_at = 0.0
        self._hook_editing = False
        self._paint_hook()
        self._uid_changed(save=False)

    def _check(self, parent, text, on, command, small=False):
        c = ctk.CTkCheckBox(parent, text=text, onvalue=True, offvalue=False,
                            checkbox_width=16 if small else 18,
                            checkbox_height=16 if small else 18,
                            corner_radius=5, border_width=1,
                            border_color=KEYCAP_LINE, fg_color=ACCENT,
                            hover_color=ACCENT_SOFT, checkmark_color=INK,
                            text_color=SUBTLE if small else TEXT,
                            font=self.F(11 if small else 12),
                            command=command)
        if on:
            c.select()
        else:
            c.deselect()
        return c

    def _build_hook_preview(self, sp):
        """A Discord message, drawn: who it's from, the coloured edge, the
        title, the fields and the footer."""
        card = self._card(sp, corner_radius=14)
        card.pack(fill="x")
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=14, pady=12)
        head = ctk.CTkFrame(inner, fg_color="transparent")
        head.pack(fill="x")
        gem = tk.Canvas(head, width=self._px(28), height=self._px(28),
                        bg=CARD, highlightthickness=0, bd=0)
        draw_gem(gem, self._px(2), self._px(2), self._px(24))
        gem.pack(side="left")
        ctk.CTkLabel(head, text=APP_NAME, text_color=TEXT, height=18,
                     font=self.F(13, semi=True)).pack(side="left",
                                                     padx=(10, 0))
        ctk.CTkLabel(head, text="APP", text_color=TEXT, height=16, width=30,
                     corner_radius=4, fg_color="#5865f2",
                     font=self.F(9, semi=True)).pack(side="left", padx=(6, 0))
        self.lbl_prev_ping = ctk.CTkLabel(head, text="", text_color=ACCENT,
                                          height=18, font=self.F(12))
        self.lbl_prev_ping.pack(side="left", padx=(8, 0))
        emb = ctk.CTkFrame(inner, fg_color=PANEL, corner_radius=6)
        emb.pack(fill="x", padx=(38, 0), pady=(6, 0))
        edge = tk.Frame(emb, bg=ACCENT, width=self._px(4), bd=0,
                        highlightthickness=0)
        edge.pack(side="left", fill="y")
        body = ctk.CTkFrame(emb, fg_color="transparent")
        body.pack(side="left", fill="x", expand=True, padx=12, pady=10)
        ctk.CTkLabel(body, text="Run started", text_color=TEXT, height=18,
                     anchor="w", font=self.F(13, semi=True)).pack(fill="x")
        ctk.CTkLabel(body, text="Playing Free For All.", text_color=SUBTLE,
                     height=16, anchor="w", font=self.F(12)).pack(fill="x")
        f = ctk.CTkFrame(body, fg_color="transparent")
        f.pack(fill="x", pady=(6, 0))
        life = span(self._stats.get("total_secs", 0))
        ctk.CTkLabel(f, text="Lifetime", text_color=TEXT, height=14,
                     anchor="w", font=self.F(11, semi=True)).pack(fill="x")
        ctk.CTkLabel(f, text=life, text_color=SUBTLE, height=16,
                     anchor="w", font=self.F(11)).pack(fill="x")
        ctk.CTkLabel(body, text=f"{APP_NAME} {APP_VER} · run "
                                f"#{self._stats.get('runs', 0) + 1}",
                     text_color=MUTED, height=14, anchor="w",
                     font=self.F(10)).pack(fill="x", pady=(8, 0))

    def _hook_settings(self):
        wh = self.settings.get("webhook")
        if not isinstance(wh, dict):
            wh = webhook_defaults()
            self.settings["webhook"] = wh
        return wh

    def _paint_hook(self):
        """The link row: masked once saved, an editable box otherwise."""
        wh = self._hook_settings()
        saved = normalize_hook(wh.get("url"))
        e = self.e_hook
        e.configure(state="normal")
        self._entry_set(e, "")
        if saved and not self._hook_editing:
            e.configure(show="")
            e.insert(0, mask_hook(saved))
            e.configure(state="disabled", text_color=SUBTLE)
            self.btn_hook_a.configure(text="Change", command=self._hook_change)
            self.btn_hook_b.configure(text="Remove", command=self._hook_remove)
            self.btn_eye.pack_forget()
            self.lbl_hook.configure(text="Saved. Send test to check it "
                                         "reaches your channel.",
                                    text_color=GREEN)
            self.sw_hook.configure(state="normal")
            if wh.get("enabled") is True:
                self.sw_hook.select()
            else:
                self.sw_hook.deselect()
        else:
            e.configure(show="" if self._hook_reveal else "•",
                        text_color=TEXT)
            self.btn_hook_a.configure(text="Save", command=self._hook_save)
            self.btn_hook_b.configure(text="Paste", command=self._hook_paste)
            if not self.btn_eye.winfo_manager():
                self.btn_eye.pack(side="right", padx=(8, 0))
            if not saved:
                self.sw_hook.deselect()
                self.sw_hook.configure(state="disabled")
            self._hook_validate()
        self.btn_hook_test.configure(state="normal" if saved else "disabled")

    def _hook_validate(self):
        """The line under the box, as the link is typed or pasted."""
        if self.e_hook.cget("state") == "disabled":
            return
        text = self.e_hook.get().strip()
        if not text:
            msg, colour, ok = ("In Discord: channel settings › Integrations › "
                               "Webhooks › New Webhook › Copy Webhook URL, "
                               "then Paste."), MUTED, False
        elif normalize_hook(text):
            msg, colour, ok = "Looks right · press Save.", GREEN, True
        else:
            msg, colour, ok = ("That isn't a Discord webhook link. It starts "
                               "https://discord.com/api/webhooks/"), RED, False
        self.lbl_hook.configure(text=msg, text_color=colour)
        self.btn_hook_a.configure(state="normal" if ok else "disabled")

    @staticmethod
    def _entry_set(e, text):
        """Replace a CTkEntry's text. Deleting from an entry that shows its
        placeholder blanks the placeholder (a CustomTkinter quirk), so only
        delete real text."""
        if e.get():
            e.delete(0, "end")
        if text:
            e.insert(0, text)

    def _hook_eye(self):
        self._hook_reveal = not self._hook_reveal
        if self.e_hook.cget("state") != "disabled":
            self.e_hook.configure(show="" if self._hook_reveal else "•")

    def _hook_paste(self):
        try:
            text = self.clipboard_get()
        except Exception:
            text = ""
        self._entry_set(self.e_hook, str(text).strip()[:300])
        self._hook_validate()

    def _hook_save(self):
        url = normalize_hook(self.e_hook.get())
        if not url:
            self._hook_validate()
            return
        wh = self._hook_settings()
        wh["url"] = url
        save_settings(self.settings)
        self._hook_editing = False
        self._hook_reveal = False
        self._paint_hook()
        self.log("webhook: link saved")

    def _hook_change(self):
        self._hook_editing = True
        self._paint_hook()
        self.e_hook.focus_set()

    def _hook_remove(self):
        wh = self._hook_settings()
        wh["url"], wh["enabled"] = "", False
        save_settings(self.settings)
        self._sender = None
        self._hook_editing = False
        self._paint_hook()
        self.log("webhook: link removed - alerts are off")

    def _hook_switched(self):
        wh = self._hook_settings()
        on = bool(self.sw_hook.get()) and bool(normalize_hook(wh.get("url")))
        wh["enabled"] = on
        save_settings(self.settings)
        if not on:
            self.sw_hook.deselect()

    def _hook_options_changed(self):
        wh = self._hook_settings()
        ev = wh.setdefault("events", {})
        for key, chk in self.chk_hook.items():
            ev[key] = bool(chk.get())
        me = wh.setdefault("mention", {})
        for key, chk in self.chk_mention.items():
            me[key] = bool(chk.get())
        save_settings(self.settings)
        self._paint_ping()

    def _uid_changed(self, save=True):
        uid = self.e_uid.get().strip()
        ok = bool(re.fullmatch(r"\d{17,20}", uid))
        if not uid:
            self.lbl_uid.configure(text="No ID: alerts never @mention anyone.",
                                   text_color=MUTED)
        elif ok:
            self.lbl_uid.configure(text="Ticked alerts will @mention you.",
                                   text_color=GREEN)
        else:
            self.lbl_uid.configure(text="A user ID is 17 to 20 digits.",
                                   text_color=RED)
        if save:
            wh = self._hook_settings()
            new = uid if ok else ""
            if wh.get("user_id", "") != new:
                wh["user_id"] = new
                save_settings(self.settings)
        self._paint_ping()

    def _paint_ping(self):
        wh = self._hook_settings()
        me = wh.get("mention") if isinstance(wh.get("mention"), dict) else {}
        on = re.fullmatch(r"\d{17,20}", str(wh.get("user_id") or "")) and \
            any(me.values())
        self.lbl_prev_ping.configure(text="@you · only on problems"
                                     if on else "")

    def _hook_test(self):
        """Send test: only ever on a click, at most once every 5 seconds,
        on its own thread. GLASSMACRO_NO_SEND still blocks it."""
        url = normalize_hook(self._hook_settings().get("url"))
        if not url:
            return
        now = time.time()
        if now - self._hook_test_at < 5:
            return
        self._hook_test_at = now
        self.btn_hook_test.configure(state="disabled", text="Sending…")
        self.lbl_hook_test.configure(text="", text_color=MUTED)
        wh = self._hook_settings()
        uid = str(wh.get("user_id") or "")
        me = wh.get("mention") if isinstance(wh.get("mention"), dict) else {}
        mention = [uid] if re.fullmatch(r"\d{17,20}", uid) and any(
            me.values()) else []
        embed = build_embed(
            "start", "Test from GlassMacro",
            "Alerts reach this channel. Nothing else was sent.", HOOK_ACCENT,
            [("Lifetime", span(self._stats.get("total_secs", 0)))],
            f"{APP_NAME} {APP_VER} · test")
        payload = build_payload([embed], mention)

        def run():
            try:
                sender = WebhookSender(url, log=self._hook_log)
                sender._hurry = True          # one try: no minute of retries
                status = sender.post_now(payload)
            except Exception:
                status = "dropped"
            self._ui(lambda s=status: self._hook_tested(s))
        threading.Thread(target=run, daemon=True).start()

    def _hook_tested(self, status):
        text, colour = {
            "ok": ("Sent · check the channel.", GREEN),
            "blocked": ("Not sent: sending is switched off in this copy.",
                        MUTED),
            "dead": ("Discord says this link doesn't work. Copy it again.",
                     RED),
        }.get(status, ("Couldn't reach Discord. Try again in a bit.", AMBER))
        self.lbl_hook_test.configure(text=text, text_color=colour)
        self.after(max(0, int(5000 - (time.time() - self._hook_test_at)
                              * 1000)),
                   lambda: self.btn_hook_test.configure(
                       state="normal", text="Send test"))

    # ---- About ----
    SHORTCUTS = (("F8", "Start or stop · works while Rivals is in front"),
                 ("F8", "During setup: save the spot under the mouse"),
                 ("Esc", "Cancel a setup or a teach"))

    def _build_about(self, page):
        sp = self._scroll_page(page)
        card = self._card(sp)
        card.pack(fill="x", pady=(16, 0))
        self._sheen(card)
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=18, pady=18)
        gem = tk.Canvas(inner, width=self._px(44), height=self._px(44),
                        bg=CARD, highlightthickness=0, bd=0)
        draw_gem(gem, 0, 0, self._px(44))
        gem.pack(side="left", anchor="n")
        txt = ctk.CTkFrame(inner, fg_color="transparent")
        txt.pack(side="left", fill="x", expand=True, padx=(16, 0))
        name = ctk.CTkFrame(txt, fg_color="transparent")
        name.pack(fill="x")
        ctk.CTkLabel(name, text="Glass", text_color=ACCENT, height=28,
                     font=self.F(22, semi=True)).pack(side="left")
        ctk.CTkLabel(name, text="Macro", text_color=TEXT, height=28,
                     font=self.F(22, semi=True)).pack(side="left")
        ctk.CTkLabel(name, text=f"Version {APP_VER}", text_color=MUTED,
                     height=28, font=self.F(12)).pack(side="left",
                                                      padx=(10, 0))
        tag = ctk.CTkLabel(txt, text="Keeps you in Rivals Free For All while "
                                     "you're away: picks Grenade Launcher + "
                                     "Random, rejoins and recovers by itself.",
                           text_color=SUBTLE, font=self.F(12), anchor="w",
                           justify="left", height=18, wraplength=500)
        tag.pack(fill="x", pady=(2, 0))
        self._rewrap_on(txt, (tag,))
        res = ctk.CTkFrame(txt, fg_color="transparent")
        res.pack(fill="x", pady=(8, 0))
        ctk.CTkLabel(res, text="Screen", text_color=MUTED, height=16,
                     font=self.F(11)).pack(side="left")
        self.lbl_res_about = ctk.CTkLabel(res, text="", height=16,
                                          font=self.F(11, semi=True))
        self.lbl_res_about.pack(side="left", padx=(8, 0))
        sw, sh = screen_size()
        self._paint_res(sw, sh, self._scaling)

        self._group(sp, "UPDATES")
        card = self._card(sp, corner_radius=14)
        card.pack(fill="x")
        self.sw_update = self._switch_row(
            card, "Check for updates",
            "Asks GitHub if there's a newer version. Nothing about you is sent.",
            first=True, on=self.settings.get("check_updates", True),
            command=self._update_switched)
        ctk.CTkFrame(card, height=1, fg_color=HAIRLINE,
                     corner_radius=0).pack(fill="x", padx=1)
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=10)
        self.btn_check = self._ghost(row, "Check now", self._check_now)
        self.btn_check.configure(width=100)
        self.btn_check.pack(side="right")
        self.lbl_upd = ctk.CTkLabel(row, text="", text_color=SUBTLE,
                                    font=self.F(12), anchor="w", height=18)
        self.lbl_upd.pack(side="left", fill="x", expand=True)
        self._paint_upd(None)

        self._group(sp, "SHORTCUTS")
        card = self._card(sp, corner_radius=14)
        card.pack(fill="x")
        for i, (key, what) in enumerate(self.SHORTCUTS):
            if i:
                ctk.CTkFrame(card, height=1, fg_color=HAIRLINE,
                             corner_radius=0).pack(fill="x", padx=1)
            row = ctk.CTkFrame(card, fg_color="transparent")
            row.pack(fill="x", padx=14, pady=8)
            cap = ctk.CTkFrame(row, fg_color=PANEL, corner_radius=6,
                               border_width=1, border_color=KEYCAP_LINE,
                               width=44, height=24)
            cap.pack(side="left")
            cap.pack_propagate(False)
            ctk.CTkLabel(cap, text=key, text_color=SUBTLE, height=18,
                         font=self.F(10, semi=True, family="Consolas")
                         ).pack(expand=True)
            ctk.CTkLabel(row, text=what, text_color=TEXT, font=self.F(12),
                         anchor="w", height=18).pack(side="left",
                                                     padx=(12, 0))

        link = ctk.CTkLabel(sp, text="Release notes on GitHub ›",
                            text_color=ACCENT, font=self.F(11, semi=True),
                            height=16, cursor="hand2")
        link.pack(pady=(16, 8))
        link.bind("<Button-1>", lambda _e: self._open_release_page())

    def _paint_upd(self, note, colour=None):
        """About's update line: what the last check found."""
        lbl = getattr(self, "lbl_upd", None)
        if lbl is None:
            return
        if note is None:
            on = self.settings.get("check_updates", True)
            note = ("Checks at start-up and every 12 hours." if on
                    else "Update checks are off.")
        elif note.startswith("update check: up to date"):
            note = f"Up to date · checked {time.strftime('%H:%M')}"
            colour = GREEN
        elif note.startswith("update check:"):
            note, colour = "Couldn't reach GitHub · it'll try again later.", \
                AMBER
        lbl.configure(text=note, text_color=colour or SUBTLE)

    def _check_now(self):
        """'Check now' on About: one check, on its own thread, only ever
        from this click."""
        if getattr(self, "_checking", False):
            return
        if not self.settings.get("check_updates", True):
            self._paint_upd("Turn on Check for updates first.", AMBER)
            return
        self._checking = True
        self.btn_check.configure(state="disabled", text="Checking…")

        def run():
            try:
                self._check_updates_once()
            except Exception:
                pass
            finally:
                self._ui(self._check_done)
        threading.Thread(target=run, daemon=True).start()

    def _check_done(self):
        self._checking = False
        self.btn_check.configure(state="normal", text="Check now")

    def _group(self, parent, title):
        lbl = ctk.CTkLabel(parent, text=title, text_color=MUTED, anchor="w",
                           font=self.F(11, semi=True), height=16)
        lbl.pack(fill="x", padx=4, pady=(16, 6))
        return lbl

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
        about = getattr(self, "lbl_res_about", None)    # the About page's
        if about is not None:
            about.configure(text=text, text_color=SUBTLE if colour == MUTED
                            else colour)

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

    def _notify(self, headline, details):
        """Windows' warning pop-up with the Foreground sound - at most one a
        minute, and never two at once. On its own thread, so the window and
        the macro carry on behind it."""
        if not hasattr(self, "_popup"):
            self._popup = WarningPopup()
        pop = self._popup
        if pop.open or time.time() - pop.last < NOTIFY_GAP:
            return
        pop.last = time.time()

        def run():
            try:
                choice = pop.show("GlassMacro", headline, details)
            except Exception as exc:
                self._ui(lambda e=str(exc): self.log(
                    f"could not show the warning pop-up: {e}"))
                return
            if choice == "settings":
                try:
                    os.startfile("ms-settings:display")
                except Exception:
                    pass
            self._ui(lambda c=choice: self.log(
                f"warning pop-up shown: {headline} ({c or 'not shown'})"))
        threading.Thread(target=run, daemon=True).start()

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
            self._notify(f"Windows scaling is {pct}%",
                         "GlassMacro only works at 100% scaling - at "
                         f"{pct}% its clicks land in the wrong place.\n\n"
                         "Set it to 100% in Settings \u203a System \u203a "
                         "Display \u203a Scale.")
        elif (sw, sh) != SUPPORTED_SCREEN:
            self._notify(f"This screen is {sw}\u00d7{sh}",
                         "GlassMacro only works on 1920\u00d71080 screens for "
                         "now, so its clicks may land in the wrong place.")
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
        self._paint_upd(None)

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
        info = release_info()
        found = (info["version"], info["page"]) if info else None
        if found and version_tuple(found[0]) > version_tuple(APP_VER):
            self._ui(lambda f=found, i=info: self._show_update(*f, info=i))
            return True                   # one notice is enough
        # one quiet line on the Log page / log.txt, so "did it even check?" has
        # an answer - the feed ignores it
        note = (f"update check: up to date (latest on GitHub is {found[0]})"
                if found
                else "update check: couldn't reach GitHub - will try later")
        self._ui(lambda n=note: (self.log(n), self._paint_upd(n)))
        return False

    def _show_update(self, version, url, info=None):
        self._update_url = url
        self._update_info = info or {"version": version, "page": url,
                                     "zip": None, "sha256": None}
        self.lnk_update.configure(text=f"Update {version} available \u203a")
        self.lbl_res.pack_forget()
        self.lnk_update.pack(side="left", padx=20)
        self._paint_upd(f"Update {version} is out · the link is "
                        f"bottom-left", ACCENT)
        self.log(f"update available: GlassMacro {version}")
        self._ask_update()

    def _open_update(self):
        """The header link: ask again, the same way."""
        self._update_asked = False
        self._ask_update()

    def _open_release_page(self):
        try:
            os.startfile(self._update_url)
        except Exception as exc:
            self.log(f"could not open the release page: {exc}")

    def _ask_update(self):
        """'An update is available - do you want to update?'. Never while a
        run is going: that would end someone's AFK session - it waits for
        the run to stop instead (see _tick)."""
        info = getattr(self, "_update_info", None)
        if not info or getattr(self, "_updating", False):
            return
        if self.running:
            self._update_pending = True
            return
        if getattr(self, "_update_asked", False):
            return
        if not hasattr(self, "_popup"):
            self._popup = WarningPopup()
        pop = self._popup
        if pop.open:
            # another pop-up (a screen warning) is up - ask once it's gone,
            # rather than never
            self.after(3000, self._ask_update)
            return
        self._update_asked = True
        self._update_pending = False

        def run():
            pop.open, pop.last = True, time.time()
            try:
                choice = ask_to_update(info["version"])
            except Exception as exc:
                choice = None
                self._ui(lambda e=str(exc): self.log(
                    f"could not show the update pop-up: {e}"))
            finally:
                pop.open = False
            self._ui(lambda c=choice: self._update_answer(c))
        threading.Thread(target=run, daemon=True).start()

    def _update_answer(self, choice):
        self.log(f"update pop-up: {choice or 'not shown'}")
        if choice != "yes":
            return
        if self.running:
            # they said yes, then started a run before clicking it - keep the
            # yes and update the moment the run stops (see _tick)
            self._update_yes_pending = True
            self.log("update: will install when this run stops")
            return
        self._start_update()

    def _can_self_update(self):
        """Only a built app can replace itself, and only somewhere it may
        write. Anything else: the release page does it by hand."""
        if not getattr(sys, "frozen", False):
            return False
        info = self._update_info
        if not info.get("zip") or not info.get("sha256"):
            return False
        here = os.path.dirname(os.path.abspath(sys.executable))
        probe = os.path.join(here, ".glassmacro-write-test")
        try:
            with open(probe, "w") as fh:
                fh.write("ok")
            os.remove(probe)
            return True
        except OSError:
            return False

    def _start_update(self):
        if self.running or getattr(self, "_updating", False):
            return
        if not self._can_self_update():
            self.log("update: this copy can't update itself - opening the "
                     "download page")
            self._open_release_page()
            return
        self._updating = True
        info = self._update_info
        self.set_state(f"Updating to {info['version']}",
                       "Downloading \u00b7 0%", ACCENT)
        threading.Thread(target=self._update_job, args=(info,),
                         daemon=True).start()

    def _update_job(self, info):
        """Worker thread: download, check, unpack, then hand over to the new
        version and close. Any failure: nothing changes."""
        try:
            os.makedirs(UPDATE_DIR, exist_ok=True)
            zip_path = os.path.join(UPDATE_DIR, f"GlassMacro-v{info['version']}.zip")
            shown = [-1]

            def progress(done, total):
                pct = int(done * 100 / total) if total else 0
                if pct != shown[0]:
                    shown[0] = pct
                    mb = f" of {total / 1048576:.0f} MB" if total else ""
                    self._ui(lambda p=pct, m=mb: self.set_state(
                        f"Updating to {info['version']}",
                        f"Downloading \u00b7 {p}%{m}", ACCENT))
            if not download_update(info["zip"], info["sha256"], zip_path,
                                   progress):
                raise RuntimeError("the download didn't arrive in one piece")
            self._ui(lambda: self.set_state(f"Updating to {info['version']}",
                                            "Unpacking...", ACCENT))
            new_exe = stage_update(zip_path, os.path.join(UPDATE_DIR, "new"))
            try:
                os.remove(zip_path)
            except OSError:
                pass
            if not new_exe:
                raise RuntimeError("the download wasn't a GlassMacro build")
            here = os.path.dirname(os.path.abspath(sys.executable))
            subprocess.Popen(
                [new_exe, "--finish-update", here, str(os.getpid()), APP_VER],
                cwd=os.path.dirname(new_exe), close_fds=True,
                creationflags=0x00000008 | 0x00000200)  # DETACHED, NEW_GROUP
            self._ui(lambda: self.log(
                f"update: handing over to {info['version']} - GlassMacro "
                f"will reopen by itself"))
            self._ui(self._close)
        except Exception as exc:
            self._updating = False
            self._ui(lambda e=str(exc): self._update_failed(e))

    def _update_failed(self, why):
        self.log(f"update failed: {why}")
        self.set_state("Update didn't work",
                       "Nothing changed. Opening the download page instead.",
                       RED)
        self._open_release_page()

    def _after_update(self):
        """First start after an update: say so, and tidy the staging folder
        once the hand-over process has had time to exit."""
        flag = os.path.join(UPDATE_DIR, "updated.txt")
        try:
            with open(flag, encoding="utf-8") as fh:
                old = fh.read().strip()
            os.remove(flag)
        except OSError:
            return          # not just updated: never touch the staging folder,
            #                 a hand-over might be running from it right now
        self.log(f"updated: GlassMacro {old} -> {APP_VER}")

        def tidy():
            time.sleep(20)
            shutil.rmtree(os.path.join(UPDATE_DIR, "new"), ignore_errors=True)
            shutil.rmtree(os.path.join(
                os.path.dirname(os.path.abspath(sys.executable)),
                "_internal.old"), ignore_errors=True)
        threading.Thread(target=tidy, daemon=True).start()

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
        self._paint_pill()

    def _status(self, text, colour):
        """RUNNING / IDLE / WATCHING. Only the dot shows it now - the old
        header pill said the same thing as the card, and they could briefly
        disagree."""
        self._run_mode = text
        if text != "RUNNING":
            self._dot_colour = colour if text == "WATCHING" else MUTED
        self._paint_dot()
        self._paint_pill()

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
                text="Counts once you press Start")
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
        for v in (self.val_picks, self.val_joins, self.val_recov):
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
                self._live_tick(now)
            elif self._was_running:
                # stopped - by F8, by Stop, or the worker ending on an error
                self._was_running = False
                keep_awake(False)
                self._apply_topmost()
                if getattr(self, "_update_yes_pending", False):
                    self._update_yes_pending = False
                    self.after(1500, self._start_update)
                elif getattr(self, "_update_pending", False):
                    self.after(1500, self._ask_update)
                self._last_run = {"secs": now - (self._run_t0 or now),
                                  "end": time.strftime("%H:%M")}
                self._show_playtime(self._last_run["secs"], live=False)
                if self._live_run:
                    # a moment later, so an error line the worker queued just
                    # before it stopped has been read first
                    self.after(200, lambda n=now, r=self._last_run["secs"],
                               s=self._run_seq: self._run_ended(n, ran=r,
                                                                seq=s))
            if self.running and self._state_since:
                self.lbl_since.configure(text="since " + time.strftime(
                    "%H:%M", time.localtime(self._state_since)))
            else:
                self.lbl_since.configure(text="")
            if self._ticks % 5 == 0 or self._live_run:
                today = self._today_text()
                if self.val_today.cget("text") != today:
                    self.val_today.configure(text=today)
            if self._page == "stats":
                self._paint_stats()
            if self._setup_active and self._guide["t0"]:
                # no countdown: the setup worker only checks its time limit
                # when a key arrives, so a clock reaching 0:00 would be a lie
                self._guide_status("Esc cancels · take your time",
                                   SUBTLE)
            self._paint_pill()
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
         "The Log page says what happened.", "RED", None),
        ("Roblox closed - reopening Rivals", "Reopening Rivals",
         "Roblox closed, so it's starting Rivals again.", "AMBER", None),
        ("Roblox keeps closing", "Roblox keeps closing",
         "It won't reopen it again this hour.", "RED", None),
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
    # are still on the Log page and in log.txt.
    FEED_RULES = (
        ("stopped on an error", "err", "×", "RED",
         "Stopped on an error", "see the Log page"),
        ("Roblox closed - reopening Rivals", "reopen", "!", "AMBER",
         "Reopened Rivals", "Roblox had closed"),
        ("Roblox keeps closing", "reopenstop", "×", "RED",
         "Roblox keeps closing", "not reopening it again this hour"),
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
        ("updated: GlassMacro", "updated", "\u2713", "GREEN",
         "Updated", "you're on the newest version"),
        ("update failed:", "updfail", "!", "AMBER", "Update didn't work",
         "nothing changed - try the download page"),
        ("update available:", "update", "\u2191", "ACCENT",
         "Update available", "the link is bottom-left"),
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

    # Lifetime stats and Discord alerts: (needle, key), first match wins.
    # Read by _events_from_log; counters only move during a real run.
    EVENT_RULES = (
        ("stopped on an error", "error"),
        ("Roblox keeps closing", "gave_up"),
        ("could not get into a match", "no_join"),
        ("Roblox closed - reopening Rivals", "reopen"),
        ("still stuck - restarting Roblox", "restart"),
        ("connection dialog", "reconnect"),
        ("PAUSED - Roblox is not the focused window", "paused"),
        ("Roblox is back in front - resuming", "resumed"),
        ("quiet for", "quiet"),
        ("NOT the launcher there", "skipped"),
        ("updated: GlassMacro", "updated"),
    )
    # event key -> the stats.json counter it bumps
    STAT_OF = {"error": "errors", "gave_up": "reopen_giveups",
               "no_join": "join_failures", "reopen": "roblox_reopens",
               "restart": "roblox_restarts", "reconnect": "reconnects",
               "quiet": "quiet_stretches", "skipped": "skipped_picks"}
    # alert kind -> the settings group that switches it on
    HOOK_GROUP = {"start": "start_stop", "stop": "start_stop",
                  "closed": "start_stop", "hourly": "hourly",
                  "error": "error", "gave_up": "stuck", "no_join": "stuck",
                  "paused10": "paused", "reopen": "recover",
                  "restart": "recover", "reconnect": "recover",
                  "updated": "updated"}

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
            self._end_reason = "stopped"
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
        # the model behind every view: same merge rule, newest first
        items = self._feed_items
        it = {"key": key, "glyph": glyph, "colour": colour, "title": title,
              "sub": sub, "count": 1, "ts": time.strftime("%H:%M")}
        if items and items[0]["key"] == key and key not in ("start", "stop"):
            it["count"] = items[0]["count"] + 1
            items[0] = it
        else:
            items.insert(0, it)
            del items[300:]
        if self._feed_filter != "All":
            # the textbox merge below assumes its top row is _feed_top, which
            # isn't true under a filter - redraw from the model instead
            self._feed_top = (key, it["count"])
            self._feed_rows = len(items)
            self._feed_rebuild()
            self._paint_recent()
            return
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
        self._paint_feed_empty()
        self._paint_recent()

    # ---- lifetime stats and Discord alerts follow the log too ----
    # Nothing here can count or send unless _live_run is set, and only the
    # start path of toggle_run sets it - so the tests and the screenshot tool,
    # which only ever log lines, can never post or add to stats.json.
    HOOK_TEXT = {
        "paused10": ("Paused for 10 minutes",
                     "Rivals isn't the focused window. Click into it and "
                     "the run carries on."),
        "gave_up": ("Roblox kept closing",
                    "It won't be reopened again this hour."),
        "no_join": ("Couldn't join a match",
                    "Tried 3 times - it keeps trying."),
        "reopen": ("Reopened Rivals",
                   "Roblox had closed, so it was started again."),
        "restart": ("Restarted Roblox",
                    "Reconnecting didn't work, so Roblox was started fresh."),
        "reconnect": ("Reconnected",
                      "Got disconnected and clicked Reconnect."),
        "updated": ("Updated", ""),
    }

    def _events_from_log(self, msg):
        text = str(msg)
        if text.startswith("webhook:"):
            return                       # never react to our own lines
        for needle, key in self.EVENT_RULES:
            if needle in text:
                break
        else:
            return
        if key == "paused":
            if self._paused_since is None:
                self._paused_since = time.time()
            return
        if key == "resumed":
            self._paused_since, self._paused_sent = None, False
            return
        if key in ("reopen", "restart"):
            # Roblox came back without a "resuming" line; if it isn't in
            # front the worker logs PAUSED again and the clock restarts
            self._paused_since, self._paused_sent = None, False
        wh = self.settings.get("webhook")
        detail = scrub(text, wh.get("url") if isinstance(wh, dict) else None)
        if key == "updated":
            # logged at start-up, before any run - it goes out with the
            # next real Start
            self._updated_note = detail
            return
        if self._live_run and key in self.STAT_OF:
            st = self._stats
            st[self.STAT_OF[key]] = st.get(self.STAT_OF[key], 0) + 1
        if self._live_run and key in ("reopen", "restart", "reconnect"):
            self.n_recov += 1
            self.val_recov.configure(text=str(self.n_recov))
        if key == "error":
            # the worker has stopped; the stop alert carries this
            self._end_reason, self._end_detail = "error", detail
            return
        if key in self.HOOK_GROUP:
            self._hook(key, detail)

    def _stats_flush(self, now, counters=False, save=False):
        """Add the time since the last tick (0-5 s, so a clock that jumps
        can't add hours) and, with counters/save, the new loadouts and
        rejoins."""
        st = self._stats
        d = min(5.0, max(0.0, now - self._flushed_at))
        stats_add_time(st, now - d, now)
        self._flushed_at = now
        cur = st.get("current")
        if cur is not None:
            cur["secs"] = cur.get("secs", 0) + d
        if counters or save:
            st["loadouts"] += max(0, self.n_picks - self._flushed_picks)
            st["rejoins"] += max(0, self.n_joins - self._flushed_joins)
            self._flushed_picks, self._flushed_joins = self.n_picks, self.n_joins
        if save:
            if cur is not None:
                cur["flushed_at"] = now
            save_stats(st)
            self._stats_saved_at = now

    def _live_tick(self, now):
        """Every second of a real run: stats (saved once a minute), the
        hourly alert and the 10-minute pause alert."""
        if not self._live_run:
            return
        try:
            due = (now - self._stats_saved_at >= 60
                   or now < self._stats_saved_at)
            self._stats_flush(now, save=due)
        except Exception:
            pass
        try:
            h = int((now - self.session_start) // 3600)
            if self._hour_sent is not None and h > self._hour_sent:
                self._hour_sent = h
                self._hook("hourly")
            if (self._paused_since is not None and not self._paused_sent
                    and now - self._paused_since >= 600):
                self._paused_sent = True
                self._hook("paused10")
        except Exception:
            pass

    def _run_started(self):
        """The start path of toggle_run, once the worker is running: the only
        place a run starts counting, and the only way alerts get armed."""
        now = time.time()
        if self._live_run:               # restarted before the end was seen
            self._run_ended(now)
        st = self._stats
        stamp = time.strftime("%Y-%m-%d %H:%M")
        self._run_seq += 1
        self._live_run = True
        self._hour_sent = 0
        self._paused_since, self._paused_sent = None, False
        self._end_reason, self._end_detail = "stopped", ""
        self._flushed_at = self._stats_saved_at = now
        self._flushed_picks, self._flushed_joins = self.n_picks, self.n_joins
        st["runs"] = st.get("runs", 0) + 1
        st["first_run"] = st.get("first_run") or stamp
        st["last_run"] = stamp
        st["current"] = {"start": stamp, "secs": 0, "flushed_at": now}
        save_stats(st)
        self._hook("start")
        if self._updated_note:
            self._hook("updated", self._updated_note)
            self._updated_note = None

    def _run_ended(self, now, kind="stop", ran=None, seq=None):
        """A real run ended (Stop, F8, an error, or closing the app): final
        stats, the longest run, and the stop alert."""
        if not self._live_run or (seq is not None and seq != self._run_seq):
            return
        st = self._stats
        secs = 0
        try:
            self._stats_flush(now, counters=True)
            cur = st.get("current") or {}
            secs = cur.get("secs", 0)
            if secs > st.get("longest_secs", 0):
                st["longest_secs"] = secs
                st["longest_on"] = str(cur.get("start", ""))[:10]
            st["current"] = None
            save_stats(st)
        except Exception:
            pass
        try:
            self._hook(kind, self._end_detail,
                       ran=secs if ran is None else ran)
        finally:
            self._live_run = False
            self._hour_sent = None
            self._paused_since, self._paused_sent = None, False

    def _hook_log(self, line):
        """The sender's 'webhook:' lines, from its own thread."""
        self._ui(lambda: self.log(line))

    def _hook(self, kind, detail="", ran=None):
        """Queue one Discord alert - only during a real run, with alerts
        switched on, a valid link and that kind of alert ticked."""
        try:
            if not self._live_run:
                return False
            if kind not in ("stop", "closed") and not (
                    self._worker is not None and self._worker.is_alive()):
                return False
            wh = self.settings.get("webhook")
            if not isinstance(wh, dict) or wh.get("enabled") is not True:
                return False
            url = normalize_hook(wh.get("url"))
            if not url:
                return False
            events = wh.get("events")
            events = events if isinstance(events, dict) else {}
            group = self.HOOK_GROUP.get(kind)
            if (kind == "stop" and self._end_reason == "error"
                    and events.get("error")):
                group = "error"
            if not group or not events.get(group):
                return False
            uid = str(wh.get("user_id") or "").strip()
            mentions = wh.get("mention")
            mentions = mentions if isinstance(mentions, dict) else {}
            mention = uid if (group in ("error", "stuck", "paused")
                              and mentions.get(group)
                              and re.fullmatch(r"\d{17,20}", uid)) else None
            embed = self._hook_embed(kind, detail, ran)
            if self._sender is None or self._sender.url != url:
                self._sender = WebhookSender(url, log=self._hook_log)
            pri = (PRI_PERIODIC if kind == "hourly" else
                   PRI_MILESTONE if kind in ("start", "updated", "reopen",
                                             "restart", "reconnect")
                   else PRI_NORMAL)
            return self._sender.enqueue({"embed": embed, "mention": mention},
                                        pri)
        except Exception:
            return False

    def _hook_embed(self, kind, detail, ran=None):
        st = self._stats
        live = time.time() - self.session_start if self.session_start else 0
        life = ("Lifetime", span(st.get("total_secs", 0)))
        counts = [("Loadouts", self.n_picks), ("Rejoins", self.n_joins)]
        colour = None
        if kind == "start":
            title, desc = "Run started", "Playing Free For All."
            fields = [life]
        elif kind in ("stop", "closed"):
            secs = ran if ran is not None else live
            if kind == "stop" and self._end_reason == "error":
                title, colour = "Stopped by an error", HOOK_RED
                desc = detail or "The log says what happened."
            elif kind == "closed":
                title, desc = "GlassMacro was closed", "The run ended with it."
                colour = HOOK_MUTED
            else:
                title, desc, colour = "Run stopped", "", HOOK_MUTED
            fields = [("Ran", span(secs))] + counts + [life]
        elif kind == "hourly":
            title, desc = f"{self._hour_sent}h of playtime", "Still going."
            fields = [("Playtime", span(live))] + counts + [life]
        else:
            title, desc = self.HOOK_TEXT.get(kind, (kind, ""))
            if kind == "updated":
                desc = detail
            fields = [("Playtime", span(live))] if self.session_start else []
        footer = f"{APP_NAME} {APP_VER} · run #{st.get('runs', 0)}"
        return build_embed(kind, title, desc, colour, fields, footer)

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
                               "The Log page has the details.", RED)
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
            if is_cur and not self._guide_wide:
                self._draw_picker(row["pic"], i - 1, CARD_HI)
                row["pic"].pack(side="right", padx=(8, 0))
            else:
                row["pic"].pack_forget()
        if self._guide_wide:
            step = min(3, max(1, current))
            self._draw_picker(self.pic_preview, step - 1, INSET,
                              self.PREVIEW_K)
            self.lbl_preview_cap.configure(text=f"STEP {step} OF 3")
            self.lbl_preview.configure(text=self.STEPS[step - 1][0])
            if not self._preview.winfo_manager():
                self._preview.pack(side="right", fill="y", padx=(16, 0),
                                   before=self._guide_body)
        elif self._preview.winfo_manager():
            self._preview.pack_forget()
        if self._setup_active:
            self.btn_cal.configure(state="disabled",
                                   text="Hover it and press F8",
                                   fg_color=CARD_HI)
        else:
            self.btn_cal.configure(state="normal",
                                   text="I'm on the weapon picker · "
                                        "start setup", fg_color=ACCENT)

    def _refresh_layout(self):
        """Home shows the status card once setup is done; until then a card
        pointing at the Weapons page, where the guide lives."""
        want = self._setup_active or not self._ready()
        if want == self._guide_visible:
            return
        self._guide_visible = want
        if want:
            self.hero.grid_remove()
            self.tiles.grid_remove()
            self.home_low.grid_remove()
            self.cta.grid(row=0, column=0, sticky="ew")
            # the Weapons page: the guide instead of the summary
            self.weap_ready.pack_forget()
            self.guide.pack(fill="x", pady=(16, 0), before=self._det_group)
        else:
            self.cta.grid_remove()
            self.hero.grid(row=0, column=0, sticky="ew")
            self.tiles.grid(row=1, column=0, sticky="ew", pady=(12, 0))
            self.home_low.grid(row=2, column=0, sticky="nsew", pady=(12, 0))
            self.guide.pack_forget()
            self.weap_ready.pack(fill="x", pady=(16, 0),
                                 before=self._det_group)

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
            first = int(self.txt.index("end-1c").split(".")[0])
            self.txt.insert("end", f"{stamp}  ", "ts")
            sub = str(msg).startswith("  ")
            self.txt.insert("end", f"{msg}\n", "sub" if sub else "top")
            # coloured and filtered as it goes in
            tb = self.txt._textbox
            text = str(msg)
            end = int(self.txt.index("end-1c").split(".")[0])
            cls = self._log_class(text)
            if cls:
                tb.tag_add(cls, f"{first}.10", f"{end}.0")
            if self._log_query or self._log_mode != "All":
                if self._log_visible(text):
                    self._log_shown += 1
                    q = self._log_query
                    at = text.lower().find(q) if q else -1
                    if at >= 0:
                        tb.tag_add("hit", f"{first}.{at + 10}",
                                   f"{first}.{at + 10 + len(q)}")
                else:
                    tb.tag_add("hide", f"{first}.0", f"{end}.0")
            # A day-long run logs tens of thousands of lines, and a Text widget
            # that size gets slow. The full history is in log.txt anyway.
            lines = int(self.txt.index("end-1c").split(".")[0])
            if lines > 2000:
                self.txt.delete("1.0", f"{lines - 1500}.0")
                if self._log_query or self._log_mode != "All":
                    self._log_refilter()
            if self._page == "log" and self._log_follow:
                self.txt.see("end")
            self.txt.configure(state="disabled")
            self._paint_log_count()
        except Exception:
            pass
        for fn in (self._status_from_log, self._feed_from_log,
                   self._guide_from_log, self._events_from_log,
                   self._way_from_log):
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
        elif self.calibrating:
            # Start is ignored during setup (_run_clicked, _hotkey)
            b.configure(text="Setting up", glyph="…",
                        fg_color=CARD_HI, hover_color=CARD_HI, text_color=MUTED,
                        glyph_color=ACCENT, border_color=LINE, key_fg=PANEL,
                        key_border=KEYCAP_LINE, key_text=MUTED)
        elif self._ready():
            b.configure(text="Start", glyph="▶", fg_color=ACCENT,
                        hover_color=ACCENT_SOFT, text_color=INK,
                        glyph_color=INK, border_color=ACCENT,
                        key_fg="#4ab8ec", key_border="#2b93d1", key_text=INK)
        else:
            b.configure(text="Set up first", glyph="!",
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
            cal_text = (f"Grenade Launcher + {self._saved_randoms()} Random"
                        + (f" · set up {when}" if when else ""))
            for tile, title, sub in (
                    (self.weap_tile, self.lbl_weap, self.lbl_cal),
                    (self._wp_tile, self._wp_title, self._wp_cal)):
                title.configure(text="Weapons ready")
                sub.configure(text=cal_text)
                tile.configure(text="✓", fg_color=GREEN_DIM,
                               text_color=GREEN)
        self._paint_run()
        self._paint_guide()
        self._refresh_layout()
        if not self.running and self._ready() and not self._setup_active:
            self.set_state("Ready", "Open Rivals and press F8, or hit Start.",
                           MUTED)
        self._apply_topmost()        # the pin comes back once setup ends
        self._paint_pill()

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
        self._drop_pin()             # setup grabs the screen under us
        self._paint_run()
        self._show_page("weapons")
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
        self._drop_pin()
        self._paint_run()
        self._show_page("wayback")
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
            self._ui(self._setup_over)

    def _setup_over(self):
        """Tk thread, after a setup or Live test ends: pin and Start back."""
        if self._way["step"]:            # the teach flow failed part-way
            self._way.update(step=0, done=set())
            self._paint_way()
        self._apply_topmost()
        self._paint_run()
        self._paint_pill()

    def _show_ffa(self):
        if not hasattr(self, "lbl_ffa"):
            return
        taught = bool((self.cal or {}).get("ffa_path"))
        self.lbl_ffa.configure(
            text="Taught on this PC" if taught else "Built in",
            text_color=TEXT)
        self.lbl_ffa_home.configure(
            text="Taught on this PC" if taught else "Built in · works as is")

    # ---- Roblox closed or crashed. Worker thread only.
    def _roblox_closed_check(self, log):
        """If Roblox has been gone for ROBLOX_GONE_AFTER mid-run, reopen
        Rivals and walk back into Free For All. True if it reopened it.
        Alt-tabbing away is not "gone" - the window still exists then."""
        now = time.time()
        if roblox_hwnd():
            self._roblox_gone_since = 0.0
            self._roblox_windowless_since = 0.0
            return False
        if not self.sw_reconnect.get():
            return False
        if not getattr(self, "_roblox_gone_since", 0.0):
            self._roblox_gone_since = now
            return False
        if now - self._roblox_gone_since < ROBLOX_GONE_AFTER:
            return False
        if roblox_running():
            # Starting up shows the "Roblox" window within seconds. Running
            # with NO window for longer than that is stuck - a crash box, or
            # a client that closed but never exited (a common Roblox bug) -
            # and waiting on it would sit "Paused" all night.
            if not getattr(self, "_roblox_windowless_since", 0.0):
                self._roblox_windowless_since = self._roblox_gone_since
            if now - self._roblox_windowless_since < ROBLOX_STUCK_AFTER:
                self._roblox_gone_since = now
                return False
            log("Roblox is running with no window - restarting it")
        recent = [t for t in getattr(self, "_reopens", []) if now - t < 3600]
        if len(recent) >= ROBLOX_REOPENS_PER_HOUR:
            if not getattr(self, "_reopen_gave_up", False):
                self._reopen_gave_up = True
                log("Roblox keeps closing - not reopening it again this hour")
            return False
        self._reopens = recent + [now]
        self._reopen_gave_up = False
        self._roblox_gone_since = 0.0
        self._roblox_windowless_since = 0.0
        log("Roblox closed - reopening Rivals")
        relaunch_roblox(self.place_id(), lambda m: log("  " + m))
        for _ in range(int(RELAUNCH_WAIT * 2)):
            if not self.running:
                return True
            time.sleep(0.5)
        if self.running and focused():
            self.rejoin_ffa(log)
        return True

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
            # deferred: when Start stops the test, running is set by then
            # and the pin is not re-raised over Rivals for an instant
            self.after_idle(self._apply_topmost)
            return
        if not self.cal:
            self.log("calibrate first")
            return
        if self.running:
            self.log("stop the macro first")
            return
        self.watching = True
        self._drop_pin()             # the test reads the screen under us
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
        self._ui(self._apply_topmost)

    def toggle_run(self):
        if getattr(self, "_updating", False):
            self.log("updating - GlassMacro will reopen in a moment")
            return
        if self.running:
            self.running = False
            self._apply_topmost()    # _tick misses a run shorter than 1 s
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
        if getattr(self, "_live_run", False):
            # restarted before _tick saw the last run end: close it while
            # its loadouts/rejoins are still counted
            try:
                self._run_ended(time.time())
            except Exception:
                pass
        self._fs = self._fresh_fs()
        self._warn_display()
        self.running = True
        self._drop_pin()             # the pin would catch the macro's clicks
        keep_awake(True)
        self._paint_run()
        self.session_start = time.time()
        self.n_picks = self.n_joins = 0
        self.n_recov = 0
        self.val_picks.configure(text="0")
        self.val_joins.configure(text="0")
        self.val_recov.configure(text="0")
        self._status("RUNNING", GREEN)
        self.log("started - F8 stops it")
        self._worker = threading.Thread(target=self._run_worker, daemon=True)
        self._worker.start()
        try:
            self._run_started()
        except Exception:
            pass                      # stats and alerts never stop a run

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
                if self._roblox_closed_check(wlog):
                    picked_at = 0.0
                    fails = 0
                    warned = False
                    last_known = time.time()
                    continue
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
        was_running = self.running
        self.running = self.watching = False
        if getattr(self, "_live_run", False):
            # closing mid-run is a clean end: final stats and the alert
            try:
                now = time.time()
                self._run_ended(now, "closed" if was_running else "stop",
                                ran=now - self.session_start
                                if self.session_start else None)
            except Exception:
                pass
            try:
                if self._sender:
                    self._sender.flush(2)
            except Exception:
                pass
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
    if len(sys.argv) >= 5 and sys.argv[1] == "--finish-update":
        finish_update(sys.argv[2], sys.argv[3], sys.argv[4])
    elif claim_single_instance():
        rotate_log()
        GlassMacro().mainloop()
