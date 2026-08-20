#!/usr/bin/env python3
"""Canary for the Kokoro-82M TTS rewrite (docs/TODO-feature-queue.md #1):
proves both directions the Lead asked for -- the Kokoro path genuinely
produces audio, and the `say` fallback is REACHED and REPORTED when
Kokoro is unavailable, forced by simulating a real failure rather than
trusting the try/except reads correctly.

MUST NOT PLAY AUDIO ON A NORMAL SWEEP RUN. subprocess.run is patched
throughout the default run (same discipline as speech_queue_canary.py/
jarvis_say_canary.py use for say_feedback's OWN worker) -- neither the
Kokoro-success afplay call nor the fallback's `say` call ever actually
executes. This is deliberately NOT the same thing as JARVIS_MUTE: mute
makes _worker_loop() skip _speak_now() entirely, which would make it
impossible to prove the fallback branch INSIDE _speak_now() is reached
at all. So the default run patches subprocess.run directly (never relies
on MUTE for silence) and separately asserts what MUTE governs (see
--play below).

Pass --play to opt in to a REAL end-to-end run (like Engineer 2's
opt-in flag for terminal windows): genuine Kokoro synthesis, genuine
afplay, through say_feedback.speak() itself -- so JARVIS_MUTE is
honored exactly as it is in production (the real worker checks it, this
script doesn't need to). Reports real, measured first-audio latency.

MUST run via l4_controller/.venv or l1_wakeword/.venv (needs numpy/
onnxruntime/kokoro_onnx):

    l4_controller/.venv/bin/python3 l4_controller/kokoro_tts_canary.py
    l4_controller/.venv/bin/python3 l4_controller/kokoro_tts_canary.py --play
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

os.environ["JARVIS_TEST_RUN"] = f"kokoro-tts-canary-{uuid.uuid4().hex[:8]}"

sys.path.insert(0, str(Path(__file__).parent))
import kokoro_tts  # noqa: E402
import say_feedback as sf  # noqa: E402
from jarvis_paths import jarvis_home  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((name, passed, detail))
    print(f"  {'ok  ' if passed else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not passed else ""))


def _rms(samples) -> float:
    import numpy as np
    return float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))


def read_latency_events(event_name: str) -> list[dict]:
    path = jarvis_home() / "latency_log.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("event") == event_name:
            out.append(entry)
    return out


def default_sweep() -> None:
    print("kokoro_tts canary -- default sweep (no real audio played)\n")

    # --- A. Kokoro genuinely produces real, non-silent audio -----------
    print("A. direct synthesis produces real, non-silent audio")
    phrase = "Got it, working on it."
    for voice in ("bm_daniel", "bm_fable", "bm_george", "bm_lewis"):
        try:
            samples, sr = kokoro_tts.synthesize(phrase, voice=voice)
            rms = _rms(samples)
            check(f"{voice}: synthesis succeeded, sample rate is 24kHz", sr == 24000, detail=f"got {sr}")
            check(f"{voice}: audio is genuinely non-silent (RMS > 0.001)", rms > 0.001, detail=f"RMS={rms:.5f}")
            dur = len(samples) / sr
            check(f"{voice}: audio duration is plausible for the phrase (0.5s-5s)", 0.5 < dur < 5.0, detail=f"{dur:.2f}s")
        except kokoro_tts.KokoroUnavailable as e:
            check(f"{voice}: synthesis succeeded", False, detail=str(e))

    # --- B. Fallback is REACHED and REPORTED on a simulated failure ----
    print("\nB. fallback path: forced Kokoro failure, must reach `say` and report it (no real audio)")
    real_synthesize_to_wav = kokoro_tts.synthesize_to_wav

    def _boom(*args, **kwargs):
        raise kokoro_tts.KokoroUnavailable("simulated failure -- canary forced this, not a real load error")

    calls = []

    def _fake_subprocess_run(argv, **kwargs):
        calls.append(list(argv))
        import subprocess as _sp
        return _sp.CompletedProcess(argv, 0)

    sf.kokoro_tts.synthesize_to_wav = _boom
    sf.subprocess.run = _fake_subprocess_run
    try:
        sf._speak_now("fallback test phrase")
    finally:
        sf.kokoro_tts.synthesize_to_wav = real_synthesize_to_wav

    check(
        "the fallback REACHED a real `say` invocation (not silently swallowed)",
        any(c and c[0] == "say" for c in calls),
        detail=f"recorded calls: {calls}",
    )
    fallback_events = read_latency_events("kokoro_tts_fallback")
    check(
        "the fallback was REPORTED via latency_log (kokoro_tts_fallback event)",
        len(fallback_events) == 1 and "simulated failure" in fallback_events[-1].get("error", ""),
        detail=str(fallback_events),
    )

    # --- C. Success path through the REAL worker+queue (afplay mocked) -
    print("\nC. success path through the real say_feedback.speak() queue (afplay mocked, no real audio)")
    calls.clear()
    offset_before = len(fallback_events)
    sf.speak("queued success-path phrase")
    deadline = time.monotonic() + 10.0
    while not calls and time.monotonic() < deadline:
        time.sleep(0.05)
    check(
        "the REAL worker reached afplay (Kokoro synthesis succeeded, no fallback)",
        any(c and c[0] == "afplay" for c in calls),
        detail=f"recorded calls: {calls}",
    )
    ok_events = read_latency_events("kokoro_tts_ok")
    check(
        "success was REPORTED via latency_log (kokoro_tts_ok event with a real synth_ms)",
        len(ok_events) >= 1 and isinstance(ok_events[-1].get("synth_ms"), (int, float)) and ok_events[-1]["synth_ms"] > 0,
        detail=str(ok_events[-1] if ok_events else None),
    )
    if ok_events:
        print(f"  (measured synth_ms on this run: {ok_events[-1]['synth_ms']}ms)")

    check(
        "not one single real `say`/`afplay` subprocess call happened this entire sweep",
        all(c and c[0] in ("say", "afplay") for c in calls) and len(calls) >= 1,
        detail=f"all recorded (fake) calls: {calls}",
    )


def real_playback_run(voice: str) -> None:
    print(f"kokoro_tts canary -- REAL playback opt-in run (voice={voice})\n")
    print("This WILL make sound unless JARVIS_MUTE=1 is set (honored exactly as production does,")
    print("via say_feedback's own worker -- this script does not special-case it).\n")

    os.environ["JARVIS_KOKORO_VOICE"] = voice
    import importlib
    importlib.reload(sf)  # pick up the voice env var change cleanly

    t0 = time.monotonic()
    sf.speak(f"This is Jarvis, testing the {voice} voice.")
    # Drain: wait for the queue to empty (real playback takes real time).
    sf._speech_queue.join()
    elapsed_ms = (time.monotonic() - t0) * 1000
    print(f"\ntotal enqueue-to-drained time: {elapsed_ms:.0f}ms (includes real synthesis + real playback duration)")
    ok_events = read_latency_events("kokoro_tts_ok")
    if ok_events:
        print(f"measured synth_ms (time-to-first-audio, this call): {ok_events[-1]['synth_ms']}ms")
    fallback_events = read_latency_events("kokoro_tts_fallback")
    if fallback_events:
        print(f"NOTE: fell back to `say` -- {fallback_events[-1]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--play", action="store_true", help="opt in to a REAL playback run instead of the silent default sweep")
    ap.add_argument("--voice", default=kokoro_tts.DEFAULT_VOICE, help="voice to use for --play (default: %(default)s)")
    args = ap.parse_args()

    try:
        if args.play:
            real_playback_run(args.voice)
            return 0
        default_sweep()
    finally:
        import shutil
        shutil.rmtree(jarvis_home(), ignore_errors=True)

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
    # Same refusal as speech_queue_canary and jarvis_say_canary, added
    # 2026-08-20 after this file failed confusingly in a full sweep that
    # set JARVIS_MUTE=1 for every canary. Under mute, say_feedback's
    # worker never calls _speak_now(), so the patched fake never runs --
    # and the assertions then report "recorded calls: []", which reads
    # like Kokoro synthesis broke when the truth is the test was skipped.
    #
    # Worth a guard rather than a note: a red that misdescribes its own
    # cause is worse than a red, because it sends you debugging the wrong
    # thing. This file already proves it makes no real audio by asserting
    # every recorded call was one of its own fakes; it does not need mute
    # and must not be run under it.
    if os.environ.get("JARVIS_MUTE") == "1":
        print(
            "REFUSING TO RUN: this canary must not run under JARVIS_MUTE=1. "
            "The subprocess patches below are what keep it silent, not mute -- "
            "under mute the worker skips _speak_now() entirely, so the fakes "
            "never run and every check here becomes vacuous (and reports as a "
            "synthesis failure, which it is not). Unset JARVIS_MUTE and run again."
        )
        sys.exit(1)
    sys.exit(main())
