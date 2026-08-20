#!/usr/bin/env python3
"""
Instant-acknowledgement canary (SPEC-orchestration.md SS1.1).

Run with: /Users/aymanmohammed/Desktop/Jarvis/l1_wakeword/.venv/bin/python3
l4_controller/instant_ack_canary.py -- from l4_controller/, since it needs
daemon.py's real numpy/whisper dependencies (l1_wakeword's venv, not this
directory's).

Exercises the REAL wiring end to end: daemon.default_deliver(), not
instant_ack.py's functions called in isolation -- a regression that
reorders the ack after the handoff call, or drops the live_deliver gate,
or changes the priority, would not be caught by a unit test of
instant_ack_phrase() alone. Isolated via JARVIS_TEST_RUN + a scratch
teams.json (never touches ~/Jarvis/teams.json or the real say_log.jsonl).
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

TEST_RUN_ID = "instant-ack-canary"
os.environ["JARVIS_MUTE"] = "1"
os.environ["JARVIS_TEST_RUN"] = TEST_RUN_ID

sys.path.insert(0, str(Path(__file__).parent.parent / "l5_console" / "state"))
sys.path.insert(0, str(Path(__file__).parent.parent / "l1_wakeword"))
sys.path.insert(0, str(Path(__file__).parent))

from teams import save_registry  # noqa: E402
from jarvis_paths import jarvis_home  # noqa: E402


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


def read_new_say_log_texts(log_path: Path, offset: int) -> list[str]:
    if not log_path.exists():
        return []
    lines = log_path.read_text().splitlines()[offset:]
    texts = []
    for line in lines:
        try:
            texts.append(json.loads(line)["text"])
        except (json.JSONDecodeError, KeyError):
            continue
    return texts


def main() -> int:
    results: list[CheckResult] = []
    save_registry(
        [
            {"id": "gateway", "aliases": ["gateway", "the gateway project"], "root": "/tmp/x", "inbox": "", "members": []},
            {"id": "billing", "aliases": ["billing"], "root": "/tmp/y", "inbox": "", "members": []},
        ]
    )

    import daemon  # noqa: E402 -- after sys.path is set up above

    log_path = jarvis_home() / "say_log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.touch(exist_ok=True)

    try:
        # --- live_deliver=False must NOT speak at all (smoke-test path) ---
        offset = len(log_path.read_text().splitlines())
        daemon.default_deliver("tell gateway to run its tests", live_deliver=False)
        texts = read_new_say_log_texts(log_path, offset)
        results.append(
            CheckResult(
                name="live_deliver=False produces no speech at all (including no ack)",
                passed=texts == [],
                detail=f"got: {texts}",
            )
        )

        # --- live_deliver=True: ack fires, names a mentioned team, FIRST ---
        offset = len(log_path.read_text().splitlines())
        daemon.default_deliver(
            "tell gateway to run its tests", live_deliver=True, orchestrator_target="nonexistent-canary-target-xyz"
        )
        time.sleep(0.3)  # speech queue worker thread -- log write may lag the call returning
        texts = read_new_say_log_texts(log_path, offset)
        results.append(
            CheckResult(
                name="ack is the FIRST thing spoken for a live dictation",
                passed=bool(texts) and texts[0] == "Okay -- gateway.",
                detail=f"got: {texts}",
            )
        )

        # --- Multi-team mention ---
        offset = len(log_path.read_text().splitlines())
        daemon.default_deliver(
            "tell gateway and billing to run their tests",
            live_deliver=True,
            orchestrator_target="nonexistent-canary-target-xyz",
        )
        time.sleep(0.3)
        texts = read_new_say_log_texts(log_path, offset)
        results.append(
            CheckResult(
                name="both mentioned teams named, in registry order",
                passed=bool(texts) and texts[0] == "Okay -- gateway and billing.",
                detail=f"got: {texts}",
            )
        )

        # --- No known team mentioned -> generic receipt, never a guess ---
        offset = len(log_path.read_text().splitlines())
        daemon.default_deliver(
            "what's the weather like today", live_deliver=True, orchestrator_target="nonexistent-canary-target-xyz"
        )
        time.sleep(0.3)
        texts = read_new_say_log_texts(log_path, offset)
        results.append(
            CheckResult(
                name="unrecognized transcript gets the generic ack, not a fabricated team name",
                passed=bool(texts) and texts[0] == "Okay, one sec.",
                detail=f"got: {texts}",
            )
        )

        # --- NO orchestrator_target, NO concierge attached: the coverage
        # hole the Lead flagged from Engineer 1's audit (2026-08-20). Every
        # check above passes an explicit orchestrator_target=, which skips
        # the concierge-lookup branch in default_deliver() entirely -- that
        # branch (engine_roles.get_role_record("concierge") -> None ->
        # "No concierge is attached...") had ZERO canary coverage, which is
        # exactly how a HIGH-priority failure jumping the queue ahead of the
        # ack shipped undetected. Ruling stands: the ack fires before the
        # preflight on purpose (receipt only, never an outcome, and moving
        # it after the lookup reintroduces the silence this layer exists to
        # remove) -- so the correct, asserted behavior is ack FIRST, THEN
        # the failure, never the reverse and never the failure alone. ---
        offset = len(log_path.read_text().splitlines())
        daemon.default_deliver("tell gateway to run its tests", live_deliver=True)  # no orchestrator_target
        time.sleep(0.3)
        texts = read_new_say_log_texts(log_path, offset)
        results.append(
            CheckResult(
                name="no orchestrator_target + no concierge attached: ack is STILL spoken first",
                passed=bool(texts) and texts[0] == "Okay -- gateway.",
                detail=f"got: {texts}",
            )
        )
        results.append(
            CheckResult(
                name="no orchestrator_target + no concierge attached: the failure follows the ack, never precedes it",
                passed=len(texts) >= 2 and "concierge" in texts[1].lower() and "attached" in texts[1].lower(),
                detail=f"got: {texts}",
            )
        )

        # --- Never asserts an outcome ---
        outcome_words = ("sending", "sent", "delivered", "dispatching", "routing")
        ack_text = texts[0] if texts else ""
        results.append(
            CheckResult(
                name="ack phrase never asserts an outcome (receipt only)",
                passed=not any(w in ack_text.lower() for w in outcome_words),
                detail=f"ack text: {ack_text!r}",
            )
        )

    finally:
        save_registry([])
        import shutil

        shutil.rmtree(jarvis_home(), ignore_errors=True)

    all_ok = True
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        if not r.passed:
            all_ok = False
        print(f"[{status}] {r.name} -- {r.detail}")

    if not all_ok:
        print("\nCANARY FAILED.")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
