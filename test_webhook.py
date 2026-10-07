"""Check Discord alerts and lifetime stats - fully offline.

    python test_webhook.py

Nothing here can reach Discord: GLASSMACRO_NO_SEND is set, and both the app's
one network door (_http) and urllib's urlopen are replaced by a tripwire that
records the call and refuses it. The transport cases use a fake transport
passed straight into WebhookSender. The webhook link below is made up.
Sandboxed: LOCALAPPDATA points at a temp folder before import.
"""
import glob
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import types
import urllib.error
import urllib.request

os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="glass_hook_")
os.environ["GLASSMACRO_NO_SEND"] = "1"                         # never post to Discord
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import keyboard                                              # noqa: E402
keyboard.add_hotkey = lambda *a, **k: None                   # never arm F8
import glassmacro as G                                       # noqa: E402

NET = []


def _tripwire(*a, **k):
    NET.append(a)
    raise ConnectionRefusedError("tests never touch the network")


G._http = _tripwire
urllib.request.urlopen = _tripwire
G.latest_release = lambda *a, **k: None
G.release_info = lambda *a, **k: None
G.ask_to_update = lambda *a, **k: None
G.WarningPopup = type("P", (), {"last": 0.0, "open": False,
                                "show": lambda *a, **k: "ok"})
assert G.DATA_DIR.startswith(os.environ["LOCALAPPDATA"]), G.DATA_DIR

fails = 0


def check(ok, what):
    global fails
    fails += not ok
    print(("PASS  " if ok else "FAIL  ") + what)


HOOK_ID = "123456789012345678"
TOKEN = "fakeTOKEN_-" * 6 + "abcd"                # 70 made-up characters
URL = f"https://discord.com/api/webhooks/{HOOK_ID}/{TOKEN}"
USER = "876543210987654321"

# ---- the link ---------------------------------------------------------------
good = [URL, URL + "/", "  " + URL + "\n",
        f"https://ptb.discord.com/api/webhooks/{HOOK_ID}/{TOKEN}",
        f"https://canary.discord.com/api/webhooks/{HOOK_ID}/{TOKEN}",
        f"https://discordapp.com/api/webhooks/{HOOK_ID}/{TOKEN}",
        f"https://discord.com/api/v10/webhooks/{HOOK_ID}/{TOKEN}"]
bad = ["", None, "hello", URL.replace("https://", "http://"),
       f"https://discord.com.evil.example/api/webhooks/{HOOK_ID}/{TOKEN}",
       f"https://evil.example/discord.com/api/webhooks/{HOOK_ID}/{TOKEN}",
       f"https://discord.com/api/webhooks/{HOOK_ID[:16]}/{TOKEN}",
       f"https://discord.com/api/webhooks/{HOOK_ID}/{TOKEN[:59]}",
       f"https://discord.com/api/webhooks/{HOOK_ID}/{TOKEN[:-1]}!",
       URL + "?wait=true", URL + "/extra", URL + " " + URL,
       f"https://www.discord.com/api/webhooks/{HOOK_ID}/{TOKEN}"]
check(all(G.normalize_hook(u) == URL for u in good),
      "accepts discord/ptb/canary/discordapp links, v10, a trailing slash "
      "and spaces - all normalised to one form")
check(not any(G.normalize_hook(u) for u in bad),
      "rejects http, look-alike hosts, short ids/tokens, bad characters and "
      "anything extra")
masked = G.mask_hook(URL)
check(TOKEN not in masked and TOKEN[:8] not in masked
      and masked.startswith("discord.com/") and HOOK_ID[:4] in masked
      and HOOK_ID[-4:] in masked and HOOK_ID not in masked,
      f"the mask shows only the ends of the id ({masked!r})")
check(G.mask_hook("nope") == "", "an invalid link masks to nothing")
s = G.scrub(f"failed {URL} and {TOKEN} in C:\\Users\\someone\\x.png "
            + "y" * 400, URL)
