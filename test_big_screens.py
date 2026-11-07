"""Bigger (and smaller) 16:9 screens: everything must work the same as on
1920x1080. Offline, nothing is clicked or moved for real.

    python test_big_screens.py

Uses the real screenshots in test_screens/ resized to 2560x1440, 3840x2160
and 1366x768, a fake cursor for the clicks, and a throwaway setup folder.
Sandboxed: LOCALAPPDATA points at a temp folder before import.
"""
import glob
import json
import os
import sys
import tempfile

os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="glass_big_")
os.environ["GLASSMACRO_NO_SEND"] = "1"                         # never post to Discord
os.environ.pop("GLASSMACRO_TEST_SCALING", None)
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cv2                                                   # noqa: E402
import numpy as np                                           # noqa: E402
import glassmacro as G                                       # noqa: E402
from PIL import Image                                        # noqa: E402

assert G.DATA_DIR.startswith(os.environ["LOCALAPPDATA"]), G.DATA_DIR
fails = 0


def check(ok, what):
    global fails
    fails += not ok
    print(("PASS  " if ok else "FAIL  ") + what)


SIZES = {"1440p": 1440, "4K": 2160, "768p": 768}
DETECTORS = {"join": G.find_join_button, "hub": G.in_lobby,
             "reconnect": G.find_reconnect}

# ---- a fake screen and a fake cursor ----------------------------------------
screen = {"size": (1920, 1080), "img": None}
cursor = [0, 0]
sent = []


class FakeUser32:
    """The real user32 for everything except the screen size and cursor."""

    def __init__(self, real):
        self.real = real

    def __getattr__(self, name):
        return getattr(self.real, name)

    def GetSystemMetrics(self, i):
        return screen["size"][i] if i in (0, 1) else self.real.GetSystemMetrics(i)

    def GetCursorPos(self, ref):
        ref._obj.x, ref._obj.y = cursor
        return 1


def fake_move(dx, dy):
    # Windows pointer acceleration: fast moves travel further than asked
    f = 1.4 if max(abs(dx), abs(dy)) > 20 else 1.0
    w, h = screen["size"]
    cursor[0] = max(0, min(w - 1, cursor[0] + int(round(dx * f))))
    cursor[1] = max(0, min(h - 1, cursor[1] + int(round(dy * f))))


G._user32 = FakeUser32(G._user32)
G._move_rel = fake_move
G._send = lambda inp: sent.append(inp)          # button presses go nowhere
G.ImageGrab.grab = lambda *a, **k: screen["img"]


def show(img, size=None):
    screen["img"] = img
    screen["size"] = size or img.size


def scaled(img, h):
    f = h / 1080
    return img.resize((int(round(img.width * f)), int(round(img.height * f))),
                      Image.LANCZOS)


# ---- 1. every detector, every screenshot, every size -------------------------
files = [f for f in sorted(glob.glob(os.path.join(HERE, "test_screens", "*.*")))
         if not f.endswith(".txt")]
if not files:
    print("no screenshots in test_screens/ - see test_detectors.py")
for scaling in ("100", "150"):
    os.environ["GLASSMACRO_TEST_SCALING"] = scaling
    for path in files:
        name = os.path.basename(path)
        img = Image.open(path).convert("RGB")
        show(img, (1920, 1080))
        base = {k: fn() for k, fn in DETECTORS.items()}
        for label, h in SIZES.items():
            show(scaled(img, h), (int(round(1920 * h / 1080)), h))
            got = {k: fn() for k, fn in DETECTORS.items()}
            same = all((base[k] is None) == (got[k] is None) for k in DETECTORS)
            near = all(base[k] is None or (abs(base[k][0] - got[k][0]) <= 4
                                            and abs(base[k][1] - got[k][1]) <= 4)
                       for k in DETECTORS)
            fired = [k for k in DETECTORS if got[k] is not None] or "-"
            check(same and near, f"{scaling}% {label:5} {name:34} fired={fired}")
os.environ.pop("GLASSMACRO_TEST_SCALING")

# ---- 2. Roblox's own dialog drawn in real pixels on a big screen -------------
# On a 4K screen Roblox may NOT grow its Disconnected box with the game: then
# it's the 1080p size in the middle of a 3840x2160 screen. Cut the box out of
# each real screenshot and put it there, at 1:1 and at 150% scaling.
recon = [f for f in files if os.path.basename(f).startswith("reconnect")]
others = [f for f in files if not os.path.basename(f).startswith("reconnect")]


def dialog_on_4k(path, grow=1.0, dark=14):
    img = Image.open(path).convert("RGB")
    show(img, (1920, 1080))
    bx, by = G.find_reconnect()                 # where the button is on 1080p
    f = img.height / 1080                       # a cropped shot was stretched to 1080
    bx, by = bx * f, by * f
    box = img.crop((int(bx) - 420, int(by) - 260, int(bx) + 260, int(by) + 70))
    if grow != 1.0:
        box = box.resize((int(box.width * grow), int(box.height * grow)),
                         Image.LANCZOS)
    big = Image.new("RGB", (3840, 2160), (dark,) * 3)
    # the button lands 92 right and 86 below the centre (times grow), as on 1080p
    tx, ty = 1920 + 92 * grow, 1080 + 86 * grow
    big.paste(box, (int(tx - 420 * grow), int(ty - 260 * grow)))
    return big, (tx / 2, ty / 2)


