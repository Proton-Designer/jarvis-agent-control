"""
Spoken feedback via Kokoro-82M (docs/TODO-feature-queue.md #1), and the
parallel echo+cancel-window primitive used for plan confirmation.

KOKORO IS PRIMARY, `say` IS THE FALLBACK ONLY. Every queued item goes
through kokoro_tts.py first; `say` runs only when Kokoro fails to load
or a specific synthesis call errors. The fallback is never silent: it
speaks in a different (lesser) voice, which is itself an audible signal
something changed, AND every fallback is logged via latency_log's
`kokoro_tts_fallback` event (grep-able, and what the fallback canary
asserts against) -- a silent degrade to the old voice is exactly the
failure class this project keeps finding and ruling against. See
_speak_now()'s docstring and kokoro_tts.py's module docstring for the
measured cold-start/warm-latency numbers this design is based on.

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

SERIALIZED (SPEC-orchestration.md SS0.3). Used to be a bare
subprocess.Popen per call -- safe when only one process (L3, or the
concierge) ever called speak(). The two-tier architecture multiplies
callers: the Haiku concierge, the Sonnet router, and every team lead via
jarvis_say() can all speak. Two overlapping `say` processes garble into
one confusing sentence, which is worse than silence because Ayman can't
tell it's a bug -- the same problem voicemode solves with its
single-writer "conch" socket.

Every speak()/speak_with_cancel_window() call now ENQUEUES onto one
priority queue serviced by a single background worker thread that runs
`say` synchronously, one at a time, in priority-then-submission order --
never two `say` processes running concurrently. The public call signature
is unchanged for existing callers (speak(text) still fire-and-forget,
returns immediately) except for the new optional `priority` kwarg.
PRIORITY_HIGH (0) is for anything timing-sensitive or safety-relevant
(refusals, the cancel-window confirmation); PRIORITY_NORMAL (1, the
default) is everything else. Lower number speaks first; FIFO within the
same priority.

The say_log write happens inside the worker, at the point a queued item
is actually about to be spoken (or would have been, under mute) -- not
at the moment speak() is called and enqueued. This is deliberate: the
log is the thing SPEC-orchestration.md's verification bar for 0.3 uses to
prove two simultaneous calls actually came out in priority order ("use
JARVIS_MUTE and the say_log to verify ordering without making the laptop
talk") -- a log stamped at enqueue time would only ever show call order,
which can't distinguish "the queue reordered these correctly" from "they
just happened to be called in priority order already." JARVIS_MUTE still
governs only the `say` subprocess, logged exactly the same either way,
same as before this change -- only WHEN the log write happens moved, not
whether it's unconditional.

speak_with_cancel_window()'s listener no longer starts counting the
instant the function is called -- it waits for a signal that this
specific queued item has actually reached the front of the queue and
`say` is about to start (or would have, under mute), THEN arms
listen_for_cancel(cancel_window_s). This preserves the original intent
exactly (the window must not be eaten by waiting for speech to finish,
and must not start ticking before Ayman can possibly be hearing anything
either) under a queue that can now introduce real delay before playback
begins if something else is ahead of it.
"""

from __future__ import annotations

import itertools
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import kokoro_tts
from cancel_listener import listen_for_cancel
from latency_log import log_event

sys.path.insert(0, str(Path(__file__).parent.parent))
from jarvis_paths import jarvis_home  # noqa: E402

MUTE = os.environ.get("JARVIS_MUTE", "0") == "1"
SAY_LOG_PATH = jarvis_home() / "say_log.jsonl"

# Kokoro's own voice (kokoro_tts.py:DEFAULT_VOICE, "bm_george" -- see that
# module's docstring for why, over the equally-graded bm_fable and the
# lower-graded bm_daniel/bm_lewis). Overridable so a later ear-check
# result (Ayman's, or a future voice) is a config change, not a code one.
KOKORO_VOICE = os.environ.get("JARVIS_KOKORO_VOICE", kokoro_tts.DEFAULT_VOICE)

