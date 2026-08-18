"""
Team management actions (SPEC-teams.md): everything beyond create/adopt
(team_registry_tools.py, ue6rruxg's) and discovery (teams.py) -- pick-time
uniqueness checks, lead assignment, relocate, and remove. New module
rather than extending team_registry_tools.py, to keep that file's
ownership boundary ("I own these three functions + their correctness")
intact.

All writes here go through teams.load_registry()/save_registry() --
there is exactly one persisted shape and one place that reads/writes it,
same discipline as everywhere else state gets a single source of truth
in this project.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "l4_controller"))
from providers import claude_session_id, list_sessions as _list_live_sessions  # noqa: E402

import team_context  # noqa: E402
from teams import _lead_claude_session, load_registry, save_registry  # noqa: E402


# --- SS1.2: uniqueness, checkable at pick time AND enforced at write time
# (setup.create_team() also enforces all three -- these are the same
# checks, exposed standalone so the UI can refuse before the rest of the
# flow runs, per "refuse at the moment the directory is chosen, not
# after"). A UI-only check is advisory; the real boundary is in
# create_team() itself. -----------------------------------------------

def check_root_available(root: str) -> dict:
    """{"ok": bool, "detail": str, "conflicting_team": str|None}.
    Resolves `root` before comparing (SS1.1's /tmp-symlink discipline)."""
    resolved = str(Path(root).resolve())
    for entry in load_registry():
        if entry["root"] == resolved:
            return {
                "ok": False,
                "detail": f"That directory is already registered as team {entry['id']!r}.",
                "conflicting_team": entry["id"],
            }
    return {"ok": True, "detail": "", "conflicting_team": None}


def check_team_id_available(team_id: str) -> dict:
    if any(t["id"] == team_id for t in load_registry()):
        return {"ok": False, "detail": f"a team called {team_id!r} is already registered"}
    return {"ok": True, "detail": ""}


def check_alias_available(alias: str) -> dict:
    """Case-insensitive -- best-available reading of "however the router
    normalises" (nothing code-level normalizes team aliases today)."""
    normalized = alias.strip().lower()
    for entry in load_registry():
        for a in entry.get("aliases", []):
            if a.strip().lower() == normalized:
                return {
                    "ok": False,
                    "detail": f"{alias!r} is already used by team {entry['id']!r}.",
                    "conflicting_team": entry["id"],
                }
    return {"ok": True, "detail": "", "conflicting_team": None}


# --- SS2: the lead --------------------------------------------------

def set_lead(team_id: str, claude_session: str) -> dict:
    """Points team_id's lead at claude_session. Refuses if claude_session
    is not one of this team's own members (SS2's "nothing currently
    stops pointing at another team's agent"). Also completes the
    inbox->lead migration for this team in place (writes the new "lead"
    key, drops the old "inbox" key if present) -- teams.py's read path
    already understands both shapes, this is what makes the ON-DISK
    shape catch up the next time anyone actually changes the lead."""
    registry = load_registry()
    entry = next((t for t in registry if t["id"] == team_id), None)
    if entry is None:
        return {"ok": False, "detail": f"no team registered with id {team_id!r}"}

    member = next((m for m in entry.get("members", []) if m["claude_session"] == claude_session), None)
    if member is None:
        return {"ok": False, "detail": "that session is not a member of this team"}

    entry["lead"] = claude_session
    entry.pop("inbox", None)  # complete the migration for this record
    save_registry(registry)
    return {"ok": True, "detail": f"lead set to {member.get('tmux') or claude_session}"}


# --- SS1.3: relocate --------------------------------------------------

def relocate_team_member(team_id: str, tmux: str) -> dict:
    """`tmux` is an UNASSIGNED session (from discover_teams_and_unassigned()'s
    existing unassigned list) the user believes is one of team_id's
    members, moved to a new path. Verifies identity via ONE /status
    round-trip (adoption-time-acceptable cost, same as
    providers.adoption_info() already pays elsewhere) and refuses if it
    doesn't match any member of team_id BY CLAUDE_SESSION -- never by
    name or path. On match, updates that member's root/tmux to the live,
    resolved cwd. "No matching improvement fixes a renamed directory" --
    this is the explicit action that does, rather than trying to infer
    it from a liveness check."""
    registry = load_registry()
    entry = next((t for t in registry if t["id"] == team_id), None)
    if entry is None:
        return {"ok": False, "detail": f"no team registered with id {team_id!r}"}

    live_by_name = {s["session_id"]: s for s in _list_live_sessions()}
    live = live_by_name.get(tmux)
    if live is None:
        return {"ok": False, "detail": f"{tmux!r} is not a currently running session"}

    live_claude_session = claude_session_id(tmux)
    if live_claude_session is None:
        return {"ok": False, "detail": "couldn't verify that session's identity -- pane may not be ready"}

    member = next((m for m in entry.get("members", []) if m["claude_session"] == live_claude_session), None)
    if member is None:
        return {
            "ok": False,
            "detail": "that session's identity doesn't match any member of this team",
        }

    old_root = entry["root"]
    new_root = live["working_dir"]
    member["tmux"] = tmux
    entry["root"] = new_root
    save_registry(registry)
    return {"ok": True, "detail": f"relocated from {old_root} to {new_root}"}


# --- SS6: remove -- detaching never kills a live session --------------

def remove_team(team_id: str) -> dict:
    """Deletes the registry entry AND its context file (so a later team
    re-registered under the same id never silently inherits a stale
    capture) -- NEVER touches the actual tmux sessions or processes."""
    registry = load_registry()
    if not any(t["id"] == team_id for t in registry):
        return {"ok": False, "detail": f"no team registered with id {team_id!r}"}
    registry = [t for t in registry if t["id"] != team_id]
    save_registry(registry)
    team_context.remove_context(team_id)
    return {"ok": True, "detail": f"team {team_id!r} removed (sessions were not touched)"}


def remove_member(team_id: str, claude_session: str) -> dict:
    """Detaches one member. Refuses if it's currently the lead (swap
    first -- an explicit decision, never silently promoted/cleared) or
    if it's the team's only member (remove_team() instead of leaving a
    zero-member team, which create_team() now refuses to create in the
    first place). NEVER touches the actual session."""
    registry = load_registry()
    entry = next((t for t in registry if t["id"] == team_id), None)
    if entry is None:
        return {"ok": False, "detail": f"no team registered with id {team_id!r}"}

    members = entry.get("members", [])
    if not any(m["claude_session"] == claude_session for m in members):
        return {"ok": False, "detail": "that session is not a member of this team"}

    if _lead_claude_session(entry) == claude_session:
        return {"ok": False, "detail": "that member is the team's lead -- swap the lead before removing it"}

    if len(members) <= 1:
        return {"ok": False, "detail": "removing this member would leave the team with zero members -- remove the whole team instead"}

    entry["members"] = [m for m in members if m["claude_session"] != claude_session]
    save_registry(registry)
    return {"ok": True, "detail": "member removed (session was not touched)"}
