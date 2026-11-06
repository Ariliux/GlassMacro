"""Check the 1.1.2 hotkeys: F1 starts, F2 stops, and all three keys can be
changed on the Settings page. Offline, nothing is pressed or started.

    python test_hotkeys.py

Sandboxed: LOCALAPPDATA points at a temp folder before import, and the
global keyboard hook is faked - no real hotkey is ever registered.
"""
import json
import os
import sys
import tempfile

os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="glass_keys_")
os.environ["GLASSMACRO_NO_SEND"] = "1"                         # never post to Discord
import tests_guard                                           # noqa: E402
tests_guard.start()                     # never let a test window keep the keyboard
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import keyboard                                              # noqa: E402
armed, removed = [], []
keyboard.add_hotkey = lambda key, fn, *a, **k: armed.append((key, fn)) or key
keyboard.remove_hotkey = lambda h: removed.append(h)
import glassmacro as G                                       # noqa: E402
G.latest_release = lambda *a, **k: None   # tests never touch the network
G.release_info = lambda *a, **k: None
G.ask_to_update = lambda *a, **k: None    # never open the real update dialog
G.WarningPopup = type("P", (), {"last": 0.0, "open": False,
                                "show": lambda *a, **k: "ok"})

assert G.DATA_DIR.startswith(os.environ["LOCALAPPDATA"]), G.DATA_DIR
fails = 0


def check(ok, what):
    global fails
    fails += not ok
    print(("PASS  " if ok else "FAIL  ") + what)


# ---- the rules ---------------------------------------------------------------
check(G.clean_key("f5") == "F5" and G.clean_key(" F12 ") == "F12"
      and G.clean_key("F24") == "F24",
      "F1-F24 are allowed, any case")
check(not any(G.clean_key(k) for k in ("a", "5", "space", "F0", "F25", "",
                                       None, "ctrl+f5")),
      "letters, numbers, Space and combos are not")
check(G.load_keys(None) == {"start": "F1", "stop": "F2", "setup": "F8"},
      "the defaults are F1 start, F2 stop, F8 setup")
check(G.load_keys({"start": "f6", "stop": "F7"})["start"] == "F6",
      "saved keys are used")
check(G.load_keys({"start": "F11"})["start"] == "F1"
      and G.load_keys({"stop": "q"})["stop"] == "F2",
      "a blocked or bad saved key falls back to the default")
check(G.load_keys({"start": "F2"}) == G.DEFAULT_KEYS,
      "two jobs on one key (hand-edited file) falls back to all defaults")

# ---- the app -----------------------------------------------------------------
app = G.GlassMacro()
app.withdraw()
app.update()


def drain():
    while not app._q.empty():
        app._q.get_nowait()()


def press(keysym):
    return app._key_pressed(type("E", (), {"keysym": keysym})())


check([k for k, _ in armed] == ["f1", "f2"],
      "on open, F1 and F2 are the only global keys (not F8)")

toggles = []
real_toggle = app.toggle_run
app.toggle_run = lambda: toggles.append(app.running)
app._hotkey_start()
drain()
check(toggles == [False], "F1 starts when nothing is running")
app.running = True
app._hotkey_start()
drain()
check(toggles == [False], "F1 does nothing while it's already running")
app._hotkey_stop()
drain()
check(toggles == [False, True], "F2 stops a run")
app.running = False
app._hotkey_stop()
drain()
check(toggles == [False, True], "F2 does nothing when nothing is running")
app.calibrating = True
app._hotkey_start()
drain()
check(len(toggles) == 2, "F1 is ignored during setup")
app.calibrating = False

# ---- changing a key in Settings ----------------------------------------------
app._pick_key("start")
check(app._key_btns["start"].cget("text") == "Press a key…",
      "clicking the Start key waits for a new one")
app._hotkey_start()
drain()
check(len(toggles) == 2, "...and the old key can't start a run meanwhile")
check(press("a") == "break" and app.keys["start"] == "F1"
      and "can't be a hotkey" in app.lbl_keys.cget("text"),
      "a letter is refused, with a reason")
check(press("F11") == "break" and app.keys["start"] == "F1"
      and "fullscreen" in app.lbl_keys.cget("text"),
      "F11 is refused - the macro presses it itself")
check(press("F2") == "break" and app.keys["start"] == "F1"
      and "already Stop" in app.lbl_keys.cget("text"),
      "a key that's already Stop is refused")
armed.clear()
check(press("F5") == "break" and app.keys["start"] == "F5",
      "F5 is taken")
check([k for k, _ in armed] == ["f5", "f2"] and "f1" in removed,
      "the old F1 is released and F5 + F2 are armed")
saved = json.load(open(G.SETTINGS_PATH, encoding="utf-8"))
check(saved.get("keys") == {"start": "F5", "stop": "F2", "setup": "F8"},
      "the new key is saved in settings.json")
check(app._key_btns["start"].cget("text") == "F5"
      and app._key_picking is None,
      "the button shows F5 and stops waiting")
check(app.btn_run._keytext.cget("text") == "F5",
      "the big Start button's keycap says F5")
caps = [lbl.cget("text") for lbl, t in app._keyed_widgets if t == "{start}"]
check(caps == ["F5"], "the About page's shortcut list says F5")
check(any("F5 starts, F2 stops" in lbl.cget("text")
          for lbl, _ in app._keyed_widgets),
      "the footer says F5 starts, F2 stops")

app._pick_key("setup")
check(press("Escape") == "break" and app.keys["setup"] == "F8"
      and app._key_btns["setup"].cget("text") == "F8",
      "Esc cancels and keeps the old key")
app._pick_key("setup")
press("F6")
check(app.keys["setup"] == "F6"
      and any("press F6" in lbl.cget("text") for lbl, _ in app._keyed_widgets),
      "the setup key changes the setup texts too")
check(press("F7") is None and app.keys["stop"] == "F2",
      "keys pressed when nothing is waiting change nothing")

app.running = True
app._pick_key("stop")
check(app._key_picking is None and "Stop the macro" in app.lbl_keys.cget("text"),
      "keys can't be changed during a run")
app.running = False

# ---- the log line the status card and stats key off --------------------------
app.toggle_run = real_toggle
check(app.keys["stop"] == "F2", "stop key still F2")
app._status_from_log("started - F2 stops it")
check(app._state_title == "Starting", "'started - F2 stops it' reads as Starting")

app.destroy()

# ---- a fresh start reads the saved keys -------------------------------------
armed.clear()
app = G.GlassMacro()
app.withdraw()
app.update()
check(app.keys == {"start": "F5", "stop": "F2", "setup": "F6"}
      and [k for k, _ in armed] == ["f5", "f2"],
      "the changed keys are back after a restart")
app.destroy()

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
