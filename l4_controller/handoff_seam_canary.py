#!/usr/bin/env python3
"""Canary for l2_l3_handoff.deliver_transcript() (SPEC-orchestration.md
SS1.4 -- "the handoff seam", the Lead's acceptance criterion: A FAILED
HANDOFF IS ALWAYS AUDIBLE). Proves it against the real deliver_transcript()
function -- a FakeTransport stands in for tmux (no real session needed),
and orchestrator_has_tools()/write_dictation() are monkeypatched only for
the specific failure scenarios that need to force them to fail; every
other code path (write_dictation, mark_dispatch_forwarded, dispatch_state
lookups) is the real thing, running against a JARVIS_TEST_RUN-isolated
~/.jarvis, so this also exercises the real 0.1 keyed-dispatch-state wiring
end to end.

Three failure modes, each asserted to speak an explicit, distinct message
(never silence, never a bare exception):
  1. Preflight: the router's MCP tools aren't connected at all.
  2. Enqueue: writing the dictation file itself fails (disk full,
     permissions, ...) -- this is the ordering fix SS1.4 required: the
     "Got it, working on it" ack must never be spoken before the enqueue
     it's confirming has actually succeeded.
  3. Pointer delivery: the enqueue succeeds but the tmux send to the
     router's session fails (session gone, refused, tmux_error, ...) --
     also proves dispatch_state for that dictation is corrected to
     "failed" immediately, not left reading "forwarded" (implying active
     work) for up to DISPATCH_ABANDONED_AFTER_S.

Plus the happy path, proving the ack is spoken, the pointer is delivered,
and dispatch_state ends up "forwarded" under the dictation's own ref.

MUST run via l4_controller/.venv (2026-08-20: say_feedback.py now imports
kokoro_tts at module load, transitively pulled in through transport.py).

Run after touching l2_l3_handoff.py or dispatch_state.py:

    l4_controller/.venv/bin/python3 l4_controller/handoff_seam_canary.py
"""
from __future__ import annotations

import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

os.environ["JARVIS_TEST_RUN"] = f"handoff-seam-canary-{uuid.uuid4().hex[:8]}"

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import l2_l3_handoff as h  # noqa: E402
import dispatch_state as ds  # noqa: E402
from jarvis_paths import jarvis_home  # noqa: E402
from transport import DeliveryResult, Transport  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
SPOKEN: list[str] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((name, passed, detail))
    print(f"  {'ok  ' if passed else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not passed else ""))


def _fake_speak(text: str, priority: int = 0) -> None:
    SPOKEN.append(text)


h.speak = _fake_speak  # capture every audible message this module produces, don't touch real audio/queue


@dataclass
class FakeTransport(Transport):
    ok: bool = True
    reason: str | None = None
    detail: str = ""

    def deliver(self, target: str, payload: str) -> DeliveryResult:
        return DeliveryResult(ok=self.ok, detail=self.detail, reason=self.reason)

    def session_exists(self, target: str) -> bool:
        return True


def _latest_dictation_ref() -> str | None:
    dispatches = ds.all_dispatches()
    if not dispatches:
        return None
    return max(dispatches.values(), key=lambda d: d.get("forwarded_at") or d.get("failed_at") or 0)["dictation_ref"]


def run() -> int:
    print("handoff seam canary (SS1.4 -- a failed handoff is always audible)\n")

    # --- 1. Preflight failure: no router MCP tools connected --------------
    print("1. router has no MCP tools connected")
    SPOKEN.clear()
    h.orchestrator_has_tools = lambda: False
    result = h.deliver_transcript("do something", FakeTransport(), orchestrator_target="claude-test-target")
    check("returns None (never reached the transport)", result is None)
    check(
        "spoke an explicit, specific failure -- not silence",
        any("no tools connected" in s for s in SPOKEN),
        detail=f"spoke: {SPOKEN}",
    )
    check("nothing else was spoken (no misleading ack before the refusal)", len(SPOKEN) == 1, detail=f"spoke: {SPOKEN}")
    h.orchestrator_has_tools = lambda: True  # restore for the rest

    # --- 2. Enqueue failure: the dictation write itself fails -------------
    print("\n2. writing the dictation file fails (disk full / permissions / ...)")
    SPOKEN.clear()

    def _broken_write_dictation(text: str):
        raise OSError("simulated: no space left on device")

    real_write_dictation = h.write_dictation
    h.write_dictation = _broken_write_dictation
    result = h.deliver_transcript("do something", FakeTransport(), orchestrator_target="claude-test-target")
    h.write_dictation = real_write_dictation
    check("returns None (never reached the transport)", result is None)
    check(
        "spoke an explicit failure -- never a silent crash after a truthful-sounding ack",
        any("couldn't record that dictation" in s for s in SPOKEN),
        detail=f"spoke: {SPOKEN}",
    )
    check(
        "the ack ('Got it, working on it') was NEVER spoken -- the enqueue never actually succeeded",
        not any("Got it" in s for s in SPOKEN),
        detail=f"spoke: {SPOKEN} -- an ack spoken before a confirmed enqueue is exactly the bug SS1.4 called out",
    )

    # --- 3. Pointer delivery failure: enqueue succeeds, tmux send fails ---
    print("\n3. enqueue succeeds but the pointer delivery to the router's session fails")
    SPOKEN.clear()
    before_count = len(ds.all_dispatches())
    result = h.deliver_transcript(
        "do something",
        FakeTransport(ok=False, reason="no_session", detail="no such tmux session"),
        orchestrator_target="claude-test-target",
    )
    check("returns the failed DeliveryResult (not None -- the enqueue DID happen)", result is not None and not result.ok)
    check(
        "spoke the ack (enqueue really did succeed) AND an explicit delivery failure",
        any("Got it" in s for s in SPOKEN) and any("couldn't reach the orchestrator session" in s for s in SPOKEN),
        detail=f"spoke: {SPOKEN}",
    )
    ref = _latest_dictation_ref()
    check("a new dispatch record was created for this dictation", len(ds.all_dispatches()) == before_count + 1)
    record = ds.dispatch_state(ref) if ref else None
    check(
        "dispatch_state is corrected to 'failed' immediately, not left as misleading 'forwarded'",
        record is not None and record["stage"] == "failed",
        detail=f"got stage={record.get('stage') if record else None!r}",
    )
    check("failure_reason is recorded on the dispatch record", record is not None and record.get("failure_reason") == "no_session")

    # --- 4. Happy path ------------------------------------------------------
    print("\n4. happy path: enqueue succeeds, pointer delivers cleanly")
    SPOKEN.clear()
    result = h.deliver_transcript("do something real", FakeTransport(ok=True), orchestrator_target="claude-test-target")
    check("returns an ok DeliveryResult", result is not None and result.ok)
    check("spoke the ack, no failure language", any("Got it" in s for s in SPOKEN) and not any("couldn't" in s for s in SPOKEN), detail=f"spoke: {SPOKEN}")
    ref = _latest_dictation_ref()
    record = ds.dispatch_state(ref) if ref else None
    check(
        "dispatch_state shows this dictation as 'forwarded' under its own ref",
        record is not None and record["stage"] == "forwarded",
        detail=f"got {record}",
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
        try:
            for p in sorted(jarvis_home().glob("**/*"), reverse=True):
                if p.is_file():
                    p.unlink()
            for p in sorted(jarvis_home().glob("**/*"), reverse=True):
                if p.is_dir():
                    p.rmdir()
            jarvis_home().rmdir()
        except OSError:
            pass
