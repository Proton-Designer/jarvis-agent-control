"""
Spoken feedback via macOS `say`, and the parallel echo+cancel-window
primitive used for plan confirmation.

Every refusal, delivery, and batch summary is spoken — a refusal Ayman
doesn't hear is functionally identical to a lost instruction, and with auto
mode removing tool-permission prompts, the cancel window is the only
human-in-the-loop control left in the system, so it must never be silently
skipped on any code path.
"""

from __future__ import annotations

import subprocess
import threading

from cancel_listener import listen_for_cancel


def speak(text: str) -> None:
    """Fire-and-forget TTS. Non-blocking: callers that need to do something
    else (like listen for the cancel trigger) while speech plays must not
    wait on this."""
    subprocess.Popen(["say", text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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
    """
    if cancel_window_s <= 0:
        speak(text)
        return {"cancelled": False, "available": True}

    result = {}

    def _listen():
        result["cancel_result"] = listen_for_cancel(cancel_window_s)

    listener_thread = threading.Thread(target=_listen, daemon=True)
    listener_thread.start()
    speak(f"{text} Say Hey Jarvis to cancel.")  # non-blocking Popen; overlaps with the listener
    listener_thread.join()

    cancel_result = result["cancel_result"]
    if not cancel_result.available:
        speak("Cancel unavailable.")
    return {"cancelled": cancel_result.cancelled, "available": cancel_result.available}
