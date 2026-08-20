#!/usr/bin/env python3
"""Test helper for return_queue_race_canary.py, NOT a canary itself and
NOT production code -- deliberately underscore-prefixed so it never
looks like one. A real, separate OS process (not a thread): waits at a
shared barrier file, then races every other copy of itself to call
return_queue.flush_now() at the same instant, logging its own lock
acquire/release timestamps and every "spoken" text to shared files so
the parent canary can verify mutual exclusion actually held.

Usage: _return_queue_race_worker.py <barrier_path> <spoken_log_path> <lock_trace_path>
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import return_queue as rq  # noqa: E402
import say_feedback as sf  # noqa: E402

barrier_path = Path(sys.argv[1])
spoken_log_path = Path(sys.argv[2])
lock_trace_path = Path(sys.argv[3])


def _fake_speak(text, priority=1):
    # flush_now() does `from say_feedback import speak` INSIDE its own
    # body on every call (a deliberate local import, per that function's
    # own module) -- so patching the ATTRIBUTE on the say_feedback module
    # object before flush_now() ever runs is enough; the local import
    # resolves it fresh each call, same mechanism the existing
    # return_queue_canary.py's mock.patch("say_feedback.speak") relies on.
    with open(spoken_log_path, "a") as f:
        f.write(text + "\n")


sf.speak = _fake_speak

real_lock = rq._cross_process_lock


def _traced_lock():
    cm = real_lock()

    class _Traced:
        def __enter__(self):
            cm.__enter__()
            with open(lock_trace_path, "a") as f:
                f.write(f"{os.getpid()} acquire {time.monotonic()}\n")
            return self

        def __exit__(self, *exc):
            with open(lock_trace_path, "a") as f:
                f.write(f"{os.getpid()} release {time.monotonic()}\n")
            return cm.__exit__(*exc)

    return _Traced()


rq._cross_process_lock = _traced_lock

# Wait at the barrier -- every copy of this script spins here until the
# parent canary creates barrier_path, which is the starting gun. Busy-wait
# with a short sleep rather than blocking on file creation via inotify/
# fsevents: this is a test helper, not something that needs to be
# elegant, and the poll interval (1ms) is far tighter than anything that
# would blur the race.
while not barrier_path.exists():
    time.sleep(0.001)

rq.flush_now()
