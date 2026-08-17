#!/usr/bin/env python3
"""Post-transcription filter for Whisper's confabulation-on-low-speech
behavior.

The VAD speech-content gate (vad_chunker.py) cuts off most of the audio
that triggers this, but not all of it -- background noise during a real
pause in a long dictation, a marginal VAD call, etc. can still produce a
chunk that passes the gate but is still confabulated. This is the second
layer, not a replacement for the first.

Why this matters more than a typical STT quality issue: a hallucinated
sentence doesn't look different from a real one by the time it reaches
L3 -- it becomes a routed instruction with no signal that Ayman never
said it. Fail toward dropping: a dropped real sentence costs a
re-dictation Ayman will notice missing at the plan-confirmation summary;
a hallucinated one that gets through is silent and can't be un-delivered.
Same asymmetry that's governed every other call on this project.

Every drop is logged (not silently swallowed) so the false-drop rate is
observable rather than assumed -- this is a heuristic filter and it WILL
occasionally drop something real; the question is whether it's rare
enough to live with, and that can only be answered by watching the logs.
"""
import logging
import re

log = logging.getLogger(__name__)

# Whisper's most common confabulations on low/no-speech audio come from its
# training corpus of subtitled video -- these are recognizable, repeated
# patterns across many independent reports of the same behavior, not
# specific to this project. Matched case-insensitively, substring or
# regex as noted. Not exhaustive by design -- see MAX_REPEAT_RATIO below
# for the structural catch-all that doesn't depend on a pattern list.
_HALLUCINATION_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\bthanks? for watching\b",
        r"\bthank you for watching\b",
        r"\bplease (like and )?subscribe\b",
        r"\bdon'?t forget to subscribe\b",
        r"\bsee you (in the )?next (video|time)\b",
        r"\bsubtitles? (by|provided by|courtesy of)\b",
        r"\bcaptions? by\b",
        r"\bclosed captions? by\b",
        r"\btranslat(ed|ion) by\b",
        r"\bamara\.org\b",
        r"\bwww\.\S+\.(com|org|net)\b",
        r"\bcopyright \d{4}\b",
        r"\ball rights reserved\b",
        r"\[music\]",
        r"\[applause\]",
        r"\bmbc\s*뉴스\b",  # a specific, frequently-reported Whisper hallucination
    ]
]

# If a short phrase repeats this many times or more back-to-back, treat the
# whole chunk as suspect regardless of what the phrase is -- verbatim
# looping is a hallmark of Whisper decoding into a degenerate repeat loop
# on unclear/low-content audio, not something a person actually says.
MAX_CONSECUTIVE_REPEATS = 4


def _has_pattern_match(text: str) -> str | None:
    for pattern in _HALLUCINATION_PATTERNS:
        if pattern.search(text):
            return pattern.pattern
    return None


def _has_degenerate_repetition(text: str) -> str | None:
    """Checks for a short PHRASE (1-4 words), not just a single word,
    repeating back-to-back MAX_CONSECUTIVE_REPEATS+ times -- Whisper's
    degenerate decoding loops repeat multi-word phrases as often as single
    words ("I don't know I don't know I don't know...")."""
    words = [w.lower() for w in text.split()]
    for n in range(1, 5):
        if len(words) < n * MAX_CONSECUTIVE_REPEATS:
            continue
        i = 0
        while i + n <= len(words):
            phrase = words[i : i + n]
            run = 1
            j = i + n
            while j + n <= len(words) and words[j : j + n] == phrase:
                run += 1
                j += n
            if run >= MAX_CONSECUTIVE_REPEATS:
                return f'"{" ".join(phrase)}" repeated {run}x consecutively'
            i = j if run > 1 else i + 1
    return None


def filter_transcript(text: str, source: str = "chunk") -> str | None:
    """Returns text unchanged if it looks like real speech, or None (and
    logs why) if it matches a known hallucination pattern. Call this on
    every chunk transcript before it's added to the assembled dictation --
    not just ones from low-confidence paths, since the whole point is not
    trusting a single signal to catch all of this."""
    stripped = text.strip()
    if not stripped:
        return None

    pattern = _has_pattern_match(stripped)
    if pattern:
        log.warning("hallucination_filter: dropped %s (matched %r): %r", source, pattern, stripped)
        return None

    repetition = _has_degenerate_repetition(stripped)
    if repetition:
        log.warning("hallucination_filter: dropped %s (%s): %r", source, repetition, stripped)
        return None

    return text


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    for line in sys.stdin:
        result = filter_transcript(line.rstrip("\n"))
        print(f"KEEP: {result!r}" if result else "DROPPED")