# FALLBACK-ONLY now (Kokoro is primary -- see _speak_now()). "Daniel" is
# real macOS British-male voice (verified live via `say -v '?'`, en_GB,
# installed by default -- confirmed present on this machine, no download
# needed) -- chosen so even a degraded fallback keeps the voice
# CHARACTER Ayman actually asked for (British male), rather than jumping
# to an unrelated American female voice on the one path that's already
# a worse experience. Previously "Samantha", back when `say` was the
# only voice this module had. Overridable via JARVIS_VOICE.
VOICE = os.environ.get("JARVIS_VOICE", "Daniel")

PRIORITY_HIGH = 0    # refusals, cancel-window confirmations -- timing/safety-sensitive
PRIORITY_NORMAL = 1  # everything else; the default for untouched call sites

_seq_counter = itertools.count()
_speech_queue: "queue.PriorityQueue[tuple[int, int, str, object]]" = queue.PriorityQueue()
_worker_lock = threading.Lock()
_worker_started = False


def _log_say(text: str, muted: bool) -> None:
    SAY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "muted": muted,
        "text": text,
    }
    with SAY_LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _ensure_worker_started() -> None:
    """Lazy singleton, not started as an import side effect -- a module
    that imports say_feedback just to reach a constant (there are several)
    shouldn't spin up a background thread it never uses. Started on first
    real enqueue instead."""
    global _worker_started
    if _worker_started:
        return
    with _worker_lock:
        if _worker_started:
            return
        threading.Thread(target=_worker_loop, daemon=True, name="say-feedback-worker").start()
        _worker_started = True


def _worker_loop() -> None:
    """Services _speech_queue forever, one item at a time, lowest
    priority-number first, FIFO within a tie (the seq field). Runs
    _speak_now() SYNCHRONOUSLY so the next item can never start until
    this one's audio has actually finished -- that's the entire
    serialization guarantee, unchanged by the Kokoro rewrite. A per-item
    exception is caught and logged rather than killing the worker: a
    dead worker thread means every future speak() call silently stops
    producing audio forever, which is exactly the kind of failure this
    project's own rules say must never be silent."""
    while True:
        _priority, _seq, text, on_start = _speech_queue.get()
        try:
            # Log BEFORE signaling on_start: a caller blocked on on_start
            # (speak_with_cancel_window's listener thread) must never be
            # able to observe "playback started" before the log write for
            # this exact item has already landed -- otherwise a caller
            # that unblocks on on_start and immediately checks say_log.jsonl
            # (test_mute_mode.py does exactly this) can race the write.
            _log_say(text, MUTE)
            if on_start is not None:
                on_start()
            if not MUTE:
                _speak_now(text)
        except Exception as e:  # noqa: BLE001
            log_event("say_feedback_worker_error", error=str(e), text_chars=len(text))
        finally:
            _speech_queue.task_done()


