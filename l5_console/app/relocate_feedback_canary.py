#!/usr/bin/env python3
"""Canary for U2 (docs/PLAN-silence-and-ux.md): TeamManageScreen's relocate
picker was the one to_thread call site in the whole app with no immediate
feedback -- select an item, then nothing visible for however long
relocate_team_member() takes (measured live, 2026-08-20: ~1.2s, one real
/status round trip) before the screen updates at all.

Every OTHER to_thread call site in the app (engine_flow.py's create/
attach, quick_adopt_flow.py, revive_flow.py, this screen's own
_do_refresh_context) already showed a working-state message first --
this proves relocate now matches that pattern, and checks the ORDER, not
just that a message eventually appears: "Relocating <tmux>..." must be
visible WHILE the slow call is still in flight, not only after it
resolves (a check that only asserts "the final state is correct" would
pass even if the fix were reverted, since the final state was already
correct before this fix -- only the interim silence was the bug).

Monkeypatches team_actions.relocate_team_member() to a real (short)
asyncio-friendly sleep rather than driving a real tmux session -- proves
the ORDERING, which doesn't need a real /status round trip; the ~1.2s
live number is measured separately and cited above, not re-measured here.

    l5_console/app/.venv/bin/python3 l5_console/app/relocate_feedback_canary.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "state"))

from textual.app import App, ComposeResult  # noqa: E402
from textual.widgets import Static  # noqa: E402

import team_actions  # noqa: E402
import team_flow  # noqa: E402
from models import Team, TeamMember, UnassignedSession, LIVENESS_RUNNING  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((name, passed, detail))
    print(f"  {'ok  ' if passed else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not passed else ""))


FAKE_TEAM = Team(
    id="relocatecanary", aliases=["relocatecanary"], root="/tmp/relocatecanary-old",
    has_lead=True, lead_reachable=True,
    members=[TeamMember(tmux="fake-relocate-tmux", claude_session="fake-session", liveness=LIVENESS_RUNNING, activity=None, is_lead=True)],
)
FAKE_UNASSIGNED = [UnassignedSession(tmux="fake-relocate-tmux", claude_session=None, working_dir="/tmp/relocatecanary-new", display_path="~/relocatecanary-new")]


class _FakeState:
    teams = [FAKE_TEAM]
    unassigned = FAKE_UNASSIGNED


class _T(App):
    def on_mount(self) -> None:
        self.push_screen(team_flow.TeamManageScreen())


def _slow_relocate_sync(team_id: str, tmux: str) -> dict:
    # asyncio.to_thread() runs a SYNC callable in a worker thread -- the
    # real relocate_team_member() is sync (subprocess-based), so the stub
    # must be too.
    time.sleep(0.3)
    return {"ok": True, "detail": "relocated fake-relocate-tmux (stubbed)"}


async def main() -> int:
    original_relocate = team_actions.relocate_team_member
    team_actions.relocate_team_member = _slow_relocate_sync
    original_get_state = team_flow.state.get_state
    team_flow.state.get_state = lambda: _FakeState()

    try:
        app = _T()
        async with app.run_test() as pilot:
            screen = app.screen
            assert isinstance(screen, team_flow.TeamManageScreen)
            screen.team_id = "relocatecanary"
            await screen._show_relocate_step()
            await pilot.pause()

            task = asyncio.create_task(screen._on_relocate_chosen("fake-relocate-tmux"))
            await asyncio.sleep(0.05)  # well before the stub's 0.3s resolves

            body = screen._body()
            parts = []
            for c in body.children:
                if hasattr(c, "render"):
                    r = c.render()
                    parts.append(r.plain if hasattr(r, "plain") else str(r))
            mid_text = "\n".join(parts)
            check(
                "the working message is visible WHILE the slow call is still in flight",
                "Relocating" in mid_text and "fake-relocate-tmux" in mid_text,
                mid_text,
            )
            check("the slow call has NOT resolved yet at this point (proves this isn't a race)", not task.done())

            await task
            await pilot.pause()

            result_text = screen.query_one("#team_manage_result", Static)
            r = result_text.render()
            final_text = r.plain if hasattr(r, "plain") else str(r)
            check("after it resolves, the real result lands correctly", "relocated" in final_text, final_text)

    finally:
        team_actions.relocate_team_member = original_relocate
        team_flow.state.get_state = original_get_state

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
    sys.exit(asyncio.run(main()))
