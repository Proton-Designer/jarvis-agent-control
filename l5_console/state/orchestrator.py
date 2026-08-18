"""
Orchestrator lifecycle state (SPEC-TUI.md SS5.4): the singleton, not a
team, lives at ~/Jarvis with a fixed tmux name (DEFAULT_ORCHESTRATOR_TARGET,
"claude-orchestrator"). Same 3-state liveness model as team members
(running / stopped / lost), same "poll fresh, never remember" rule.

session_id resolution is deliberately NOT via /status here, even though
that's the authoritative mechanism (see providers.claude_session_id) --
/status is an intrusive ~1s round-trip, adoption-time only, never a
polling operation (the Lead's explicit ruling, 2026-08-18). The
orchestrator is a genuine singleton: exactly one Claude Code session ever
runs in ~/Jarvis at a time, so unlike a multi-member team sharing a
directory, there's no ambiguity in "the most recently modified transcript
file in this directory is the live session's own history" -- verified
live, exact match against /status's authoritative answer, and it's a
cheap directory-listing + mtime-sort, appropriate for the ~1s cadence
this section polls at.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "l4_controller"))
from l2_l3_handoff import DEFAULT_ORCHESTRATOR_TARGET  # noqa: E402
from l2_l3_handoff import orchestrator_has_tools  # noqa: E402
from registry import SessionRegistry  # noqa: E402

from models import LIVENESS_LOST, LIVENESS_RUNNING, LIVENESS_STOPPED  # noqa: E402
from teams import CLAUDE_PROJECTS_DIR, encode_project_path  # noqa: E402

ORCHESTRATOR_HOME = str(Path.home() / "Jarvis")

_registry = SessionRegistry()


def _most_recent_session_id() -> str | None:
    project_dir = CLAUDE_PROJECTS_DIR / encode_project_path(ORCHESTRATOR_HOME)
    if not project_dir.is_dir():
        return None
    jsonl_files = sorted(project_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not jsonl_files:
        return None
    return jsonl_files[0].stem


def _has_any_history() -> bool:
    return _most_recent_session_id() is not None


def compute_orchestrator_state() -> dict:
    """Returns {"liveness", "session_id", "tools_reachable"} -- the caller
    (the poller) attaches polled_at/expected_interval/error, since those
    are cross-cutting concerns this function doesn't need to know about."""
    # SessionRegistry doesn't expose a direct has-session check; reuse the
    # same live-session enumeration everything else in this codebase
    # already trusts, rather than adding a second way to ask tmux the
    # same question.
    live_names = {s.session_id for s in _registry.list_sessions()}
    is_running = DEFAULT_ORCHESTRATOR_TARGET in live_names

    if is_running:
        return {
            "liveness": LIVENESS_RUNNING,
            "session_id": _most_recent_session_id(),
            "tools_reachable": orchestrator_has_tools(),
        }

    session_id = _most_recent_session_id()
    if session_id is not None:
        return {"liveness": LIVENESS_STOPPED, "session_id": session_id, "tools_reachable": False}
    return {"liveness": LIVENESS_LOST, "session_id": None, "tools_reachable": False}
