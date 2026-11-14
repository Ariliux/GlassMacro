"""Other screen shapes and Windows scalings: 125% and 150%, 16:10 and 21:9.

    python test_layouts.py

Offline, nothing is clicked or moved for real. Uses real Rivals pictures in
test_screens/layouts/ (local only - they show other players' names). They
were taken from a Roblox WINDOW of each size: at Windows scaling S Roblox
lays its menus out for a screen of (size / S), and a window of that size at
100% lays out the same way. So "1280x720" below is what a 1920x1080 screen
shows at 150%, "1536x864" at 125%, "1280x800" a 1920x1200 screen at 150%,
"1720x720" a 3440x1440 ultrawide at 200%. Each is blown up to 1080 tall
first, which is the picture GlassMacro works in on any screen.

Without the pictures only the rules are checked.
"""
import glob
import os
import sys
import tempfile

os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="glass_lay_")
os.environ["GLASSMACRO_NO_SEND"] = "1"                         # never post to Discord
os.environ.pop("GLASSMACRO_TEST_SCALING", None)
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cv2                                                   # noqa: E402
import numpy as np                                           # noqa: E402
import glassmacro as G                                       # noqa: E402

assert G.DATA_DIR.startswith(os.environ["LOCALAPPDATA"]), G.DATA_DIR
LAY = os.path.join(HERE, "test_screens", "layouts")
fails = 0


def check(ok, what):
    global fails
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        fails += 1


def virt(path):
    img = cv2.imread(path)
    h, w = img.shape[:2]
    if h != G.VIRT_H:
        img = cv2.resize(img, (int(round(w * G.VIRT_H / h)), G.VIRT_H),
                         interpolation=cv2.INTER_CUBIC)
    return img


def show(img):
    """Make every detector see this picture as the screen."""
    G.grab_bgr = lambda: img
    G.grab_gray = lambda: cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def size_of(path):
    return os.path.basename(path).rsplit("_", 2 if "_full" in path else 1)[1 if "_full" not in path else 1]


# ---- 1. the rules -------------------------------------------------------------------------
for size in ((1920, 1080), (2560, 1440), (3840, 2160), (1366, 768), (1920, 1200), (2560, 1600),
             (1280, 800), (2560, 1080), (3440, 1440)):
    check(G.screen_supported(*size), f"{size[0]}x{size[1]} is a supported screen")
for size in ((1280, 1024), (2256, 1504), (1024, 768), (5120, 1440), (1280, 600)):
    check(not G.screen_supported(*size), f"{size[0]}x{size[1]} still gets the warning")
for sh, pct, ok in ((1080, 100, True), (1080, 125, True), (1080, 150, True), (1080, 175, False),
                    (1440, 150, True), (1440, 200, True), (1440, 225, False), (2160, 150, True),
                    (2160, 300, True), (2160, 350, False), (768, 100, True), (768, 125, False),
                    (1200, 150, True), (1200, 175, False)):
    check(G.scaling_supported(sh, pct) is ok, f"{sh} tall at {pct}%: {'fine' if ok else 'too much scaling'}")
check([G.best_scaling(h) for h in (768, 1080, 1200, 1440, 2160)] == [100, 150, 150, 200, 300],
      "the most scaling each screen can have: 768->100, 1080->150, 1200->150, 1440->200, 2160->300")
check(G.layout_height(1080, 150) == 720 and G.layout_height(2160, 150) == 1440, "layout height = height / scaling")
check(abs(G.ui_growth(1080, 100) - 1.0) < 1e-9 and abs(G.ui_growth(1080, 125) - 1.045) < 0.003
      and abs(G.ui_growth(1080, 150) - 1.09) < 0.003 and G.ui_growth(2160, 100) < 1.0
      and abs(G.ui_growth(2160, 200) - 1.0) < 1e-9,
      "tile growth matches what was measured: 1.00 / 1.045 / 1.09, and 4K at 200% = plain 1080p")
