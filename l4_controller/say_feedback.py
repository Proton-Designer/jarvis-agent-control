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
    else (like listen for a cancel word) while speech plays must not wait
    on this."""
    subprocess.Popen(["say", text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def speak_with_cancel_window(text: str, cancel_window_s: float) -> dict:
    """Speak `text` and listen for a cancel word IN PARALLEL (not
    sequentially — the window must not be eaten by waiting for speech to
    finish first, which would just add dead time to an already-tight
    budget). Returns {"cancelled": bool, "available": bool}.

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
    is a different, intentional case from the socket being down).
    """
    if cancel_window_s <= 0:
        speak(text)
        return {"cancelled": False, "available": True}

    result = {}

    def _listen():
        result["cancel_result"] = listen_for_cancel(cancel_window_s)

    listener_thread = threading.Thread(target=_listen, daemon=True)
    listener_thread.start()
    speak(text)  # non-blocking Popen; overlaps with the listener thread
    listener_thread.join()

    cancel_result = result["cancel_result"]
    if not cancel_result.available:
        speak("Cancel unavailable.")
    return {"cancelled": cancel_result.cancelled, "available": cancel_result.available}
