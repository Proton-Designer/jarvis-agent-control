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
from pane_state import PaneState, PaneStatePatterns, classify_pane_ansi  # noqa: E402
from providers import list_sessions as _list_live_sessions, transport as _transport  # noqa: E402

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
CONTEXT_TURN_START_TIMEOUT_S = 10.0  # how long to wait for the turn to actually START (see _wait_for_reply_settled)
CONTEXT_TURN_START_POLL_INTERVAL_S = 0.3  # short: a genuinely fast BUSY window must not be missed between polls
CONTEXT_SETTLE_TIMEOUT_S = 90.0  # how long to wait for it to FINISH, once started
CONTEXT_SETTLE_POLL_INTERVAL_S = 1.0

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


def _last_match(pattern: str, text: str) -> "re.Match | None":
    """The LAST occurrence, not the first. Found live (ue6rruxg,
    2026-08-18): CONTENT_PROMPT's own text contains the literal strings
    "SUMMARY:", "SUBSYSTEMS:", "TECH_STACK:" -- it's teaching the format
    by showing it -- and Claude Code always echoes the typed prompt back
    into the pane BEFORE the real reply appears. re.search (first match)
    was grabbing the echoed PROMPT's own placeholder text
    ("<one sentence on what it does>") instead of the actual answer,
    which sits further down the pane, correctly formatted. The echoed
    input always precedes the real reply in the pane, so "last match" is
    the correct, version-independent fix -- no dependency on a specific
    Claude Code UI turn marker that could change across an update."""
    matches = list(re.finditer(pattern, text))
    return matches[-1] if matches else None


def _parse_context_response(pane_text: str) -> dict | None:
    summary_m = _last_match(r"SUMMARY:\s*(.+)", pane_text)
    subsystems_m = _last_match(r"SUBSYSTEMS:\s*(.+)", pane_text)
    tech_m = _last_match(r"TECH_STACK:\s*(.+)", pane_text)
    if not (summary_m and subsystems_m and tech_m):
        return None
    return {
        "summary": summary_m.group(1).strip(),
        "subsystems": [s.strip() for s in subsystems_m.group(1).split(";") if s.strip()],
        "tech_stack": [s.strip() for s in tech_m.group(1).split(";") if s.strip()],
    }


def _wait_for_reply_settled(tmux_name: str) -> bool:
    """Waits for an OBSERVED BUSY -> READY transition, not just "reads
    READY right now". Found live (ue6rruxg, 2026-08-18): right after
    send-keys delivers the context prompt, the pane can still read READY
    for one tick BEFORE the model actually starts processing -- a single
    poll (reconnect.wait_for_ready(), correct for its own use case of
    "is an already-idle session back up after a relaunch") caught that
    pre-processing blip and returned true ~6s before the real reply had
    even been written; the saved context file was still literally the
    prompt's own placeholder text.

    This is the mirror image of the race pane_state_canary.py's
    wait_for_stable_ready() already guards against (a momentary READY
    blip MID-turn, between two busy segments) -- here the blip is
    BEFORE the turn starts, not during it. Two-consecutive-READY-reads
    (that fix's approach) would not have caught this specific case: the
    very first read already sees READY, so a second read moments later
    could just see the SAME stale READY again if the model hasn't
    started yet, which is exactly what happened live. Requiring an
    ACTUAL BUSY OBSERVATION first is a strictly stronger guarantee --
    it needs real evidence the turn started, not just two timestamps
    agreeing.

    The BUSY-detection phase polls fast (CONTEXT_TURN_START_POLL_INTERVAL_S,
    0.3s) specifically so a genuinely brief BUSY window is not itself
    missed between polls -- the same class of miss this function exists
    to prevent, just one level earlier. Never observing BUSY at all
    within CONTEXT_TURN_START_TIMEOUT_S fails closed (returns False)
    rather than assuming a stray READY means done."""
    patterns = PaneStatePatterns()

    deadline = time.monotonic() + CONTEXT_TURN_START_TIMEOUT_S
    saw_busy = False
    while time.monotonic() < deadline:
        if not _transport.session_exists(tmux_name):
            return False
        if classify_pane_ansi(_transport.capture_pane(tmux_name), patterns) == PaneState.BUSY:
            saw_busy = True
            break
        time.sleep(CONTEXT_TURN_START_POLL_INTERVAL_S)
    if not saw_busy:
        return False

    deadline = time.monotonic() + CONTEXT_SETTLE_TIMEOUT_S
    while time.monotonic() < deadline:
        if not _transport.session_exists(tmux_name):
            return False
        if classify_pane_ansi(_transport.capture_pane(tmux_name), patterns) == PaneState.READY:
            return True
        time.sleep(CONTEXT_SETTLE_POLL_INTERVAL_S)
    return False


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

    if not _wait_for_reply_settled(target_tmux):
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
