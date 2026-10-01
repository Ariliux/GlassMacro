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
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import keyboard                                              # noqa: E402
keyboard.add_hotkey = lambda *a, **k: None                   # never arm F8
import glassmacro as G                                       # noqa: E402

assert G.DATA_DIR.startswith(os.environ["LOCALAPPDATA"]), G.DATA_DIR

src = open(os.path.join(HERE, "glassmacro.py"), encoding="utf-8").read()
# the rule tables themselves do not count as somewhere the text is logged
body = re.sub(r"(STATUS_RULES|FEED_RULES) = \(.*?\n    \)\n", "", src,
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


for table in ("STATUS_RULES", "FEED_RULES"):
    for row in getattr(G.GlassMacro, table):
        needle = row[0]
        found = needle in body or any(needle in r for r in RUNTIME)
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
app.destroy()

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
