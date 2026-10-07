"""Check the status card, activity feed and setup guide still match the log.

    python test_ui_rules.py

The UI never talks to the macro directly - it reads the log lines the macro
writes. So if a log message is reworded, the rule that reads it silently stops
matching. This fails if any rule's text no longer appears in the code, and
pushes real log lines through the app to check what the card and feed show.
Sandboxed: LOCALAPPDATA points at a temp folder before import.
"""
import os
import re
import sys
import tempfile

os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="glass_rules_")
os.environ["GLASSMACRO_NO_SEND"] = "1"                         # never post to Discord
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import keyboard                                              # noqa: E402
keyboard.add_hotkey = lambda *a, **k: None                   # never arm F8
import glassmacro as G                                       # noqa: E402
G.latest_release = lambda *a, **k: None   # tests never touch the network
G.release_info = lambda *a, **k: None     # ...nor does the background update check
G.ask_to_update = lambda *a, **k: None    # never open the real update dialog

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

src = open(os.path.join(HERE, "glassmacro.py"), encoding="utf-8").read()
# the rule tables themselves do not count as somewhere the text is logged
body = re.sub(r"(STATUS_RULES|FEED_RULES|EVENT_RULES) = \(.*?\n    \)\n", "", src,
              flags=re.S)
# lines built at runtime from pieces, exactly as the macro produces them
RUNTIME = [
    "hover GRENADE LAUNCHER and press F8",
    "hover the FIRST loadout slot at the top and press F8",
    "  got RANDOM at (812, 402)",
    "  got GRENADE LAUNCHER at (1268, 486)",
    "  got the FIRST loadout slot at the top at (970, 256)",
    "now hover FREE FOR ALL (scroll down to it first), F8",
    "now hover PLAY on the Free For All screen, F8",
]

fails = 0


def check(ok, what):
    global fails
    fails += not ok
    print(("PASS  " if ok else "FAIL  ") + what)


BODY_LINES = body.splitlines()


def logged(needle):
    """The needle sits inside a log( call (on its line or up to 2 lines
    above) - UI labels that merely mention it don't count, so rewording the
    real log line can't hide behind a checkbox or help text."""
    for i, line in enumerate(BODY_LINES):
        if needle in line and any("log(" in BODY_LINES[j]
                                  for j in range(max(0, i - 2), i + 1)):
            return True
    return False


for table in ("STATUS_RULES", "FEED_RULES", "EVENT_RULES"):
    for row in getattr(G.GlassMacro, table):
        needle = row[0]
        found = logged(needle) or any(needle in r for r in RUNTIME)
        check(found, f"{table}: {needle!r} is still logged somewhere")

# behaviour: real lines in, what does a person see?
app = G.GlassMacro()
app.withdraw()
SEQ = [
    ("started - F8 stops it", "Starting", "Started"),
    ("in the hub - joining FFA", "Joining Free For All",
     "Joining Free For All"),
    ("  in a match", "In a match", "Back in Free For All"),
    ("picker up (match 1.00) - choosing loadout", "Picking a loadout", None),
    ("  done, back to jumping", "In a match", "Picked a loadout"),
    ("  done, back to jumping", "In a match", "Picked a loadout"),
    ("PAUSED - Roblox is not the focused window", "Paused", "Paused"),
    ("Roblox is back in front - resuming", "Back in Rivals", "Resumed"),
    ("quiet for 12 min (not died, nothing to do - still playing) - saved x",
     "All quiet", "All quiet"),
]
for line, title, feed in SEQ:
    before = app._feed_rows
    top = app._feed_top
    app.log(line)
    app.update()
    check(app._state_title == title,
          f"card after {line[:40]!r}: {app._state_title!r} == {title!r}")
    if feed:
        first = app.feed._textbox.get("1.0", "1.end")
        check(feed in first, f"feed after {line[:40]!r} shows {feed!r}")
check("12 min" in app.lbl_detail.cget("text"), "quiet detail carries the minutes")
check(app.n_picks == 2 and app.n_joins == 1, "loadout and rejoin counters")
first = app.feed._textbox.get("1.0", "end")
check("×2" in first, "two picks in a row merge into one row with x2")
check("random 1/3" not in first and "match 1.00" not in first,
      "developer detail stays out of the feed")

# Windows scaling: 150% must warn everywhere, 100% must say nothing
real_scaling, real_size = G.display_scaling, G.screen_size
G.screen_size = lambda: (1920, 1080)
G.display_scaling = lambda: 150
lines_before = app.txt._textbox.get("1.0", "end").count("heads up")
app._warn_display()
app.update()
check(app._state_title == "Set scaling to 100%",
      f"150% scaling: card says {app._state_title!r}")
