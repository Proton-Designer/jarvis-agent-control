"""
Deterministic data providers for the L2.5 concierge (SPEC-L2.5-concierge.md
requirement 1: "deterministic before model" -- tmux/filesystem facts, never
inferred). Plain functions with no MCP dependency, so the concierge (a
separate process from L3's MCP loop) can import this module directly --
same sys.path-insert pattern daemon.py already uses to reach l4_controller
for deliver_transcript.

server.py's MCP tools are thin wrappers over these same functions and the
same registry/transport instances defined here, so L3 sees identical
behavior to before this module existed -- this is not a second
implementation of session discovery/delivery, just a shared one two
callers reach differently.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import blocked_state
from pane_state import PaneState, classify_pane_ansi
from registry import SessionRegistry
from transport import TmuxTransport
from view_parsers import parse_model, parse_session_id, summarize_view

sys.path.insert(0, str(Path(__file__).parent.parent))
from jarvis_paths import jarvis_project_home  # noqa: E402

# Direct file read, NOT an import of l5_console/state/teams.py. L4 must
# not depend on L5 (the Lead's ruling, 2026-08-20, found live): teams.py
# already does `from providers import list_sessions, session_activity`,
# so importing teams.py back from here is a REAL import cycle, not a
# theoretical one -- it broke server_readonly.py's import entirely
# ("cannot import name 'list_sessions' from partially initialized module
# 'providers'"), which means the concierge's whole MCP tool surface
# would have been unreachable. Same registry file teams.py's own
# TEAMS_REGISTRY_PATH resolves to (~/Jarvis/teams.json via
# jarvis_project_home()) -- read directly here rather than through
# teams.py's richer discover_teams_and_unassigned(), since
# _resolve_blocked_member() below only needs the raw claude_session/tmux
# fields already on disk, not live liveness enrichment.
TEAMS_REGISTRY_PATH = jarvis_project_home() / "teams.json"


def _load_teams_registry() -> list[dict]:
    if not TEAMS_REGISTRY_PATH.exists():
        return []
    try:
        return json.loads(TEAMS_REGISTRY_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return []

registry = SessionRegistry()
transport = TmuxTransport(registry=registry)

# Best-effort, regex-extracted description of what a BUSY pane is doing
# right now. Checked in order; first match wins. Returns None (never
# guesses) when nothing recognizable is on screen.
#
# Only two specific patterns are shipped, because that's all that's been
# confirmed against a real live Claude Code v2.1.234 pane (2026-08-17,
# throwaway tmux session claude-l4test): a live Bash tool call renders as
# "⎿  $ <command> (...)", not the "⏺ Bash(...)" shape an earlier draft of
# this file assumed before checking -- an assumption would have silently
# never matched anything. A live Read tool call renders as "⏺ Reading N
# file(s)…". Edit/Write/Grep/Glob/Task were NOT captured live in this
# environment (the nested test session fell back to Bash for search
# instead of using native Grep/Glob) -- left out rather than guessed at;
# add them here only once confirmed the same way, per this project's
# established rule against shipping unverified pane-format assumptions.
_ACTIVITY_PATTERNS = [
    (re.compile(r"⎿\s*\$\s"), "running a shell command"),
    (re.compile(r"⏺\s*Reading\s+\d+\s+file"), "reading a file"),
    (re.compile(r"[✻✢✽✶⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]\s*\S+…"), "thinking"),
]


def list_sessions() -> list[dict]:
    """Enumerate real, currently-running tmux sessions with their working
    directory. Never returns a session that isn't actually running."""
    return [
        {
            "session_id": s.session_id,
            "working_dir": s.working_dir,
            "alias": s.alias,
            "custom_commands": s.custom_commands,
        }
        for s in registry.list_sessions()
    ]


