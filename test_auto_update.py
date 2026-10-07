"""Check the self-updater on throwaway folders - offline, nothing real touched.

    python test_auto_update.py

Downloads come from local files (file:// URLs), the "app folder" is a temp
folder, and the pop-up, the hand-over process and closing the app are faked.
Sandboxed: LOCALAPPDATA points at a temp folder before import.
"""
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.request
import zipfile

os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="glass_auto_")
os.environ["GLASSMACRO_NO_SEND"] = "1"                         # never post to Discord
import tests_guard                                           # noqa: E402
tests_guard.start()                     # never let a test window keep the keyboard
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import keyboard                                              # noqa: E402
keyboard.add_hotkey = lambda *a, **k: None                   # never arm F8
import glassmacro as G                                       # noqa: E402

assert G.DATA_DIR.startswith(os.environ["LOCALAPPDATA"]), G.DATA_DIR
G.latest_release = lambda *a, **k: None                      # no network
REAL_URLOPEN = urllib.request.urlopen
WORK = tempfile.mkdtemp(prefix="glass_auto_work_")
fails = 0


def check(ok, what):
    global fails
    fails += not ok
    print(("PASS  " if ok else "FAIL  ") + what)


def fake_api(answer):
    def urlopen(req, timeout=None):
        return io.BytesIO(json.dumps(answer).encode())
    urllib.request.urlopen = urlopen


def make_zip(path, files):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


# ---- reading the release -------------------------------------------------
good = {"tag_name": "v9.9.9",
        "html_url": "https://github.com/Ariliux/GlassMacro/releases/tag/v9.9.9",
        "assets": [{"name": "GlassMacro-v9.9.9.zip",
                    "browser_download_url": G.ASSET_PREFIX + "v9.9.9/GlassMacro-v9.9.9.zip",
                    "digest": "sha256:" + "ab" * 32}]}
fake_api(good)
info = G.release_info()
check(info["version"] == "9.9.9" and info["zip"].endswith("GlassMacro-v9.9.9.zip")
      and info["sha256"] == "ab" * 32, "the release's zip and fingerprint are read")
for tweak, why in (
        ({"browser_download_url": "https://evil.example/GlassMacro-v9.9.9.zip"},
         "a zip hosted anywhere but this repo"),
        ({"digest": None}, "a zip with no fingerprint"),
        ({"name": "GlassMacro-v9.9.9.exe"}, "something that isn't the app zip")):
    bad = json.loads(json.dumps(good))
    bad["assets"][0].update(tweak)
    fake_api(bad)
    i = G.release_info()
    check(i is not None and i["zip"] is None,
          f"{why} is never used for an auto-update")
urllib.request.urlopen = REAL_URLOPEN
G.release_info = lambda *a, **k: None   # the app's background check stays offline

# ---- downloading ---------------------------------------------------------
src_zip = os.path.join(WORK, "src.zip")
sha = make_zip(src_zip, {"GlassMacro/GlassMacro.exe": b"NEW-EXE",
                         "GlassMacro/_internal/new.txt": b"new"})
url = "file:///" + src_zip.replace("\\", "/")
dest = os.path.join(WORK, "dl.zip")
seen = []
check(G.download_update(url, sha, dest, lambda d, t: seen.append(d))
      and os.path.exists(dest) and seen, "a download with the right fingerprint is kept")
os.remove(dest)
check(not G.download_update(url, "00" * 32, dest) and not os.path.exists(dest)
      and not os.path.exists(dest + ".part"),
      "a download with the WRONG fingerprint is thrown away completely")

# ---- unpacking -----------------------------------------------------------
stage = os.path.join(WORK, "stage")
exe = G.stage_update(src_zip, stage)
check(exe and exe.endswith(os.path.join("GlassMacro", "GlassMacro.exe")),
      "a GlassMacro folder build unpacks and its exe is found")
evil = os.path.join(WORK, "evil.zip")
make_zip(evil, {"GlassMacro/GlassMacro.exe": b"x", "GlassMacro/_internal/a": b"x",
                "../../outside.txt": b"gotcha"})
check(G.stage_update(evil, os.path.join(WORK, "stage2")) is None
      and not os.path.exists(os.path.join(WORK, "outside.txt")),
      "a zip that tries to write outside its folder is refused")
half = os.path.join(WORK, "half.zip")
make_zip(half, {"GlassMacro/GlassMacro.exe": b"x"})
check(G.stage_update(half, os.path.join(WORK, "stage3")) is None,
      "a zip without the app's _internal folder is refused")

# ---- swapping the files --------------------------------------------------
app_dir = os.path.join(WORK, "app")
os.makedirs(os.path.join(app_dir, "_internal"))
os.makedirs(os.path.join(app_dir, "backup"))
open(os.path.join(app_dir, "GlassMacro.exe"), "wb").write(b"OLD-EXE")
open(os.path.join(app_dir, "_internal", "old.txt"), "w").write("old")
open(os.path.join(app_dir, "backup", "keep.exe"), "w").write("keep")
check(G.swap_in_update(os.path.dirname(exe), app_dir),
      "the swap reports success")
check(open(os.path.join(app_dir, "GlassMacro.exe"), "rb").read() == b"NEW-EXE",
      "...the exe is the new one")
check(os.listdir(os.path.join(app_dir, "_internal")) == ["new.txt"],
      "..._internal is exactly the new one (no leftovers)")
check(open(os.path.join(app_dir, "backup", "keep.exe")).read() == "keep"
      and not os.path.exists(os.path.join(app_dir, "_internal.old")),
      "...anything else in the folder is untouched, and nothing is left aside")

