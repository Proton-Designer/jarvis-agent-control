"""
L2 -> L3 handoff: deliver_transcript(text) -> DeliveryResult.

Contract agreed with the L1/L2 owner: a plain importable function, called
from their daemon after VAD-endpointed capture + STT. Signature is stable;
what happens inside it changed with the architecture pivot to long-form
dictation (up to 5-6 minutes, ~5,000 characters, multiple instructions for
multiple targets in one utterance):

  Do NOT type a multi-thousand-character transcript into a tmux input box.
  Write it to a durably-logged file instead, and deliver a short pointer
  instruction telling the orchestrator where to read it. This also gives
  the system a forensic trail — auto-mode target sessions have no
  permission prompts to fall back on, so the dictation log is the record
  of what was actually said if a misroute ever happens.

L1/L2 does not need to change anything on their end for this — same
function, same signature, same busy/rejected DeliveryResult contract they
already agreed to.

IMMEDIATE ACK: the real end-to-end pipeline run (2026-08-17) measured
~2 minutes from Ayman finishing speaking to the last delivery landing,
dominated by orchestrator reasoning, not audio processing. The actual UX
failure isn't that duration -- it's the SILENCE: Ayman has no signal the
system heard him until routing finishes, which risks him re-triggering
the wake word and starting a second, overlapping dictation. So
deliver_transcript speaks a short, generic acknowledgement (via
say_feedback, so JARVIS_MUTE governs it like everything else) the instant
it's called -- before writing the file, before the pointer delivery,
before any orchestrator reasoning has happened. Deliberately generic, not
"routing N instructions": at this point nothing has parsed the dictation
yet, so a specific count would be a guess dressed up as a fact.
"""

from __future__ import annotations

import time
from pathlib import Path

from dispatch_state import mark_dispatch_forwarded
from latency_log import log_event
from say_feedback import speak
from transport import Transport

DICTATIONS_DIR = Path.home() / ".jarvis" / "dictations"

# Naming convention matches sessions.json's "claude-<project>" pattern.
DEFAULT_ORCHESTRATOR_TARGET = "claude-orchestrator"


def write_dictation(text: str) -> Path:
    DICTATIONS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%dT%H%M%S")
    path = DICTATIONS_DIR / f"{timestamp}.txt"
    # Exclusive-create with a numeric suffix on collision, rather than
    # silently overwriting an existing dictation from the same second.
    n = 1
    while path.exists():
        path = DICTATIONS_DIR / f"{timestamp}-{n}.txt"
        n += 1
    path.write_text(text)
    return path


def deliver_transcript(
    text: str,
    transport: Transport,
    orchestrator_target: str = DEFAULT_ORCHESTRATOR_TARGET,
):
    log_event("handoff_received", chars=len(text))
    speak("Got it, working on it.")
    path = write_dictation(text)
    pointer = f"New dictation at {path} — read it and route the instructions inside."
    # Code-driven "forwarded" marker for the concierge's dispatch-in-flight
    # state -- written here, not by L3, because this is the actual moment
    # of handoff and this call already fires exactly once per real
    # forward. See dispatch_state.py for why this must not be an
    # LLM self-report.
    mark_dispatch_forwarded(str(path))
    result = transport.deliver(orchestrator_target, pointer)
    log_event("pointer_delivered", ok=result.ok, target=orchestrator_target)
    return result
