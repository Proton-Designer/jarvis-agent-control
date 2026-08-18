"""
Dispatch-in-flight state: lets the concierge tier answer "is it done yet?"
/ "what's it doing?" without waiting on the router (SPEC-orchestration.md
Phase 0.1, superseding SPEC-L2.5-concierge.md requirement 4). This is most
of the felt improvement -- Ayman is never standing in silence wondering
whether the system heard him.

Authoritative stage transitions are CODE-DRIVEN, not LLM self-report --
the same "deterministic before model" rule the rest of this project
applies to facts about *data* applies here to facts about *state*. A
scheme where the router is responsible for calling a "mark my own
progress" tool at each step has the same fabrication risk the spec calls
out for the local model: it's easy to skip under load, and a skipped
report makes this file go stale silently, so the concierge would
confidently tell Ayman the wrong thing with no signal anything's wrong.

So the two states that matter are each written by code that already knows
the fact for other reasons, not by a self-report:
  - "forwarded" is written by l2_l3_handoff.deliver_transcript() at the
    exact moment a transcript is actually handed off -- that call already
    exists and already fires exactly once per real forward; nothing new
    has to remember to call it.
  - "complete" is written by server.deliver_batch() at the point it
    already has a real delivery result (count/failures) -- also not a
    self-report, just recording a fact the code already has in hand.

report_dispatch_stage() exists ONLY for optional color the router may
choose to push mid-turn (e.g. the narrated plan text, which really is only
knowable from inside its own reasoning turn). dispatch_state() must give a
correct forwarded/complete answer even if this is never called -- callers
should combine it with a live providers.session_activity() poll for
"what's it doing", not treat it as the source of truth for whether
anything is happening.

KEYED BY DICTATION_REF (SPEC-orchestration.md SS0.1) -- this file used to
be a single global slot, correct only because one dictation was ever
handled at a time. The two-tier architecture breaks that on purpose: the
concierge hands off dictation #2 while the router is still working #1
(the whole point of the split is that nothing waits on the router). A
singleton meant #2's forward overwrote #1's record, and whichever
finished first closed out the wrong one -- Ayman asks "is it done?" and
gets a confident yes about work that is still running. Keyed the same way
blocked_state.py already keys by session UUID: multiple genuinely
concurrent entries are a normal case here, not an edge case, so the data
shape needs to say so rather than a second engineer having to rediscover
this by reasoning about a JSON blob with an implicit single-dispatch
assumption baked in.

MIGRATION: a live dispatch_state.json from before this change is the old
flat singleton shape ({"stage": ..., "dictation_ref": ..., ...}, no
"dispatches" key). _read() detects and migrates it into the new shape
in memory, keyed by its own dictation_ref (or a fallback key if that was
ever None) -- it is never crashed on, only upgraded on next read.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from jarvis_paths import jarvis_home  # noqa: E402

DISPATCH_STATE_PATH = jarvis_home() / "dispatch_state.json"

# A real dispatch completes in ~90s. Past this, "forwarded" is not a turn
# still running -- it is a turn whose router restarted or crashed
# mid-flight and never got to call mark_dispatch_complete(). That happens
# routinely (a router restart abandons whatever was in flight), so
# "forwarded" must not be trusted as live state forever: same "liveness is
# always polled, never remembered" discipline l5_console/state/teams.py
# applies to team membership, applied here to dispatch stage. Generous on
# purpose -- a few minutes of margin over the real ~90s completion time,
# not a tight SLA. Applied per-dictation now, not to one global slot, so
# one abandoned dispatch can self-heal without touching any other
# genuinely in-flight dispatch's record.
DISPATCH_ABANDONED_AFTER_S = 180.0

_LEGACY_FALLBACK_KEY = "legacy-unknown"


def mark_dispatch_forwarded(dictation_ref: str) -> None:
    state, _ = _read_raw()
    state["dispatches"][dictation_ref] = {
        "stage": "forwarded",
        "dictation_ref": dictation_ref,
        "forwarded_at": time.time(),
        "completed_at": None,
        "result_summary": None,
        "l3_note": None,
    }
    _write(state)


def mark_dispatch_complete(dictation_ref: str, result_summary: dict) -> None:
    """Matches and closes out ONLY `dictation_ref`'s own record -- never
    "whichever record happens to still say forwarded," which was the bug.

    Preserves the legitimate pre-existing case (its own comment, unchanged
    in spirit): deliver_batch() called with no matching "forwarded" record
    for this ref -- e.g. a test harness or an adversarial scenario script
    invoking it directly, outside the normal dictation flow. That still
    records a completion rather than silently dropping the signal; it just
    now creates/overwrites only THIS ref's entry, not some other
    dictation's in-flight one."""
    state, _ = _read_raw()
    existing = state["dispatches"].get(dictation_ref, {})
    state["dispatches"][dictation_ref] = {
        "dictation_ref": dictation_ref,
        "forwarded_at": existing.get("forwarded_at"),
        "l3_note": existing.get("l3_note"),
        "stage": "complete",
        "completed_at": time.time(),
        "result_summary": result_summary,
    }
    _write(state)