def _speak_now(text: str) -> None:
    """The ONE place audio actually plays. Kokoro is tried first; `say`
    is the fallback ONLY when Kokoro is unavailable (missing model
    files, a load failure) or this specific synthesis call errors --
    never a silent degrade, see the module docstring. Canaries patch
    THIS function (not the subprocess calls inside it, and not
    kokoro_tts directly) so ordering/priority/mute tests stay decoupled
    from which backend actually produced the audio -- see
    speech_queue_canary.py and jarvis_say_canary.py.

    Every call is reported via latency_log (`kokoro_tts_ok` with the
    measured synthesis time, or `kokoro_tts_fallback` with the error) --
    this is the "audible or logged" requirement's LOGGED half; the
    audible half is that a fallback genuinely sounds different (Daniel
    via `say`, not Kokoro's bm_george), which Ayman can hear without
    reading a log at all."""
    t0 = time.monotonic()
    try:
        wav_path = kokoro_tts.synthesize_to_wav(text, voice=KOKORO_VOICE)
    except Exception as e:  # noqa: BLE001 -- kokoro_tts.KokoroUnavailable, or anything else it didn't anticipate
        log_event("kokoro_tts_fallback", error=str(e), text_chars=len(text))
        subprocess.run(["say", "-v", VOICE, text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return

    synth_ms = (time.monotonic() - t0) * 1000
    log_event("kokoro_tts_ok", synth_ms=round(synth_ms, 1), text_chars=len(text), voice=KOKORO_VOICE)
    try:
        subprocess.run(["afplay", str(wav_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    finally:
        wav_path.unlink(missing_ok=True)


def _enqueue(text: str, priority: int, on_start=None) -> None:
    _ensure_worker_started()
    _speech_queue.put((priority, next(_seq_counter), text, on_start))


def speak(text: str, priority: int = PRIORITY_NORMAL) -> None:
    """Enqueues `text` to be spoken, in priority-then-submission order
    relative to every other queued speak()/speak_with_cancel_window()
    call -- never a second concurrent `say` process. Non-blocking:
    returns immediately, same as before this module was queued. Logging
    (including under JARVIS_MUTE) happens when the worker actually
    processes this item, not at call time -- see the module docstring for
    why."""
    _enqueue(text, priority)


def speak_with_cancel_window(text: str, cancel_window_s: float, priority: int = PRIORITY_HIGH) -> dict:
    """Speak `text` (with a "say Hey Jarvis to cancel" hint appended — see
    below) and listen for the cancel trigger IN PARALLEL (not sequentially
    — the window must not be eaten by waiting for speech to finish first,
    which would just add dead time to an already-tight budget). Returns
    {"cancelled": bool, "available": bool}.

    Defaults to PRIORITY_HIGH: this call gates real safety behavior
    (deliver_batch independently still refuses to a non-test target if the
    window wasn't real, but the window itself is the only human-in-the-
    loop control auto mode leaves in place) and should not sit behind a
    backlog of informational speech.

    The listener does not start counting the instant this function is
    called -- it waits for a signal that THIS queued item has reached the
    front of the queue and `say` is actually about to run (or would have,
    under mute), then arms listen_for_cancel(cancel_window_s). Under the
    old unqueued design that signal was implicit (speak() was a bare,
    immediate Popen); now that speech can queue behind other priority-HIGH
    items, arming on enqueue instead of on-actual-start would let the
    window elapse before Ayman could possibly have heard anything.

    The cancel trigger, per the 2026-08-17 ruling, is a re-detection of the
    "Hey Jarvis" wake word during this window — not a separate "cancel"
    keyword (openWakeWord has no pretrained model for one, and a custom one
    would carry an unmeasured false-negative rate on the one control that
    can't afford it). The spoken hint says so explicitly, baked in here
    rather than left to whatever the router happens to put in its summary
    — a prompt telling Ayman to say a word that won't work is worse than
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
        speak(text, priority=priority)
        log_event("cancel_window_closed", cancelled=False, available=True)
        return {"cancelled": False, "available": True}

    result = {}
    started = threading.Event()

    def _listen():
        started.wait()  # arm only once this item actually reaches the front of the queue
        result["cancel_result"] = listen_for_cancel(cancel_window_s)

    listener_thread = threading.Thread(target=_listen, daemon=True)
    listener_thread.start()
    log_event("confirm_spoken", cancel_window_s=cancel_window_s)
    # non-blocking enqueue; on_start=started.set fires from the worker
    # thread the instant this item is dequeued, before the (possibly
    # mute-skipped) `say` call -- that's the moment playback really begins.
    _enqueue(f"{text} Say Hey Jarvis to cancel.", priority, on_start=started.set)
    listener_thread.join()

    cancel_result = result["cancel_result"]
    if not cancel_result.available:
        speak("Cancel unavailable.", priority=PRIORITY_HIGH)
    log_event(
        "cancel_window_closed", cancelled=cancel_result.cancelled, available=cancel_result.available
    )
    return {"cancelled": cancel_result.cancelled, "available": cancel_result.available}
