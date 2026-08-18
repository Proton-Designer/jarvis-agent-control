"""
Blocked-session tracking (docs/SPEC-blockers.md stage 1: detect, surface,
escalate -- nothing beyond that). Unlike dispatch_state.py (one in-flight
dispatch at a time), multiple team members can be genuinely blocked
simultaneously, so this is keyed by Claude session UUID -- the stable
identity, never tmux name (a member's tmux binding can change across a
reconnect; its UUID doesn't).

Same governing principle as everywhere else state gets tracked in this
project: `surfaced` is set only once the escalation has actually been
spoken (say_feedback.speak()), matching held.json's
`surfaced_and_unresolved` -- a write to this file is not itself the
surfacing event, speaking it is. The poller (l5_console/state/poller.py)
is the only writer: it's the thing that already does the pane capture
needed to detect the state at all, so there's no separate "self-report"
path for this to go stale the way an LLM-driven one would.

The captured question/options are untrusted content end to end -- stored
and returned for display/speech only, never interpreted as instructions
by anything that reads this file.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from jarvis_paths import jarvis_home  # noqa: E402

BLOCKED_STATE_PATH = jarvis_home() / "blocked_state.json"


def _read() -> dict:
    if not BLOCKED_STATE_PATH.exists():
        return {}
    try:
        return json.loads(BLOCKED_STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _write(state: dict) -> None:
    BLOCKED_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BLOCKED_STATE_PATH.write_text(json.dumps(state))


def mark_blocked(claude_session: str, question: str, options: list[str]) -> bool:
    """Records a member as blocked if not already tracked. Returns True
    if this is a NEW blocking episode (the caller should escalate/speak
    it and then call mark_surfaced()); False if this member was already
    tracked as blocked (already surfaced or awaiting surfacing -- don't
    re-escalate every poll tick for the same episode)."""
    state = _read()
    if claude_session in state:
        return False
    state[claude_session] = {
        "question": question,
        "options": options,
        "since": time.time(),
        "surfaced": False,
    }
    _write(state)
    return True


def mark_surfaced(claude_session: str) -> None:
    state = _read()
    if claude_session in state:
        state[claude_session]["surfaced"] = True
        _write(state)


def clear_blocked(claude_session: str) -> None:
    """Called once a member is observed no longer in BLOCKED_QUESTION
    state -- resolved (Ayman or the orchestrator answered it) or moved
    on some other way. Removing the entry, not just marking it resolved,
    is deliberate: if the SAME session blocks again later, that's a new
    episode with its own `since`, not a stale continuation."""
    state = _read()
    if claude_session in state:
        del state[claude_session]
        _write(state)


def get_blocked(claude_session: str) -> dict | None:
    return _read().get(claude_session)


def all_blocked() -> dict:
    return _read()
