"""
Spoken feedback via macOS `say`, and the parallel echo+cancel-window
primitive used for plan confirmation.

Every refusal, delivery, and batch summary is spoken — a refusal Ayman
doesn't hear is functionally identical to a lost instruction, and with auto
mode removing tool-permission prompts, the cancel window is the only
human-in-the-loop control left in the system, so it must never be silently
skipped on any code path.

MUTE MODE — a real product feature, not test scaffolding. Ayman will want
to route instructions silently when he's on a call, in a meeting, or
around other people, not just during testing (this module's audio firing
mid-adversarial-test-run on his own laptop, unannounced, is what prompted
building this properly rather than as a workaround). Set JARVIS_MUTE=1 to
suppress the `say` subprocess. What mute does NOT do: skip the cancel
window. Suppressing the audio is fine; suppressing the human-in-the-loop
control itself would just be another quiet way to remove it — the same
failure class as the cancel_listener fail-open bug. speak_with_cancel_window
still arms listen_for_cancel and honors the timeout under mute; only the
`say` subprocess is skipped. Every speak() call, muted or not, is logged to
~/.jarvis/say_log.jsonl with an explicit `muted` field, so a clean muted
test run is never mistaken for evidence that the audio path itself works —
it hasn't been exercised.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

from cancel_listener import listen_for_cancel
from latency_log import log_event

MUTE = os.environ.get("JARVIS_MUTE", "0") == "1"
SAY_LOG_PATH = Path.home() / ".jarvis" / "say_log.jsonl"

# Default `say` voice is a novelty/robotic system voice (Fred/Alex-class),
# not something meant to be heard on every turn. Samantha is the best
# quality voice actually installed on this machine (checked via `say -v
# '?'` — no Siri/Enhanced/Premium voices are downloaded locally; those
# require a one-time System Settings > Accessibility > Spoken Content
# download this can't trigger headlessly). Overridable via JARVIS_VOICE so
# picking a downloaded premium voice later is a config change, not a code
# change.
VOICE = os.environ.get("JARVIS_VOICE", "Samantha")


def _log_say(text: str, muted: bool) -> None:
    SAY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "muted": muted,
        "text": text,
    }
    with SAY_LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def speak(text: str) -> None:
    """Fire-and-forget TTS, unless JARVIS_MUTE=1 -- then this only logs what
    would have been spoken. Non-blocking either way: callers that need to
    do something else (like listen for the cancel trigger) while speech
    plays must not wait on this."""
    _log_say(text, MUTE)
    if MUTE:
        return
    subprocess.Popen(
        ["say", "-v", VOICE, text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def speak_with_cancel_window(text: str, cancel_window_s: float) -> dict:
    """Speak `text` (with a "say Hey Jarvis to cancel" hint appended — see
    below) and listen for the cancel trigger IN PARALLEL (not sequentially
    — the window must not be eaten by waiting for speech to finish first,
    which would just add dead time to an already-tight budget). Returns
    {"cancelled": bool, "available": bool}.

    The cancel trigger, per the 2026-08-17 ruling, is a re-detection of the
    "Hey Jarvis" wake word during this window — not a separate "cancel"
    keyword (openWakeWord has no pretrained model for one, and a custom one
    would carry an unmeasured false-negative rate on the one control that
    can't afford it). The spoken hint says so explicitly, baked in here
    rather than left to whatever L3 happens to put in its summary — a
    prompt telling Ayman to say a word that won't work is worse than
    saying nothing.

    `available=False` means the cancel socket was missing/unreachable —
    there was NO real cancel window, not a window that simply wasn't
    triggered. This is spoken explicitly ("Cancel unavailable.") rather
    than silently folded into cancelled=False, because a missing safety
    control must never look identical to that control having run and
    found nothing to cancel. Callers MUST check `available` before
    treating a batch as safe to deliver to a real (non-test) target.

    cancel_window_s == 0 disables the cancel window entirely (config
    parameter, not a hardcoded behavior) and this becomes a plain speak()
    with available=True (the window was deliberately not requested, which
    is a different, intentional case from the socket being down) and no
    cancel hint appended.

    Under JARVIS_MUTE=1, the listener still arms and the timeout is still
    honored (this function's control flow doesn't change) -- only speak()'s
    internal `say` call is suppressed. Mute silences the room, not the
    control.
    """
    if cancel_window_s <= 0:
        log_event("confirm_spoken", cancel_window_s=cancel_window_s)
        speak(text)
        log_event("cancel_window_closed", cancelled=False, available=True)
        return {"cancelled": False, "available": True}

    result = {}

    def _listen():
        result["cancel_result"] = listen_for_cancel(cancel_window_s)

    listener_thread = threading.Thread(target=_listen, daemon=True)
    listener_thread.start()
    log_event("confirm_spoken", cancel_window_s=cancel_window_s)
    speak(f"{text} Say Hey Jarvis to cancel.")  # non-blocking Popen; overlaps with the listener
    listener_thread.join()

    cancel_result = result["cancel_result"]
    if not cancel_result.available:
        speak("Cancel unavailable.")
    log_event(
        "cancel_window_closed", cancelled=cancel_result.cancelled, available=cancel_result.available
    )
    return {"cancelled": cancel_result.cancelled, "available": cancel_result.available}