check(TOKEN not in s and HOOK_ID not in s and "C:\\" not in s
      and len(s) <= 300,
      "scrub removes the token, the link and paths, and caps at 300")

# ---- the message -----------------------------------------------------------
e = G.build_embed("start", "Run started", "x", None, [("Lifetime", "2h")],
                  "GlassMacro 1.0 · run #3")
check(e["color"] == 0x5ECBFF and e["footer"]["text"].endswith("run #3")
      and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", e["timestamp"]),
      "embed: start colour, footer, UTC timestamp")
p1 = G.build_payload([e])
p2 = G.build_payload([e], [USER])
p3 = G.build_payload([e], ["@everyone", "abc"])
check(all(p["allowed_mentions"]["parse"] == [] for p in (p1, p2, p3)),
      "allowed_mentions.parse is always empty")
check(p1["content"] == "" and "users" not in p1["allowed_mentions"]
      and p2["content"] == f"<@{USER}>"
      and p2["allowed_mentions"]["users"] == [USER]
      and p3["content"] == "" and "users" not in p3["allowed_mentions"],
      "a mention only for a real user id, and only that user is allowed")
check(p1["username"] == "GlassMacro", "posts as GlassMacro")


# ---- the sender, with a fake transport ---------------------------------------
class Resp:
    def __init__(self, code):
        self.status = code

    def close(self):
        pass


class FakeTransport:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, req, timeout):
        self.calls.append((req, timeout))
        a = self.answers.pop(0) if self.answers else 204
        if isinstance(a, BaseException):
            raise a
        if isinstance(a, tuple):                  # (code, body)
            code, body = a
        else:
            code, body = a, b""
        if 200 <= code < 300:
            return Resp(code)
        raise urllib.error.HTTPError(req.full_url, code, "x", {},
                                     io.BytesIO(body))

    def payloads(self):
        return [json.loads(r.data.decode("utf-8")) for r, _ in self.calls]


def sender(answers, log=None):
    ft = FakeTransport(answers)
    sleeps = []
    lines = [] if log is None else None
    s = G.WebhookSender(URL, transport=ft, sleep=sleeps.append,
                        log=log or lines.append)
    s._start = lambda: None                       # no thread: pump() by hand
    return s, ft, sleeps, lines


def ev(title="t", mention=None, desc="d"):
    return {"embed": G.build_embed("start", title, desc), "mention": mention}


