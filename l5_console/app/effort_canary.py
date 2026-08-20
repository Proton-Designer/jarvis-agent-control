#!/usr/bin/env python3
"""Canary for effort collection + render (TODO-feature-queue.md item 2).

Two halves, both real gaps before this change:
  1. COLLECTION: no effort step existed in the fresh-team flow at all
     (setup_flow.py), so TeamMember.effort was always None. Fixed by
     adding a step, the model step's obvious sibling, between model and
     launch.
  2. RENDER: even once collected, nothing showed it. Fixed with
     format_helpers.model_effort_suffix(), shared by console.py's
     TeamsPanel and team_flow.py's detail view so they can't disagree.

BOTH DIRECTIONS, same discipline as blocked_render_canary.py /
identity_staleness_canary.py: a check that only proves "effort shows up"
passes just as happily if a placeholder like "unknown" showed up for
every adopted member too -- which would be worse than silence, because it
would look like a real fact. Every present-case check has an
absent-case counterpart.

Pure functions only for the render half (no Textual app). The collection
half is verified against the real state layer (isolated JARVIS_TEST_RUN
registry, no real tmux/Claude launch needed -- create_team()/
discover_teams_and_unassigned() don't touch tmux at all, only
create_fresh_member() does, and that's setup.py's concern, already
covered by its own effort passthrough docstring + team_registry_tools_canary.py).

    l5_console/app/.venv/bin/python3 l5_console/app/effort_canary.py
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

os.environ["JARVIS_TEST_RUN"] = f"effort-canary-{uuid.uuid4().hex[:8]}"

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "state"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "l4_controller"))

import format_helpers as fh  # noqa: E402
from models import TeamMember, LIVENESS_RUNNING  # noqa: E402
from teams import TEAMS_REGISTRY_PATH, save_registry, discover_teams_and_unassigned  # noqa: E402

assert "test_runs" in str(TEAMS_REGISTRY_PATH), (
    f"TEAMS_REGISTRY_PATH is NOT test-isolated ({TEAMS_REGISTRY_PATH}) -- refusing to run"
)

FAILURES: list[str] = []


def check(desc: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ok    {desc}")
    else:
        FAILURES.append(desc)
        print(f"  FAIL  {desc}{('  -- ' + detail) if detail else ''}")


def member(**kw) -> TeamMember:
    base = dict(tmux="t", claude_session="u", liveness=LIVENESS_RUNNING, activity=None, is_lead=False)
    base.update(kw)
    return TeamMember(**base)


def run() -> int:
    print("RENDER half -- model_effort_suffix(), both directions")
    both = member(model="sonnet", effort="high")
    check("model + effort fold together", fh.model_effort_suffix(both) == "sonnet · high", fh.model_effort_suffix(both))

    model_only = member(model="opus (claude-opus-5)", effort=None)
    check("effort=None is OMITTED, never a placeholder", fh.model_effort_suffix(model_only) == "opus",
          fh.model_effort_suffix(model_only))
    check("...never literally 'None' in the string", "None" not in fh.model_effort_suffix(model_only))

    effort_only = member(model=None, effort="low")
    check("model=None still shows a known effort", fh.model_effort_suffix(effort_only) == "low",
          fh.model_effort_suffix(effort_only))

    neither = member(model=None, effort=None)
    check("neither known -- empty string, not a placeholder", fh.model_effort_suffix(neither) == "")

    print()
    print("COLLECTION half -- flows through the real state layer, both member kinds")
    save_registry([{
        "id": "effortcanary", "aliases": ["effortcanary"], "root": "/tmp/effortcanary-nonexistent",
        "lead": "fresh-session",
        "members": [
            # A FRESH member: setup_flow.py's effort step ran, a real value was chosen.
            {"tmux": "nonexistent-fresh", "claude_session": "fresh-session", "model": "sonnet", "effort": "high"},
            # An ADOPTED member: no "effort" key at all, matching what
            # setup_state.adopt_candidate_info() actually returns
            # (verified live 2026-08-20: /status has no Effort field).
            {"tmux": "nonexistent-adopted", "claude_session": "adopted-session", "model": "opus (claude-opus-5)"},
        ],
    }])
    teams, _ = discover_teams_and_unassigned()
    t = next(tm for tm in teams if tm.id == "effortcanary")
    fresh_m = next(m for m in t.members if m.claude_session == "fresh-session")
    adopted_m = next(m for m in t.members if m.claude_session == "adopted-session")

    check("a fresh member's chosen effort survives the round trip", fresh_m.effort == "high", fresh_m.effort)
    check("an adopted member's effort is None -- never invented", adopted_m.effort is None, adopted_m.effort)
    check("...and renders accordingly", fh.model_effort_suffix(fresh_m) == "sonnet · high" and fh.model_effort_suffix(adopted_m) == "opus")

    import shutil
    shutil.rmtree(TEAMS_REGISTRY_PATH.parent, ignore_errors=True)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(run())
