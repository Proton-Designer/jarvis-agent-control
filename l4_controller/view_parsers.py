"""
Extracts a short, speakable answer from a captured read-only view
(/cost, /usage, /status, ...) instead of dumping the raw dashboard text.

Ayman's actual ask when he chose this transport was to be able to check
usage/cost by voice — a dashboard rendered in a tmux pane he isn't looking
at delivers nothing; the value is the spoken answer. But these are plain
regex extractions over Claude Code's rendered text, not real
understanding of it — if a parser doesn't find its expected fields, it
returns None rather than guessing. NEVER speak a fabricated figure from a
misparse; the caller's job on a None is to say something honest
("usage is on screen in the API session, couldn't parse a clean number")
instead of a confidently wrong one.

/cost and /usage were confirmed live (2026-08-17) to render the same
"Total cost" / session-percentage content — sharing one parser rather than
guessing they'd diverge until evidence says otherwise.
"""

from __future__ import annotations

import re

VIEW_PARSERS = {}  # populated below


def parse_cost_or_usage(view_text: str) -> str | None:
    cost_m = re.search(r"Total cost:\s+\$([\d.]+)", view_text)
    session_pct_m = re.search(r"Current session\D*?(\d+)%\s+used", view_text, re.DOTALL)
    if not cost_m and not session_pct_m:
        return None
    parts = []
    if cost_m:
        parts.append(f"${cost_m.group(1)} spent this session")
    if session_pct_m:
        parts.append(f"{session_pct_m.group(1)} percent of the session limit used")
    return ", ".join(parts) + "."


def parse_status(view_text: str) -> str | None:
    model_m = re.search(r"Model:\s+(\S.*)", view_text)
    if not model_m:
        return None
    return f"Running {model_m.group(1).strip()}."


def parse_session_id(view_text: str) -> str | None:
    """Structured extraction, not a spoken sentence -- for the l5_console
    state layer's team-member discovery (mapping a live tmux pane to its
    Claude session UUID for teams.json). Verified live (2026-08-17):
    /status's "Session ID: <uuid>" line is an exact match to the
    transcript filename at ~/.claude/projects/<encoded-path>/<uuid>.jsonl.

    This exists because the obvious alternatives are actively wrong or
    unreliable: `ps eww <pid>`'s reported CLAUDE_CODE_SESSION_ID is a
    tmux-server-global env var inherited from whenever the server was
    first started (confirmed live -- every pane in a server reports the
    SAME stale value, `tmux show-environment -g` shows it as global, not
    per-pane), and lsof doesn't show the transcript file as a held-open
    descriptor. /status asking the process to self-report is the
    reliable, in-band way to do this."""
    id_m = re.search(r"Session ID:\s+([0-9a-fA-F-]{36})", view_text)
    if not id_m:
        return None
    return id_m.group(1)


def parse_model(view_text: str) -> str | None:
    """Structured extraction (not a spoken sentence, unlike parse_status)
    -- the raw model name from the same /status capture, for l5_console's
    adoption flow (SPEC-TUI.md SS5.1: candidates are listed with model +
    self-written summary). Reads from the same view_content as
    parse_session_id() so adoption only needs one /status round-trip per
    candidate, not two."""
    model_m = re.search(r"Model:\s+(\S.*)", view_text)
    if not model_m:
        return None
    return model_m.group(1).strip()


VIEW_PARSERS.update(
    {
        "/cost": parse_cost_or_usage,
        "/usage": parse_cost_or_usage,
        "/status": parse_status,
    }
)


def summarize_view(command: str, view_text: str) -> str | None:
    """command: the leading token of the delivered payload, e.g. '/cost'.
    Returns a short spoken summary, or None if no parser matched or the
    parser couldn't find its expected fields — caller must speak an
    honest fallback on None, never fabricate a number."""
    parser = VIEW_PARSERS.get(command)
    if parser is None:
        return None
    return parser(view_text)
