"""
CONTROL / QUERY / CHAT / DISPATCH / UNSURE classifier for the L2.5
concierge (SPEC-L2.5-concierge.md). Tiered, per the spec's own
recommendation: a small set of high-confidence keyword patterns wins
immediately (near-zero latency, no ambiguity on the clearest cases);
everything else falls to the local model.

Why tiered rather than model-only, measured not assumed: a bare "classify
this" prompt against qwen2.5:3b, no category definitions or examples,
gave inconsistent answers on three identical calls of the same
QUERY-shaped input ("what is the gateway doing" -> CONTROL, CHAT, CHAT).
Adding real category definitions and few-shot examples (see
ollama_client.CLASSIFY_SYSTEM) fixed 10/12 on a 12-case test -- but the
keyword tier removes the model, and its cost and residual error rate,
from the cases that don't need it at all: an exact "cancel" or "never
mind" doesn't benefit from a ~150-200ms model round trip when a regex
anchor answers it for free and can't be ambiguous.

The governing rule (classification fails toward DISPATCH) is enforced
structurally here, not just by prompt wording: any model response that
isn't one of the five known labels becomes UNSURE, never silently
dropped or defaulted to CHAT.

Second hard rule, added after a 28-case DANGEROUS-direction-weighted
adversarial test (see ollama_client.py's docstring for the full
numbers): a transcript that names a real, currently-running session is
never CHAT, regardless of what the model says. The two worst measured
failures were both a real instruction, naming a real session, landing on
CHAT -- silently speaking nothing and forwarding nothing. CHAT is
structurally unavailable in that case; see mentions_live_session and the
constrained re-ask in classify().

Third addition: assess_retention(), NOT_ADDRESSED's retention half
(SPEC-L2.5's sixth intent class). Not a 6th label in classify()'s return
value -- CHAT is already fully suppressed (never speaks, never forwards,
see concierge.py's guard), so the only remaining question for a
CHAT-classified transcript is whether it stays on disk. See
assess_retention's own docstring for the asymmetric confidence bar.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ollama_client import assess_addressed, classify_with_model, reclassify_dispatch_or_query
from session_match import mentions_live_session

CONTROL = "CONTROL"
QUERY = "QUERY"
CHAT = "CHAT"
DISPATCH = "DISPATCH"
UNSURE = "UNSURE"

VALID_LABELS = {CONTROL, QUERY, CHAT, DISPATCH, UNSURE}


@dataclass
class Classification:
    label: str
    tier: str  # "keyword" or "model"
    matched_pattern: str | None = None  # set only for tier == "keyword"


# Anchored to the whole (normalized) transcript, not a substring search
# over arbitrary-length dictation content -- a keyword tier is only safe
# to trust over the model when a HIT is unambiguous. A dispatch
# instruction that happens to mention "cancel" partway through real
# content ("cancel the old deploy and redeploy with the new config") must
# not become CONTROL just because the word appears; these patterns only
# match short, complete utterances that consist of nothing else.
_CONTROL_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"^cancel(\s+(that|it))?\.?$",
        r"^never\s?mind\.?$",
        r"^stop\.?$",
        r"^(say\s+that|repeat(\s+that)?)\s+again\.?$",
        r"^say\s+that\s+again\.?$",
        r"^what\s+did\s+you\s+say\??\.?$",
    ]
]

# Anchored (^...$) exactly like CONTROL now -- was a bare .search()
# ("routing a QUERY-shaped chunk to the deterministic answer path costs
# nothing if wrong, worst case an oddly-phrased response"), and that
# reasoning was WRONG for a compound utterance. Found live (gu2s6tnt's
# review): "what's running right now, and tell the billing session to
# redeploy" matched the "running" pattern via .search(), classified QUERY
# via the keyword tier, and the redeploy clause never reached the model
# or deliver_transcript() at all -- worse than silent loss, see
# _handle_query's docstring for how it also produced a FABRICATED
# confirmation that the redeploy happened. A compound utterance now
# correctly fails every anchored pattern and falls through to the model,
# which is what the tiered design was for -- costs ~200ms on a case that
# used to be free, in exchange for not silently dropping (and then lying
# about) a real instruction. Trailing punctuation still tolerated
# (\.?$), leading filler ("hey, what's...") is not -- that now falls
# through to the model too, same tradeoff.
_QUERY_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"^what'?s\s+(\w+\s+)?(running|going on|happening)\??\.?$",
        r"^what'?s\s+.+\s+doing\??\.?$",
        r"^is\s+.+\s+done\s+yet\??\.?$",
        r"^how\s+much\s+(have\s+i|did\s+i)\s+spen[dt]\??\.?$",
        r"^what'?s\s+the\s+status\??\.?$",
    ]
]


# A second, independent, deterministic gate on discarding -- not on
# classification. Applies only inside assess_retention(), which is only
# ever called for a CHAT-classified transcript (mentions_live_session
# already keeps anything naming a real session out of CHAT in the first
# place, per the hard rule above -- this catches the case where an
# instruction has no named target at all, e.g. "can you check on that").
# Deliberately loose/over-inclusive: a false positive here just means an
# ambient remark gets kept on disk instead of discarded (harmless, per
# the retention asymmetry below), so there's no cost to casting wide.
_IMPERATIVE_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\b(please\s+)?(check|run|restart|redeploy|fix|stop|start|compact|pull|deploy|update|revert|retry)\b",
        r"\b(can|could|would)\s+you\b",
        r"\bgo\s+ahead\s+and\b",
        r"\bi\s+want\s+you\s+to\b",
        r"\bmake\s+it\b",
    ]
]


def _looks_imperative(text: str) -> bool:
    return any(p.search(text) for p in _IMPERATIVE_PATTERNS)


def assess_retention(text: str) -> tuple[bool, str]:
    """Only meaningful for a CHAT-classified transcript. Decides whether
    the dictation gets written to ~/.jarvis/dictations/ at all, or
    discarded -- the retention half of NOT_ADDRESSED (SPEC-L2.5's sixth
    intent class). CHAT itself already never speaks or forwards (see
    concierge.py's guard); this is purely about whether an artifact
    survives.

    The one place the project's usual fail-toward-dispatch asymmetry
    inverts, and inverts for a specific reason: forwarding an ambiguous
    turn to L3 costs nothing but time and is fully recoverable.
    Discarding a transcript is not -- there is no artifact left to
    diagnose from if the discard was wrong. So discard only on HIGH
    confidence (an independent deterministic imperative check AND the
    model both have to agree it's ambient); anything else -- including a
    plain model UNSURE -- keeps the file. A kept-but-silent transcript
    and a genuinely-addressed kept transcript are behaviorally identical
    from Ayman's side (neither speaks); the only stakes are the retention
    decision, so there's no harm in defaulting to keep whenever unsure.

    Returns (retain, reason) -- reason is for the log event and must
    never be or contain the transcript text itself (see
    concierge.py's retention-decision logging, "log the event, never the
    content")."""
    if _looks_imperative(text):
        return True, "imperative-shaped despite CHAT classification, retaining"
    # Lowercased before the model call -- found by testing, not a
    # theoretical concern: "Thanks, appreciate it." (capital T, the exact
    # shape Whisper always produces -- it capitalizes sentence starts)
    # verdicts AMBIENT, while the semantically identical "thanks,
    # appreciate it" verdicts UNSURE, every time, deterministically (not
    # sampling noise -- reproduced 3x each way). A decision this
    # consequential can't ride on capitalization Whisper adds for free on
    # every real transcript; lowercasing removes that whole axis of
    # instability rather than trying to prompt around it.
    verdict = assess_addressed(text.lower())
    if verdict == "AMBIENT":
        return False, "high confidence not-addressed (ambient + no imperative pattern)"
    return True, f"retaining, insufficient confidence to discard (verdict={verdict})"


def classify(text: str) -> Classification:
    stripped = text.strip()
    for pat in _CONTROL_PATTERNS:
        if pat.match(stripped):
            return Classification(CONTROL, tier="keyword", matched_pattern=pat.pattern)
    for pat in _QUERY_PATTERNS:
        if pat.search(stripped):
            return Classification(QUERY, tier="keyword", matched_pattern=pat.pattern)
    # Lowercased before every model call in this function, not just
    # assess_retention's -- same fix, same reason. classify_with_model
    # itself was re-verified stable across casing on the full 28-case
    # adversarial set (0/28 flipped, same 1/20 residual either way), but
    # a narrower reproduction on a different phrasing did flip DISPATCH
    # vs UNSURE by capitalization alone. Normalizing removes the whole
    # axis rather than trusting that the validated set covers every
    # phrasing this will ever see in production.
    normalized = stripped.lower()
    label = classify_with_model(normalized)
    if label not in VALID_LABELS:
        label = UNSURE  # model returned something unrecognized -- fail toward forwarding, not toward chat

    if label == CHAT and mentions_live_session(stripped):
        # Hard rule, deterministic, applies regardless of which model is
        # behind classify_with_model: a transcript naming a real running
        # session is never idle chat -- the two worst measured failures
        # (a real instruction landing on CHAT, silently speaking nothing
        # and forwarding nothing) both named a live session. CHAT is
        # structurally unavailable here; re-ask constrained to DISPATCH
        # or QUERY only, so the model still gets to distinguish "do this"
        # from "tell me about this" rather than everything becoming a
        # blind forward. See ollama_client.reclassify_dispatch_or_query.
        label = reclassify_dispatch_or_query(normalized)
        if label not in (DISPATCH, QUERY):
            label = UNSURE  # constrained re-ask still didn't land cleanly -- fail toward forwarding
        return Classification(label, tier="session_override")

    return Classification(label, tier="model")