for path in recon:
    name = os.path.basename(path)
    for grow, scaling in ((1.0, "100"), (1.5, "150")):
        os.environ["GLASSMACRO_TEST_SCALING"] = scaling
        big, want = dialog_on_4k(path, grow)
        show(big, (3840, 2160))
        got = G.find_reconnect()
        check(got is not None and abs(got[0] - want[0]) <= 6
              and abs(got[1] - want[1]) <= 6,
              f"4K, Roblox box at real size x{grow} ({scaling}%): {name} -> {got}")
os.environ.pop("GLASSMACRO_TEST_SCALING", None)

# ...and nothing else, drawn the same way, may look like it
for scaling in ("100", "150"):
    os.environ["GLASSMACRO_TEST_SCALING"] = scaling
    hits = []
    for path in others:
        img = Image.open(path).convert("RGB")
        big = Image.new("RGB", (3840, 2160), (14, 14, 14))
        big.paste(img, ((3840 - img.width) // 2, (2160 - img.height) // 2))
        show(big, (3840, 2160))
        if G.find_reconnect() is not None:
            hits.append(os.path.basename(path))
    check(not hits, f"{scaling}%: no other screen at real size on 4K looks like "
                    f"the box ({hits or 'none'})")
os.environ.pop("GLASSMACRO_TEST_SCALING", None)

# ---- 3. clicks land on the real screen ---------------------------------------
for label, h in SIZES.items():
    w = int(round(1920 * h / 1080))
    screen["size"] = (w, h)
    s = h / 1080
    for spot in ((960, 886), (1122, 753), (40, 30), (1880, 1050)):
        cursor[:] = [5, 5]
        sent.clear()
        ok = G.click(*spot, settle=0)
        real = (int(round(spot[0] * s)), int(round(spot[1] * s)))
        check(ok and abs(cursor[0] - real[0]) <= 1 and abs(cursor[1] - real[1]) <= 1
              and len(sent) == 2,
              f"{label}: click at {spot} lands on {tuple(cursor)} (real {real})")
        check(abs(G.cursor_pos()[0] - spot[0]) <= 1
              and abs(G.cursor_pos()[1] - spot[1]) <= 1,
              f"{label}: ...and the cursor reads back as {G.cursor_pos()}")
    path = G.default_ffa_path()
    check(path == [[960, 886], [959, 764], [1122, 753]],
          f"{label}: the built-in FFA path is the same 1080p spots ({path})")
screen["size"] = (1920, 1080)
cursor[:] = [100, 100]
G.move_to(700, 400)
check(tuple(cursor) == (700, 400), "1920x1080: no scaling at all")

# ---- 4. a setup saved on a bigger screen before this version -----------------
os.makedirs(os.path.dirname(G.CAL_PATH), exist_ok=True)
rng = np.random.default_rng(7)
tile = rng.integers(0, 255, (147, 147), dtype=np.uint8)
cv2.imwrite(G.TEMPLATE_PATH, tile)
cv2.imwrite(G.GL_TEMPLATE_PATH, tile)
old = {"screen": [2560, 1440], "anchor_offset": [73, 73], "gl_offset": [400, 200],
       "tab_offset": [-120, -480], "ffa_path": [[1280, 1181], [1279, 1019],
                                                [1496, 1004]],
       "gl_nudge": [4, -8], "threshold": 0.82, "made": "2026-11-01 10:00:00"}
with open(G.CAL_PATH, "w", encoding="utf-8") as fh:
    json.dump(old, fh)
screen["size"] = (2560, 1440)
cal, t = G.load_cal()
check(cal["screen"] == [1920, 1080] and cal["anchor_offset"] == [55, 55]
      and cal["gl_offset"] == [300, 150] and cal["tab_offset"] == [-90, -360]
      and cal["gl_nudge"] == [3, -6],
      f"a 1440p setup is moved to 1080p spots ({cal['gl_offset']})")
check(cal["ffa_path"] == [[960, 886], [959, 764], [1122, 753]],
      f"...the taught FFA path too ({cal['ffa_path']})")
check(t.shape == (110, 110) and cal["_gl_tile"].shape == (110, 110),
      f"...and both pictures are resized ({t.shape})")
check(os.path.exists(G.CAL_PATH + ".bak-native")
      and os.path.exists(G.TEMPLATE_PATH + ".bak-native"),
      "...with the old files kept as .bak-native")
cal2, t2 = G.load_cal()
check(cal2["gl_offset"] == [300, 150] and t2.shape == (110, 110),
      "loading it again changes nothing")
with open(G.CAL_PATH, "w", encoding="utf-8") as fh:
    json.dump(dict(old, screen=[1920, 1080]), fh)
cv2.imwrite(G.TEMPLATE_PATH, tile)
cal3, t3 = G.load_cal()
check(cal3["gl_offset"] == [400, 200] and t3.shape == (147, 147),
      "a 1080p setup is never touched")

# ---- 5. the weapon picture still matches after the resize --------------------
picks = [f for f in others if os.path.basename(f)[0].isdigit()]
if picks:
    img = Image.open(picks[0]).convert("RGB")
    show(img, (1920, 1080))
    gray = G.grab_gray()
    cut = gray[400:510, 700:810].copy()                    # any 110 px patch
    for label, h in SIZES.items():
        show(scaled(img, h))
        score, pos = G.find_random({"anchor_offset": [55, 55]}, cut, 0.82,
                                   shot=G.grab_gray())
        check(pos is not None and abs(pos[0] - 755) <= 3 and abs(pos[1] - 455) <= 3,
              f"{label}: a 1080p setup picture still matches ({score:.2f} at {pos})")

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
