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


def speak_with_cancel_window(text: str, cancel_window_s: float) -> bool:
    """Speak `text` and listen for a cancel word IN PARALLEL (not
    sequentially — the window must not be eaten by waiting for speech to
    finish first, which would just add dead time to an already-tight
    budget). Returns True if cancelled.

    cancel_window_s == 0 disables the cancel window entirely (config
    parameter, not a hardcoded behavior) and this becomes a plain speak().
    """
    if cancel_window_s <= 0:
        speak(text)
        return False

    result = {"cancelled": False}

    def _listen():
        result["cancelled"] = listen_for_cancel(cancel_window_s)

    listener_thread = threading.Thread(target=_listen, daemon=True)
    listener_thread.start()
    speak(text)  # non-blocking Popen; overlaps with the listener thread
    listener_thread.join()
    return result["cancelled"]