check(G.layout_tag(1920, 1080, 100) == [1920, 1080] and G.layout_tag(1920, 1080, 150) == [1920, 720]
      and G.layout_tag(3440, 1440, 100) == [2580, 1440] and G.layout_tag(3840, 2160, 200) == [1920, 1080],
      "layout tag: 1080p, 1080p at 150%, ultrawide, 4K at 200%")

if not os.path.isdir(LAY):
    print("\n(no pictures in test_screens/layouts - only the rules were checked)")
    print("FAILED" if fails else "all passed")
    sys.exit(1 if fails else 0)

# ---- 2. the main hub's Play button --------------------------------------------------------
GREEN_LO, GREEN_HI = (35, 110, 120), (80, 255, 255)
hubs = sorted(glob.glob(os.path.join(LAY, "1_hub_*.png")))
check(len(hubs) == 8, f"8 hub pictures ({len(hubs)})")
for p in hubs:
    img = virt(p)
    show(img)
    got = G.find_play_button()
    name = os.path.basename(p)[6:-4]
    if not got:
        check(False, f"hub {name}: Play found")
        continue
    x, y = int(round(got[0])), int(round(got[1]))
    # The button has a pale shiny stripe across it and white lettering, so the click's own
    # pixel need not be green. What matters: it is between the button's first and last green
    # rows, and not on the red reward banner underneath.
    hsv = cv2.cvtColor(np.ascontiguousarray(img[y - 60:y + 60, x - 130:x + 130]), cv2.COLOR_BGR2HSV)
    rows = np.where((cv2.inRange(hsv, GREEN_LO, GREEN_HI) > 0).sum(axis=1) >= 150)[0] + y - 60
    h0, s0, _v0 = (int(v) for v in hsv[60, 130])
    on_banner = (h0 <= 10 or h0 >= 170) and s0 > 140
    on_green = len(rows) > 0 and rows.min() <= y <= rows.max() and not on_banner
    check(on_green and abs(x - img.shape[1] / 2) < 12, f"hub {name}: click ({x},{y}) is on the green button")
    check(bool(G.at_hub()) and not G.in_lobby(), f"hub {name}: counts as the hub (and is not the FFA lobby)")
    check(G.find_join_button() is None, f"hub {name}: no Join button seen")
# why the remembered spot was not enough: at 125% and 150% it sits on the reward banner
for name, expect in (("1920x1080_full", True), ("1536x864", False), ("1280x720", False)):
    img = virt(os.path.join(LAY, f"1_hub_{name}.png"))
    px = cv2.cvtColor(np.ascontiguousarray(img[883:890, 900:920]), cv2.COLOR_BGR2HSV)
    green = float(cv2.inRange(px, GREEN_LO, GREEN_HI).mean()) / 255 > 0.8
    check(green is expect, f"old spot (y 886) on {name}: {'on Play' if expect else 'on the reward banner, not Play'}")

others = [p for p in glob.glob(os.path.join(LAY, "*.png")) if "1_hub_" not in os.path.basename(p)]
hit = []
for p in others:
    show(virt(p))
    if G.find_play_button():
        hit.append(os.path.basename(p))
check(not hit and len(others) >= 30, f"Play is never seen on the {len(others)} menu / spectate / picker / death pictures {hit}")
OLD_HUBS_WITH_PLAY = {"hub_20260211_192427929.png", "hub_20260807_141029390.png", "hub_20260926_094451049.png"}
wrong = []
for p in glob.glob(os.path.join(HERE, "test_screens", "*.png")) + glob.glob(os.path.join(HERE, "test_screens", "*.webp")):
    img = cv2.imread(p)
    if img is None:
        continue
    show(virt(p))
    if bool(G.find_play_button()) != (os.path.basename(p) in OLD_HUBS_WITH_PLAY):
        wrong.append(os.path.basename(p))
check(not wrong, f"older pictures: Play seen on the three real hub shots and nothing else {wrong}")

