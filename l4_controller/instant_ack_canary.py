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
    from instant_ack import instant_ack_phrase as _ack_phrase
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
                # WAS "ack is the FIRST thing spoken". The ack is no longer
                # spoken up front at all -- it is a fallback that fires only
                # if nothing else speaks within ACK_FALLBACK_AFTER_S (Ayman,
                # 2026-08-20: "I need a direct response to my statement not
                # an ack"). So this now asserts the PHRASE CHOICE, which is
                # what it was really protecting: naming the team he actually
                # said, never a guess. The timing is asserted separately,
                # with the clock shortened, in the fallback section below.
                name="a team-naming dictation produces a team-naming ack phrase",
                passed=_ack_phrase("tell gateway to run its tests") == "Okay -- gateway.",
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
                passed=_ack_phrase("tell gateway and billing to run their tests")
                == "Okay -- gateway and billing.",
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
                passed=_ack_phrase("what's the weather like today") == "Okay, one sec.",
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
                name="no concierge attached: the failure is spoken, and spoken ALONE",
                passed=bool(texts) and "concierge" in texts[0].lower(),
                detail=f"got: {texts}",
            )
        )
        results.append(
            CheckResult(
                # The ack is now SUPPRESSED here, and that is correct: the
                # failure message is itself a real reply, so telling him
                # "Okay, one sec." afterwards would promise work that was
                # just refused. Previously the ack came first and the
                # failure second, which was the best available behaviour
                # when the ack was unconditional.
                name="...and the ack does NOT also fire -- a refusal is a reply, not a wait",
                passed=not any("one sec" in t.lower() or t.startswith("Okay -- ") for t in texts),
                detail=f"got: {texts}",
            )
        )

        # --- Never asserts an outcome ---
        outcome_words = ("sending", "sent", "delivered", "dispatching", "routing")
        ack_text = _ack_phrase("something with no known team in it")
        results.append(
            CheckResult(
                name="ack phrase never asserts an outcome (receipt only)",
                passed=not any(w in ack_text.lower() for w in outcome_words),
                detail=f"ack text: {ack_text!r}",
            )
        )

        # --- The ack is a FALLBACK now, not a preamble (Ayman, 2026-08-20)
        #
        # "I don't wanna hear ok one second after I say hello, that's just
        # weird, I need a direct response to my statement not an ack."
        #
        # The ack solved a real problem -- 30+ second silences under the
        # old single-tier orchestrator -- and that problem stopped
        # existing when the concierge started answering in ~2.3s.
        # Announcing that an answer is coming, two seconds before it
        # arrives, delays the answer and makes a fast system feel slow.
        #
        # BOTH DIRECTIONS, and the second is why this wasn't just a
        # deletion: removing the ack outright is the easy read of his
        # complaint and the wrong one. The silence it was built for is
        # still reachable -- a wedged concierge, a genuinely slow router
        # turn -- and that is exactly the case where he'd be left with
        # nothing at all.
        import instant_ack as _ia
        import unittest.mock as _mock
        _prev = _ia.ACK_FALLBACK_AFTER_S
        _ia.ACK_FALLBACK_AFTER_S = 0.4
        try:
            with _mock.patch.object(_ia, "speak") as _sp:
                with _mock.patch.object(_ia, "_say_log_size", side_effect=[100, 250]):
                    _ia.speak_instant_ack("hello")
                    time.sleep(0.9)
            results.append(CheckResult(
                name="a real reply speaking first SUPPRESSES the ack",
                passed=not _sp.called,
                detail=f"speak called: {_sp.called}",
            ))

            with _mock.patch.object(_ia, "speak") as _sp2:
                with _mock.patch.object(_ia, "_say_log_size", side_effect=[100, 100]):
                    _ia.speak_instant_ack("hello")
                    time.sleep(0.9)
            results.append(CheckResult(
                name="but SILENCE still fires it -- the case the ack exists for is not regressed",
                passed=_sp2.called,
                detail=f"speak called: {_sp2.called}",
            ))

            with _mock.patch.object(_ia, "speak") as _sp3:
                with _mock.patch.object(_ia, "_say_log_size", return_value=None):
                    _ia.speak_instant_ack("hello")
                    time.sleep(0.9)
            results.append(CheckResult(
                name="an unreadable say_log fails toward SPEAKING, never toward silence",
                passed=_sp3.called,
                detail=f"speak called: {_sp3.called}",
            ))
        finally:
            _ia.ACK_FALLBACK_AFTER_S = _prev

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
