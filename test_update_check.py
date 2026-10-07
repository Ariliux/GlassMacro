"""Check the update checker without touching the internet.

    python test_update_check.py

GitHub's answer is faked, so this runs offline and never sends a request.
Sandboxed: LOCALAPPDATA points at a temp folder before import.
"""
import io
import json
import os
import sys
import tempfile
import urllib.request

os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="glass_upd_")
os.environ["GLASSMACRO_NO_SEND"] = "1"                         # never post to Discord
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import keyboard                                              # noqa: E402
keyboard.add_hotkey = lambda *a, **k: None                   # never arm F8
import glassmacro as G                                       # noqa: E402

assert G.DATA_DIR.startswith(os.environ["LOCALAPPDATA"]), G.DATA_DIR

class _FakeNotifier:
    """Records warning pop-ups instead of showing them - tests never open
    real ones on the screen."""
    shown = []

    def __init__(self):
        self.last = 0.0
        self.open = False

    def show(self, title, headline, details, settings_button=True):
        _FakeNotifier.shown.append((headline, details))
        return "ok"


G.WarningPopup = _FakeNotifier
G.ask_to_update = lambda *a, **k: None        # never open the real update dialog
G.play_foreground_sound = lambda: None        # ...or play its sound
G.os.startfile = lambda *a, **k: None         # never open a browser/release page

fails = 0


def check(ok, what):
    global fails
    fails += not ok
    print(("PASS  " if ok else "FAIL  ") + what)


calls = []


def fake(answer):
    """Make urlopen return `answer` (a dict, an exception, or raw bytes)."""
    def urlopen(req, timeout=None):
        calls.append(req.full_url)
        if isinstance(answer, Exception):
            raise answer
        body = answer if isinstance(answer, bytes) else json.dumps(answer).encode()
        return io.BytesIO(body)
    urllib.request.urlopen = urlopen


page = "https://github.com/Ariliux/GlassMacro/releases/tag/v9.9.9"
newer = {"tag_name": "v9.9.9", "html_url": page}

check(G.version_tuple("v1.0.10") > G.version_tuple("1.0.9"), "1.0.10 is newer than 1.0.9")
check(G.version_tuple("v1.0.3") == (1, 0, 3), "tags like v1.0.3 parse")
check(G.version_tuple("nonsense") == (), "a tag with no numbers parses as nothing")
check(G.version_tuple("v1.0.4-x64") == G.version_tuple("1.0.4"), "extra numbers in a tag do not count")

fake(newer)
check(G.latest_release() == ("9.9.9", page), "a newer release is read")
check(calls[-1] == G.LATEST_API, "it asks GitHub's API for THIS repo only")
fake({"tag_name": "v9.9.9", "html_url": "https://evil.example/download"})
check(G.latest_release()[1] == G.RELEASES_URL,
      "a link outside the repo is never used - it falls back to the release page")
for answer, why in ((dict(newer, draft=True), "a draft"),
                    (dict(newer, prerelease=True), "a pre-release"),
                    ({"message": "API rate limit exceeded"}, "a rate limit"),
                    (b"<html>not json</html>", "garbage"),
                    (OSError("no network"), "no internet")):
    fake(answer)
    check(G.latest_release() is None, f"{why} means 'no news', not a crash")

app = G.GlassMacro()
app.withdraw()
app.update()
shown = []
app._ui = lambda fn: shown.append(fn)

for tag, expect in (("v" + G.APP_VER, False), ("v1.0.2", False),
                    ("v1.0.99", False), ("v1.1.99", True)):
    shown.clear()
    fake({"tag_name": tag, "html_url": page})
    got = app._check_updates_once()
    for fn in shown:                  # run what the check queued for the UI
        fn()
    app.update()
    visible = bool(app.lnk_update.winfo_manager())
    check(got == expect and visible == expect,
          f"running {G.APP_VER}, GitHub has {tag}: notice={'yes' if expect else 'no'}")
log_text = app.txt._textbox.get("1.0", "end")
check("update check: up to date (latest on GitHub is " + G.APP_VER + ")" in log_text,
      "an up-to-date check leaves one line in Full log")
check("update check" not in app.feed._textbox.get("1.0", "end"),
      "...and stays out of the activity feed")
fake(OSError("offline"))
shown.clear()
app._check_updates_once()
for fn in shown:
    fn()
check("couldn't reach GitHub" in app.txt._textbox.get("1.0", "end"),
      "an offline check says so in Full log")

shown.clear()
calls.clear()
app.settings["check_updates"] = False
fake({"tag_name": "v1.0.99", "html_url": page})
check(app._check_updates_once() is False and not calls and not shown,
      "with the switch off, GitHub is never even asked")
app.settings["check_updates"] = True

app._show_update("1.0.99", page)
app.update()
check("1.0.99" in app.lnk_update.cget("text") and app.lnk_update.winfo_manager(),
      "the header shows the update link")
check("Update available" in app.feed._textbox.get("1.0", "1.end"),
      "the activity feed says an update is out")

G.save_settings({"check_updates": False})
check(G.load_settings() == {"check_updates": False}, "the switch is remembered")
app.destroy()

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
