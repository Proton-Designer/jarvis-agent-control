"""
Guards against partial/unrecognized slash-command payloads reaching a
live Claude Code input box.

Why this exists (confirmed live, not theoretical): typing a COMPLETE but
unrecognized slash word ("/zzz-not-a-real-command-99") submits safely —
Claude Code responds "Unknown command: ..." with no side effect. But typing
a PARTIAL slash word ("/comp") leaves Claude Code's own command-completion
overlay open with a suggestion highlighted, and pressing Enter at that point
submits the HIGHLIGHTED SUGGESTION ("/compact"), not the literal text that
was typed. A malformed/truncated payload reaching this transport — an L3
bug, a bad voice-transcript resolution, anything — could therefore execute
a completely different command than the one actually resolved. So: any
payload that looks like a slash command must exactly match a known,
complete command, or it gets refused rather than typed.

The known-commands list is necessarily a static snapshot (built-ins plus
whatever skills happen to be installed) — it will go stale as skills are
added/removed. That's fine: staleness only ever causes an over-cautious
refusal (a legitimate new slash command gets rejected until the list is
updated), never an unsafe delivery. Update known_slash_commands.json as
new commands are added to the target sessions.
"""

from __future__ import annotations

import json
from pathlib import Path

DEFAULT_KNOWN_COMMANDS_PATH = Path(__file__).parent / "known_slash_commands.json"


def load_known_commands(path: Path = DEFAULT_KNOWN_COMMANDS_PATH) -> set[str]:
    return set(json.loads(path.read_text()))


def is_safe_slash_payload(payload: str, known_commands: set[str]) -> bool:
    """
    payload is assumed already normalized (stripped, no embedded newlines).
    Only the leading token (up to the first space) is checked against the
    known-commands list — everything after is the command's own arguments,
    which the command itself is responsible for parsing.
    """
    if not payload.startswith("/"):
        return True  # not a slash command at all, guard doesn't apply
    leading = payload.split(" ", 1)[0]
    return leading in known_commands
