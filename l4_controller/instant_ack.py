"""
Instant acknowledgement (SPEC-orchestration.md SS1.1): fires at ~0ms, the
moment a dictation ends, before ANYTHING else happens with the transcript
-- so there is never silence between "that's it" and the concierge
tier's own ~2s reply. Templated, not generated: there is no local model
any more (the concierge disconnection, 2026-08-18) and this must not
become a reason to bring one back.

Team names come from the SAME registered-team ids/aliases the console
already renders (~/Jarvis/teams.json, via teams.load_registry() -- a
plain, cheap file read, safe to call on this hot path) -- matched against
the raw transcript by plain, case-insensitive, whole-word substring, zero
inference. This is receipt, never an outcome: the actual routing decision
hasn't been made yet when this fires, so the phrase must never assert
what will happen ("sending this to gateway") -- only that something with
that name was heard ("Okay -- gateway.").

Queued at PRIORITY_HIGH (say_feedback.py) so it preempts any
informational return-channel backlog that might be mid-flush when a new
dictation starts (SPEC-orchestration.md SS2.3) -- the whole guarantee
this exists for is defeated if the ack sits behind three queued
completion messages.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "l5_console" / "state"))
from teams import load_registry  # noqa: E402

import threading
import time

from say_feedback import PRIORITY_HIGH, speak, SAY_LOG_PATH
from latency_log import log_event  # noqa: E402

GENERIC_ACK = "Okay, one sec."


def _mentioned_team_names(transcript: str) -> list[str]:
    """Every registered team NAME (the specific id/alias text that
    actually matched, not the team's canonical id) that appears as a
    whole word/phrase in the transcript, in registry order, one entry
    per team even if more than one of its aliases matches. Whole-word
    match, not bare substring -- a team called "api" must not match
    inside "capitalism". Returns the matched text itself (e.g. "the
    gateway project" if that's the alias that fired), not the team's
    slugified id, so the spoken phrase reflects something closer to what
    was actually said rather than an internal identifier."""
    found = []
    for entry in load_registry():
        names = [entry["id"], *entry.get("aliases", [])]
        for name in names:
            if not name:
                continue
            if re.search(rf"\b{re.escape(name)}\b", transcript, re.IGNORECASE):
                found.append(name)
                break  # one mention per team, even if several of its names match
    return found


def instant_ack_phrase(transcript: str) -> str:
    """The phrase to speak, computed with zero inference -- plain
    substring matching against known team names, never a guess at what
    the transcript MEANS or what will be done about it. Falls back to a
    generic receipt phrase when no known team name appears (most
    utterances, and any transcript that doesn't name a registered team by
    its exact id/alias)."""
    names = _mentioned_team_names(transcript)
    if not names:
        return GENERIC_ACK
    if len(names) == 1:
        return f"Okay -- {names[0]}."
    return "Okay -- " + " and ".join(names) + "."


# How long to wait for a REAL reply before falling back to the ack.
#
# Ayman, 2026-08-20: "I don't wanna hear ok one second after I say hello,
# that's just weird, I need a direct response to my statement not an ack."
# He is right, and the reasoning behind the old behaviour had expired
# without anyone noticing.
#
# The ack was designed when a reply took 30+ seconds -- one big
# orchestrator did the thinking, and silence that long is
# indistinguishable from "it didn't hear me". That problem is real and
# the ack solved it. But the concierge now answers in ~2.3s (measured
# live: pointer delivered 16:45:52.053, jarvis_say 16:45:54.398). An
# announcement that something is coming, two seconds before it arrives,
# is worse than nothing: it delays the actual answer and makes a fast
# system feel slow.
#
# So the ack becomes a FALLBACK rather than a preamble. If a real reply
# lands first, he never hears it at all. If nothing has been said by the
# deadline -- a slow router turn, a wedged concierge, a dispatch that
# genuinely takes time -- it still fires, and the silence problem it was
# built for stays solved.
#
# 3.0s, slightly above the measured 2.3s: below that a normal answer
# would race the ack and sometimes lose, which would be the old weirdness
# reappearing at random.
ACK_FALLBACK_AFTER_S = 3.0


def speak_instant_ack(transcript: str) -> None:
    """Speaks the ack ONLY IF nothing else has spoken within
    ACK_FALLBACK_AFTER_S. Returns immediately; the wait happens on a
    daemon thread.

    "Has anything been said" is read from say_feedback's own say_log,
    which every speaker appends to REGARDLESS OF PROCESS -- the concierge
    answers from inside an MCP server while this runs in the wake daemon,
    so an in-process flag could not see it. The log is the only signal
    both sides already share.

    Fails toward SPEAKING: if the log can't be read, the ack fires. A
    spurious "Okay, one sec." is mildly annoying; silence after a
    dictation is the failure this whole layer exists to prevent, and that
    asymmetry is why the ack existed in the first place."""
    phrase = instant_ack_phrase(transcript)
    baseline = _say_log_size()

    def _maybe_speak() -> None:
        time.sleep(ACK_FALLBACK_AFTER_S)
        now = _say_log_size()
        if now is None or baseline is None or now > baseline:
            if now is not None and baseline is not None and now > baseline:
                # Something real was already said -- he does not need to
                # be told his words were received by something that has
                # already replied to them.
                log_event("instant_ack_suppressed", reason="real_reply_spoke_first")
                return
        speak(phrase, priority=PRIORITY_HIGH)
        log_event("instant_ack_spoken", after_s=ACK_FALLBACK_AFTER_S)

    threading.Thread(target=_maybe_speak, daemon=True, name="instant-ack").start()


def _say_log_size() -> int | None:
    """Byte length of the shared speech log, or None if unreadable.
    Cheap, and any append at all means somebody spoke."""
    try:
        return SAY_LOG_PATH.stat().st_size
    except OSError:
        return None
