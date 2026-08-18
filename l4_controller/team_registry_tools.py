"""
Router-facing wrappers around the team registry (SPEC-orchestration.md
SS1.3). The registry itself -- storage, discovery, liveness computation --
is NOT reimplemented here: it already exists as l5_console/state/teams.py
+ setup.py (~/Jarvis/teams.json), built for the console's Adopt/Create-Fresh
flows. This module gives the router the SAME functions by direct import,
not a second registry or a second file format -- "this is also what the
console's add-team flow should write to" (the Lead, 2026-08-18).

Registry vs. liveness, restated for a router caller: teams.json records
INTENT (id, aliases, root, members' identity) -- never status, structurally
(Team/TeamMember as persisted have no status field to disagree with a live
poll in the first place). Every read here recomputes liveness fresh via
teams.discover_teams_and_unassigned(), the exact function the console's
poller calls -- there is one persisted file and one liveness computation,
not two pollers that could disagree.

Sonnet is allowed to pay this call's real latency (tmux list-sessions plus
per-member file-existence checks -- cheap, but a live subprocess round
trip, not a memory read) because SS1's whole point is that nothing waits on
the router; Haiku's tools instead read the console's cached poll layer
(l5_console/state/api.py) because Haiku's tier cannot afford even that.
Two different latency budgets consuming the same underlying source, not
two sources of truth.

SURVIVES A ROUTER RESTART OR COMPACTION FOR FREE: teams.json is a file on
disk, untouched by anything in the router's own context window. Nothing
here, or in teams.py, ever treats the router's conversation memory as the
record of what teams exist -- every call re-reads the file from disk.

WHAT HAPPENS WHEN A REGISTERED TEAM'S SESSION NO LONGER EXISTS: not a
special case handled here -- it already falls out of
discover_teams_and_unassigned() as LIVENESS_STOPPED (transcript history on
disk, reconnectable) or LIVENESS_LOST (no history), the same 3-state result
the console already renders. A router caller sees exactly what the console
sees for the same team, from the same computation.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "l5_console" / "state"))
from teams import discover_teams_and_unassigned, load_registry  # noqa: E402
from setup import adopt_candidate_info, create_fresh_member, create_team  # noqa: E402


def _team_to_dict(team) -> dict:
    return {
        "id": team.id,
        "aliases": team.aliases,
        "root": team.root,
        # Renamed alongside models.py's Team/TeamMember rename
        # (SPEC-teams.md SS2 -- "lead", not "inbox"; has_lead is new,
        # distinguishing "no lead assigned" from "lead unreachable").
        "has_lead": team.has_lead,
        "lead_reachable": team.lead_reachable,
        "members": [
            {
                "tmux": m.tmux,
                "claude_session": m.claude_session,
                "liveness": m.liveness,
                "is_lead": m.is_lead,
            }
            for m in team.members
        ],
    }


def list_teams() -> list[dict]:
    """Every registered team with FRESH liveness -- never the registry's
    own stored intent alone. Callers that need to know whether a team is
    actually reachable right now should call this, not load_registry()
    directly (which only has identity, by design -- see module docstring).
    """
    teams, _unassigned = discover_teams_and_unassigned()
    return [_team_to_dict(t) for t in teams]


def _working_dir_for_unassigned(tmux_name: str) -> str | None:
    """The live working directory for a currently-unassigned tmux session
    -- NEVER guessed or asked of the caller. Must match byte-for-byte what
    teams.member_liveness() will compare against on every future poll (see
    teams.py's own docstring on why root/working_dir are exact, never
    display_path)."""
    _teams, unassigned = discover_teams_and_unassigned()
    for u in unassigned:
        if u.tmux == tmux_name:
            return u.working_dir
    return None


def register_team_by_adoption(
    team_id: str, tmux_name: str, aliases: list[str] | None = None, is_inbox: bool = True
) -> dict:
    """Registers an ALREADY-RUNNING, currently-unassigned tmux/Claude Code
    session as a new one-member team -- voice registration's common case
    (Ayman already started a session and wants Jarvis to know about it).
    Reuses adopt_candidate_info() + create_team() verbatim, the exact
    functions the console's Adopt flow already calls
    (l5_console/app/setup_flow.py) -- not a second identity-resolution
    path.

    Returns {"ok": False, "detail": ...} rather than raising on any
    refusal -- an uncaught exception is a worse outcome for a router-facing
    tool than a spoken refusal with a reason.
    """
    if any(t["id"] == team_id for t in load_registry()):
        return {"ok": False, "detail": f"a team called {team_id!r} is already registered"}

    root = _working_dir_for_unassigned(tmux_name)
    if root is None:
        return {
            "ok": False,
            "detail": f"no currently-unassigned tmux session named {tmux_name!r} "
            "(it may not be running, or may already belong to a team)",
        }

    info = adopt_candidate_info(tmux_name)
    if info["claude_session"] is None:
        return {
            "ok": False,
            "detail": f"{tmux_name} did not report a Claude session id via /status "
            "-- is it actually a running Claude Code session?",
        }

    create_team(
        team_id=team_id,
        aliases=aliases or [],
        root=root,
        inbox_tmux=tmux_name if is_inbox else "",
        # model included -- SPEC-teams.md SS6, dropping it here meant the
        # console's TeamsPanel rendered no model at all despite
        # adopt_candidate_info() already having captured it (found live
        # 2026-08-18, same gap fixed at the same time in setup_flow.py's
        # own members-list construction).
        members=[{"tmux": tmux_name, "claude_session": info["claude_session"], "model": info.get("model")}],
    )
    return {"ok": True, "detail": f"registered {tmux_name} as team {team_id!r}"}


def register_team_fresh(
    team_id: str, root: str, aliases: list[str] | None = None, model: str = "sonnet"
) -> dict:
    """Launches a brand-new Claude Code session at `root` and registers it
    as a new one-member team -- wraps setup.create_fresh_member() +
    create_team() verbatim, the console's Create-Fresh path. Slower than
    adoption (a real tmux launch, MCP-trust preseed, and a wait-for-ready
    poll), which is fine here: this is a router tool, no latency budget
    (SS1.3)."""
    if any(t["id"] == team_id for t in load_registry()):
        return {"ok": False, "detail": f"a team called {team_id!r} is already registered"}

    tmux_name = f"claude-{team_id}"
    result = create_fresh_member(tmux_name, root, model)
    if not result["ok"]:
        return {"ok": False, "detail": result["detail"]}

    # The RESOLVED root, not the caller's original -- create_fresh_member
    # may have collapsed a symlinked prefix (e.g. /tmp -> /private/tmp),
    # and registering under any other value would not byte-for-byte match
    # the live session's actual pane_current_path (see that function's
    # docstring for the live-found incident this guards against).
    resolved_root = result["root"]
    create_team(
        team_id=team_id,
        aliases=aliases or [],
        root=resolved_root,
        inbox_tmux=tmux_name,
        members=[{"tmux": tmux_name, "claude_session": result["claude_session"], "model": model}],
    )
    return {"ok": True, "detail": f"created {tmux_name} at {resolved_root} and registered it as team {team_id!r}"}
