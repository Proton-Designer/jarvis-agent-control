#!/usr/bin/env python3
"""
One-command pane inspection: `python3 pane_status.py <session>`.

WHY THIS EXISTS (2026-08-17): the ghost-text-vs-real-typed-text
discriminator (see pane_state.py's classify_pane_ansi) is correct and has
been for a while -- the problem was never the discriminator, it was that
the DEFAULT way to look at a pane (`tmux capture-pane -p`, no `-e`) strips
the exact ANSI signal that tells the two apart. Three different people
independently reached for that default under time pressure in one day and
misread ghost/autosuggest text as a real human response: the Lead once,
then Sonnet ENG 1, then Sonnet ENG 2. The discriminator was never the
gap; its accessibility was. This script makes the correct check the easy
one -- one command, no flags to remember, no ANSI to read by eye.

Usage:
  python3 pane_status.py <tmux-session-name>

Exit codes: 0 if the session exists and was inspected (regardless of what
state it's in -- this is a diagnostic tool, not a test), 1 if the session
doesn't exist or capture failed.
"""

from __future__ import annotations

import sys

from pane_state import PaneState, PaneStatePatterns, _has_dim_code, _strip_ansi, classify_pane_ansi
from transport import TmuxTransport
from registry import SessionRegistry


def _input_line_status(ansi_text: str, patterns: PaneStatePatterns) -> tuple[str, str]:
    """Returns (label, content) for what's actually in the input box right
    now -- independent of the overall PaneState, since e.g. a BUSY pane's
    input line isn't meaningful to report this way. Callers should only
    call this when state is READY or UNKNOWN (the two states where the
    input line itself is the thing in question)."""
    glyph = patterns.prompt_glyph or ""
    idx = ansi_text.rfind(glyph)
    if idx == -1:
        return "unknown", ""
    after = ansi_text[idx + len(glyph):]
    nl = after.find("\n")
    if nl != -1:
        after = after[:nl]
    plain = _strip_ansi(after).strip()
    if not plain:
        return "empty", ""
    if _has_dim_code(after):
        return "ghost/autosuggest (safe to type over)", plain
    return "REAL TYPED TEXT -- do not overwrite, do not submit blind", plain


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <tmux-session-name>", file=sys.stderr)
        return 1
    session = sys.argv[1]

    transport = TmuxTransport(registry=SessionRegistry())
    if not transport.session_exists(session):
        print(f"no such tmux session: {session}")
        return 1

    patterns = transport.patterns
    ansi_text = transport.capture_pane(session)
    state = classify_pane_ansi(ansi_text, patterns)

    print(f"session:      {session}")
    print(f"pane state:   {state.value.upper()}")

    if state in (PaneState.READY, PaneState.UNKNOWN):
        label, content = _input_line_status(ansi_text, patterns)
        print(f"input box:    {label}")
        if content:
            print(f"              {content!r}")
    else:
        print("input box:    (not meaningful in this state -- see the tail below)")

    print()
    print("safe to send keystrokes into this pane right now: "
          + ("YES" if state == PaneState.READY else "NO"))

    print()
    print("last few lines (plain):")
    plain_pane = transport.capture_pane_plain(session)
    tail_lines = [ln for ln in plain_pane.splitlines() if ln.strip()][-8:]
    for ln in tail_lines:
        print(f"  {ln}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
