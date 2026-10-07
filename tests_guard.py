"""Keeps the tests from taking the keyboard away from whoever is using the PC.

Tk takes the foreground when a process maps its first window - even a
withdrawn, never-shown one - whenever Windows' foreground lock has lapsed
(nobody has typed or clicked for a while). An invisible test window could
then hold keyboard focus for the whole test, swallowing typing and pausing a
real GlassMacro run that is going at the same time.

start() runs a daemon thread that checks every 15 ms and, if a window of this
process is in front, hands the foreground straight back to the last window
that wasn't ours. It never touches any other process's windows except to give
that one its foreground back. It sends no input and makes no network calls.

Each test file calls tests_guard.start() before it creates its first window.
Set TESTS_GUARD_REPORT=1 to print how often it had to step in.
"""
import atexit
import ctypes
import os
import sys
import threading
import time

TOOK = []                       # monotonic times the foreground was handed back
_started = []

if sys.platform == "win32":
    _u32 = ctypes.windll.user32
else:                           # nothing to guard off Windows
    _u32 = None


def _ours(h):
    pid = ctypes.c_ulong()
    _u32.GetWindowThreadProcessId(h, ctypes.byref(pid))
    return bool(h) and pid.value == os.getpid()


def check(last):
    h = _u32.GetForegroundWindow()
    if not h:
        return
    if not _ours(h):
        last[0] = h
        return
    TOOK.append(time.monotonic())
    if last[0] and _u32.IsWindow(last[0]):
        _u32.SetForegroundWindow(last[0])


def _loop(period, last, sleep, check_fn):
    while True:
        try:
            check_fn(last)
        except Exception:
            pass
        sleep(period)


def start(period=0.015):
    """Start the guard once per process. Safe to call more than once."""
    if _started or _u32 is None:
        return
    _started.append(1)
    last = [_u32.GetForegroundWindow()]
    # bind time.sleep and Thread now: some tests later replace module-wide
    # names (threading.Thread, time.time) and the guard must not notice.
    threading.Thread(target=_loop, args=(period, last, time.sleep, check),
                     daemon=True, name="tests_guard").start()
    if os.environ.get("TESTS_GUARD_REPORT"):
        atexit.register(lambda: sys.stderr.write(
            f"[tests_guard] handed the foreground back {len(TOOK)} time(s)\n"))
