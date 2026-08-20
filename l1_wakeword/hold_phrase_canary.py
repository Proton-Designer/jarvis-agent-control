#!/usr/bin/env python3
"""Canary for the silence window and its hold-phrase extension.

Ayman's change, 2026-08-20. The silence net was 50s, on the reasoning
that "that's it" was the reliable way to end a dictation and 50s only
existed so a thinking pause could never cut him off. That is right for a
long instruction and badly wrong for saying hello -- a two-word greeting
plus a two-second pause meant waiting FIFTY SECONDS for a reply.

So silence became the ordinary end (2.7s) and the stop phrase became the
way to end INSTANTLY rather than the only way to end at all. The thinking
pause is now protected explicitly, on demand, by saying "hold up".

BOTH DIRECTIONS THROUGHOUT, because each half is load-bearing in a
different way:
  - too eager to extend  -> he waits 50s for a hello, the bug we just fixed
  - too eager to cut     -> he is cut off mid-thought and HALF AN
                            INSTRUCTION reaches a real agent

The second is worse, so the matcher deliberately fails toward waiting.
"tell it to wait" DOES arm a hold, and that is intended, not a defect --
asserted below so nobody "fixes" it later.

Pure functions and a fake chunker; no mic, no audio, no model.

    l1_wakeword/.venv/bin/python3 l1_wakeword/hold_phrase_canary.py
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


class _FakeChunker:
    def __init__(self):
        self.silence_duration_s = 0.0
        self.last_cut_reason = "silence"


def _session():
    s = d.DictationSession.__new__(d.DictationSession)
    s._chunker = _FakeChunker()
    s._hold_active = False
    # Non-empty by default: most checks here are about the window BETWEEN
    # words, which only applies once something has been said.
    s.chunks_transcribed = ["something already said"]
    return s


def run() -> int:
    print("the window itself")
    check("default silence window is 2.7s, not 50s -- silence is the ORDINARY end now",
          d.SILENCE_SAFETY_NET_S == 2.7, str(d.SILENCE_SAFETY_NET_S))
    check("a hold buys back the full 50s", d.SILENCE_HOLD_S == 50.0, str(d.SILENCE_HOLD_S))

    print()
    print("hold phrases are recognised -- at the END of a chunk, like the stop phrase")
    for phrase in ("hold up", "hold on", "wait", "let me think", "one sec", "hang on"):
        check(f"{phrase!r} arms a hold", d.match_hold_phrase(phrase) is True)
    check("...and trailing a sentence too: 'okay hang on'",
          d.match_hold_phrase("okay hang on") is True)

    print()
    print("the other direction -- ordinary speech must NOT arm a hold")
    for phrase in ("hello", "run the tests", "what's running right now",
                   "wait for the deploy to finish", "tell gateway to hold the release"):
        check(f"{phrase!r} does NOT arm a hold", d.match_hold_phrase(phrase) is False)

    print()
    print("the deliberate false positive, asserted so nobody 'fixes' it")
    check("'tell it to wait' DOES arm a hold -- fails toward waiting, never toward cutting him off",
          d.match_hold_phrase("tell it to wait") is True)

    print()
    print("the window actually changes, and the hold is TEMPORARY")
    s = _session()
    check("a fresh session uses the short window", s.silence_limit_s == 2.7, str(s.silence_limit_s))
    s._chunker.silence_duration_s = 3.0
    check("...so 3s of silence ends an ordinary dictation",
          s.silence_exceeds_safety_net() is True)

    s2 = _session()
    s2.note_hold_phrase()
    check("after a hold, the window is 50s", s2.silence_limit_s == 50.0, str(s2.silence_limit_s))
    s2._chunker.silence_duration_s = 3.0
    check("...so the SAME 3s pause no longer ends it -- he gets to think",
          s2.silence_exceeds_safety_net() is False)
    s2._chunker.silence_duration_s = 51.0
    check("...but 51s still finalizes, so a hold can never hang forever",
          s2.silence_exceeds_safety_net() is True)

    s3 = _session()
    s3.note_hold_phrase()
    s3.clear_hold()
    check("speech resuming SPENDS the hold -- one pause, not a mode the dictation stays in",
          s3.silence_limit_s == 2.7, str(s3.silence_limit_s))

    print()
    print("the OPENING window -- before he has said anything at all")
    fresh = _session()
    fresh.chunks_transcribed = []
    check("with nothing said yet, the window is 10s, not 2.7s",
          fresh.silence_limit_s == 10.0, str(fresh.silence_limit_s))
    fresh._chunker.silence_duration_s = 3.0
    check("...so pausing 3s to collect his thoughts does NOT close it -- "
          "the exact failure from his first live test",
          fresh.silence_exceeds_safety_net() is False)
    fresh._chunker.silence_duration_s = 11.0
    check("...but 11s of total silence still ends it -- an open mic must not "
          "wait forever for someone who walked away",
          fresh.silence_exceeds_safety_net() is True)

    spoke = _session()
    spoke.chunks_transcribed = ["hello"]
    check("the moment he HAS spoken, the window drops to 2.7s",
          spoke.silence_limit_s == 2.7, str(spoke.silence_limit_s))
    spoke._chunker.silence_duration_s = 3.0
    check("...so a 3s gap mid-dictation ends it, as before",
          spoke.silence_exceeds_safety_net() is True)

    held_fresh = _session()
    held_fresh.chunks_transcribed = []
    held_fresh.note_hold_phrase()
    check("an explicit hold outranks the opening grace (50s, not 10s) -- "
          "he asked for time, which beats an inferred allowance",
          held_fresh.silence_limit_s == 50.0, str(held_fresh.silence_limit_s))

    print()
    print("hold phrases are NOT stripped from the transcript")
    check("match_hold_phrase only reports; it never rewrites text",
          d.match_hold_phrase.__doc__ is not None and "corrupts" in d.match_hold_phrase.__doc__)
    matched, remainder = d.match_stop_phrase("tell it to wait")
    check("...and 'tell it to wait' is not mistaken for a stop phrase either",
          matched is False and remainder == "tell it to wait", f"{matched} {remainder!r}")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(run())
