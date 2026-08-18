"""
Dispatch-in-flight state: lets the L2.5 concierge answer "is it done yet?"
/ "what's it doing?" without waiting on L3 (SPEC-L2.5-concierge.md
requirement 4). This is most of the felt improvement -- Ayman is never
standing in silence wondering whether the system heard him.

Authoritative stage transitions are CODE-DRIVEN, not LLM self-report --
the same "deterministic before model" rule the rest of this project
applies to facts about *data* applies here to facts about *state*. A
scheme where L3 is responsible for calling a "mark my own progress" tool
at each step has the same fabrication risk the spec calls out for the
local model: it's easy to skip under load, and a skipped report makes
this file go stale silently, so the concierge would confidently tell
Ayman the wrong thing with no signal anything's wrong.

So the two states that matter are each written by code that already knows
the fact for other reasons, not by a self-report:
  - "forwarded" is written by l2_l3_handoff.deliver_transcript() at the
    exact moment a transcript is actually handed to L3 -- that call
    already exists and already fires exactly once per real forward;
    nothing new has to remember to call it.
  - "complete" is written by server.deliver_batch() at the point it
    already has a real delivery result (count/failures) -- also not a
    self-report, just recording a fact the code already has in hand.

report_dispatch_stage() exists ONLY for optional color L3 may choose to
push mid-turn (e.g. the narrated plan text, which really is only knowable
from inside its own reasoning turn). dispatch_state() must give a correct
forwarded/complete answer even if this is never called -- callers should
combine it with a live providers.session_activity() poll for "what's it
doing", not treat it as the source of truth for whether anything is
happening.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from jarvis_paths import jarvis_home  # noqa: E402

DISPATCH_STATE_PATH = jarvis_home() / "dispatch_state.json"

# A real dispatch completes in ~90s. Past this, "forwarded" is not a
# turn still running -- it is a turn whose orchestrator restarted or
# crashed mid-flight and never got to call mark_dispatch_complete().
# That happens routinely (an orchestrator restart abandons whatever was
# in flight), so "forwarded" must not be trusted as live state forever:
# same "liveness is always polled, never remembered" discipline
# l5_console/state/teams.py applies to team membership, applied here to
# dispatch stage. Generous on purpose -- a few minutes of margin over the
# real ~90s completion time, not a tight SLA.
DISPATCH_ABANDONED_AFTER_S = 180.0


def mark_dispatch_forwarded(dictation_ref: str) -> None:
    _write(
        {
            "stage": "forwarded",
            "dictation_ref": dictation_ref,
            "forwarded_at": time.time(),
            "completed_at": None,
            "result_summary": None,
            "l3_note": None,
        }
    )


def mark_dispatch_complete(result_summary: dict) -> None:
    state = _read()
    if state is None or state.get("stage") != "forwarded":
        # No matching in-flight "forwarded" record -- e.g. deliver_batch
        # called outside the normal dictation flow (a test harness, an
        # adversarial scenario script). Still record completion rather
        # than silently dropping the signal; this is a legitimate case,
        # not an error.
        state = {"dictation_ref": None, "forwarded_at": None, "l3_note": None}
    state["stage"] = "complete"
    state["completed_at"] = time.time()
    state["result_summary"] = result_summary
    _write(state)


def report_dispatch_stage(stage: str, detail: str = "") -> None:
    """Optional, non-authoritative color L3 may push mid-turn. Never
    required for dispatch_state() to answer correctly."""
    state = _read() or {}
    state["l3_note"] = {"stage": stage, "detail": detail, "at": time.time()}
    _write(state)


def dispatch_state() -> dict | None:
    """None means no dispatch has ever been recorded (fresh install / log
    rotated), not "nothing is in flight" -- callers combine this with a
    live session_activity() poll rather than trusting staleness alone.

    Self-heals a "forwarded" record that has sat past
    DISPATCH_ABANDONED_AFTER_S into stage "abandoned", persisting the
    change so every caller (not just this one) sees the healed value --
    a stuck "forwarded" entry must degrade to "nothing is really in
    flight" for anything that keys off dispatch stage, not just for
    whichever caller happens to notice first."""
    state = _read()
    if state is not None and state.get("stage") == "forwarded":
        forwarded_at = state.get("forwarded_at")
        if forwarded_at is not None and (time.time() - forwarded_at) > DISPATCH_ABANDONED_AFTER_S:
            state["stage"] = "abandoned"
            state["abandoned_at"] = time.time()
            _write(state)
    return state


def _read() -> dict | None:
    if not DISPATCH_STATE_PATH.exists():
        return None
    try:
        return json.loads(DISPATCH_STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _write(state: dict) -> None:
    DISPATCH_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    DISPATCH_STATE_PATH.write_text(json.dumps(state))