# ---- 3. spectate: Join this game ----------------------------------------------------------
spect = sorted(glob.glob(os.path.join(LAY, "4_ffa_screen_*.png")))
spect = [p for p in spect if "_full" not in p]                 # the fullscreen one is the teleport screen
check(len(spect) == 7, f"7 spectate pictures ({len(spect)})")
for p in spect:
    img = virt(p)
    show(img)
    got = G.find_join_button()
    name = os.path.basename(p)[13:-4]
    ok = bool(got) and 0.55 * img.shape[1] < got[0] < 0.63 * img.shape[1] and 0.62 * 1080 < got[1] < 0.69 * 1080
    check(ok, f"spectate {name}: Join found at {None if not got else (round(got[0]), round(got[1]))}")
    check(not G.at_hub(), f"spectate {name}: not taken for the hub")

# ---- 4. the death screen's Respawn is not Join, and not Play ------------------------------
for name in ("1707x960", "1536x960", "1280x800", "1863x780", "1720x720"):
    show(virt(os.path.join(LAY, f"5_picker_{name}.png")))
    check(G.find_join_button() is None and not G.at_hub(), f"death screen {name}: Respawn is neither Join nor Play")

# ---- 5. the weapon picker: a setup works on the layout it was made on ---------------------
SPOTS = {"1920x1080_full": ((637, 480), (1268, 486)), "1536x864": ((621, 477), (1281, 484)),
         "1280x720": ((605, 475), (1293, 480))}                  # Random and launcher tile centres
PCT = {"1920x1080_full": "100", "1536x864": "125", "1280x720": "150"}
G.screen_size = lambda: (1920, 1080)
tiles = {}
for name, ((rx, ry), (gx, gy)) in SPOTS.items():
    img = virt(os.path.join(LAY, f"5_picker_{name}.png"))
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    half = G.GRAB // 2
    tile = gray[ry - half:ry + half, rx - half:rx + half].copy()
    gl = gray[gy - half:gy + half, gx - half:gx + half].copy()
    tiles[name] = (tile, gl)
    cal = {"anchor_offset": [half, half], "gl_offset": [gx - rx, gy - ry], "_gl_tile": gl}
    os.environ["GLASSMACRO_TEST_SCALING"] = PCT[name]
    show(img)
    score, pos = G.find_random(cal, tile, G.DEFAULT_THRESHOLD)
    check(pos is not None and abs(pos[0] - rx) <= 2 and abs(pos[1] - ry) <= 2,
          f"picker {name}: a setup made at {PCT[name]}% finds Random at {pos} ({score:.2f})")
    score, where = G.find_launcher_in_grid(cal, (rx, ry), G.DEFAULT_THRESHOLD)
    check(where is not None and abs(where[0] - gx) <= 3 and abs(where[1] - gy) <= 3,
          f"picker {name}: ...and the launcher in the grid at {where} ({score:.2f})")
    check(not G.at_hub() and G.find_join_button() is None, f"picker {name}: not the hub, no Join button")
check(631 == SPOTS["1920x1080_full"][1][0] - SPOTS["1920x1080_full"][0][0]
      and abs((SPOTS["1280x720"][1][0] - SPOTS["1280x720"][0][0]) / 631.0 - G.ui_growth(1080, 150)) < 0.01
      and abs((SPOTS["1536x864"][1][0] - SPOTS["1536x864"][0][0]) / 631.0 - G.ui_growth(1080, 125)) < 0.01,
      "the tiles sit further apart with scaling, by what ui_growth() says")
# ...and that is why a setup made at 100% has to be redone after switching to 150%
os.environ["GLASSMACRO_TEST_SCALING"] = "150"
show(virt(os.path.join(LAY, "5_picker_1280x720.png")))
cal = {"anchor_offset": [G.GRAB // 2, G.GRAB // 2], "gl_offset": [631, 6], "_gl_tile": tiles["1920x1080_full"][1]}
score, pos = G.find_random(cal, tiles["1920x1080_full"][0], G.DEFAULT_THRESHOLD)
check(pos is None, f"a setup made at 100% does NOT match the picker at 150% ({score:.2f}) - hence 'redo the weapon setup'")
os.environ.pop("GLASSMACRO_TEST_SCALING", None)

print("\nFAILED" if fails else "\nall passed")
sys.exit(1 if fails else 0)
