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
length removes that whole class of false positive.

_NAMING_STOPWORDS exists for a second, related false positive found
later (gu2s6tnt's review): "let me test something real quick" matched a
session named "claude-<project>-test", because "test" -- long enough to
survive MIN_TOKEN_LEN, but a naming-convention SUFFIX here, not a
distinguishing project name -- is also just an ordinary English word.
Over-triggers into the DISPATCH-or-QUERY hard rule rather than dropping
anything (a nuisance, not a loss), but the same family of bug as the
single-letter one: a token that's technically part of a session's name
but carries no real "this is about that session" signal on its own.
Scoped narrowly to this project's actual naming convention (environment/
lifecycle suffixes seen in practice: -test, -dev, -staging, -prod,
-demo), not a general English stopword list -- broadening it risks
suppressing a real project name that happens to also be a common word.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "l4_controller"))
from providers import list_sessions  # noqa: E402

MIN_TOKEN_LEN = 3
_NAMING_STOPWORDS = {"test", "tests", "dev", "staging", "prod", "demo", "temp", "tmp"}


def session_tokens(session_id: str, alias: str | None) -> set[str]:
    name = session_id[len("claude-"):] if session_id.lower().startswith("claude-") else session_id
    tokens = {
        t for t in re.split(r"[-_\s]+", name.lower())
        if len(t) >= MIN_TOKEN_LEN and t not in _NAMING_STOPWORDS
    }
    if alias and len(alias) >= MIN_TOKEN_LEN and alias.lower() not in _NAMING_STOPWORDS:
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
