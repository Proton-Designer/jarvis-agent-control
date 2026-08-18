#!/usr/bin/env python3
"""Canary for say_feedback.py's serialized speech queue
(SPEC-orchestration.md SS0.3). Same purpose and discipline as the other
canaries in this directory: the property under test ("two `say` processes
never run concurrently, and priority order is honored") is exactly the
kind of thing that looks fine in isolation and only breaks under real
concurrent load, which unit tests of a single call can't exercise.

Monkeypatches the actual `subprocess.run` call say_feedback's worker
makes, so this never makes the laptop talk and never depends on real
`say` timing -- an artificial delay is injected instead, which is what
actually lets check 1 detect overlap (with instant no-op calls, two
threads racing to "speak" would never observably collide even if the
code had zero serialization). Deliberately does NOT set JARVIS_MUTE=1:
mute skips the `say` call entirely (`if not MUTE: subprocess.run(...)`),
which would make the patched fake never run at all and every timing/
ordering check below silently vacuous -- the patch on subprocess.run is
what keeps this silent, not mute. Also monkeypatches
say_feedback.listen_for_cancel so check 5 doesn't depend on L1's real
cancel socket being up.

Run after touching say_feedback.py:

    python3 l4_controller/speech_queue_canary.py
"""
from __future__ import annotations

import os
import sys
import threading
import time
import uuid
from pathlib import Path

if os.environ.get("JARVIS_MUTE") == "1":
    # Fails LOUD and specific here, not as a confusing downstream timing
    # assertion -- found live (the Lead, first run): running this muted
    # made the `subprocess.run` patch below never fire at all (mute skips
    # that call entirely), so every timing/ordering check quietly measured
    # nothing and failed on the arming assertion with no clue why. A test
    # whose failure doesn't say what's wrong teaches the next person to
    # distrust it rather than fix their invocation.
    print(
        "REFUSING TO RUN: this canary must not run under JARVIS_MUTE=1. "
        "The subprocess.run patch below is what keeps it silent, not mute -- "
        "under mute, say_feedback's worker skips the `say` call entirely, so "
        "the patched fake never runs and every timing/ordering check in this "
        "file becomes vacuous. Unset JARVIS_MUTE and run again."
    )
    sys.exit(1)

