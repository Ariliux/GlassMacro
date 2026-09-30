"""Check every screen detector against real screenshots. Run after ANY change
to find_join_button / in_lobby / find_reconnect:

    python test_detectors.py

Each screenshot says by its name what it is (see test_screens/README.txt), and
every detector must fire on its own screen and stay silent on all the others.
Sandboxed: LOCALAPPDATA points at a temp folder before import, so the real
data folder is never touched.
"""
import glob
import os
import sys
import tempfile

os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="glass_det_")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import glassmacro as G                                      # noqa: E402
from PIL import Image                                       # noqa: E402

assert G.DATA_DIR.startswith(os.environ["LOCALAPPDATA"]), G.DATA_DIR

DETECTORS = {
    "join": G.find_join_button,
    "hub": G.in_lobby,
    "reconnect": G.find_reconnect,
}


def expected(name):
    for key in DETECTORS:
        if name.startswith(key):
            return key
    return None                     # gameplay, picker, respawn: nothing fires


fails = 0
files = sorted(glob.glob(os.path.join(HERE, "test_screens", "*.*")))
files = [f for f in files if not f.endswith(".txt")]
if not files:
    # the screenshots are not in the repo - they show other players' names
    print("no screenshots in test_screens/ - add real 1920x1080 Rivals "
          "screenshots named as in test_screens/README.txt to run this")
    sys.exit(0)
for path in files:
    name = os.path.basename(path)
    img = Image.open(path).convert("RGB")
    G.ImageGrab.grab = lambda img=img: img
    fired = [k for k, fn in DETECTORS.items() if fn() is not None]
    want = expected(name)
    ok = fired == ([want] if want else [])
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name:34} fired={fired or '-'}"
          f"  want={want or 'nothing'}")

print(f"\n{len(files) - fails}/{len(files)} passed")
sys.exit(1 if fails else 0)
