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

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from jarvis_paths import jarvis_project_home  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "l4_controller"))
import blocked_state  # noqa: E402
from l2_l3_handoff import DEFAULT_ORCHESTRATOR_TARGET  # noqa: E402
from providers import list_sessions, session_activity  # noqa: E402

from models import LIVENESS_LOST, LIVENESS_RUNNING, LIVENESS_STOPPED, Team, TeamMember, UnassignedSession  # noqa: E402

# jarvis_project_home(), not a hardcoded Path.home()/"Jarvis" -- a real
# collision (2026-08-18): a canary backed up this file, replaced it with
# test data mid-run, and restored it in a `finally` -- a real
# registration made during that window was read as gone, and the restore
# only happened to land correctly because nothing else raced it. Under
# JARVIS_TEST_RUN this resolves to an isolated path a canary structurally
# cannot make the real file disappear from, even if it crashes before its
# own cleanup runs. See jarvis_paths.py's module docstring for the full
# incident.
TEAMS_REGISTRY_PATH = jarvis_project_home() / "teams.json"
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


DISPLAY_PATH_MAX_LEN = 40


def _display_path(path: str) -> str:
    """Collapses a $HOME prefix to "~" first (loses no information), then
    -- if still long -- keeps only the last two path components behind a
    leading ellipsis. $HOME-collapsing alone doesn't help a path entirely
    outside it: the actual case that prompted this (found live
    2026-08-18) was a ~120-char /private/tmp/claude-.../scratchpad/...
    session path, unreadable at any console width and not under $HOME at
    all. The tail (the directory name Ayman would actually recognize) is
    what matters for identification; this is a reasonable default, not a
    substitute for width-aware truncation, which belongs at render time
    where the real available width is known."""
    home = str(Path.home())
    if path == home:
        collapsed = "~"
    elif path.startswith(home + "/"):
        collapsed = "~" + path[len(home):]
    else:
        collapsed = path

    if len(collapsed) <= DISPLAY_PATH_MAX_LEN:
        return collapsed
    parts = collapsed.rstrip("/").split("/")
    tail = "/".join(parts[-2:]) if len(parts) >= 2 else collapsed
    return f".../{tail}"


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


