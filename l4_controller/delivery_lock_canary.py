#!/usr/bin/env python3
"""Canary for transport.py's per-target delivery lock
(SPEC-orchestration.md SS0.3, second half -- "serialize deliveries per
target session"). Proves two properties about the REAL deliver() locking
wrapper (not a reimplementation of it), without touching a real tmux
session:

  1. Two concurrent deliver() calls to the SAME target never run their
     bodies overlapping -- the exact scenario that can interleave
     keystrokes into one pane (a /btw racing a queued instruction).
  2. Two concurrent deliver() calls to DIFFERENT targets are NOT
     serialized against each other -- the lock is per-target, not a
     single global choke point that would needlessly cost throughput
     across unrelated sessions.

Monkeypatches TmuxTransport._deliver_locked (everything deliver() calls
after acquiring the per-target lock) with an instrumented stand-in that
records overlap and sleeps briefly -- long enough that two genuinely
concurrent calls would actually collide and get caught, the same
technique speech_queue_canary.py uses for say_feedback's worker. The real
deliver() method (and its locking) is untouched and is what's under test.

MUST run via l4_controller/.venv (2026-08-20: say_feedback.py now imports
kokoro_tts at module load, transitively pulled in through transport.py).

Run after touching transport.py's deliver()/_lock_for_target:

    l4_controller/.venv/bin/python3 l4_controller/delivery_lock_canary.py
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import transport as tp  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((name, passed, detail))
    print(f"  {'ok  ' if passed else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not passed else ""))


# --- Instrumented fake delivery body: records per-target overlap --------
_active_by_target: dict[str, int] = {}
_max_concurrent_by_target: dict[str, int] = {}
_lock_for_tracking = threading.Lock()
_call_order: list[tuple[str, float]] = []  # (target, start time)


def _fake_deliver_locked(self, target: str, payload: str):
    with _lock_for_tracking:
        _active_by_target[target] = _active_by_target.get(target, 0) + 1
        _max_concurrent_by_target[target] = max(
            _max_concurrent_by_target.get(target, 0), _active_by_target[target]
        )
        _call_order.append((target, time.monotonic()))
    time.sleep(0.2)  # long enough that real overlap would be caught
    with _lock_for_tracking:
        _active_by_target[target] -= 1
    return tp.DeliveryResult(ok=True, detail=f"fake delivered to {target}")


def run() -> int:
    print("delivery per-target lock canary\n")
    tp.TmuxTransport._deliver_locked = _fake_deliver_locked
    transport = tp.TmuxTransport()

    # --- 1. Same target: 5 concurrent deliver() calls, never overlapping --
    print("1. concurrent deliver() calls to the SAME target never overlap")
    _active_by_target.clear()
    _max_concurrent_by_target.clear()
    _call_order.clear()
    threads = [
        threading.Thread(target=transport.deliver, args=("claude-same-target", f"payload-{i}"))
        for i in range(5)
    ]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    elapsed = time.monotonic() - t0
    check(
        "max concurrent deliveries to the same target was 1",
        _max_concurrent_by_target.get("claude-same-target") == 1,
        detail=f"got {_max_concurrent_by_target.get('claude-same-target')} -- this is the keystroke-interleaving bug",
    )
    check(
        "5 deliveries to one target took roughly 5x0.2s serialized, not ~0.2s parallel",
        elapsed >= 5 * 0.2 * 0.8,  # 80% margin for scheduling slack
        detail=f"took {elapsed:.2f}s -- too fast to have been serialized",
    )

    # --- 2. Different targets: NOT serialized against each other ----------
    print("\n2. concurrent deliver() calls to DIFFERENT targets run in parallel (lock is per-target)")
    _active_by_target.clear()
    _max_concurrent_by_target.clear()
    _call_order.clear()
    targets = [f"claude-target-{i}" for i in range(4)]
    threads = [threading.Thread(target=transport.deliver, args=(target, "payload")) for target in targets]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    elapsed = time.monotonic() - t0
    check(
        "each distinct target independently saw exactly 1 concurrent delivery (no cross-target interleaving)",
        all(_max_concurrent_by_target.get(t) == 1 for t in targets),
        detail=f"per-target max: { {t: _max_concurrent_by_target.get(t) for t in targets} }",
    )
    check(
        "4 deliveries to 4 DIFFERENT targets ran in parallel (~0.2s), not serialized (~0.8s)",
        elapsed < 0.6,
        detail=f"took {elapsed:.2f}s -- looks serialized, meaning the lock is wrongly global rather than per-target",
    )

    # --- 3. The lock registry itself is safe to build under a race --------
    print("\n3. _lock_for_target never hands out two different Lock objects for the same key under a race")
    tp._target_locks.clear()
    tp._target_locks.pop("claude-race-target", None)
    got_locks: list[object] = []

    def _grab():
        got_locks.append(tp._lock_for_target("claude-race-target"))

    threads = [threading.Thread(target=_grab) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check(
        "all 20 racing lookups returned the SAME lock instance",
        len(set(id(lock) for lock in got_locks)) == 1,
        detail=f"got {len(set(id(lock) for lock in got_locks))} distinct lock objects for one target",
    )

    print()
    failures = [r for r in RESULTS if not r[1]]
    if failures:
        print(f"{len(failures)}/{len(RESULTS)} FAILED:")
        for name, _, detail in failures:
            print(f"  - {name}" + (f" ({detail})" if detail else ""))
        return 1
    print(f"all {len(RESULTS)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(run())