check("150%" in app.lbl_detail.cget("text"), "the card names the current 150%")
check(app.lbl_res.cget("text").startswith("150% scaling"),
      f"header warns: {app.lbl_res.cget('text')!r}")
check("Scaling isn't 100%" in app.feed._textbox.get("1.0", "1.end"),
      "the feed says scaling isn't 100%")
G.display_scaling = lambda: 100
before = app.txt._textbox.get("1.0", "end").count("heads up")
app._warn_display()
app.update()
check(app.txt._textbox.get("1.0", "end").count("heads up") == before,
      "at 100% there is no heads-up at all")
check(app.lbl_res.cget("text") == "1920×1080",
      "at 100% the header goes back to plain 1920x1080")
G.screen_size = lambda: (2560, 1440)
G.display_scaling = lambda: 125
app._warn_display()
check("2560" in app.lbl_res.cget("text") and "125%" in app.lbl_res.cget("text"),
      f"both wrong: header names both ({app.lbl_res.cget('text')!r})")

# fixing scaling WHILE THE APP IS OPEN clears every warning by itself - even
# during setup, where there is no Start button to press
G.screen_size = lambda: (1920, 1080)
G.display_scaling = lambda: 150
app._recheck_display()
app.update()
check(bool(app._warn_box.winfo_manager()) and "150%" in app.lbl_warn.cget("text"),
      "setup guide shows the scaling box at 150%")
G.display_scaling = lambda: 100
app._recheck_display()
app.update()
check(not app._warn_box.winfo_manager(),
      "after switching to 100% the guide's box disappears on its own")
check(app.lbl_res.cget("text") == "1920×1080",
      "...and the header goes back to normal")
check("Screen settings look right" in app.feed._textbox.get("1.0", "1.end"),
      "...and the feed says the screen settings look right")
before = app.txt._textbox.get("1.0", "end").count("screen check")
app._recheck_display()
check(app.txt._textbox.get("1.0", "end").count("screen check") == before,
      "nothing changed = nothing logged (no spam every 5 seconds)")

# the warning pop-up (faked here - tests never open real ones)
import time as _time                                         # noqa: E402


def wait_for(n):
    """The pop-up runs on its own thread - give it a moment."""
    for _ in range(50):
        if len(_FakeNotifier.shown) >= n:
            return
        _time.sleep(0.02)


_FakeNotifier.shown.clear()
if hasattr(app, "_popup"):
    del app._popup
G.screen_size = lambda: (1920, 1080)
G.display_scaling = lambda: 150
app._warn_display()
wait_for(1)
check(len(_FakeNotifier.shown) == 1
      and _FakeNotifier.shown[0][0] == "Windows scaling is 150%"
      and "Settings" in _FakeNotifier.shown[0][1],
      f"150%: one pop-up, naming 150% and where to fix it ({_FakeNotifier.shown})")
app._warn_display()
wait_for(2)
check(len(_FakeNotifier.shown) == 1,
      "a second warning within a minute does NOT pop up again")
app._popup.last = 0.0                         # a minute later...
G.display_scaling = lambda: 100
app._warn_display()
wait_for(2)
check(len(_FakeNotifier.shown) == 1, "at 100% there is no pop-up")
G.screen_size = lambda: (2560, 1440)
app._warn_display()
wait_for(2)
check(len(_FakeNotifier.shown) == 2 and "2560" in _FakeNotifier.shown[1][0],
      "a wrong screen size pops up too")
app._popup.last = 0.0
app._popup.open = True                        # one already on screen
G.display_scaling = lambda: 150
app._warn_display()
wait_for(3)
check(len(_FakeNotifier.shown) == 2, "never two pop-ups at once")
app._popup.open = False
G.display_scaling, G.screen_size = real_scaling, real_size

# a run stopped within a second must report ITS length, not the last run's
import time                                                  # noqa: E402
app.running, app.session_start = True, time.time() - 7200    # run A: 2h
app.log("started - F8 stops it")
app.running, app.session_start = False, None
app.log("stopped")
check("Ran 2h" in app.lbl_detail.cget("text"), "a 2h run reports 2h")
app.running, app.session_start = True, time.time()           # run B: instant
app.log("started - F8 stops it")
app.running, app.session_start = False, None
app.log("stopped")
check("Ran 0s" in app.lbl_detail.cget("text"),
      f"an instant stop reports ~0s, not the last run "
      f"({app.lbl_detail.cget('text')!r})")