broken_src = os.path.join(WORK, "broken")
os.makedirs(broken_src)
open(os.path.join(broken_src, "GlassMacro.exe"), "wb").write(b"X")
try:
    G.swap_in_update(broken_src, app_dir)            # no _internal: must fail
    raised = False
except Exception:
    raised = True
check(raised and os.listdir(os.path.join(app_dir, "_internal")) == ["new.txt"]
      and open(os.path.join(app_dir, "GlassMacro.exe"), "rb").read() == b"NEW-EXE",
      "a swap that fails half way puts everything back as it was")

# review fixes: copy first, switch last - nothing breaks part way
check(os.path.isfile(os.path.join(stage, "manifest.json"))
      and "GlassMacro.exe" in json.load(open(os.path.join(stage, "manifest.json"))),
      "unpacking writes the list of new files and sizes")
manifest = json.load(open(os.path.join(stage, "manifest.json")))
lying = dict(manifest)
lying[os.path.join("_internal", "missing.dll")] = 123
try:
    G.swap_in_update(os.path.dirname(exe), app_dir, lying)
    refused = False
except Exception:
    refused = True
check(refused and os.listdir(os.path.join(app_dir, "_internal")) == ["new.txt"]
      and not os.path.exists(os.path.join(app_dir, "_internal.new"))
      and not os.path.exists(os.path.join(app_dir, "GlassMacro.exe.new")),
      "an incomplete copy is refused, and the install is exactly as it was")
held = open(os.path.join(app_dir, "_internal", "new.txt"))   # "in use"
result = G.swap_in_update(os.path.dirname(exe), app_dir, manifest, tries=2)
held.close()
check(result is False and os.listdir(os.path.join(app_dir, "_internal")) == ["new.txt"]
      and not os.path.exists(os.path.join(app_dir, "_internal.new"))
      and not os.path.exists(os.path.join(app_dir, "_internal.old")),
      "if a file is in use, the switch is called off and nothing is changed")

# ---- the pop-up and the hand-over ----------------------------------------
class FakePopup:
    answer = "yes"
    asked = []

    def __init__(self):
        self.last, self.open = 0.0, False

    def show(self, title, headline, details, settings_button=True, yes_no=False):
        FakePopup.asked.append((headline, yes_no))
        return FakePopup.answer


G.WarningPopup = FakePopup
# the update question is its own pop-up now - fake that one too, so the
# tests never open a real dialog
G.ask_to_update = lambda *a, **k: (
    FakePopup.asked.append(("An update is available", True)) or FakePopup.answer)
app = G.GlassMacro()
app.withdraw()
app.update()
queued = []
app._ui = lambda fn: queued.append(fn)


def drain(seconds=2.0):
    end = time.time() + seconds
    while time.time() < end:
        while queued:
            queued.pop(0)()
        app.update()
        time.sleep(0.02)


# review fix: an ordinary start never touches the staging folder - a hand-over
# could be running from it right now
staging = os.path.join(G.UPDATE_DIR, "new")
os.makedirs(staging, exist_ok=True)
threads = []
real_thread = G.threading.Thread
G.threading.Thread = lambda *a, **k: threads.append(k) or real_thread(target=lambda: None)
app._after_update()
G.threading.Thread = real_thread
check(not threads and os.path.isdir(staging),
      "a start that isn't right after an update leaves the update folder alone")

launched, closed = [], []
G.subprocess.Popen = lambda args, **k: launched.append(args)
app._close = lambda: closed.append(True)
info = {"version": "9.9.9", "page": G.RELEASES_URL, "zip": url, "sha256": sha}

app.running = True                                   # mid-run: never ask
app._show_update("9.9.9", G.RELEASES_URL, info=info)
drain(0.5)
check(not FakePopup.asked and app._update_pending,
      "during a run it does NOT ask - it waits")
app.running = False

FakePopup.answer = "no"
app._ask_update()
drain(0.8)
check(FakePopup.asked == [("An update is available", True)] and not launched,
      "after the run it asks 'An update is available' with Yes/No - and No does nothing")

# review finding: a warning pop-up already on screen must not cancel the
# update question for good - it asks once that pop-up is gone
FakePopup.asked.clear()
app._update_asked = False
app._popup.open = True
app._ask_update()
drain(0.3)
check(not FakePopup.asked and not app._update_asked,
      "while another pop-up is up, the update question waits (not cancelled)")
app._popup.open = False
drain(3.5)
check(FakePopup.asked == [("An update is available", True)],
      "...and asks once that pop-up has gone")

# review finding: Yes clicked after a run was started is kept, not lost
started = []
app._start_update = lambda: started.append(True)
app.running = True
app._update_answer("yes")
check(not started and app._update_yes_pending,
      "a Yes during a run is remembered, not thrown away")
app.running = False
app._was_running = True                              # the run just stopped
drain(2.5)
check(started == [True], "...and the update starts as soon as the run stops")
del app._start_update                                # back to the real method

FakePopup.answer = "yes"
sys.frozen = True                                    # pretend to be the built app
G.sys.executable = os.path.join(app_dir, "GlassMacro.exe")
app._open_update()                                   # the header link asks again
drain(4)
ok = (launched and launched[0][1] == "--finish-update"
      and launched[0][2] == app_dir and launched[0][4] == G.APP_VER
      and os.path.isfile(launched[0][0]) and closed)
check(ok, f"Yes downloads, unpacks, starts the hand-over and closes ({launched})")
del sys.frozen
app.destroy()
shutil.rmtree(WORK, ignore_errors=True)

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
