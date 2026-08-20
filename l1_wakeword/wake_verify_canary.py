#!/usr/bin/env python3
"""Canary for the wake-word VERIFICATION stage's transcript matcher.

Two failures this guards, pulling in opposite directions -- which is why
every case here is asserted in both.

REJECTING THE REAL WAKE WORD. Found live, 2026-08-20: Ayman said "hey
Jarvis" and was rejected six times in a row. The acoustic model scored
0.90-0.98 every time -- he said it correctly -- but Whisper transcribed
"hey Jorvis" / "Hey Jorvis" / "ei jorvis", and the matcher only accepted
`jarvis|jervis`. A verifier that rejects the real wake word is worse than
no verifier: it teaches him to repeat himself and distrust the system.

ACCEPTING A NAME THAT ISN'T. This stage exists because the acoustic model
cannot tell "Hey Charles" (0.998) or "Hey Travis" (0.983) from a real
"Hey Jarvis" (0.999) -- measured, same voice, same batch. No threshold
separates them. Whisper can, because the phonetic content is nothing
alike. So widening the spelling must never widen the WORD, and a false
accept here opens the microphone on the room.

The point of the file: those two pressures are in tension, so loosening
for one must be checked against the other, every time. Fixing the
rejections by matching more loosely would have quietly re-armed the
false-accept the verifier was built to stop.

Pure regex; no mic, no model, no Whisper.

    l1_wakeword/.venv/bin/python3 l1_wakeword/wake_verify_canary.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import daemon as d  # noqa: E402

FAILURES: list[str] = []


def check(desc: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ok    {desc}")
    else:
        FAILURES.append(desc)
        print(f"  FAIL  {desc}{('  -- ' + detail) if detail else ''}")


# Every one of these was produced by Whisper for a real, correctly-spoken
# "hey Jarvis", or is a phonetically plausible sibling of one that was.
MUST_ACCEPT = [
    ("jarvis", "the plain spelling"),
    ("Jarvis", "capitalised, as Whisper usually renders a name"),
    ("jervis", "already known"),
    ("jorvis", "REJECTED LIVE 2026-08-20 -- the bug this file exists for"),
    ("Hey Jorvis", "REJECTED LIVE, capitalised"),
    ("ei jorvis", "REJECTED LIVE -- 'hey' itself misheard too"),
    ("hey jarvis", "the ordinary case, with the greeting"),
    ("jarvus", "vowel swap"),
    ("jurvis", "vowel swap"),
    ("jarviss", "doubled s"),
    ("javis", "dropped r"),
    ("garvis", "g/j confusion at the front"),
    ("jorvis,", "trailing punctuation"),
    ("JARVIS", "all caps"),
]

# A false accept here opens the microphone on whatever is said next.
MUST_REJECT = [
    ("travis", "MEASURED 0.983 on the acoustic model -- the reason this stage exists"),
    ("Hey Travis", "the full phrase that fools the acoustic model"),
    ("charles", "MEASURED 0.998 -- scores higher than some real wake words"),
    ("Hey Charles", "the full phrase"),
    ("marvis", "one letter off, and still not his assistant"),
    ("harvest", "contains 'arve'"),
    ("carve", "contains 'arv'"),
    ("starve", "contains 'arv'"),
    ("service", "contains 'rvi'"),
    ("jarred", "starts with 'jar'"),
    ("that's it", "the stop phrase must never start a dictation"),
    ("jaa", "a real observed partial -- fails closed"),
    ("", "empty transcript fails closed"),
    ("hey there", "ordinary speech"),
]


def run() -> int:
    print("must ACCEPT -- a real wake word, however Whisper spells it")
    for text, why in MUST_ACCEPT:
        check(f"{text!r} accepted ({why})", bool(d._JARVIS_TRANSCRIPT_RE.search(text)))

    print()
    print("must REJECT -- a false accept opens the mic on the room")
    for text, why in MUST_REJECT:
        check(f"{text!r} rejected ({why})", not d._JARVIS_TRANSCRIPT_RE.search(text))

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"all {len(MUST_ACCEPT) + len(MUST_REJECT)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(run())