def report_dispatch_stage(dictation_ref: str, stage: str, detail: str = "") -> None:
    """Optional, non-authoritative color the router may push mid-turn for
    ITS OWN dictation. Never required for dispatch_state() to answer
    correctly. A ref with no existing record yet (color pushed before the
    forwarded write somehow raced ahead of it) still gets an entry rather
    than being silently dropped."""
    state, _ = _read_raw()
    existing = state["dispatches"].get(dictation_ref, {"dictation_ref": dictation_ref})
    existing["l3_note"] = {"stage": stage, "detail": detail, "at": time.time()}
    state["dispatches"][dictation_ref] = existing
    _write(state)


def dispatch_state(dictation_ref: str | None = None) -> dict | None:
    """dictation_ref given: that specific dispatch's record, or None if no
    such dictation was ever recorded.

    dictation_ref omitted: the MOST RECENT dispatch (by forwarded_at,
    falling back to completed_at) -- preserved for callers that don't
    have a specific ref to check, e.g. a bare "is it done yet?" with no
    named target. This is a real, narrower guarantee than a specific ref:
    it answers "what's the latest thing that happened," not "what's THE
    dispatch," because under concurrent dictations there no longer is a
    single one. Callers that can identify which dictation they mean
    (Haiku's conversation memory, a console row for a specific team)
    should always pass the ref.

    None (as the return value) means no dispatch has ever been recorded
    (fresh install / log rotated), not "nothing is in flight" -- callers
    combine this with a live session_activity() poll rather than trusting
    staleness alone.

    Self-heals any "forwarded" record that has sat past
    DISPATCH_ABANDONED_AFTER_S into stage "abandoned", persisting the
    change so every caller (not just this one) sees the healed value --
    applied per-record, so one stuck dictation degrades to "not really in
    flight" without affecting any other dictation's real, live record."""
    state = _heal_and_maybe_write()
    dispatches = state["dispatches"]
    if dictation_ref is not None:
        return dispatches.get(dictation_ref)
    if not dispatches:
        return None
    return max(
        dispatches.values(),
        key=lambda d: d.get("forwarded_at") or d.get("completed_at") or 0.0,
    )


def all_dispatches() -> dict[str, dict]:
    """Every recorded dispatch, keyed by dictation_ref, self-healed. For
    the console (Phase 3) and for verification -- proving two dictations
    completed against their own refs means being able to look at both at
    once, not just "the current one.\""""
    return dict(_heal_and_maybe_write()["dispatches"])


def any_forwarded() -> bool:
    """True if ANY dispatch is currently in-flight (stage=="forwarded",
    post self-heal). Replaces the old single-slot check
    (`dispatch_state() is not None and dispatch_state()["stage"] ==
    "forwarded"`) -- that check only ever looked at the single global
    slot, which is exactly the assumption this file no longer makes.
    Checking only the MOST RECENT dispatch would miss an earlier
    dictation that is still genuinely in flight while a later one has
    already been forwarded too -- gating on "is anything at all still
    forwarded" is the correct generalization of "nothing in flight," not
    "is the latest thing done.\""""
    return any(d.get("stage") == "forwarded" for d in _heal_and_maybe_write()["dispatches"].values())


def _heal_and_maybe_write() -> dict:
    state, migrated = _read_raw()
    healed = False
    now = time.time()
    for record in state["dispatches"].values():
        if record.get("stage") != "forwarded":
            continue
        forwarded_at = record.get("forwarded_at")
        if forwarded_at is not None and (now - forwarded_at) > DISPATCH_ABANDONED_AFTER_S:
            record["stage"] = "abandoned"
            record["abandoned_at"] = now
            healed = True
    if healed or migrated:
        # migrated: persist the upgrade even on a read-only call (no
        # healing needed) -- every future reader must see the keyed
        # shape, not just this one call's in-memory view of it.
        _write(state)
    return state


def _read_raw() -> tuple[dict, bool]:
    """Always returns ({"dispatches": {...}}, migrated: bool), migrating
    an old flat singleton record in memory if that's what's on disk.
    Never raises on a missing/corrupt/legacy file -- same "degrade to
    empty, don't crash" discipline as the old _read()."""
    if not DISPATCH_STATE_PATH.exists():
        return {"dispatches": {}}, False
    try:
        raw = json.loads(DISPATCH_STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {"dispatches": {}}, False
    if not isinstance(raw, dict):
        return {"dispatches": {}}, False
    if "dispatches" in raw and isinstance(raw["dispatches"], dict):
        return raw, False
    if "stage" in raw:
        # Old flat singleton shape -- one record, no "dispatches" wrapper.
        key = raw.get("dictation_ref") or _LEGACY_FALLBACK_KEY
        return {"dispatches": {key: raw}}, True
    return {"dispatches": {}}, False


def _write(state: dict) -> None:
    DISPATCH_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    DISPATCH_STATE_PATH.write_text(json.dumps(state))
