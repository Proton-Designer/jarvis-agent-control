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

from say_feedback import PRIORITY_HIGH, speak  # noqa: E402

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


def speak_instant_ack(transcript: str) -> None:
    """Fire-and-forget entry point -- call this FIRST, before handing the
    transcript to the concierge tier for real classification/routing."""
    speak(instant_ack_phrase(transcript), priority=PRIORITY_HIGH)