ALL_LINES = []
saved = os.environ.pop("GLASSMACRO_NO_SEND")
try:
    s, ft, sleeps, lines = sender([204])
    s.enqueue(ev())
    out = s.pump()
    req = ft.calls[0][0] if ft.calls else None
    check(out == ["ok"] and len(ft.calls) == 1 and req.get_method() == "POST"
          and req.full_url == URL and ft.calls[0][1] == 10
          and req.get_header("User-agent", "").startswith(
              f"GlassMacro/{G.APP_VER} (+https://github.com/Ariliux/GlassMacro)")
          and req.get_header("Content-type") == "application/json"
          and not lines,
          "204: one POST with the User-Agent and JSON header, nothing logged")

    s, ft, sleeps, lines = sender([(429, b'{"retry_after": 1.5}'), 204])
    s.enqueue(ev())
    out = s.pump()
    check(out == ["ok"] and len(ft.calls) == 2 and 1.75 in sleeps,
          f"429: waits retry_after + 0.25 s, then sends ({sleeps})")
    s, ft, sleeps, lines = sender([(429, b'{"retry_after": 0.1}')] * 9)
    s.enqueue(ev())
    check(s.pump() == ["dropped"] and len(ft.calls) == 4,
          "429 four times: gives up after 3 waits")

    s, ft, sleeps, lines = sender([503, urllib.error.URLError("x"), 204])
    s.enqueue(ev())
    out = s.pump()
    ALL_LINES += lines
    check(out == ["ok"] and 2 in sleeps and 5 in sleeps
          and lines == ["webhook: couldn't reach Discord - will retry",
                        "webhook: reaching Discord again"],
          f"503 / no network: backs off 2 s, 5 s, logs only the two changes "
          f"({lines})")
    s, ft, sleeps, lines = sender([503] * 9)
    s.enqueue(ev())
    check(s.pump() == ["dropped"] and len(ft.calls) == 6
          and [x for x in sleeps if x in (2, 5, 15, 30, 60)]
          == [2, 5, 15, 30, 60] and len(lines) == 1,
          "an outage: 2/5/15/30/60 s back-off, then the batch is dropped")

    s, ft, sleeps, lines = sender([404, 204])
    s.enqueue(ev())
    out = s.pump()
    ALL_LINES += lines
    check(out == ["dead"] and s.dead and len(ft.calls) == 1
          and len(lines) == 1 and "(404)" in lines[0]
          and TOKEN not in lines[0] and HOOK_ID not in lines[0],
          f"404: dead after one log line, no link in it ({lines})")
    check(s.enqueue(ev()) is False and s.pump() == [] and len(ft.calls) == 1,
          "...and nothing more is sent this session")
    for code in (401, 403):
        s, ft, sleeps, lines = sender([code])
        s.enqueue(ev())
        s.pump()
        check(s.dead, f"{code}: the link is dead too")
    s, ft, sleeps, lines = sender([400, 204])
    s.enqueue(ev())
    check(s.pump() == ["dropped"] and not s.dead and len(ft.calls) == 1,
          "400: that message is dropped, the link stays alive")

    s, ft, sleeps, lines = sender([ValueError("odd"), OSError("boom"),
                                   TimeoutError(), RuntimeError()] + [503] * 9)
    s.enqueue(ev())
    try:
        s.pump()
        check(True, "exceptions from the transport are swallowed")
    except Exception as exc:
        check(False, f"exceptions from the transport are swallowed ({exc!r})")

    s, ft, sleeps, lines = sender([204, 204])
    for i in range(5):
        s.enqueue(ev(f"e{i}"))
    s.pump()
    check(len(ft.calls) == 1 and len(ft.payloads()[0]["embeds"]) == 5,
          "batching: 5 events become 1 POST")
    s, ft, sleeps, lines = sender([204] * 5)
    for i in range(12):
        s.enqueue(ev(f"e{i}"))
    s.pump()
    check([len(p["embeds"]) for p in ft.payloads()] == [10, 2],
          "at most 10 embeds a POST")
    s, ft, sleeps, lines = sender([204] * 5)
    for i in range(3):
        s.enqueue(ev(f"e{i}", desc="x" * 2500))
    s.pump()
    check([len(p["embeds"]) for p in ft.payloads()] == [2, 1],
          "at most 6000 characters a POST")
    s, ft, sleeps, lines = sender([204] * 3)
    s.enqueue(ev("a"))
    s.enqueue(ev("b", mention=USER))
    s.pump()
    p = ft.payloads()[0]
    check(p["allowed_mentions"]["parse"] == [] and p["content"] == f"<@{USER}>"
          and p["allowed_mentions"]["users"] == [USER],
          "a batch with one mention event mentions that user once")
    s, ft, sleeps, lines = sender([204, 204])
    s.enqueue(ev())
    s.pump()
    s.enqueue(ev())
    s.pump()
    check(any(abs(x - G.HOOK_GAP) < 0.5 for x in sleeps),
          f"posts are paced {G.HOOK_GAP} s apart ({sleeps})")

    s, ft, sleeps, lines = sender([204])
    check(s.post_now(G.build_payload([ev()["embed"]])) == "ok"
          and lines == ["webhook: test message sent"],
          "Send test: one post, one line")
    ALL_LINES += lines
finally:
    os.environ["GLASSMACRO_NO_SEND"] = saved

