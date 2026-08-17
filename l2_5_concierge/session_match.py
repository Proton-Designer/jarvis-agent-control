"""
Shared "does this transcript reference a live session" logic, used by
both classifier.py (the CHAT-suppression hard rule) and concierge.py
(resolving which session a QUERY is about). Factored out after finding a
real bug: the two files started as independent copies of the same
token-overlap logic and it would have needed the same fix twice.

Token overlap against a session's id (minus the "claude-" prefix) and
alias, not a fixed list -- reuses list_sessions(), the same live-tmux
source already used for --prompt biasing.

MIN_TOKEN_LEN=3 exists because of a real false positive found during
testing: a throwaway session named "claude-heldfix-a" tokenizes to
{"heldfix", "a"}, and the single-letter token "a" matched the word "a" in
"What a nice day today" -- an entirely unrelated, ordinary sentence.
Single- and double-character tokens are too generic to be a meaningful
"this text is about that session" signal; excluding them below this
length removes that whole class of false positive without needing a
stopword list.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "l4_controller"))
from providers import list_sessions  # noqa: E402

MIN_TOKEN_LEN = 3


def session_tokens(session_id: str, alias: str | None) -> set[str]:
    name = session_id[len("claude-"):] if session_id.lower().startswith("claude-") else session_id
    tokens = {t for t in re.split(r"[-_\s]+", name.lower()) if len(t) >= MIN_TOKEN_LEN}
    if alias and len(alias) >= MIN_TOKEN_LEN:
        tokens.add(alias.lower())
    return tokens


def _text_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) >= MIN_TOKEN_LEN}


def mentions_live_session(text: str) -> bool:
    """Does `text` reference any currently-running session by name at
    all? Used to decide whether CHAT should be structurally unavailable
    -- doesn't need to know which session, just whether one was named."""
    words = _text_words(text)
    return any(session_tokens(s["session_id"], s.get("alias")) & words for s in list_sessions())


def resolve_session(text: str) -> dict | None:
    """Which single session (if exactly one) does `text` reference?
    Returns None on no match OR an ambiguous multiple match -- callers
    must treat that as "don't know," never guess one."""
    words = _text_words(text)
    matches = [s for s in list_sessions() if session_tokens(s["session_id"], s.get("alias")) & words]
    return matches[0] if len(matches) == 1 else None
