"""Check the 1.0.9 safety features, offline and without touching Roblox.

    python test_reliability.py

Roblox, the relaunch and the clock are all faked - nothing is closed,
launched or clicked. Sandboxed: LOCALAPPDATA points at a temp folder.
"""
import os
import sys
import tempfile
import uuid

os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="glass_rel_")
os.environ["GLASSMACRO_NO_SEND"] = "1"                         # never post to Discord
import tests_guard                                           # noqa: E402
tests_guard.start()                     # never let a test window keep the keyboard
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import keyboard                                              # noqa: E402
keyboard.add_hotkey = lambda *a, **k: None
import glassmacro as G                                       # noqa: E402

assert G.DATA_DIR.startswith(os.environ["LOCALAPPDATA"]), G.DATA_DIR
fails = 0


def check(ok, what):
    global fails
    fails += not ok
    print(("PASS  " if ok else "FAIL  ") + what)


# ---- one copy only ---------------------------------------------------------
G.SINGLE_INSTANCE_MUTEX = "Local\\GlassMacroTest." + uuid.uuid4().hex
G.APP_NAME = "GlassMacro test " + uuid.uuid4().hex  # no real window to raise
check(G.claim_single_instance() is True, "the first copy gets to run")
check(G.claim_single_instance() is False, "a second copy is turned away")

# ---- keep the PC awake -----------------------------------------------------
check(G.keep_awake(True) and G.keep_awake(False),
      "Windows accepts the keep-awake request, and its release")

# ---- the log can't grow forever --------------------------------------------
log = os.path.join(G.DATA_DIR, "log.txt")
open(log, "w").write("x" * 1000)
check(G.rotate_log(log, limit=5000) is False and os.path.exists(log),
      "a small log is left alone")
open(log, "w").write("x" * 6000)
old = os.path.join(G.DATA_DIR, "log.old.txt")
check(G.rotate_log(log, limit=5000) and not os.path.exists(log)
      and os.path.getsize(old) == 6000,
      "a big log becomes log.old.txt and a fresh one starts")

# ---- Roblox closed or crashed mid-run --------------------------------------
G.WarningPopup = type("P", (), {"last": 0.0, "open": False,
                                "show": lambda *a, **k: "ok"})
G.latest_release = lambda *a, **k: None
G.release_info = lambda *a, **k: None     # the background update check stays offline
G.ask_to_update = lambda *a, **k: None    # never open the real update dialog
app = G.GlassMacro()
app.withdraw()
app.update()
state = {"window": True, "process": True, "now": 1000.0}
launched, logs = [], []
G.roblox_hwnd = lambda: 123 if state["window"] else 0
G.roblox_running = lambda: state["process"]
G.relaunch_roblox = lambda place, log=None: launched.append(place)
G.focused = lambda: False
G.RELAUNCH_WAIT = 0
G.time.time = lambda: state["now"]
app.running = True
app._reopens = []


def tick(seconds):
    state["now"] += seconds
    return app._roblox_closed_check(logs.append)


check(not tick(1) and not launched, "Roblox open (even alt-tabbed): nothing happens")
state["window"] = False
check(not tick(1) and not tick(30) and not launched,
      "Roblox gone for under 45 seconds: still waiting")
state["process"] = True
check(not tick(20) and not launched,
      "the window is gone but Roblox is still running (loading): waits")
state["process"] = False
tick(1)
check(tick(50) and launched == [G.RIVALS_PLACE]
      and "Roblox closed - reopening Rivals" in logs,
      "gone for 45+ seconds: reopens Rivals")
for _ in range(2):
    tick(1)
    tick(50)
check(len(launched) == 3, "reopens again when it closes again (3 times)")
tick(1)
check(not tick(50) and len(launched) == 3
      and any("keeps closing" in m for m in logs),
      "a 4th time within the hour: stops reopening and says so")
state["now"] += 3700
first, second = tick(1), tick(50)     # Roblox stayed gone all along, so the
check((first or second) and len(launched) == 4,   # very next check may act
      "an hour later it will reopen again")
# review fix: Roblox's process stays alive with no window (crash box, or a
# client that closed but never exited) - that's stuck, not "starting up"
app._reopens = []
logs.clear()
state["window"], state["process"] = False, True
before = len(launched)
acted = [tick(50) for _ in range(4)]           # ~200s of "running, no window"
check(any(acted) and len(launched) == before + 1
      and any("running with no window" in m for m in logs),
      "a stuck Roblox (process alive, no window) is restarted after ~90s")
state["window"] = True
tick(1)                                        # it came back
state["window"] = False
check(not tick(50) and not tick(30),
      "...and once Roblox is back, the clock starts over")
state["process"] = False

app.sw_reconnect.deselect()
app._roblox_gone_since = 0.0
app._reopens = []
count = len(launched)
tick(1)
check(not tick(50) and not tick(100) and len(launched) == count,
      "with Reconnect automatically off, it never reopens Roblox")

# ---- 1.1.2: Roblox froze - the game picture stopped moving -----------------
app.sw_reconnect.select()
G.save_event_shot = lambda *a, **k: None
pic = {"img": G.np.zeros((36, 64), G.np.int16)}
G.frame_thumb = lambda: pic["img"].copy()
app._reopens, app._reopen_gave_up = [], False
app._frame, app._frame_since, app._frame_at = None, 0.0, 0.0
logs.clear()
count = len(launched)


def fz(seconds, change=0):
    pic["img"] = pic["img"] + change
    state["now"] += seconds
    return app._frozen_check(logs.append)


check(not any(fz(5, 9) for _ in range(120)) and len(launched) == count,
      "a moving picture (10 min of it) is never 'frozen'")
check(not any(fz(5) for _ in range(40)) and len(launched) == count,
      "the picture still for under 4 minutes: waits")
check(any(fz(5) for _ in range(10)) and len(launched) == count + 1
      and any("Roblox froze" in m for m in logs),
      "the picture still for 4 minutes: restarts Roblox and says why")
check(not any(fz(5, 1 if i % 2 else -1) for i in range(40))
      and any(fz(5) for _ in range(20)) and len(launched) == count + 2,
      "a flicker of 1 shade still counts as the same picture")
check(any(fz(5) for _ in range(60)) and len(launched) == count + 3,
      "restarts again if it freezes again")
check(not any(fz(5) for _ in range(60)) and len(launched) == count + 3
      and any("keeps freezing" in m for m in logs),
      "a 4th time within the hour: stops restarting and says so")
app._reopens, app._reopen_gave_up = [state["now"]] * 2, False
app._roblox_gone_since, state["window"], state["process"] = 0.0, False, False
tick(1)
tick(50)
check(len(launched) == count + 4 and not tick(1) and not tick(50)
      and any("keeps closing" in m for m in logs),
      "closing and freezing share the 3 restarts an hour - no loop between them")
app.sw_reconnect.deselect()
app._reopens, app._frame = [], None
check(not any(fz(5) for _ in range(120)) and len(launched) == count + 4,
      "with Reconnect automatically off, a frozen picture is left alone")
app.running = False
app.destroy()

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