# overflow: periodic goes first, then milestones, then the rest
s, ft, sleeps, lines = sender([])
for i in range(20):
    s.enqueue(ev(f"p{i}"), G.PRI_PERIODIC)
for i in range(10):
    s.enqueue(ev(f"m{i}"), G.PRI_MILESTONE)
pri = lambda: [p for p, _ in list(s._q.queue)]               # noqa: E731
check(s._q.full() and s.enqueue(ev("n0"), G.PRI_NORMAL)
      and pri().count(G.PRI_PERIODIC) == 19 and s.dropped == 1
      and s._q.qsize() == 30,
      "full queue: a normal event pushes out the oldest periodic one")
titles = [e["embed"]["title"] for _, e in list(s._q.queue)]
check("p0" not in titles and "p1" in titles, "...the OLDEST periodic one")
for i in range(19):
    s.enqueue(ev(f"n{i + 1}"), G.PRI_NORMAL)
check(pri().count(G.PRI_PERIODIC) == 0
      and pri().count(G.PRI_MILESTONE) == 10, "...periodic all gone first")
s.enqueue(ev("n20"), G.PRI_NORMAL)
check(pri().count(G.PRI_MILESTONE) == 9, "...then milestones")
check(s.enqueue(ev("late"), G.PRI_PERIODIC) is False and s.dropped == 22
      and s._q.qsize() == 30,
      "a periodic event into a queue of more important ones is dropped, "
      "never blocking")

# the kill switch: with GLASSMACRO_NO_SEND nothing is even attempted
s, ft, sleeps, lines = sender([204])
s.enqueue(ev())
check(s.pump() == ["blocked"] and not ft.calls,
      "GLASSMACRO_NO_SEND blocks every post before the transport")

# ---- the app: a fake run can never send or count -----------------------------
app = G.GlassMacro()
app.withdraw()
app.update()
check(app.settings["webhook"]["enabled"] is False
      and app.settings["webhook"]["events"]["hourly"] is True
      and app.settings["webhook"]["events"]["recover"] is False,
      "webhook defaults: off, hourly on, recoveries off")
wh = app.settings["webhook"]
wh.update(enabled=True, url=URL, user_id=USER)
for k in wh["events"]:
    wh["events"][k] = True
for k in wh["mention"]:
    wh["mention"][k] = True
# exactly what render_states.py does: attributes and log lines, no toggle_run
app.running = True
app._status("RUNNING", G.GREEN)
app.session_start = time.time() - 7300
app.log("started - F8 stops it")
for needle, _key in G.GlassMacro.EVENT_RULES:
    app.log(needle + " 12 (fake)")
app._paused_since = time.time() - 700
for _ in range(3):
    app._tick()
    app.update()
app.running, app.session_start = False, None
app._status("IDLE", G.MUTED)
app.log("stopped")
app._tick()
end = time.time() + 0.5
while time.time() < end:
    app.update()
    time.sleep(0.02)
stats_file = os.path.join(G.DATA_DIR, "stats.json")
runs = json.load(open(stats_file))["runs"] if os.path.exists(stats_file) else 0
check(not NET and app._sender is None and not app._live_run,
      f"a fake run with alerts on: 0 network calls ({len(NET)}), no sender")
check(runs == 0 and app._stats["runs"] == 0
      and all(app._stats[k] == 0 for k in G.STATS_COUNTERS)
      and app._stats["total_secs"] == 0,
      "...and nothing counted in stats.json")

# no echo: the sender's own lines must not move the card, feed or events
check(len(set(ALL_LINES)) == 4, f"all four webhook lines seen ({ALL_LINES})")
needles = [row[0] for t in ("STATUS_RULES", "FEED_RULES", "EVENT_RULES")
           for row in getattr(G.GlassMacro, t)]
check(all(line.startswith("webhook: ") and line not in ("stopped", "cancelled")
          and not any(n in line for n in needles) for line in ALL_LINES),
      "no webhook: line contains any STATUS/FEED/EVENT needle")
