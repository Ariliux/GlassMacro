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
import shutil
import sys
import subprocess
import threading
import time

import cv2
import numpy as np
from PIL import ImageGrab

import customtkinter as ctk
import keyboard

APP_NAME, APP_VER = "GlassMacro", "1.2"

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
class GlassMacro(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        self.title(APP_NAME)
        self.geometry("540x800")
        self.minsize(500, 660)
        self.configure(fg_color=BG)
        self._set_icon()
        self.after(50, self._style_titlebar)

        # session stats shown on the status card
        self.session_start = None
        self.n_picks = 0
        self.n_joins = 0
        self._fs = self._fresh_fs()

        self.cal, self.tile = load_cal()
        self.running = False
        self.watching = False
        self.calibrating = False
        self._q = queue.Queue()

        self._build()
        self._drain()
        keyboard.add_hotkey("f8", self._hotkey)
        self.log(f"--- {APP_NAME} v{APP_VER} opened ---")
        sw, sh = screen_size()
        if (sw, sh) != SUPPORTED_SCREEN:
            self.log(f"heads up: this screen is {sw}x{sh} - GlassMacro is made "
                     f"for 1920x1080, so clicks may miss")
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

    def _build(self):
        # ---- header ----
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=24, pady=(20, 14))
        mark = ctk.CTkFrame(head, fg_color="transparent")
        mark.pack(side="left")
        ctk.CTkLabel(mark, text="Glass", text_color=ACCENT,
                     font=ctk.CTkFont(size=24, weight="bold")).pack(side="left")
        ctk.CTkLabel(mark, text="Macro", text_color=TEXT,
                     font=ctk.CTkFont(size=24, weight="bold")).pack(side="left")
        ctk.CTkLabel(mark, text=f"v{APP_VER}", text_color=MUTED,
                     font=ctk.CTkFont(size=11)
                     ).pack(side="left", padx=(8, 0), pady=(9, 0))

        chip = ctk.CTkFrame(head, fg_color=CARD, corner_radius=14,
                            border_width=1, border_color=LINE)
        chip.pack(side="right")
        self.dot = ctk.CTkLabel(chip, text="\u25cf", text_color=MUTED,
                                font=ctk.CTkFont(size=12))
        self.dot.pack(side="left", padx=(12, 6), pady=6)
        self.pill = ctk.CTkLabel(chip, text="IDLE", text_color=SUBTLE,
                                 font=ctk.CTkFont(size=11, weight="bold"))
        self.pill.pack(side="left", padx=(0, 14), pady=6)

        # ---- status: the one thing the main screen is for ----
        card = ctk.CTkFrame(self, fg_color=CARD, corner_radius=18,
                            border_width=1, border_color=LINE)
        card.pack(fill="x", padx=24)
        self.lbl_state = ctk.CTkLabel(card, text="Ready", text_color=TEXT,
                                      anchor="w",
                                      font=ctk.CTkFont(size=22, weight="bold"))
        self.lbl_state.pack(fill="x", padx=22, pady=(20, 2))
        self.lbl_detail = ctk.CTkLabel(card, text="", text_color=SUBTLE,
                                       anchor="w", justify="left",
                                       wraplength=440,
                                       font=ctk.CTkFont(size=12))
        self.lbl_detail.pack(fill="x", padx=22)

        stats = ctk.CTkFrame(card, fg_color="transparent")
        stats.pack(fill="x", padx=16, pady=(18, 0))
        for c in range(3):
            stats.grid_columnconfigure(c, weight=1, uniform="stat")
        self.val_time = self._stat(stats, 0, "0:00:00", "Running")
        self.val_picks = self._stat(stats, 1, "0", "Loadouts")
        self.val_joins = self._stat(stats, 2, "0", "Rejoins")

        self.btn_run = ctk.CTkButton(card, text="Start    F8", height=52,
                                     corner_radius=14, fg_color=ACCENT,
                                     hover_color=ACCENT_SOFT, text_color=INK,
                                     font=ctk.CTkFont(size=15, weight="bold"),
                                     command=self.toggle_run)
        self.btn_run.pack(fill="x", padx=22, pady=(18, 22))

        # ---- setup: one row, not a tutorial ----
        setup = ctk.CTkFrame(self, fg_color=CARD, corner_radius=14,
                             border_width=1, border_color=LINE)
        setup.pack(fill="x", padx=24, pady=(12, 0))
        left = ctk.CTkFrame(setup, fg_color="transparent")
        left.pack(side="left", fill="x", expand=True, padx=(18, 8), pady=12)
        ctk.CTkLabel(left, text="Weapons", text_color=TEXT, anchor="w",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(fill="x")
        self.lbl_cal = ctk.CTkLabel(left, text="", text_color=SUBTLE,
                                    anchor="w", justify="left", wraplength=320,
                                    font=ctk.CTkFont(size=11))
        self.lbl_cal.pack(fill="x")
        self.btn_cal = ctk.CTkButton(setup, text="Set up", width=116, height=34,
                                     corner_radius=10, fg_color="transparent",
                                     hover_color=CARD_HI, border_width=1,
                                     border_color=LINE, text_color=TEXT,
                                     command=self.calibrate)
        self.btn_cal.pack(side="right", padx=14)

        # ---- activity and settings ----
        tabs = self.tabs = ctk.CTkTabview(
            self, fg_color=PANEL, corner_radius=14, border_width=1,
            border_color=LINE, segmented_button_fg_color=BG,
            segmented_button_unselected_color=BG,
            segmented_button_unselected_hover_color=CARD_HI,
            segmented_button_selected_color=ACCENT_DEEP,
            segmented_button_selected_hover_color=ACCENT,
            text_color=TEXT, height=230)
        tabs.pack(fill="both", expand=True, padx=24, pady=(12, 20))
        t_log = tabs.add("Activity")
        t_set = tabs.add("Settings")

        self.txt = ctk.CTkTextbox(t_log, fg_color=BG, text_color="#c3cfdd",
                                  border_width=0, wrap="word",
                                  font=ctk.CTkFont(family="Consolas", size=11))
        self.txt.pack(fill="both", expand=True, padx=4, pady=4)
        self.txt.tag_config("ts", foreground=MUTED)
        self.txt.tag_config("top", foreground="#d5e0ec")
        self.txt.tag_config("sub", foreground=SUBTLE)
        self.txt.configure(state="disabled")

        sp = ctk.CTkScrollableFrame(t_set, fg_color="transparent")
        sp.pack(fill="both", expand=True)

        self.sw_fullscreen = self._switch(
            sp, "Keep Roblox fullscreen",
            "Presses F11 if Roblox is in a window - every click is lined up "
            "for fullscreen. Gives up after two tries rather than keep "
            "flipping it.")
        self.sw_reconnect = self._switch(
            sp, "Reconnect automatically",
            "Handles Disconnected and Connection Failed. If Retry doesn't get "
            "you back in, it restarts Roblox and rejoins.")
        self.sw_shots = self._switch(
            sp, "Save pictures of what it clicks",
            "Every loadout pick, plus any disconnect dialog or Join prompt it "
            "clicks - newest 40 of each, in the data folder. Shows exactly "
            "what it saw if something ever goes wrong.")

        self._section(sp, "Detection")
        r = ctk.CTkFrame(sp, fg_color="transparent")
        r.pack(fill="x", pady=(2, 0))
        ctk.CTkLabel(r, text="Threshold", text_color=SUBTLE,
                     font=ctk.CTkFont(size=12)).pack(side="left", padx=(0, 8))
        self.e_thresh = ctk.CTkEntry(r, width=64, height=30, border_color=LINE,
                                     fg_color=CARD_HI)
        self.e_thresh.pack(side="left")
        self.e_thresh.insert(0, str(self._saved_threshold()))
        self.btn_watch = ctk.CTkButton(r, text="Test", width=84, height=30,
                                       corner_radius=10, fg_color="transparent",
                                       hover_color=CARD_HI, border_width=1,
                                       border_color=LINE, text_color=TEXT,
                                       command=self.toggle_watch)
        self.btn_watch.pack(side="right")
        self._note(sp, "Test shows the match score live without clicking "
                       "anything. With the weapon picker open it should read "
                       "near 1.00; the threshold sits between that and the "
                       "score with it closed.")
        self.lbl_score = ctk.CTkLabel(sp, text="", text_color=MUTED,
                                      justify="left", anchor="w",
                                      font=ctk.CTkFont(size=11,
                                                       family="Consolas"))
        self.lbl_score.pack(fill="x", pady=(4, 0))

        self._section(sp, "Way back into Free For All")
        r2 = ctk.CTkFrame(sp, fg_color="transparent")
        r2.pack(fill="x", pady=(2, 0))
        self.lbl_ffa = ctk.CTkLabel(r2, text="", text_color=SUBTLE, anchor="w",
                                    font=ctk.CTkFont(size=12))
        self.lbl_ffa.pack(side="left", fill="x", expand=True)
        self.btn_ffa = ctk.CTkButton(r2, text="Re-teach", width=96, height=30,
                                     corner_radius=10, fg_color="transparent",
                                     hover_color=CARD_HI, border_width=1,
                                     border_color=LINE, text_color=TEXT,
                                     command=self.teach_ffa)
        self.btn_ffa.pack(side="right")
        self._note(sp, "Built in, so there is nothing to do here unless the "
                       "lobby menu ever changes.")

        self._section(sp, "Files")
        ctk.CTkButton(sp, text="Open data folder", height=30, corner_radius=10,
                      fg_color="transparent", hover_color=CARD_HI,
                      border_width=1, border_color=LINE, text_color=TEXT,
                      command=self.open_data).pack(anchor="w", pady=(2, 0))
        self._note(sp, "Calibration, the log and any screenshots live here.")

        self._show_cal()
        self._show_ffa()
        self._tick()

    # ---- little building blocks, so the layout above stays readable ----
    def _stat(self, parent, col, value, label):
        f = ctk.CTkFrame(parent, fg_color=CARD_HI, corner_radius=12)
        f.grid(row=0, column=col, sticky="nsew", padx=6)
        v = ctk.CTkLabel(f, text=value, text_color=TEXT,
                         font=ctk.CTkFont(size=18, weight="bold"))
        v.pack(pady=(12, 0))
        ctk.CTkLabel(f, text=label, text_color=MUTED,
                     font=ctk.CTkFont(size=11)).pack(pady=(0, 12))
        return v

    def _switch(self, parent, title, note):
        sw = ctk.CTkSwitch(parent, text=title, text_color=TEXT,
                           progress_color=ACCENT, button_color=TEXT,
                           button_hover_color=ACCENT_SOFT,
                           font=ctk.CTkFont(size=13))
        sw.pack(anchor="w", pady=(12, 0))
        sw.select()
        self._note(parent, note, indent=50)
        return sw

    def _note(self, parent, text, indent=0):
        ctk.CTkLabel(parent, text=text, text_color=MUTED, justify="left",
                     anchor="w", wraplength=400 - indent,
                     font=ctk.CTkFont(size=11)
                     ).pack(fill="x", padx=(indent, 0), pady=(2, 0))

    def _section(self, parent, title):
        ctk.CTkLabel(parent, text=title.upper(), text_color=ACCENT_SOFT,
                     anchor="w", font=ctk.CTkFont(size=11, weight="bold")
                     ).pack(fill="x", pady=(18, 4))

    def open_data(self):
        try:
            os.startfile(DATA_DIR)
        except Exception as exc:
            self.log(f"could not open the data folder: {exc}")

    # ---- the status card ----
    def set_state(self, title, detail="", colour=None):
        self.lbl_state.configure(text=title, text_color=colour or TEXT)
        self.lbl_detail.configure(text=detail)

    def _tick(self):
        """Keep the Running counter live. Main thread only."""
        if self.session_start:
            el = int(time.time() - self.session_start)
            self.val_time.configure(
                text=f"{el // 3600}:{el % 3600 // 60:02d}:{el % 60:02d}")
        self.after(1000, self._tick)

    # A friendly reading of the log. Every path through the macro already logs
    # what it is doing, so the status card follows the log instead of each
    # worker being re-plumbed to drive it - the tested behaviour stays as is.
    # First match wins, so the more specific phrases come first.
    STATUS_RULES = (
        ("just playing", "In a match",
         "Jumping and firing until the next loadout.", "GREEN", None),
        ("choosing loadout", "Picking loadout",
         "Grenade Launcher first, then Random.", "ACCENT", None),
        ("done, back to jumping", "In a match",
         "Jumping and firing until the next loadout.", "GREEN", "picks"),
        ("in the hub - joining", "Joining Free For All",
         "Walking the lobby menu into a match.", "ACCENT", None),
        ("match started - stopping the join", "In a match",
         "A round began mid-join, so it stopped clicking the menu.",
         "GREEN", None),
        ("  in a match", "In a match",
         "Joined. Waiting for the weapon picker.", "GREEN", "joins"),
        ("PAUSED", "Paused",
         "Click into Rivals to carry on - it only acts while the game is in "
         "front.", "AMBER", None),
        ("back in front", "Resuming", "", "ACCENT", None),
        ("connection dialog", "Reconnecting",
         "Clicking through the connection dialog.", "AMBER", None),
        ("restarting Roblox", "Restarting Roblox",
         "Retry never reconnects, so this is a fresh start.", "AMBER", None),
        ("Join prompt showing", "Between rounds",
         "Waiting for the next round to start on its own.", "SUBTLE", None),
        ("still spectating", "Joining the match",
         "The Join prompt stayed up, so it clicked it.", "ACCENT", None),
        ("quiet for", "In a match",
         "Nothing's needed it for a while - still alive, or a quiet server. "
         "Both mean more time in-game.", "GREEN", None),
        ("could not get into a match", "Couldn't join",
         "Tried three times. It keeps watching and will try again.",
         "RED", None),
        ("started - F8 stops it", "Starting", "Checking where you are.",
         "ACCENT", None),
        ("hover RANDOM", "Setting up  -  1 of 3",
         "Open the weapon picker in Rivals. Hover the Random tile and press "
         "F8. Don't click.", "ACCENT", None),
        ("hover GRENADE LAUNCHER", "Setting up  -  2 of 3",
         "Hover the Grenade Launcher's name and press F8.", "ACCENT", None),
        ("hover the FIRST loadout", "Setting up  -  3 of 3",
         "Hover the first loadout slot along the top and press F8.",
         "ACCENT", None),
        ("calibration saved", "Weapons set up",
         "Press Start, or F8 inside Rivals.", "GREEN", None),
        ("calibration cancelled", "Setup cancelled", "", "SUBTLE", None),
        ("from the LOBBY", "Teaching the way back  -  1 of 3",
         "In the lobby: hover Play, press F8, then click it yourself.",
         "ACCENT", None),
        ("now hover FREE FOR ALL", "Teaching the way back  -  2 of 3",
         "Scroll to Free For All, hover it, press F8, then click it.",
         "ACCENT", None),
        ("now hover PLAY on", "Teaching the way back  -  3 of 3",
         "Hover Play on the Free For All screen and press F8.",
         "ACCENT", None),
        ("saved the way back", "Way back saved",
         "Press Start, or F8 inside Rivals.", "GREEN", None),
        ("pressing F11 for fullscreen", "Going fullscreen",
         "Roblox was in a window, so it pressed F11.", "ACCENT", None),
        ("  fullscreen now", "Fullscreen", "Carrying on.", "GREEN", None),
        ("couldn't make Roblox fullscreen", "Roblox isn't fullscreen",
         "Press F11 in Roblox - clicks can miss in a window.", "AMBER", None),
        ("heads up: this screen is", "Made for 1080p screens",
         "This screen isn't 1920x1080, so clicks may land in the wrong "
         "place.", "AMBER", None),
        ("timed out - nothing saved", "Nothing saved",
         "Took too long between presses. Try again whenever.", "SUBTLE",
         None),
        ("calibrate first", "Set up your weapons first",
         "Press Set up below - it only takes three hovers.", "AMBER", None),
        ("recalibrate - it now needs", "Redo the weapon setup",
         "It needs the first loadout slot too now.", "RED", None),
        ("calibration looks like the same", "Redo the weapon setup",
         "Random first, then Grenade Launcher, then the loadout slot.",
         "RED", None),
        ("calibration failed", "Setup failed",
         "Nothing was changed. The Activity tab says why.", "RED", None),
    )

    def _status_from_log(self, msg):
        text = str(msg)
        if text == "stopped":
            self.set_state("Stopped", "Press Start, or F8 inside Rivals.",
                           SUBTLE)
            return
        if text == "cancelled":
            self.set_state("Cancelled", "Nothing was changed.", SUBTLE)
            return
        for needle, title, detail, colour, counter in self.STATUS_RULES:
            if needle in text:
                self.set_state(title, detail, globals()[colour])
                if counter == "picks":
                    self.n_picks += 1
                    self.val_picks.configure(text=str(self.n_picks))
                elif counter == "joins":
                    self.n_joins += 1
                    self.val_joins.configure(text=str(self.n_joins))
                return

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
            self.txt.see("end")
            self.txt.configure(state="disabled")
        except Exception:
            pass
        try:
            self._status_from_log(msg)
        except Exception:
            pass                      # the card is cosmetic; never let it break

    def _status(self, text, colour):
        self.pill.configure(text=text, text_color=colour)
        self.dot.configure(text_color=colour)

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
        if self.running:
            self.btn_run.configure(text="Stop    F8", fg_color="#2a1418",
                                   hover_color="#3a1a1f", text_color="#fca5a5",
                                   border_width=1, border_color="#7f2d2d")
        elif self._ready():
            self.btn_run.configure(text="Start    F8", fg_color=ACCENT,
                                   hover_color=ACCENT_SOFT, text_color=INK,
                                   border_width=0)
        else:
            # nothing to start yet - so Set up, not Start, is the loud button
            self.btn_run.configure(text="Start    F8", fg_color=CARD_HI,
                                   hover_color=LINE, text_color=MUTED,
                                   border_width=0)

    def _paint_cal(self, text, loud):
        if loud:
            self.btn_cal.configure(text=text, fg_color=ACCENT,
                                   hover_color=ACCENT_SOFT, text_color=INK,
                                   border_width=0)
        else:
            self.btn_cal.configure(text=text, fg_color="transparent",
                                   hover_color=CARD_HI, text_color=TEXT,
                                   border_width=1)

    def _show_cal(self):
        self._paint_run()
        if not self.cal:
            self.lbl_cal.configure(
                text="Not set up yet - three quick hovers, one time only.",
                text_color=AMBER)
            self._paint_cal("Set up", loud=True)
            if not self.running:
                self.set_state("Set up your weapons",
                               "Open the weapon picker in Rivals, then press "
                               "Set up below.", AMBER)
            return
        if not self._ready():
            self.lbl_cal.configure(
                text="Needs redoing - hover Random, then Grenade Launcher, "
                     "then the first loadout slot.",
                text_color=RED)
            self._paint_cal("Redo", loud=True)
            if not self.running:
                self.set_state("Redo the weapon setup",
                               "The saved one can't be used as it is.", RED)
            return
        self.lbl_cal.configure(text="Calibrated - Grenade Launcher found.",
                               text_color=GREEN)
        self._paint_cal("Recalibrate", loud=False)
        if not self.running:
            self.set_state("Ready", "Press Start, or F8 inside Rivals.")

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
            text_color=GREEN)

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
            self.btn_watch.configure(text="Test")
            self._status("IDLE", SUBTLE)
            return
        if not self.cal:
            self.log("calibrate first")
            return
        if self.running:
            self.log("stop the macro first")
            return
        self.watching = True
        self.btn_watch.configure(text="Stop")
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
        self._ui(lambda: self.btn_watch.configure(text="Test"))

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
        sw, sh = screen_size()
        if (sw, sh) != SUPPORTED_SCREEN:
            self.log(f"heads up: this screen is {sw}x{sh} - GlassMacro is made "
                     f"for 1920x1080, so clicks may miss")
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