def _lead_claude_session(entry: dict) -> str | None:
    """Resolves a team's lead pointer to a claude_session (SPEC-teams.md
    SS2: "key it on claude_session, not tmux name"). Reads-only migration
    for an OLD record: a legacy `entry["inbox"]` value is a TMUX NAME, not
    a claude_session -- resolved here by looking up which member currently
    has that tmux, WITHOUT writing anything back (this module never
    writes; the on-disk key only actually becomes "lead" the next time
    set_lead() runs for that team -- see team_actions.py). A legacy
    pointer to a tmux name that matches no current member (renamed,
    removed) resolves to None, same as no lead at all -- fails closed,
    never a stale name mistaken for a live pointer."""
    lead = entry.get("lead")
    if lead is not None:
        return lead
    legacy_inbox_tmux = entry.get("inbox")
    if not legacy_inbox_tmux:
        return None
    member = next((m for m in entry.get("members", []) if m.get("tmux") == legacy_inbox_tmux), None)
    return member["claude_session"] if member else None


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
    # Local import, deliberately not at module top level: there's a real
    # 3-way circular import here (engine_roles.py -> reconnect.py -> back
    # to teams.py for member_liveness/load_registry; separately
    # engine_roles.py -> teams.py directly for CLAUDE_PROJECTS_DIR/
    # encode_project_path). No placement of a top-level `import
    # engine_roles` in this file survives every possible "which module
    # gets imported first" ordering -- verified live across four
    # different entry points (teams first, engine_roles first, reconnect
    # first), each breaking a different top-level placement. A call-time
    # import sidesteps the whole ordering question: by the time this
    # function is actually CALLED (never during another module's own
    # load), the full module graph has already finished importing.
    import engine_roles

    live_sessions = list_sessions()
    live_by_name = {s["session_id"]: s for s in live_sessions}

    registry = load_registry()
    teams: list[Team] = []
    claimed_tmux_names: set[str] = set()

    # Local import, same reason as the engine_roles one below: this
    # module (team_context.py) itself imports load_registry/save_registry
    # from teams.py, so a top-level import here would be circular.
    import team_context

    for entry in registry:
        lead_claude_session = _lead_claude_session(entry)
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
                    is_lead=(m["claude_session"] == lead_claude_session),
                    model=m.get("model"),
                    effort=m.get("effort"),
                    identity_verified_at=m.get("identity_verified_at"),
                )
            )
        lead_member = next((mm for mm in members if mm.is_lead), None)
        has_lead = lead_claude_session is not None
        lead_reachable = bool(lead_member and lead_member.liveness == LIVENESS_RUNNING)
        context_fields = team_context.context_fields_for(entry)
        teams.append(
            Team(
                id=entry["id"],
                aliases=list(entry.get("aliases", [])),
                root=entry["root"],
                display_path=_display_path(entry["root"]),
                has_lead=has_lead,
                lead_reachable=lead_reachable,
                members=members,
                **context_fields,
            )
        )

    # ENGINE roles (SPEC-engine-roles.md) are a second kind of "already
    # spoken for" a live tmux session, separate from team membership --
    # found live 2026-08-18 attaching real concierge/orchestrator
    # sessions: without this exclusion they showed up in TeamsPanel/
    # RailUnassigned as "unassigned -- press 'a' to adopt", which is
    # actively wrong (adopting one into a team would collide with its
    # Engine attachment) and would have confused Ayman into thinking
    # action was needed on a session that's already correctly assigned.
    # Same exclusion shape as DEFAULT_ORCHESTRATOR_TARGET below -- a
    # session can be legitimately claimed by something other than a team.
    engine_attached_tmux_names = {
        record["tmux"]
        for role in engine_roles.ROLES
        if (record := engine_roles.get_role_record(role)) is not None
    }

    unassigned = [
        UnassignedSession(
            tmux=s["session_id"], claude_session=None,
            working_dir=s["working_dir"], display_path=_display_path(s["working_dir"]),
        )
        for s in live_sessions
        if s["session_id"] not in claimed_tmux_names
        # The orchestrator is a singleton with its own OrchestratorState
        # (SS5.4), not a team-adoption candidate -- excluded here so it
        # never shows up conflated with real unassigned project sessions.
        and s["session_id"] != DEFAULT_ORCHESTRATOR_TARGET
        and s["session_id"] not in engine_attached_tmux_names
    ]

    return teams, unassigned


def enrich_running_members(teams: list[Team]) -> list[tuple[Team, TeamMember]]:
    """The "expensive" per-member tier (SS6.1, docs/SPEC-blockers.md stage
    1): one pane-capture round-trip per RUNNING member
    (providers.session_activity()) to fill in TeamMember.activity, and to
    detect/track blocked-question episodes via blocked_state.py.
    Deliberately separate from discover_teams_and_unassigned (the cheap
    tier) -- called by the caller on its own cadence, not fused into every
    liveness check.

    Mutates the given Team/TeamMember objects in place and returns the
    subset of members that just became NEWLY blocked (i.e.
    blocked_state.mark_blocked() returned True this call). Only file
    bookkeeping happens here -- deciding whether/how to escalate (speak)
    a newly-blocked member, and calling blocked_state.mark_surfaced() once
    that's actually done, is left to the caller (poller.py). Triggering
    real audio from inside a plain state-reading function would be exactly
    the kind of unannounced-audio surprise this project has been burned by
    before and specifically guards against."""
    newly_blocked: list[tuple[Team, TeamMember]] = []
    for team in teams:
        for member in team.members:
            if member.liveness != LIVENESS_RUNNING or member.tmux is None:
                continue

            result = session_activity(member.tmux)
            member.activity = result.get("activity")

            tracked = blocked_state.get_blocked(member.claude_session)
            if result.get("state") == "blocked_question" and result.get("blocked"):
                blocked = result["blocked"]
                if tracked is None:
                    blocked_state.mark_blocked(member.claude_session, blocked["question"], blocked["options"])
                    tracked = blocked_state.get_blocked(member.claude_session)
                    newly_blocked.append((team, member))
                member.blocked_question = tracked["question"]
                member.blocked_since = tracked["since"]
                member.blocked_surfaced = tracked["surfaced"]
            else:
                if tracked is not None:
                    blocked_state.clear_blocked(member.claude_session)
                member.blocked_question = None
                member.blocked_since = None
                member.blocked_surfaced = False

    return newly_blocked
