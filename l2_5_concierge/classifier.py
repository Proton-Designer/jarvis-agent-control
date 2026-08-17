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
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ollama_client import classify_with_model, reclassify_dispatch_or_query
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

# Search (not anchor-match) is deliberate here, unlike CONTROL -- QUERY
# phrasing is more varied ("hey, what's the gateway doing right now") and
# a query embedded in a slightly longer sentence should still route as a
# query. Less risky than doing the same for CONTROL/DISPATCH: routing a
# QUERY-shaped chunk to the deterministic answer path costs nothing if
# wrong (worst case: an oddly-phrased response), unlike misrouting a real
# instruction.
_QUERY_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"what'?s\s+(\w+\s+)?(running|going on|happening)\??",
        r"what'?s\s+.+\s+doing\??",
        r"is\s+.+\s+done\s+yet\??",
        r"how\s+much\s+(have\s+i|did\s+i)\s+spen[dt]\??",
        r"what'?s\s+the\s+status\b",
    ]
]


def classify(text: str) -> Classification:
    stripped = text.strip()
    for pat in _CONTROL_PATTERNS:
        if pat.match(stripped):
            return Classification(CONTROL, tier="keyword", matched_pattern=pat.pattern)
    for pat in _QUERY_PATTERNS:
        if pat.search(stripped):
            return Classification(QUERY, tier="keyword", matched_pattern=pat.pattern)
    label = classify_with_model(stripped)
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
        label = reclassify_dispatch_or_query(stripped)
        if label not in (DISPATCH, QUERY):
            label = UNSURE  # constrained re-ask still didn't land cleanly -- fail toward forwarding
        return Classification(label, tier="session_override")

    return Classification(label, tier="model")
