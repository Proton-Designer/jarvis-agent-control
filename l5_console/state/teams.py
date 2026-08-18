"""
Teams registry + discovery (SPEC-TUI.md SS4).

The registry (`~/Jarvis/teams.json`, alongside CLAUDE.md -- personal,
persistent config, NOT `~/.jarvis`'s ephemeral runtime artifacts) stores
only what cannot be discovered: root directory, aliases, inbox, and each
member's stable Claude session UUID.

**The governing rule: the registry records intent; liveness is always
polled, never remembered.** A team marked live in a file that's actually
dead is the failure this project has produced repeatedly (the toolless
orchestrator, the fail-open cancel socket, the held-instruction
resurrection bug). Nothing in this module ever writes "running"/"stopped"
into teams.json -- those are computed fresh on every call to
discover_teams_and_unassigned(), never stored as truth.

Identity vs. liveness, per the Lead's explicit ruling (2026-08-18):
  - Establishing a member's Claude session UUID (`claude_session_id()` in
    l4_controller/providers.py, via /status) is an intrusive ~1s
    round-trip. It happens ONCE, at adoption time (SS5.1), and the result
    is written to teams.json. It is NEVER called from this module's
    per-poll discovery path.
  - Per-poll liveness instead matches on tmux session name + working
    directory against what's already stored -- cheap (tmux list-sessions,
    a file-existence check), and it's answering a different question
    ("is this known member still alive") than identity resolution
    ("what is this pane's identity") is.
  - Edge case handled explicitly: a stored member's tmux name currently
    hosts a DIFFERENT session (Ayman killed the pane, started something
    else with the same name). Working-directory match is the cheap guard
    -- if the live session at that tmux name has a DIFFERENT cwd than the
    team's root, this is not that member. Fails closed: reported as not
    currently running (falls through to the stopped/lost check), never
    silently bound to the wrong pane.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "l4_controller"))
from l2_l3_handoff import DEFAULT_ORCHESTRATOR_TARGET  # noqa: E402
from providers import list_sessions  # noqa: E402

from models import LIVENESS_LOST, LIVENESS_RUNNING, LIVENESS_STOPPED, Team, TeamMember, UnassignedSession  # noqa: E402

TEAMS_REGISTRY_PATH = Path.home() / "Jarvis" / "teams.json"
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def encode_project_path(path: str) -> str:
    """Claude Code's own encoding for ~/.claude/projects/<encoded>/ --
    verified live (2026-08-17/18): both "/" and "_" collapse to "-".
    Forward-only (real path -> expected directory name); not reliably
    reversible in general (two different real paths could theoretically
    collide), but that's not needed here -- we only ever check a
    specific, already-known root for its own history, never try to
    recover a path from a directory name."""
    return path.replace("/", "-").replace("_", "-")


def load_registry() -> list[dict]:
    if not TEAMS_REGISTRY_PATH.exists():
        return []
    try:
        return json.loads(TEAMS_REGISTRY_PATH.read_text())
    except json.JSONDecodeError:
        return []


def save_registry(teams: list[dict]) -> None:
    TEAMS_REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    TEAMS_REGISTRY_PATH.write_text(json.dumps(teams, indent=2))


def _has_history(root: str, claude_session: str) -> bool:
    """Stopped (recoverable) vs. lost (nothing to resume) -- SS4.4's
    3-state distinction, which determines whether a Reconnect action
    exists at all."""
    jsonl_path = CLAUDE_PROJECTS_DIR / encode_project_path(root) / f"{claude_session}.jsonl"
    return jsonl_path.exists()


def member_liveness(root: str, member: dict, live_by_name: dict[str, dict]) -> tuple[str, str | None, str | None]:
    """Returns (liveness, tmux_binding, activity). tmux_binding is the
    CURRENT tmux name if genuinely running, else None."""
    stored_tmux = member.get("tmux")
    claude_session = member["claude_session"]

    live = live_by_name.get(stored_tmux) if stored_tmux else None
    if live is not None and live["working_dir"] == root:
        # Genuinely this member: name matches AND cwd matches. Activity
        # detail (session_activity, a real pane-capture call) is
        # deliberately NOT fetched here -- that's the "expensive" clock
        # per SS6.1, left to the caller/poller to fill in on its own
        # slower cadence, not bundled into this cheap liveness check.
        return LIVENESS_RUNNING, stored_tmux, None

    # Either not currently bound to a live tmux session, or the name is
    # live but hosts something else now (cwd mismatch) -- fails closed
    # either way: not treated as this member running.
    if _has_history(root, claude_session):
        return LIVENESS_STOPPED, None, None
    return LIVENESS_LOST, None, None


def discover_teams_and_unassigned() -> tuple[list[Team], list[UnassignedSession]]:
    """The core discovery pass (SS4.2): group live sessions by working
    directory (the team identity), reconcile against the stored
    registry for known teams, and report everything else as unassigned
    (SS6, "how you discover something to adopt"). Cheap -- tmux
    list-sessions plus per-member file-existence checks, no pane
    captures, no /status calls."""
    live_sessions = list_sessions()
    live_by_name = {s["session_id"]: s for s in live_sessions}

    registry = load_registry()
    teams: list[Team] = []
    claimed_tmux_names: set[str] = set()

    for entry in registry:
        members: list[TeamMember] = []
        for m in entry.get("members", []):
            liveness, tmux_binding, activity = member_liveness(entry["root"], m, live_by_name)
            if tmux_binding:
                claimed_tmux_names.add(tmux_binding)
            members.append(
                TeamMember(
                    tmux=tmux_binding,
                    claude_session=m["claude_session"],
                    liveness=liveness,
                    activity=activity,
                    is_inbox=(m.get("tmux") == entry.get("inbox")),
                )
            )
        inbox_member = next((mm for mm in members if mm.is_inbox), None)
        inbox_reachable = bool(inbox_member and inbox_member.liveness == LIVENESS_RUNNING)
        teams.append(
            Team(
                id=entry["id"],
                aliases=list(entry.get("aliases", [])),
                root=entry["root"],
                inbox_reachable=inbox_reachable,
                members=members,
            )
        )

    unassigned = [
        UnassignedSession(tmux=s["session_id"], claude_session=None, working_dir=s["working_dir"])
        for s in live_sessions
        if s["session_id"] not in claimed_tmux_names
        # The orchestrator is a singleton with its own OrchestratorState
        # (SS5.4), not a team-adoption candidate -- excluded here so it
        # never shows up conflated with real unassigned project sessions.
        and s["session_id"] != DEFAULT_ORCHESTRATOR_TARGET
    ]

    return teams, unassigned
