"""
TEMPORARY. gu2s6tnt has published l5_console/state/models.py (the agreed
dataclass contract) but not yet the poller/get_state() function behind
it -- they're building that next. This module fabricates a JarvisState
matching the exact contract shape so Rail can be built and visually
iterated on right now instead of blocking on their implementation
timeline.

Swap-out point, when state.get_state() exists: replace the single
`from fixtures import fixture_state as get_state` import in main.py with
`from state.poller import get_state` (or wherever they land it) -- every
widget in this app reads through that one call, nothing else references
this module.

Delete this file once the real get_state() lands. Not meant to survive
past Rail's first working version against real data.
"""
from __future__ import annotations

import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "state"))
from models import (  # noqa: E402
    JarvisState, OrchestratorState, Team, TeamMember, WakeDaemonState,
    RuntimeState, UnassignedSession, LIVENESS_RUNNING, LIVENESS_STOPPED, LIVENESS_LOST,
)


def fixture_state() -> JarvisState:
    now = time.time()
    return JarvisState(
        orchestrator=OrchestratorState(
            polled_at=now, expected_interval=1.0, error=None,
            liveness=LIVENESS_RUNNING, session_id="d5c86853-317c-4895-97be-3344efd1a1bd",
            tools_reachable=True,
        ),
        teams=[
            Team(
                id="api", aliases=["the api project", "api", "api gateway"],
                root="/Users/aymanmohammed/work/api", inbox_reachable=True,
                members=[
                    TeamMember(tmux="claude-api-lead", claude_session="fe0563eb-cf66-444c-9abf-3c489828c0da",
                               liveness=LIVENESS_RUNNING, activity="busy", is_inbox=True),
                    TeamMember(tmux="claude-api-eng1", claude_session="a91c2f70-0000-0000-0000-000000000000",
                               liveness=LIVENESS_RUNNING, activity="idle", is_inbox=False),
                ],
            ),
            Team(
                id="mobile", aliases=["mobile", "mobile app"],
                root="/Users/aymanmohammed/work/mobile", inbox_reachable=False,
                members=[
                    TeamMember(tmux=None, claude_session="9fa1aaf2-9c0b-4cc5-b95c-62b7036cb4f5",
                               liveness=LIVENESS_STOPPED, activity=None, is_inbox=True),
                ],
            ),
            Team(
                id="docs", aliases=["docs"],
                root="/Users/aymanmohammed/work/docs", inbox_reachable=False,
                members=[
                    TeamMember(tmux=None, claude_session="00000000-0000-0000-0000-000000000000",
                               liveness=LIVENESS_LOST, activity=None, is_inbox=True),
                ],
            ),
        ],
        teams_polled_at=now, teams_expected_interval=4.0, teams_error=None,
        wake=WakeDaemonState(polled_at=now, expected_interval=1.0, error=None, running=True),
        runtime=RuntimeState(
            polled_at=now, expected_interval=2.5, error=None,
            models_resident=["qwen2.5:7b-instruct-q4_K_M"], memory_free_pct=38.0,
            spend_polled_at=now, spend_expected_interval=45.0, spend_error=None,
            spend={"ok": True, "summary": "$0.42 this session, 9% of limit", "raw": None},
        ),
        unassigned=[
            UnassignedSession(tmux="claude-scratch-test", claude_session=None, working_dir="/private/tmp"),
        ],
    )