def _parse_blocked_question(plain_text: str) -> dict | None:
    """Extracts the question and numbered option labels from a real
    AskUserQuestion picker's rendered text (docs/SPEC-blockers.md stage
    1). Returns None rather than guessing if the expected shape isn't
    found -- same "never fabricate" discipline as view_parsers.py.

    Shape, confirmed live (2026-08-18, re-confirmed 2026-08-20 while
    building the answer-routing feature) against real instances: "☐
    <short label>", blank, the question line, blank, numbered options
    (some followed by an indented description line with no number
    prefix), THEN ALWAYS a trailing "N. Type something." entry (same
    numbered format as a real option -- confirmed live it is NOT
    distinguishable from a real option by shape, only by its fixed,
    literal text), a separator, then "N+1. Chat about this". Only the
    question and the top-level numbered option labels are extracted --
    descriptions and the footer are noise for this purpose, and the
    captured text is treated as untrusted content throughout (never
    interpreted as instructions by any caller).

    STOPS at "Type something." rather than collecting it (and therefore
    "Chat about this" too, which only ever follows it) -- found live
    2026-08-20 building the answer-routing feature: an earlier version
    of this loop had no stopping condition and matched every numbered
    line to the end of the pane, so `options` silently included these
    two UI-chrome entries as if Ayman could actually say "type
    something" as his answer. Confirmed live (2026-08-20) that plain
    keystroke injection cannot safely use "Type something." anyway
    (selecting it and pressing Enter DECLINES the question instead of
    opening a text field) -- so excluding it here is not just cosmetic,
    it keeps `options` limited to exactly the choices this project's
    answer-delivery path is actually able to select."""
    lines = [ln.rstrip() for ln in plain_text.splitlines()]
    non_blank = [(i, ln) for i, ln in enumerate(lines) if ln.strip()]

    label_idx = next((i for i, ln in non_blank if re.match(r"^\s*☐\s+\S", ln)), None)
    if label_idx is None:
        return None

    # Question: the next non-blank line after the label that isn't itself
    # a numbered option.
    question = None
    for i, ln in non_blank:
        if i <= label_idx:
            continue
        if re.match(r"^\s*(❯\s*)?\d+\.\s", ln):
            break
        question = ln.strip()
        break
    if not question:
        return None

    options = []
    for i, ln in non_blank:
        if i <= label_idx:
            continue
        m = re.match(r"^\s*(?:❯\s*)?\d+\.\s+(.+)$", ln)
        if not m:
            continue
        label = m.group(1).strip()
        if label == "Type something.":
            break  # UI chrome, not a real option -- see this function's docstring
        options.append(label)

    return {"question": question, "options": options}


def _resolve_blocked_member(claude_session: str) -> dict | None:
    """{"team_id", "tmux"} for the team member currently registered under
    this claude_session, or None if no team has one. Always a live
    registry read, never cached -- doubles as the liveness-adjacent
    lookup _drop_dead_blocked() needs."""
    for team in _load_teams_registry():
        for member in team.get("members", []):
            if member.get("claude_session") == claude_session:
                return {"team_id": team["id"], "tmux": member.get("tmux")}
    return None


def _drop_dead_blocked(claude_session: str, member: dict | None) -> bool:
    """docs/TODO-feature-queue.md #5's held-instruction-lifecycle lesson,
    applied to pending questions: expiry is not cleanup, but a question
    whose session has died IS dropped immediately, not aged out on a
    timer. Returns True if the entry was dropped (caller should treat
    this claude_session as no longer pending). Checked at the moment a
    caller actually wants to use the entry, not on a background timer --
    same "liveness is always polled, never scheduled" discipline as
    everywhere else."""
    if member is None or not member.get("tmux") or not transport.session_exists(member["tmux"]):
        blocked_state.clear_blocked(claude_session)
        return True
    return False


def pending_questions() -> list[dict]:
    """Every genuinely still-pending question (docs/TODO-feature-queue.md
    #5, SPEC-blockers.md SS5), self-healed against dead sessions on the
    way out. Each: {"claude_session", "team_id", "tmux", "question",
    "options", "since"} -- untrusted content (question, options) passed
    through unchanged, never interpreted. Read-only: this only reads
    blocked_state.json and the team registry, never sends a keystroke --
    see blocked_answer.answer_blocked_session() (write-tool, router
    surface only) for actually delivering an answer."""
    result = []
    for claude_session, entry in blocked_state.all_blocked().items():
        member = _resolve_blocked_member(claude_session)
        if _drop_dead_blocked(claude_session, member):
            continue
        result.append({
            "claude_session": claude_session,
            "team_id": member["team_id"],
            "tmux": member["tmux"],
            "question": entry["question"],
            "options": entry["options"],
            "since": entry["since"],
        })
    return result


