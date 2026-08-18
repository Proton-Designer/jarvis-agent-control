"""
Shared, layout-independent formatting logic -- used by both rail.py
(condensed) and console.py (fuller detail) so the two densities can't
silently disagree about what a liveness icon means or how a team's
overall state is derived. Factored out once needed in two places, same
reasoning as l2_5_concierge/session_match.py's own extraction.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "state"))
from models import LIVENESS_RUNNING, LIVENESS_STOPPED, LIVENESS_LOST  # noqa: E402


def liveness_icon(liveness: str) -> str:
    return {LIVENESS_RUNNING: "●", LIVENESS_STOPPED: "○", LIVENESS_LOST: "✕"}.get(liveness, "?")


def team_liveness(team) -> str:
    """Derived from members, never stored -- per the Lead's ruling: a
    team-level liveness field that could disagree with its own members
    is a second source of truth that can drift from the first. running
    if ANY member is running (the team is reachable through at least one
    path); otherwise stopped if any member has resumable history, else
    lost."""
    if not team.members:
        return LIVENESS_LOST
    if any(m.liveness == LIVENESS_RUNNING for m in team.members):
        return LIVENESS_RUNNING
    if any(m.liveness == LIVENESS_STOPPED for m in team.members):
        return LIVENESS_STOPPED
    return LIVENESS_LOST