title, rows = app._state_title, app._feed_rows
for line in ALL_LINES:
    app.log(line)
check(app._state_title == title and app._feed_rows == rows,
      "...so logging them changes neither the card nor the feed")
log_text = open(G.LOG_PATH, encoding="utf-8").read()
check(TOKEN not in log_text and URL not in log_text,
      "log.txt never contains the link")

# ---- the real path, still offline: a Start, events, an error, the end ------
# (_run_started is what toggle_run calls after starting the worker; the real
# worker is never started here - a thread that just waits stands in for it)
gate = threading.Event()
app._worker = threading.Thread(target=gate.wait, daemon=True)
app._worker.start()
s, ft, sleeps, _ = sender([204] * 5, log=app.log)
app._sender = s
wh["events"]["recover"] = False
app.n_picks = app.n_joins = 0
app.running, app.session_start = True, time.time()
app._run_started()
saved_stats = json.load(open(stats_file))
check(app._live_run and saved_stats["runs"] == 1
      and saved_stats["current"] is not None,
      "a real Start counts the run and saves it straight away")
app.log("Roblox keeps closing - not reopening it again this hour")
app.log("connection dialog - clicking the right-hand button at (1, 2)")
app.n_picks, app.n_joins = 4, 2
app.session_start = time.time() - 3700
app._paused_since = time.time() - 601
app._live_tick(time.time())
app.log(f"stopped on an error: bad file C:\\Users\\{os.environ.get('USERNAME', 'x')}"
        f"\\a.png {TOKEN}")
app.running = False
app._run_ended(time.time(), ran=3700)
queued = [e for _, e in list(s._q.queue)]
titles = [e["embed"]["title"] for e in queued]
check(titles == ["Run started", "Updated", "Roblox kept closing",
                 "1h of playtime",
                 "Paused for 10 minutes", "Stopped by an error"],
      f"alerts queued: start, the update (logged before the run), stuck, "
      f"hourly, paused, error - not the "
      f"recovery that is switched off ({titles})")
mentioned = [e["embed"]["title"] for e in queued if e["mention"]]
check(mentioned == ["Roblox kept closing", "Paused for 10 minutes",
                    "Stopped by an error"],
      f"mentions only on problems ({mentioned})")
blob = json.dumps(queued)
user = os.environ.get("USERNAME") or "\x00"
pc = os.environ.get("COMPUTERNAME") or "\x00"
check(TOKEN not in blob and "C:\\\\" not in blob and user not in blob
      and pc not in blob and "[path]" in blob,
      "no token, path, user name or PC name in what would be sent")
check(all(e["embed"]["footer"]["text"] == f"GlassMacro {G.APP_VER} \u00b7 run #1"
          for e in queued), "every footer is 'GlassMacro <ver> · run #1'")
st = json.load(open(stats_file))
check(st["runs"] == 1 and st["current"] is None and st["errors"] == 1
      and st["reopen_giveups"] == 1 and st["reconnects"] == 1
      and st["loadouts"] == 4 and st["rejoins"] == 2
      and not app._live_run,
      "the end: final stats saved, counters and loadouts/rejoins added")
saved = os.environ.pop("GLASSMACRO_NO_SEND")
try:
    out = s.pump()
finally:
    os.environ["GLASSMACRO_NO_SEND"] = saved
p = ft.payloads()
check(out == ["ok"] and len(p) == 1 and len(p[0]["embeds"]) == 6
      and p[0]["allowed_mentions"] == {"parse": [], "users": [USER]},
      "they go out as one batched POST (fake transport)")
check(not NET, "still 0 real network calls")
gate.set()

# after the run: alerts are disarmed again
s2, ft2, _, _ = sender([204])
app._sender = s2
app.log("Roblox keeps closing - not reopening it again this hour")
check(s2._q.empty(), "after the run ends nothing is queued")
app.destroy()