def session_activity(session_id: str) -> dict:
    """What a session is doing right now, read directly from its pane --
    no inference, no model. `state` reuses the exact same classifier the
    delivery gate uses (pane_state.classify_pane_ansi), so "busy" here
    means the identical thing it means when transport.deliver refuses a
    send. `activity` is a short label extracted from the pane's current
    tool-call/spinner line when state is "busy"; None if nothing
    recognizable matched, or if the session isn't running at all.
    `blocked` is set (question + option labels, untrusted content) only
    when state is "blocked_question"; None otherwise. Reuses the SAME
    pane capture for both -- no reason to pay for two round-trips when
    one classify_pane_ansi() call already tells us which extraction (if
    any) is relevant."""
    if not transport.session_exists(session_id):
        return {"session_id": session_id, "state": "no_session", "activity": None, "blocked": None}

    ansi_text = transport.capture_pane(session_id)
    state = classify_pane_ansi(ansi_text, transport.patterns)

    activity = None
    blocked = None
    if state == PaneState.BUSY:
        plain_text = transport.capture_pane_plain(session_id)
        for line in reversed(plain_text.splitlines()):
            line = line.strip()
            if not line:
                continue
            matched = False
            for pattern, label in _ACTIVITY_PATTERNS:
                if pattern.search(line):
                    activity = label
                    matched = True
                    break
            if matched:
                break
    elif state == PaneState.BLOCKED_QUESTION:
        plain_text = transport.capture_pane_plain(session_id)
        blocked = _parse_blocked_question(plain_text)

    return {"session_id": session_id, "state": state.value, "activity": activity, "blocked": blocked}


def claude_session_id(session_id: str) -> str | None:
    """The tmux target's own Claude Code session UUID, via /status --
    the reliable, in-band way to map a live tmux pane to its identity.
    Deliberately not `ps eww`/env-var based: verified live that
    CLAUDE_CODE_SESSION_ID is a tmux-server-global value inherited from
    server startup, not per-pane, and returns the SAME wrong UUID for
    every pane in one tmux server -- see view_parsers.parse_session_id's
    docstring for the full finding. Returns None on any failure (target
    not READY, unparseable view) -- never guesses at an identity.

    ADOPTION-TIME ONLY. NEVER call this on a polling/liveness loop.
    /status is an intrusive ~1s round-trip -- it types into the target
    session, opens a persistent view, captures, dismisses. Fine as a
    one-time cost when Ayman explicitly adopts a pre-existing session
    into a team (SPEC-TUI.md SS5.1); completely unacceptable run every
    poll tick, since it would put visible text into every one of Ayman's
    real sessions every few seconds. Resolve once at adoption, store the
    UUID in teams.json, never ask again -- per-poll liveness matches on
    tmux session name + working directory instead (cheap, and sufficient
    to answer "is this known member still alive", which is a different
    question from "what is this pane's identity"). Reconnect and fresh
    creation don't need this function at all: reconnect already knows
    the UUID (it launched `--resume <uuid>` itself), and fresh creation
    can identify its own new transcript file directly after launch,
    which is cheaper and unambiguous since nothing else is starting at
    the same moment."""
    result = transport.deliver(session_id, "/status")
    if not result.ok or result.view_content is None:
        return None
    return parse_session_id(result.view_content)


def adoption_info(session_id: str) -> dict:
    """One /status round-trip returning BOTH the session UUID and model
    -- l5_console's adoption flow (SPEC-TUI.md SS5.1) needs both per
    candidate, and there's no reason to pay the ~1s intrusive-view cost
    twice for one pane when a single capture already contains both
    fields. Same adoption-time-only constraint as claude_session_id():
    never call this from a polling loop."""
    result = transport.deliver(session_id, "/status")
    if not result.ok or result.view_content is None:
        return {"claude_session": None, "model": None}
    return {
        "claude_session": parse_session_id(result.view_content),
        "model": parse_model(result.view_content),
    }


def spend(session_id: str) -> dict:
    """Wraps the existing /cost capture+parse path used for delivery
    read-back today -- same parser (view_parsers.summarize_view), same
    "never fabricate a figure" rule: ok=False / summary=None on anything
    unparseable rather than a guessed number. Note this sends /cost, which
    requires the target pane to be READY (pane-state gated, same as any
    other delivery) -- a BUSY target refuses like any other send would."""
    result = transport.deliver(session_id, "/cost")
    if not result.ok or result.view_content is None:
        return {"ok": False, "summary": None, "raw": None}
    return {
        "ok": True,
        "summary": summarize_view("/cost", result.view_content),
        "raw": result.view_content,
    }