os.environ["JARVIS_TEST_RUN"] = f"speech-queue-canary-{uuid.uuid4().hex[:8]}"

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import say_feedback as sf  # noqa: E402
from cancel_listener import CancelResult  # noqa: E402
from jarvis_paths import jarvis_home  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((name, passed, detail))
    print(f"  {'ok  ' if passed else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not passed else ""))


def reset_log() -> None:
    sf.SAY_LOG_PATH.unlink(missing_ok=True)


def read_log_texts() -> list[str]:
    if not sf.SAY_LOG_PATH.exists():
        return []
    lines = sf.SAY_LOG_PATH.read_text().splitlines()
    import json
    return [json.loads(line)["text"] for line in lines if line.strip()]


# --- Instrumented fake `say` process: records overlap, adds a real delay
# so two truly-concurrent invocations would actually be caught racing ----
_active = 0
_active_lock = threading.Lock()
_max_concurrent = 0
_call_log: list[tuple[str, float]] = []  # (text, wall-clock start time)


def _fake_subprocess_run(args, **kwargs):
    global _active, _max_concurrent
    text = args[-1]
    with _active_lock:
        _active += 1
        _max_concurrent = max(_max_concurrent, _active)
        _call_log.append((text, time.monotonic()))
    time.sleep(0.15)  # long enough that two real-concurrent calls would overlap and get caught
    with _active_lock:
        _active -= 1


def run() -> int:
    print("say_feedback speech-queue canary\n")
    print(f"  isolated say_log: {sf.SAY_LOG_PATH}")
    reset_log()
    sf.subprocess.run = _fake_subprocess_run  # patch the module's bound name, not the stdlib

    # --- 1. True serialization: never two "say" calls active at once ----
    print("\n1. concurrent speak() calls never run `say` overlapping")
    _call_log.clear()
    global _max_concurrent
    _max_concurrent = 0
    threads = [
        threading.Thread(target=sf.speak, args=(f"concurrent-{i}",), kwargs={"priority": sf.PRIORITY_NORMAL})
        for i in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # give the worker time to drain the queue (5 items * 0.15s + margin)
    deadline = time.monotonic() + 3.0
    while len(_call_log) < 5 and time.monotonic() < deadline:
        time.sleep(0.02)
    check(
        "all 5 speak() calls were serviced",
        len(_call_log) == 5,
        detail=f"only {len(_call_log)}/5 processed within the deadline",
    )
    check(
        "max concurrent `say` invocations across 5 racing speak() calls was 1",
        _max_concurrent == 1,
        detail=f"max_concurrent={_max_concurrent} -- two overlapped, this is the garbled-audio bug",
    )

    # --- 2. Priority-then-submission order, not arrival order ------------
    print("\n2. priority order: HIGH speaks before an earlier-submitted NORMAL")
    reset_log()
    _call_log.clear()
    _max_concurrent = 0
    # Submit NORMAL first, then HIGH, but expect HIGH to be serviced first
    # (aside from whatever's already been dequeued the instant NORMAL was
    # submitted -- so submit a filler HIGH first to occupy the worker,
    # keeping both real test items queued together).
    sf.speak("filler-occupies-worker", priority=sf.PRIORITY_HIGH)
    time.sleep(0.01)  # let the worker pick up the filler so both test items queue together
    sf.speak("submitted-first-but-normal-priority", priority=sf.PRIORITY_NORMAL)
    sf.speak("submitted-second-but-high-priority", priority=sf.PRIORITY_HIGH)
    deadline = time.monotonic() + 3.0
    while len(_call_log) < 3 and time.monotonic() < deadline:
        time.sleep(0.02)
    order = [text for text, _ in _call_log]
    check(
        "HIGH-priority item spoken before the earlier-submitted NORMAL one",
        order == ["filler-occupies-worker", "submitted-second-but-high-priority", "submitted-first-but-normal-priority"],
        detail=f"actual order: {order}",
    )

    # --- 3. FIFO within the same priority ---------------------------------
    print("\n3. same-priority calls stay in submission order (FIFO, not reordered)")
    reset_log()
    _call_log.clear()
    for i in range(4):
        sf.speak(f"fifo-{i}", priority=sf.PRIORITY_NORMAL)
    deadline = time.monotonic() + 3.0
    while len(_call_log) < 4 and time.monotonic() < deadline:
        time.sleep(0.02)
    order = [text for text, _ in _call_log]
    check("same-priority calls stayed FIFO", order == ["fifo-0", "fifo-1", "fifo-2", "fifo-3"], detail=f"got {order}")

    # --- 4. say_log reflects service order (usable for ordering verification, per spec) --
    print("\n4. say_log.jsonl reflects actual service order, not call order")
    log_texts = read_log_texts()
    check("say_log has exactly the 4 fifo entries in order", log_texts == ["fifo-0", "fifo-1", "fifo-2", "fifo-3"], detail=f"got {log_texts}")

    # --- 5. speak_with_cancel_window arms the listener only once its own
    #        queued item actually starts, not at call time -------------
    print("\n5. speak_with_cancel_window's listener arms at actual playback start, not at enqueue time")
    reset_log()
    _call_log.clear()
    listen_calls: list[float] = []

    def _fake_listen_for_cancel(timeout_s, socket_path=None):
        listen_calls.append(time.monotonic())
        return CancelResult(cancelled=False, available=True)

    sf.listen_for_cancel = _fake_listen_for_cancel

    # Occupy the worker with a long filler HIGH item first, so the real
    # confirm call has to sit in queue for a measurable, known delay.
    t_call_confirm = {}

    def _do_confirm():
        t_call_confirm["called_at"] = time.monotonic()
        sf.speak_with_cancel_window("plan summary", cancel_window_s=1.0)

    sf.speak("blocking-filler", priority=sf.PRIORITY_HIGH)  # ~0.15s of worker time
    confirm_thread = threading.Thread(target=_do_confirm)
    confirm_thread.start()
    time.sleep(0.02)  # ensure speak_with_cancel_window's call (and its enqueue) happens while filler is still running
    confirm_thread.join(timeout=5)

    check("the fake listen_for_cancel was actually invoked", len(listen_calls) == 1, detail=f"got {len(listen_calls)} calls")
    if listen_calls and "called_at" in t_call_confirm:
        armed_delay = listen_calls[0] - t_call_confirm["called_at"]
        check(
            "the listener armed AFTER the filler finished (>= ~0.1s later), not immediately on call",
            armed_delay >= 0.1,
            detail=f"armed only {armed_delay*1000:.0f}ms after speak_with_cancel_window() was called -- "
                   "should have waited for the filler ahead of it in queue",
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
    try:
        sys.exit(run())
    finally:
        sf.SAY_LOG_PATH.unlink(missing_ok=True)
        try:
            jarvis_home().rmdir()
        except OSError:
            pass
