"""
Team project context (SPEC-teams.md SS3): a structured, bounded
description of what a team's project is, captured once at registration,
refreshed only by explicit action -- never a poller.

Two sources:
  - "claude_md": the team's own root/CLAUDE.md, read LIVE on every call
    (never cached, same "liveness is always polled" instinct applied to
    a file read) -- not a fabrication risk, so it renders with no
    qualifier.
  - "agent": a structured prompt delivered to the team's lead (or first
    live member if no lead), response captured and stored. Model-
    generated, unverified -- renders with a "(self-described,
    unverified)" qualifier and its captured_at date (SS3.3). Only ever
    refreshed by an explicit action, never automatically.

SS3.1's inversion (Ayman's correction): the agent is asked BY DEFAULT.
CLAUDE.md is a shortcut that SKIPS the agent turn when it already
answers -- "we can't rely on independent project structure because each
project will be structured differently," so any file-parsing rule works
on some projects and returns nothing on others, while an agent reading
the directory does not care about structure. That is why the default
path here is capture_context()'s CLAUDE.md-first check, not a
config-file-format-specific parser.

Storage: the pointer (source, captured_at) lives in teams.json itself --
small, already read every poll. The body lives in its own file,
CONTEXT_DIR/<team_id>.json, so a large capture never bloats the file
every poll already reads. The team_id is written INSIDE the body file
too, not just implied by its path -- nothing else ever reads these
files, so a mismatch caused by a rename or a reused team_id would
otherwise never surface (SS3.4).

SS3.3: routing hints only, fed to the router's own reasoning -- never
recited to Ayman as fact. Enforcing that is a RENDER/PROMPT concern, not
this module's; this module's job is only to make the source (and
therefore whether a qualifier is owed) unambiguous in the data itself.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "l4_controller"))
from providers import list_sessions as _list_live_sessions, transport as _transport  # noqa: E402

from reconnect import wait_for_ready  # noqa: E402
from teams import _lead_claude_session, load_registry, save_registry, TEAMS_REGISTRY_PATH  # noqa: E402

CONTEXT_DIR = TEAMS_REGISTRY_PATH.parent / "context"

SOURCE_AGENT = "agent"
SOURCE_CLAUDE_MD = "claude_md"

CONTEXT_PROMPT = (
    "Describe this project. Reply in EXACTLY this format, one line per field, "
    "nothing before or after it -- no preamble, no markdown, no code fences:\n"
    "SUMMARY: <one sentence on what it does>\n"
    "SUBSYSTEMS: <2-4 short phrases, semicolon-separated>\n"
    "TECH_STACK: <languages/frameworks actually used, semicolon-separated>\n"
    "Base every field only on what you can verify by reading files in this "
    "directory -- never guess or assume."
)
# A structured-description turn is more than a one-line acknowledgement
# but still bounded (reads a handful of files, writes three lines) --
# generous relative to a normal dispatch, nowhere near "no budget at all".
CONTEXT_RESPONSE_POLL_TIMEOUT_S = 90.0

_EMPTY_CONTEXT_FIELDS = {
    "context_summary": None, "context_subsystems": [], "context_tech_stack": [],
    "context_captured_at": None, "context_source": None,
}


def _context_path(team_id: str) -> Path:
    return CONTEXT_DIR / f"{team_id}.json"


def _read_claude_md(root: str) -> str | None:
    path = Path(root) / "CLAUDE.md"
    if not path.exists():
        return None
    try:
        return path.read_text()
    except OSError:
        return None


def _parse_context_response(pane_text: str) -> dict | None:
    summary_m = re.search(r"SUMMARY:\s*(.+)", pane_text)
    subsystems_m = re.search(r"SUBSYSTEMS:\s*(.+)", pane_text)
    tech_m = re.search(r"TECH_STACK:\s*(.+)", pane_text)
    if not (summary_m and subsystems_m and tech_m):
        return None
    return {
        "summary": summary_m.group(1).strip(),
        "subsystems": [s.strip() for s in subsystems_m.group(1).split(";") if s.strip()],
        "tech_stack": [s.strip() for s in tech_m.group(1).split(";") if s.strip()],
    }


def context_fields_for(entry: dict) -> dict:
    """{"context_summary", "context_subsystems", "context_tech_stack",
    "context_captured_at", "context_source"} for teams.py to fold
    directly into a Team, every poll tick (ue6rruxg's request -- one
    get_state() call renders everything, no second per-team call path).
    Cheap for both sources: a claude_md pointer means one file read (no
    /status, no pane capture, no subprocess); an agent pointer means one
    small JSON read from CONTEXT_DIR, never the live agent again."""
    context = entry.get("context")
    if context is None:
        return dict(_EMPTY_CONTEXT_FIELDS)

    source = context.get("source")
    if source == SOURCE_CLAUDE_MD:
        text = _read_claude_md(entry["root"])
        if text is None:
            # The file that sourced this was deleted/moved since capture
            # -- degrade to "no context" rather than showing stale prose
            # that no longer exists on disk. Never re-fabricate it.
            return dict(_EMPTY_CONTEXT_FIELDS)
        return {
            "context_summary": text.strip(),
            "context_subsystems": [],
            "context_tech_stack": [],
            "context_captured_at": context.get("captured_at"),
            "context_source": SOURCE_CLAUDE_MD,
        }

    if source == SOURCE_AGENT:
        path = _context_path(entry["id"])
        if not path.exists():
            return dict(_EMPTY_CONTEXT_FIELDS)
        try:
            body = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return dict(_EMPTY_CONTEXT_FIELDS)
        if body.get("team_id") != entry["id"]:
            # Orphan check (SS3.4): the file's own recorded team_id
            # doesn't match the team asking for it -- a rename/reuse
            # collision. Never surface someone else's context as this
            # team's.
            return dict(_EMPTY_CONTEXT_FIELDS)
        return {
            "context_summary": body.get("summary"),
            "context_subsystems": list(body.get("subsystems", [])),
            "context_tech_stack": list(body.get("tech_stack", [])),
            "context_captured_at": context.get("captured_at"),
            "context_source": SOURCE_AGENT,
        }

    return dict(_EMPTY_CONTEXT_FIELDS)


def _target_for_capture(entry: dict) -> str | None:
    """The lead's tmux if live, else the first live member -- never a
    dead one. None if nothing about this team is currently running."""
    live_by_name = {s["session_id"]: s for s in _list_live_sessions()}
    lead_claude_session = _lead_claude_session(entry)

    def _is_live(m: dict) -> bool:
        live = live_by_name.get(m.get("tmux"))
        return live is not None and live["working_dir"] == entry["root"]

    for m in entry.get("members", []):
        if m["claude_session"] == lead_claude_session and _is_live(m):
            return m["tmux"]
    for m in entry.get("members", []):
        if _is_live(m):
            return m["tmux"]
    return None


def capture_context(team_id: str, force_agent: bool = False) -> dict:
    """Captures context for `team_id` -- CLAUDE.md shortcut by default,
    an agent turn if force_agent=True or no CLAUDE.md exists. Returns
    {"ok": bool, "detail": str}. Called once at registration
    (automatically, by whichever flow just created/adopted the team) and
    otherwise only from an explicit Refresh action -- never a poller,
    per SS3.3's "capture at registration only."""
    registry = load_registry()
    entry = next((t for t in registry if t["id"] == team_id), None)
    if entry is None:
        return {"ok": False, "detail": f"no team registered with id {team_id!r}"}

    if not force_agent:
        claude_md = _read_claude_md(entry["root"])
        if claude_md is not None:
            entry["context"] = {"source": SOURCE_CLAUDE_MD, "captured_at": time.time()}
            save_registry(registry)
            return {"ok": True, "detail": "captured from CLAUDE.md"}

    target_tmux = _target_for_capture(entry)
    if target_tmux is None:
        return {"ok": False, "detail": "no live member to ask -- nothing running for this team right now"}

    result = _transport.deliver(target_tmux, CONTEXT_PROMPT)
    if not result.ok:
        return {"ok": False, "detail": f"couldn't deliver the context prompt: {result.detail}"}

    if not wait_for_ready(target_tmux, timeout_s=CONTEXT_RESPONSE_POLL_TIMEOUT_S):
        return {"ok": False, "detail": "the agent didn't finish responding in time"}

    pane_text = _transport.capture_pane_plain(target_tmux)
    parsed = _parse_context_response(pane_text)
    if parsed is None:
        return {"ok": False, "detail": "couldn't parse a structured response -- the agent didn't follow the format"}

    CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
    _context_path(team_id).write_text(json.dumps({"team_id": team_id, **parsed}, indent=2))
    entry["context"] = {"source": SOURCE_AGENT, "captured_at": time.time()}
    save_registry(registry)
    return {"ok": True, "detail": "captured via agent"}


def remove_context(team_id: str) -> None:
    """Deletes team_id's context file, if any -- called by remove_team()
    so a removed team's context never lingers to be silently reattached
    if the same team_id is registered again later."""
    path = _context_path(team_id)
    if path.exists():
        path.unlink()