# ---- stats.json ---------------------------------------------------------------
d = tempfile.mkdtemp(prefix="glass_stats_")
path = os.path.join(d, "stats.json")
a = G.new_stats()
a["runs"] = 1
check(G.save_stats(a, path) and json.load(open(path))["runs"] == 1
      and not os.path.exists(path + ".tmp"),
      "save: written whole, no .tmp left behind")
a["runs"] = 2
G.save_stats(a, path)
check(json.load(open(path + ".bak"))["runs"] == 1
      and json.load(open(path))["runs"] == 2,
      "save: the previous file is kept as .bak")
open(path, "w").write('{"runs": 5, "tot')                    # torn write
b = G.load_stats(path)
check(b["runs"] == 1 and not os.path.exists(path)
      and len(glob.glob(path + ".corrupt-*")) == 1,
      "damaged stats.json: loads .bak, the bad file is renamed, not deleted")
G.save_stats(b, path)
check(json.load(open(path + ".bak"))["runs"] == 1,
      "...and the good .bak is not overwritten by the damaged file")
for f in glob.glob(os.path.join(d, "*")):
    os.remove(f)
open(path, "w").write("[1, 2]")
check(G.load_stats(path) == G.new_stats(),
      "not a stats file and no .bak: a fresh start")
for f in glob.glob(os.path.join(d, "*")):
    os.remove(f)
check(G.load_stats(path) == G.new_stats() and not os.path.exists(path),
      "no file at all: fresh, and nothing written")
c = G.new_stats()
c.update(longest_secs=100, longest_on="2026-01-01",
         current={"start": "2026-10-01 10:00", "secs": 5000, "flushed_at": 1})
G.save_stats(c, path)
r = G.load_stats(path)
check(r["longest_secs"] == 5000 and r["longest_on"] == "2026-10-01"
      and r["unclean_ends"] == 1 and r["current"] is None,
      "a run that never ended still counts as the longest, as an unclean end")
check(G.load_stats(path)["unclean_ends"] == 1,
      "...once - the recovery is saved")
r = G._clean_stats({"runs": "lots", "days": {
    "2026-10-01": 5, "junk": 3, "2026-10-02": "x"}, "total_secs": -4,
    "longest_on": 7, "current": "?"})
check(r["runs"] == 0 and r["days"] == {"2026-10-01": 5}
      and r["total_secs"] == 0 and r["longest_on"] == ""
      and r["current"]["secs"] == 0, "wrong types fall back to the defaults")
many = G.new_stats()
many["days"] = {f"2026-{m:02d}-{dd:02d}": 1 for m in (7, 8, 9)
                for dd in range(1, 29)}
G.save_stats(many, path)
kept = json.load(open(path))["days"]
check(len(kept) == 60 and min(kept) == "2026-07-25" and "2026-09-28" in kept,
      "only the newest 60 days are kept")

midnight = time.mktime((2026, 10, 6, 0, 0, 0, 0, 0, -1))
m = G.new_stats()
G.stats_add_time(m, midnight - 2, midnight + 3)
check(m["days"] == {"2026-10-05": 2, "2026-10-06": 3}
      and m["total_secs"] == 5, "time across midnight is split between days")
fake = types.SimpleNamespace(_stats=G.new_stats(), _flushed_at=0,
                             n_picks=0, n_joins=0, _flushed_picks=0,
                             _flushed_joins=0, _stats_saved_at=0)
now = time.time()
fake._flushed_at = now - 3600
G.GlassMacro._stats_flush(fake, now)
check(abs(fake._stats["total_secs"] - 5) < 1e-3,
      "a tick after a long gap adds at most 5 s")
fake._flushed_at = now + 500
G.GlassMacro._stats_flush(fake, now)
check(abs(fake._stats["total_secs"] - 5) < 1e-3 and fake._flushed_at == now,
      "a clock that went backwards adds nothing")
check(not NET, "0 network calls in the whole test")

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