# 1.1 Activity filter: a filtered view never breaks the 'All' feed
app._set_feed_filter("Loadouts")
app.log("  done, back to jumping")
app.log("in the hub - joining FFA")
check("Picked a loadout" in app.feed._textbox.get("1.0", "1.end"),
      "Loadouts filter: the newest loadout is on top")
check("Joining" not in app.feed._textbox.get("1.0", "end"),
      "...and other events are left out")
app._set_feed_filter("All")
check("Joining Free For All" in app.feed._textbox.get("1.0", "1.end")
      and app._feed_rows == len(app._feed_items),
      "back on All: the newest event of any kind is on top again")
app.log("in the hub - joining FFA")
check("×2" in app.feed._textbox.get("1.0", "1.end"),
      "...and repeats still merge after a filter was used")

# 1.1 Log page: problem lines are coloured as they arrive
app.log("PAUSED - Roblox is not the focused window")
tb = app.txt._textbox
last = int(tb.index("end-1c").split(".")[0]) - 1
check("warn" in tb.tag_names(f"{last}.12"), "a PAUSED line is amber on the Log page")
app._set_log_mode("Problems")
app.log("in the hub - joining FFA")
last = int(tb.index("end-1c").split(".")[0]) - 1
check("hide" in tb.tag_names(f"{last}.0"), "Problems view hides an ordinary line")
app._set_log_mode("All")
check("hide" not in tb.tag_names(f"{last}.0"), "...and All shows it again")

# 1.1 the WHILE IT RUNS switches are saved now
app.sw_shots.toggle()
check(G.load_settings().get("save_shots") is False,
      "turning Save screenshots off is remembered")

# 1.1 polish: the shimmer follows the run's state, never a stored flag
app._run_mode, app._state_title, app._dot_colour = "RUNNING", "In a match", G.GREEN
check(app._shimmer_mode() == "run", "running: the shimmer runs")
app._state_title = "Paused"
check(app._shimmer_mode() == "amber", "paused: the shimmer turns amber")
app._run_mode, app._state_title, app._dot_colour = "IDLE", "Ready", G.MUTED
check(app._shimmer_mode() is None, "idle: no shimmer")
app._updating, app._state_detail = True, "Downloading · 42% of 60 MB"
check(app._shimmer_mode() == ("bar", 42), "downloading: a 42% bar")
app._updating = False
app.settings["motion"] = False
check(app._anim_delay() == 300, "Animations off: the clock idles at 300 ms")
app.settings["motion"] = True

# toasts: a run's own lines never raise one; the few that should, do
app._toast_hide()
for line, _t, _f in SEQ:
    app.log(line)
check(not app._toast_w.winfo_manager(), "a run's log lines show no toast")
app.log("stop the macro first")
check(app._toast_w.winfo_manager() == "place"
      and "Stop the macro" in app._toast_text.cget("text"),
      "'stop the macro first' shows a toast")
app._toast_hide()

# sidebar badges: unread Activity rows, cleared by looking
app._show_page("home")
app.log("  done, back to jumping")
check(app._unread > 0 and app._nav["activity"]["badge"][0] is not None,
      "a new feed row puts a count on Activity")
app._show_page("activity")
check(app._unread == 0 and app._nav["activity"]["badge"][0] is None,
      "...and opening Activity clears it")
app._show_page("home")
app.log("stopped on an error: test")
check(app._log_alert and app._nav["log"]["badge"][0] is not None,
      "an error line puts a red dot on Log")
app._show_page("log")
check(not app._log_alert, "...cleared once the Log page is opened")

# the narrow rail, and the footer on a short window
app._layout_for(700, 560)
check(app._rail, "under 830 across: the rail")
app._layout_for(840, 560)
check(app._rail, "830-850: stays as it was (no flicker while dragging)")
app._layout_for(900, 560)
check(not app._rail and app._foot_shown, "from 850: the full sidebar")
app._layout_for(900, 510)
check(not app._foot_shown and app.lnk_update.winfo_manager() == "",
      "under 520 high the footer goes; the update link keeps its own state")
app._layout_for(985, 550)

# the window comes back where it was - unless that's off every screen now
app.settings["window"] = {"w": 900, "h": 600, "x": 100, "y": 100}
check(app._saved_window(1.0) is not None, "a saved on-screen window is reused")
app.settings["window"] = {"w": 900, "h": 600, "x": -40000, "y": 100}
check(app._saved_window(1.0) is None, "a window off every screen is not")
app.settings["window"] = {"w": 900, "h": 600, "x": 100, "y": 100}
app.settings["remember"] = False
check(app._saved_window(1.0) is None, "Remember off: never reused")
app.settings.pop("window")
app.settings["remember"] = True
app.destroy()

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
