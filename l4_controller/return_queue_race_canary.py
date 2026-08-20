#!/usr/bin/env python3
"""Canary for R3 (docs/PLAN-silence-and-ux.md SS1): one flusher, enforced
ACROSS PROCESSES, not just across threads.

WHY THIS NEEDS REAL, SEPARATE OS PROCESSES, NOT THREADS. The bug this
fixes is specifically that jarvis_say is registered on BOTH MCP surfaces
(server.py and server_readonly.py) -- the concierge and the router are
two separate Python interpreters, each with their own copy of
return_queue's module-level `_lock` (a threading.RLock, which cannot see
across a process boundary at all). A canary using threading.Thread inside
one process would never exercise the actual failure mode: it would only
prove `_lock` works, which nobody doubted. This spawns real
subprocess.Popen() workers specifically so the cross-process
fcntl.flock() in _cross_process_lock() is the ONLY thing standing between
them and a genuine race.

TWO INDEPENDENT PROOFS, not one:
  1. Content: exactly one spoken utterance for one queued item -- proves
     the OUTCOME was correct.
  2. Timing: no two processes' lock-held intervals overlap, from their
     own acquire/release timestamps -- proves mutual exclusion actually
     held structurally, not that this particular run happened to get
     lucky with scheduling. A canary that only checks (1) could pass on
     a flaky run where the race never actually triggered.

Runs under JARVIS_TEST_RUN + JARVIS_NO_RETURN_QUEUE_WORKER=1 in the
PARENT -- the parent process only seeds one item and starts the race, it
never becomes a fourth competing flusher itself.

MUST run via l4_controller/.venv (needs numpy/onnxruntime/kokoro_onnx
transitively, same as every other say_feedback-touching canary):

    l4_controller/.venv/bin/python3 l4_controller/return_queue_race_canary.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

os.environ["JARVIS_TEST_RUN"] = f"return-queue-race-canary-{uuid.uuid4().hex[:8]}"
os.environ["JARVIS_NO_RETURN_QUEUE_WORKER"] = "1"

sys.path.insert(0, str(Path(__file__).parent))
import return_queue as rq  # noqa: E402
from jarvis_paths import jarvis_home  # noqa: E402

assert "test_runs" in str(rq.QUEUE_PATH), f"NOT ISOLATED: {rq.QUEUE_PATH}"

FAILURES: list[str] = []


def check(desc: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ok    {desc}")
    else:
        FAILURES.append(desc)
        print(f"  FAIL  {desc}{('  -- ' + detail) if detail else ''}")


N_RACERS = 4
WORKDIR = Path("/tmp/jarvis-return-queue-race-canary")


def run() -> int:
    WORKDIR.mkdir(parents=True, exist_ok=True)
    barrier_path = WORKDIR / "barrier"
    spoken_log_path = WORKDIR / "spoken.log"
    lock_trace_path = WORKDIR / "lock_trace.log"
    for p in (barrier_path, spoken_log_path, lock_trace_path):
        p.unlink(missing_ok=True)

    print(f"seeding one item, launching {N_RACERS} real subprocesses to race flush_now() on it\n")
    rq._write([])
    rq.enqueue("completion", "the one item every racer is competing to flush")
    check("setup: exactly one item queued before the race starts", len(rq.pending()) == 1)

    worker_script = str(Path(__file__).parent / "_return_queue_race_worker.py")
    procs = [
        subprocess.Popen(
            [sys.executable, worker_script, str(barrier_path), str(spoken_log_path), str(lock_trace_path)],
            env=os.environ.copy(),
        )
        for _ in range(N_RACERS)
    ]

    # Give every racer time to reach its own barrier-wait loop BEFORE
    # firing the starting gun -- if the barrier existed before some
    # racers even started, this would just measure process-launch
    # jitter, not a genuine simultaneous race.
    time.sleep(0.5)
    barrier_path.write_text("go")

    for p in procs:
        rc = p.wait(timeout=15)
        check(f"racer pid={p.pid} exited cleanly (rc=0)", rc == 0, f"rc={rc}")

    spoken_lines = spoken_log_path.read_text().splitlines() if spoken_log_path.exists() else []
    check(
        "EXACTLY ONE racer spoke the item -- not zero (lost), not 2+ (duplicated)",
        len(spoken_lines) == 1,
        f"spoken {len(spoken_lines)}x: {spoken_lines}",
    )
    check(
        "the queue is empty afterward -- the item was actually taken, not just raced over",
        rq.pending() == [],
        str(rq.pending()),
    )

    # --- The structural proof: no two racers' lock-held windows overlap ---
    print("\nverifying mutual exclusion directly from acquire/release timestamps")
    trace_lines = lock_trace_path.read_text().splitlines() if lock_trace_path.exists() else []
    events = []
    for line in trace_lines:
        pid_s, kind, t_s = line.split()
        events.append((int(pid_s), kind, float(t_s)))

    intervals = []  # (pid, acquire_t, release_t)
    open_acquire: dict[int, float] = {}
    malformed = False
    for pid, kind, t in events:
        if kind == "acquire":
            if pid in open_acquire:
                malformed = True
            open_acquire[pid] = t
        elif kind == "release":
            if pid not in open_acquire:
                malformed = True
                continue
            intervals.append((pid, open_acquire.pop(pid), t))
    check(
        "every acquire has exactly one matching release, per racer (no reentrant/unbalanced trace)",
        not malformed and not open_acquire,
        f"malformed={malformed} still_open={open_acquire}",
    )
    check(
        f"all {N_RACERS} racers actually attempted the critical section (got {len(intervals)} intervals)",
        len(intervals) == N_RACERS,
        str(intervals),
    )

    overlaps = []
    for i in range(len(intervals)):
        for j in range(i + 1, len(intervals)):
            pid_a, a_start, a_end = intervals[i]
            pid_b, b_start, b_end = intervals[j]
            if a_start < b_end and b_start < a_end:
                overlaps.append((pid_a, pid_b, (a_start, a_end), (b_start, b_end)))
    check(
        "NO two racers' lock-held windows overlap in time -- genuine mutual exclusion, not a lucky run",
        overlaps == [],
        f"overlapping pairs: {overlaps}",
    )

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        result = 1
    else:
        print("all checks passed")
        result = 0

    import shutil
    shutil.rmtree(WORKDIR, ignore_errors=True)
    for isolated_path in (jarvis_home(),):
        if "test_runs" in str(isolated_path):
            shutil.rmtree(isolated_path, ignore_errors=True)
    return result


if __name__ == "__main__":
    sys.exit(run())
