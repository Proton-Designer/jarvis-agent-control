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

import re

from pane_state import PaneState, classify_pane_ansi
from registry import SessionRegistry
from transport import TmuxTransport
from view_parsers import parse_session_id, summarize_view

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


def session_activity(session_id: str) -> dict:
    """What a session is doing right now, read directly from its pane --
    no inference, no model. `state` reuses the exact same classifier the
    delivery gate uses (pane_state.classify_pane_ansi), so "busy" here
    means the identical thing it means when transport.deliver refuses a
    send. `activity` is a short label extracted from the pane's current
    tool-call/spinner line when state is "busy"; None if nothing
    recognizable matched, or if the session isn't running at all."""
    if not transport.session_exists(session_id):
        return {"session_id": session_id, "state": "no_session", "activity": None}

    ansi_text = transport.capture_pane(session_id)
    state = classify_pane_ansi(ansi_text, transport.patterns)

    activity = None
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

    return {"session_id": session_id, "state": state.value, "activity": activity}


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
