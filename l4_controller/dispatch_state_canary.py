#!/usr/bin/env python3
"""Canary for dispatch_state.py's keyed-by-dictation shape
(SPEC-orchestration.md SS0.1). Same purpose and discipline as
pane_state_canary.py / chat_gate_canary.py: this file's whole reason to
exist is a correctness property that would otherwise fail silently --
if two dictations really can complete against each other's records, the
very first symptom is Ayman being told something finished when it didn't,
which is indistinguishable from success until it bites him.

Uses JARVIS_TEST_RUN (jarvis_paths.py) to isolate its state file from any
real ~/.jarvis/dispatch_state.json and from any other engineer's
concurrent test run -- never touches production or shared state. Pure
file-state logic, no tmux, no model, no audio -- fast and hermetic.

Run after touching dispatch_state.py, server.py's deliver_batch /
report_dispatch_stage, l2_l3_handoff.py's mark_dispatch_forwarded call, or
poller.py's _safe_to_speak:

    python3 l4_controller/dispatch_state_canary.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

# Set before importing dispatch_state: DISPATCH_STATE_PATH is resolved
# once at import time from jarvis_home(), same reason chat_gate_canary.py
# sets JARVIS_MUTE before importing concierge.
os.environ["JARVIS_TEST_RUN"] = f"dispatch-state-canary-{uuid.uuid4().hex[:8]}"

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import dispatch_state as ds  # noqa: E402
from jarvis_paths import jarvis_home  # noqa: E402


class Result:
    def __init__(self, name: str, passed: bool, detail: str = ""):
        self.name = name
        self.passed = passed
        self.detail = detail


RESULTS: list[Result] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append(Result(name, passed, detail))
    print(f"  {'ok  ' if passed else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not passed else ""))


def reset_state() -> None:
    if ds.DISPATCH_STATE_PATH.exists():
        ds.DISPATCH_STATE_PATH.unlink()


def run() -> int:
    print("dispatch_state.py canary")
    print(f"  isolated state file: {ds.DISPATCH_STATE_PATH}\n")
    reset_state()

    # --- 1. Two concurrent in-flight dispatches, forwarded independently ---
    print("1. two dictations forwarded concurrently")
    ref_a = "/tmp/dictations/canary-a.txt"
    ref_b = "/tmp/dictations/canary-b.txt"
    ds.mark_dispatch_forwarded(ref_a)
    ds.mark_dispatch_forwarded(ref_b)

    state_a = ds.dispatch_state(ref_a)
    state_b = ds.dispatch_state(ref_b)
    check("A is forwarded", state_a is not None and state_a["stage"] == "forwarded")
    check("B is forwarded", state_b is not None and state_b["stage"] == "forwarded")
    check("any_forwarded() is True with both in flight", ds.any_forwarded())

    # --- 2. THE core property: completing A must not touch B -------------
    print("\n2. completing A must not close out B (the bug this file exists to prevent)")
    ds.mark_dispatch_complete(ref_a, {"count": 1, "failures": 0})

    state_a = ds.dispatch_state(ref_a)
    state_b = ds.dispatch_state(ref_b)
    check("A is now complete", state_a is not None and state_a["stage"] == "complete")
    check(
        "B is STILL forwarded (not silently closed out by A's completion)",
        state_b is not None and state_b["stage"] == "forwarded",
        detail=f"B's stage is actually {state_b.get('stage') if state_b else None!r}",
    )
    check("any_forwarded() is still True (B alone is in flight)", ds.any_forwarded())

    # --- 3. Completing B closes it out too, independently -----------------
    print("\n3. completing B closes it out too, without touching A's result")
    ds.mark_dispatch_complete(ref_b, {"count": 3, "failures": 1})
    state_a = ds.dispatch_state(ref_a)
    state_b = ds.dispatch_state(ref_b)
    check("A's result_summary is still A's own (count=1)", state_a["result_summary"]["count"] == 1)
    check("B's result_summary is B's own (count=3), not A's", state_b["result_summary"]["count"] == 3)
    check("any_forwarded() is False once both are complete", not ds.any_forwarded())

    # --- 4. No-ref dispatch_state() returns the MOST RECENT, not either --
    print("\n4. dispatch_state() with no ref returns the most recent dispatch")
    latest = ds.dispatch_state()
    check(
        "latest is B (forwarded after A)",
        latest is not None and latest["dictation_ref"] == ref_b,
        detail=f"got {latest.get('dictation_ref') if latest else None!r}",
    )

    # --- 5. all_dispatches() sees both, independently ----------------------
    print("\n5. all_dispatches() enumerates every dictation, not just one")
    all_d = ds.all_dispatches()
    check("all_dispatches() has exactly 2 entries", len(all_d) == 2, detail=f"got {list(all_d)}")

    # --- 6. Legitimate no-matching-record completion (test harness case) --
    print("\n6. completing a ref with no prior forwarded record still records it, scoped to itself")
    reset_state()
    ds.mark_dispatch_forwarded(ref_a)
    ref_c = "/tmp/dictations/canary-c-never-forwarded.txt"
    ds.mark_dispatch_complete(ref_c, {"count": 1, "failures": 0})
    state_a = ds.dispatch_state(ref_a)
    state_c = ds.dispatch_state(ref_c)
    check(
        "A (still genuinely in flight) is UNTOUCHED by C's stray completion",
        state_a is not None and state_a["stage"] == "forwarded",
        detail=f"A's stage is actually {state_a.get('stage') if state_a else None!r} -- THIS IS THE ORIGINAL BUG",
    )
    check("C got its own completion record", state_c is not None and state_c["stage"] == "complete")

    # --- 7. Abandonment self-heals per-record, not globally ---------------
    print("\n7. an old stuck 'forwarded' record self-heals to abandoned WITHOUT touching a fresh one")
    reset_state()
    ds.mark_dispatch_forwarded(ref_a)  # fresh
    # Write ref_b directly with an old forwarded_at, simulating a router
    # that crashed mid-flight well past DISPATCH_ABANDONED_AFTER_S ago.
    raw = json.loads(ds.DISPATCH_STATE_PATH.read_text())
    raw["dispatches"][ref_b] = {
        "stage": "forwarded",
        "dictation_ref": ref_b,
        "forwarded_at": time.time() - ds.DISPATCH_ABANDONED_AFTER_S - 30,
        "completed_at": None,
        "result_summary": None,
        "l3_note": None,
    }
    ds.DISPATCH_STATE_PATH.write_text(json.dumps(raw))

    state_a = ds.dispatch_state(ref_a)
    state_b = ds.dispatch_state(ref_b)
    check("stale B self-heals to abandoned", state_b is not None and state_b["stage"] == "abandoned")
    check(
        "fresh A is untouched by B's abandonment (still forwarded)",
        state_a is not None and state_a["stage"] == "forwarded",
    )
    check("any_forwarded() is True (A is genuinely still in flight)", ds.any_forwarded())

    # --- 8. Migration: an old flat singleton file must not crash ----------
    print("\n8. an old (pre-keyed) flat singleton dispatch_state.json migrates instead of crashing")
    reset_state()
    legacy = {
        "stage": "forwarded",
        "dictation_ref": "/tmp/dictations/legacy-singleton.txt",
        "forwarded_at": time.time(),
        "completed_at": None,
        "result_summary": None,
        "l3_note": None,
    }
    ds.DISPATCH_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    ds.DISPATCH_STATE_PATH.write_text(json.dumps(legacy))

    try:
        migrated = ds.dispatch_state()
        no_crash = True
    except Exception as e:  # noqa: BLE001
        migrated = None
        no_crash = False
        check("reading a legacy singleton file does not raise", False, detail=repr(e))
    if no_crash:
        check("reading a legacy singleton file does not raise", True)
        check(
            "the legacy record is recovered correctly",
            migrated is not None and migrated["dictation_ref"] == legacy["dictation_ref"],
        )
        check("any_forwarded() works over a migrated file", ds.any_forwarded())
        # And it must upgrade on disk, not just in memory, so every future
        # reader (not just this one) sees the keyed shape.
        on_disk = json.loads(ds.DISPATCH_STATE_PATH.read_text())
        check(
            "the migration persists to disk (upgrades the file, not just this read)",
            "dispatches" in on_disk,
        )

    reset_state()
    try:
        (jarvis_home()).rmdir()
    except OSError:
        pass  # not empty (other test artifacts) or already gone -- fine either way

    print()
    failures = [r for r in RESULTS if not r.passed]
    if failures:
        print(f"{len(failures)}/{len(RESULTS)} FAILED:")
        for r in failures:
            print(f"  - {r.name}" + (f" ({r.detail})" if r.detail else ""))
        return 1

    print(f"all {len(RESULTS)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(run())
