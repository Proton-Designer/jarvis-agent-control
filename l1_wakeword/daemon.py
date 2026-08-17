#!/usr/bin/env python3
"""The L1+L2 voice-frontend daemon: wake word -> chunked dictation -> handoff.

One process, one mic, one wake-word model, three states:

  IDLE          -- listening for "hey jarvis" at the normal threshold.
                   On detection: -> CAPTURING.
  CAPTURING     -- buffering audio, VAD-chunking it as it arrives, each
                   completed chunk transcribed immediately (whisper-server,
                   warm) with a freshly-rebuilt --prompt from live tmux
                   session names. First ~3s of this state ignores a repeat
                   "hey jarvis" (so the opener "Jarvis, tell shipcheck to..."
                   doesn't immediately end the dictation it just started).
                   A second "hey jarvis" after the guard, OR ~50s of
                   continuous silence (safety net, not the primary
                   mechanism): -> IDLE, transcript assembled and handed to
                   L4 via deliver_transcript().
  CANCEL_ARMED  -- entered only via the listen_for_cancel unix-socket RPC
                   from L4 (~/.jarvis/l1.sock), for the duration of L4's
                   confirm window. Same "hey jarvis" detector, LOWERED
                   threshold: a false positive here just cancels a
                   deliverable that hasn't sent yet (cheap), a false
                   negative during the real cancel window is the failure
                   the whole control exists to prevent (expensive) -- so
                   the asymmetry is deliberately exploited, per the Lead's
                   ruling. No new acoustic model: reusing "hey jarvis" was
                   chosen specifically because it's the one detector with a
                   measured false-positive/negative profile on this
                   project, not an unmeasured custom or third-party model.

Why one process, one mic: the cancel-window RPC and the idle/capturing
loop are mutually exclusive states, not concurrent needs -- CANCEL_ARMED
only happens after a dictation has already been delivered to L3 for
routing, i.e. while this daemon is back in IDLE. Two processes fighting
over the same CoreAudio input device would be a real problem to solve for
no benefit; one state machine sidesteps it entirely.
"""
import argparse
import asyncio
import collections
import json
import re
import signal
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "l2_transcription"))

from listener import load_model, FRAME_SAMPLES as WAKEWORD_FRAME_SAMPLES, SAMPLE_RATE  # noqa: E402
from vad_chunker import SileroVAD, StreamingChunker, FRAME_SAMPLES as VAD_FRAME_SAMPLES  # noqa: E402
from whisper_daemon import WhisperDaemon  # noqa: E402
from session_vocab import build_prompt  # noqa: E402
from hallucination_filter import filter_transcript  # noqa: E402

IDLE_THRESHOLD = 0.5
# Lowered threshold, shared by two states for the same reason: the Lead's
# ruling on the cancel window ("bias toward over-triggering, a false
# positive is cheap, a false negative is the failure being guarded
# against") applies just as much to the STOP half of the dictation toggle.
# Measured directly: a standalone closing "Hey Jarvis." peaked at 0.348 in
# one real test clip -- comfortably below IDLE_THRESHOLD (0.5), meaning the
# stop trigger would have been missed and the dictation would have run to
# the 50s silence safety net instead of ending when Ayman actually stopped
# talking. A false-positive stop just costs "say Hey Jarvis again to
# resume"; a false-negative stop leaves the mic open over whatever comes
# next. Same asymmetry as the cancel window -- same fix.
RECOVERABLE_THRESHOLD = 0.3
# Measured, not assumed: 20 diverse synthetic "ordinary conversation"
# sentences (no wake-word-like phrasing) scored a max of 0.002 against this
# model -- zero false positives anywhere near 0.3. The one real risk
# category found this session ("Hey Travis") scores 0.82-0.99, comfortably
# above 0.5 too, so a lower threshold doesn't meaningfully worsen that risk
# either. Conclusion: no evidence to split the stop threshold higher than
# the cancel threshold, despite stop-FP being a worse failure than
# cancel-FP (truncates a real dictation vs. a free re-dictate) -- the
# asymmetry argument doesn't change the answer when the measured FP rate
# at the lower threshold is already ~zero on realistic content.
GUARD_S = 3.0

# How much of the end of the pending audio buffer to assume is the stop
# wake word's own acoustic content when trimming it out (see
# StreamingChunker.pop_trimming_tail). Sized from every "hey jarvis"
# detection observed this session spanning roughly 0.5-0.6s of
# above-threshold scoring; 1.0s adds margin for the quieter lead-in before
# the score crosses threshold. A fixed heuristic on synthetic timing --
# confirm against real speech before trusting it precisely.
WAKE_WORD_TAIL_TRIM_S = 1.0

# After a dictation ends (either way), ignore wake-word starts for this
# long. Exists because a single spoken "hey jarvis" scores above threshold
# across several consecutive frames (observed spans up to ~0.4s) -- without
# this, the same utterance that just fired the STOP transition can cross
# IDLE_THRESHOLD again on the very next frame and immediately open a new,
# unintended dictation. Found by testing, not anticipated: a no-pause test
# clip showed exactly this -- CAPTURING -> IDLE -> CAPTURING again within
# 40ms of simulated time, on what was acoustically one utterance.
POST_STOP_COOLDOWN_S = 1.5
SILENCE_SAFETY_NET_S = 50.0
SOCKET_PATH = Path.home() / ".jarvis" / "l1.sock"

# --- Start-trigger verification: fails CLOSED, unlike stop/cancel below ---
#
# THE GOVERNING ASYMMETRY OF THIS SYSTEM: start fails closed, stop and
# cancel fail open, because the consequences invert.
#   - A false-negative STOP or CANCEL costs Ayman one repeat of the phrase
#     -- recoverable, so those are biased toward accepting (lowered
#     RECOVERABLE_THRESHOLD, the offset ensemble maximizing detection odds).
#   - A false-ACCEPT on START opens the microphone and transcribes whatever
#     is said in the room next. That is not recoverable the same way, and
#     it is not hypothetical: measured directly, ordinary two-syllable
#     names ("Hey Charles" 0.998, "Hey Travis" 0.983) score
#     indistinguishably from a genuine "Hey Jarvis" (0.999, same voice,
#     same batch) on the acoustic model alone. No threshold separates
#     them -- 0.998 is not marginal. So START gets a second, independent
#     check that isn't fooled by the same failure mode.
#
# The acoustic wake-word model scores PROSODIC SHAPE (which is exactly
# what "Hey Charles" and "Hey Jarvis" share). Whisper transcribes PHONETIC
# CONTENT (where "Charles" and "Jarvis" are nothing alike). The two
# failure modes are uncorrelated, which is what makes stacking them
# effective rather than redundant -- a phrase that fools the acoustic
# model on shape has no particular reason to also fool Whisper on content.
#
# Cost: ~0.5s (measured whisper-server warm latency) added once, at the
# very start of a dictation that then runs for minutes. Invisible in
# context; L2 latency was already established as far below the noise
# floor for this workload.
VERIFY_BUFFER_S = 2.0  # rolling pre-roll + trigger-window buffer sent to Whisper for verification
VERIFY_BUFFER_FRAMES = int(VERIFY_BUFFER_S * SAMPLE_RATE / VAD_FRAME_SAMPLES)

# Be permissive about spelling -- Whisper may render the word "Jarvis",
# "jarvis,", "Jervis" (a real, phonetically plausible mishearing), etc.
# Reject anything that doesn't recognizably contain the word, including an
# empty or unclear transcript -- fail closed means ambiguous also rejects.
_JARVIS_TRANSCRIPT_RE = re.compile(r"jarvis|jervis", re.IGNORECASE)


def verify_wake_trigger(whisper: WhisperDaemon, preroll_frames: "collections.deque", tmp_wav_path: Path) -> tuple[bool, str]:
    """Second-stage check on a wake-word trigger before committing to
    CAPTURING. Returns (accepted, transcript) -- transcript is logged by
    the caller regardless of outcome so the false-rejection rate is
    measurable, not assumed."""
    if not preroll_frames:
        return False, ""
    audio = np.concatenate(list(preroll_frames))
    _write_wav(tmp_wav_path, audio)
    transcript = whisper.transcribe(str(tmp_wav_path), prompt="Jarvis")
    accepted = bool(_JARVIS_TRANSCRIPT_RE.search(transcript))
    return accepted, transcript


def default_deliver(text: str, orchestrator_target: str | None = None):
    """Production hookup -- imports L4's real handoff. Only called by
    --simulate when --live-deliver is explicitly passed (see below); the
    default is still print-only, so an ordinary test run never fires a
    real `tmux send-keys` by accident. --live-deliver exists specifically
    for the coordinated end-to-end pipeline test with L4/L3, not for
    routine testing."""
    sys.path.insert(0, str(Path(__file__).parent.parent / "l4_controller"))
    from l2_l3_handoff import deliver_transcript, DEFAULT_ORCHESTRATOR_TARGET  # noqa
    from transport import TmuxTransport  # noqa
    target = orchestrator_target or DEFAULT_ORCHESTRATOR_TARGET
    return deliver_transcript(text, TmuxTransport(), orchestrator_target=target)


class DictationSession:
    """Owns the state for one CAPTURING episode: rolling VAD chunker +
    accumulated transcript text."""

    def __init__(self, vad: SileroVAD, whisper: WhisperDaemon, started_at: float):
        """started_at is caller-supplied, not sampled internally via
        time.monotonic() -- this class must work identically whether "now"
        is wall-clock time (live mic) or simulated audio position
        (--simulate, which processes 10s of audio in a fraction of a real
        second, so wall-clock guard timing would never actually expire
        during a simulated run). Caught by testing, not by inspection --
        an earlier version used time.monotonic() internally and the guard
        silently never lifted in --simulate mode."""
        self.whisper = whisper
        self.started_at = started_at
        self.chunks_transcribed: list[str] = []
        self.chunk_log: list[dict] = []  # one record per Whisper call -- see _transcribe_and_append
        self._chunker = StreamingChunker(vad)
        self._total_samples_fed = 0

    def in_guard(self, now: float) -> bool:
        return (now - self.started_at) < GUARD_S

    def silence_exceeds_safety_net(self) -> bool:
        return self._chunker.silence_duration_s >= SILENCE_SAFETY_NET_S

    def _transcribe_and_append(self, audio: np.ndarray, wav_tmp_path: Path, source: str) -> str | None:
        """The one place a chunk's audio becomes text in the assembled
        transcript. Every caller (feed/flush_final/finalize_on_stop_word)
        routes through here so the hallucination filter is a structural
        guarantee on the transcript-assembly path, not a convention each
        call site has to remember to apply -- same reasoning as
        StreamingChunker's VAD-gate invariant. The VAD speech-content gate
        (vad_chunker.py) is the first layer and catches most of this
        before Whisper is even called; this is the second layer, for
        chunks that passed the VAD gate but still produced a
        confabulation (background noise VAD classified as speech, etc.).

        Also the one place a chunk_log record gets written -- audio
        position (from the running sample counter, not wall-clock, so it
        means the same thing in --simulate and live), the exact --prompt
        used, and both the raw and post-filter transcript. Built for the
        long-dictation prompt-bias-decay test: L4 needs to correlate a
        routing outcome back to the specific chunk that produced it, and
        that requires knowing which prompt was live for that chunk, not
        just the assembled transcript."""
        end_sec = self._total_samples_fed / SAMPLE_RATE
        start_sec = end_sec - (len(audio) / SAMPLE_RATE)
        prompt_used = build_prompt()
        _write_wav(wav_tmp_path, audio)
        raw_text = self.whisper.transcribe(str(wav_tmp_path), prompt=prompt_used)
        text = filter_transcript(raw_text, source=source)
        self.chunk_log.append({
            "chunk_index": len(self.chunk_log),
            "source": source,
            "start_sec": round(start_sec, 2),
            "end_sec": round(end_sec, 2),
            "prompt_used": prompt_used,
            "raw_transcript": raw_text,
            "kept_transcript": text,  # None if the hallucination filter dropped it
            "wall_clock": time.time(),
        })
        if text is None:
            return None
        self.chunks_transcribed.append(text)
        return text

    def feed(self, frame_i16: np.ndarray, wav_tmp_path: Path) -> str | None:
        """Push exactly one frame into the chunker (the VAD advances exactly
        once per frame, ever -- see StreamingChunker's docstring for why
        that invariant matters). Transcribes and returns text the moment a
        chunk boundary is found; otherwise returns None."""
        self._total_samples_fed += len(frame_i16)
        audio = self._chunker.push(frame_i16)
        if audio is None:
            return None
        return self._transcribe_and_append(audio, wav_tmp_path, source="chunk")

    def flush_final(self, wav_tmp_path: Path):
        """Called after the dictation ends normally (silence safety net) --
        transcribes whatever's still buffered, since there's no wake word
        to strip it out."""
        audio = self._chunker.flush()
        if audio is not None and len(audio) > 0:
            self._transcribe_and_append(audio, wav_tmp_path, source="final-flush")

    def finalize_on_stop_word(self, wav_tmp_path: Path):
        """Called when the dictation ends via the stop wake word. The still-
        buffered audio at that moment is the "hey jarvis" stop phrase, plus
        possibly real content before it if Ayman said it with no pause --
        the VAD only cuts on a pause, so with no pause there's nothing
        marking where real content ends and the stop phrase begins except
        roughly how long "hey jarvis" itself takes to say.

        Originally this just discarded the whole pending buffer -- correct
        for the pause-before-stop case (tested, and it's what stopped a
        literal "Hey Jarvis" showing up verbatim at the end of a
        transcript), **wrong** for the no-pause case: tested directly and
        it silently dropped an entire multi-instruction dictation
        ("...also tell Ship Check to redeploy, Hey Jarvis" with no pause
        anywhere) because the whole thing was still one undischarged
        chunk. Now trims a fixed tail instead of discarding everything --
        see StreamingChunker.pop_trimming_tail for the heuristic and its
        limits.
        """
        audio = self._chunker.pop_trimming_tail(WAKE_WORD_TAIL_TRIM_S)
        if audio is not None:
            self._transcribe_and_append(audio, wav_tmp_path, source="trimmed-tail")

    def full_transcript(self) -> str:
        return " ".join(t for t in self.chunks_transcribed if t)


def _write_wav(path: Path, pcm_i16: np.ndarray):
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm_i16.tobytes())


def _write_chunk_log(chunk_log: list[dict]) -> Path:
    """Sidecar JSON for correlating a routing outcome back to the exact
    chunk + prompt that produced it, per L4's ask -- their own timestamp
    (from write_dictation()) isn't visible from here, so this uses its own
    timestamp rather than trying to guess theirs; reported explicitly
    alongside the dictation file path instead of relying on them matching."""
    out_dir = Path.home() / ".jarvis" / "dictations"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{time.strftime('%Y%m%dT%H%M%S')}.chunks.json"
    path.write_text(json.dumps(chunk_log, indent=2))
    return path


def _report_and_deliver(text: str, chunk_log: list[dict], live_deliver: bool, orchestrator_target: str | None, stop_wall_time: float):
    print(f"FULL TRANSCRIPT: {text!r}")
    chunk_log_path = _write_chunk_log(chunk_log)
    print(f"chunk log ({len(chunk_log)} chunks): {chunk_log_path}")
    if not live_deliver:
        print("(simulate mode: not calling deliver_transcript -- would send to L4 here; pass --live-deliver to actually send)")
        return
    result = default_deliver(text, orchestrator_target=orchestrator_target)
    handoff_wall_time = time.time()
    print(f"DELIVERED via deliver_transcript: {result!r}")
    print(f"stop-word-to-handoff-return wall-clock: {handoff_wall_time - stop_wall_time:.3f}s")


def simulate(dictation_wav: str, whisper: WhisperDaemon, live_deliver: bool = False, orchestrator_target: str | None = None):
    """Drives the IDLE -> CAPTURING -> IDLE state machine over a pre-recorded
    file, as if it arrived from a live mic frame-by-frame. Print-only by
    default -- live_deliver=True is an explicit opt-in for the coordinated
    end-to-end pipeline test, not the default for routine testing, so an
    ordinary run never touches a real tmux session by accident."""
    wake_model = load_model()
    vad = SileroVAD()
    tmp_wav = Path("/tmp/jarvis_daemon_chunk.wav")

    convert_hint = (
        f"  ffmpeg -i {dictation_wav} -ar {SAMPLE_RATE} -ac 1 -c:a pcm_s16le fixed.wav\n"
        f"then run again with fixed.wav. Needs {SAMPLE_RATE}Hz mono 16-bit WAV -- "
        f"Voice Memos/QuickTime recordings (.m4a, .caf, .mov) always need this conversion."
    )
    try:
        wf = wave.open(dictation_wav, "rb")
    except wave.Error:
        raise SystemExit(f"{dictation_wav}: not a WAV file (or not a format Python's wave module reads). Convert it:\n{convert_hint}")
    with wf:
        if wf.getframerate() != SAMPLE_RATE or wf.getnchannels() != 1:
            raise SystemExit(
                f"{dictation_wav}: needs to be {SAMPLE_RATE}Hz mono, got "
                f"{wf.getframerate()}Hz {wf.getnchannels()}ch. Convert first:\n{convert_hint}"
            )
        pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)

    state = "IDLE"
    session: DictationSession | None = None
    cooldown_until: float | None = None  # see POST_STOP_COOLDOWN_S
    preroll: collections.deque = collections.deque(maxlen=VERIFY_BUFFER_FRAMES)
    # Drive the loop at the VAD's (smaller) native frame size, not the wake-word
    # model's. openWakeWord's predict() explicitly supports non-80ms input --
    # it accumulates internally, at the cost of up to one extra 80ms of
    # detection latency -- so feeding it VAD-sized frames is correct, and lets
    # both detectors share one frame source instead of needing two independent
    # batchers for two frame sizes that don't evenly divide each other
    # (1280 samples for wake-word vs 512 for VAD -- neither is a multiple of
    # the other).
    n_frames = len(pcm) // VAD_FRAME_SAMPLES

    for i in range(n_frames):
        frame = pcm[i * VAD_FRAME_SAMPLES : (i + 1) * VAD_FRAME_SAMPLES]
        t = i * VAD_FRAME_SAMPLES / SAMPLE_RATE
        score = wake_model.predict(frame)["hey_jarvis_v0.1"]
        start_fired = score >= IDLE_THRESHOLD
        stop_fired = score >= RECOVERABLE_THRESHOLD  # deliberately lower, see RECOVERABLE_THRESHOLD
        preroll.append(frame)

        if state == "IDLE":
            in_cooldown = cooldown_until is not None and t < cooldown_until
            if start_fired and not in_cooldown:
                accepted, verify_text = verify_wake_trigger(whisper, preroll, tmp_wav)
                if accepted:
                    print(f"[{t:.2f}s] wake word (score={score:.3f}) verified ({verify_text!r}) -> CAPTURING")
                    state = "CAPTURING"
                    session = DictationSession(vad, whisper, started_at=t)
                    preroll.clear()
                else:
                    print(f"[{t:.2f}s] wake word (score={score:.3f}) REJECTED by verification (heard: {verify_text!r}) -- staying IDLE")
            elif start_fired and in_cooldown:
                print(f"[{t:.2f}s] wake word ignored -- inside post-stop cooldown (score={score:.3f})")
        elif state == "CAPTURING":
            if not session.in_guard(now=t) and stop_fired:
                stop_wall_time = time.time()  # real wall-clock: whisper/deliver calls take real time even in --simulate
                print(f"[{t:.2f}s] wake word (post-guard, score={score:.3f}) -> finalize (stop detected at wall-clock {stop_wall_time:.3f})")
                session.finalize_on_stop_word(tmp_wav)  # trims the stop phrase's own audio, keeps real content
                text = session.full_transcript()
                _report_and_deliver(text, session.chunk_log, live_deliver, orchestrator_target, stop_wall_time)
                state = "IDLE"
                session = None
                cooldown_until = t + POST_STOP_COOLDOWN_S
            elif session.silence_exceeds_safety_net():
                stop_wall_time = time.time()
                print(f"[{t:.2f}s] {SILENCE_SAFETY_NET_S}s silence safety net -> finalize (no stop word heard)")
                session.flush_final(tmp_wav)  # no stop word to strip -- transcribe everything buffered
                text = session.full_transcript()
                _report_and_deliver(text, session.chunk_log, live_deliver, orchestrator_target, stop_wall_time)
                state = "IDLE"
                session = None
                cooldown_until = None  # no wake word involved in this ending, nothing to cool down from
            else:
                partial = session.feed(frame, tmp_wav)
                if partial:
                    print(f"[{t:.2f}s] chunk transcribed: {partial!r}")

    if state == "CAPTURING" and session:
        session.flush_final(tmp_wav)
        text = session.full_transcript()
        print(f"[end of file] FULL TRANSCRIPT (file ended mid-dictation, no stop word or safety-net timeout reached): {text!r}")
        _report_and_deliver(text, session.chunk_log, live_deliver, orchestrator_target, time.time())

    tmp_wav.unlink(missing_ok=True)


# Multi-offset ensemble for the cancel window: run detector instances with
# frame grids staggered by a fraction of the 512-sample hop each, take the
# max score across them. Exists because marginal wake-word scores are
# highly sensitive to where the 512-sample grid happens to fall relative to
# where the phrase starts -- measured directly: the identical closing "hey
# jarvis" phrase from the long-dictation test scored 0.323 at one frame
# offset and 0.168 at another, a ~2x swing from a 24ms shift, nothing to do
# with the audio itself. Nobody controls that phase in a live system (it's
# set by when mic capture began, minutes earlier), so for any weak
# utterance, clearing threshold is substantially alignment luck on a single
# detector. Three staggered instances, take the max, and that luck mostly
# disappears -- measured: ensemble max recovered the 0.323 case (would have
# fired) instead of depending on whichever single offset happened to be
# running.
CANCEL_ENSEMBLE_OFFSETS = [0, 171, 341]  # samples; ~0, ~1/3, ~2/3 of the 512-sample hop

# Fresh detector instances per cancel-window arm, not the long-running
# IDLE/CAPTURING one. NOT because of "cumulative stream duration" -- that
# hypothesis was tested directly (a curve across 0-283s of prior audio, with
# frame phase held constant) and it doesn't hold: scores oscillate wildly
# with no relationship to elapsed time. The real driver, confirmed by
# testing 1-10s of LOCAL context alone with a fresh detector each time and
# reproducing the same wild variance: what matters is the audio in roughly
# the last 1-3 seconds before the phrase, not how long the stream has run.
# Silence there scores highest (0.94-0.97 measured); speech there is
# variable, sometimes badly suppressive. Fresh-per-arm is still the
# reasonable choice -- it's what every "TTS plan summary + interrupt"
# scenario below was tested against, cleanly (0.999-1.000 across 9 splice
# points including mid-TTS interrupts with zero gap) -- just not for the
# "duration" reason originally assumed.


def fresh_ensemble():
    """One fresh Model() per offset, per cancel-window arm -- never the
    long-running IDLE/CAPTURING detector. See CANCEL_ENSEMBLE_OFFSETS'
    comment for why fresh-per-arm, not age-based rotation."""
    ensemble = []
    for offset in CANCEL_ENSEMBLE_OFFSETS:
        ensemble.append({
            "model": load_model(),
            "lead_in_remaining": offset,  # samples of silence to feed before real audio, to fix this instance's phase
        })
    return ensemble


def score_frame_ensemble(ensemble, frame_i16: np.ndarray) -> float:
    """Feed one real frame to every ensemble member (each still working
    through its own phase lead-in first), return the max score seen this
    call. Call once per incoming live-mic frame while a cancel window is
    armed. Unit-tested without a mic -- see the long-dictation writeup for
    the numbers (recovered the 0.323 case that a single offset could have
    missed at 0.168)."""
    best = 0.0
    for member in ensemble:
        if member["lead_in_remaining"] > 0:
            pad = min(member["lead_in_remaining"], VAD_FRAME_SAMPLES)
            member["model"].predict(np.zeros(pad, dtype=np.int16))
            member["lead_in_remaining"] -= pad
            continue
        score = member["model"].predict(frame_i16)["hey_jarvis_v0.1"]
        best = max(best, score)
    return best


class LiveController:
    """Owns the IDLE/CAPTURING/CANCEL_ARMED state machine for live-mic
    operation. One instance, fed one frame at a time by the sounddevice
    callback (via an asyncio.Queue -- the callback itself runs on
    PortAudio's own thread, not the event loop, so it can't touch asyncio
    objects directly; it just hands frames off).

    This mirrors --simulate's state machine (same transition logic, same
    DictationSession) but driven by live wall-clock time and a live frame
    source instead of iterating a pre-loaded array -- the two entrypoints
    intentionally share DictationSession/StreamingChunker/WhisperDaemon
    rather than duplicating that logic, so everything --simulate already
    validated (chunking, hallucination filtering, stop-word trimming,
    cooldown) applies unchanged here. What's new here is only the frame
    source and the CANCEL_ARMED branch --simulate never exercised.
    """

    def __init__(self, whisper: WhisperDaemon, live_deliver: bool, orchestrator_target: str | None):
        self.whisper = whisper
        self.live_deliver = live_deliver
        self.orchestrator_target = orchestrator_target
        self.wake_model = load_model()
        self.vad = SileroVAD()
        self.tmp_wav = Path("/tmp/jarvis_daemon_chunk.wav")
        self.state = "IDLE"
        self.session: DictationSession | None = None
        self.cooldown_until: float | None = None
        self.preroll: collections.deque = collections.deque(maxlen=VERIFY_BUFFER_FRAMES)
        # CANCEL_ARMED fields, set by arm_cancel(), read/cleared by on_frame()
        self._cancel_ensemble = None
        self._cancel_deadline = None
        self._cancel_detected = False

    def arm_cancel(self, timeout_s: float) -> bool:
        """Called from the cancel-socket handler. Returns False (and does
        nothing) if not currently IDLE -- per the exclusive-claim invariant,
        a cancel window only ever opens after a dictation has already been
        handed off, so this should never actually be called from CAPTURING
        in practice; refusing rather than pre-empting is the safe failure
        mode if it somehow is."""
        if self.state != "IDLE":
            return False
        self.state = "CANCEL_ARMED"
        self._cancel_ensemble = fresh_ensemble()
        self._cancel_deadline = time.time() + timeout_s
        self._cancel_detected = False
        print(f"[{time.strftime('%H:%M:%S')}] CANCEL_ARMED for {timeout_s}s")
        return True

    def on_frame(self, frame_i16: np.ndarray, now: float):
        if self.state == "CANCEL_ARMED":
            self._on_cancel_frame(frame_i16, now)
            return
        score = self.wake_model.predict(frame_i16)["hey_jarvis_v0.1"]
        start_fired = score >= IDLE_THRESHOLD
        stop_fired = score >= RECOVERABLE_THRESHOLD
        self.preroll.append(frame_i16)

        if self.state == "IDLE":
            in_cooldown = self.cooldown_until is not None and now < self.cooldown_until
            if start_fired and not in_cooldown:
                # Blocking whisper call here stalls frame_queue consumption for
                # ~0.5s -- acceptable and deliberate: nothing else needs the
                # event loop during that window (verification only runs at the
                # start of a new dictation, never mid-capture or mid-cancel),
                # and it keeps the fail-closed check simple rather than adding
                # executor/threading complexity for a rare, non-latency-
                # sensitive event.
                accepted, verify_text = verify_wake_trigger(self.whisper, self.preroll, self.tmp_wav)
                if accepted:
                    print(f"[{time.strftime('%H:%M:%S')}] wake word (score={score:.3f}) verified ({verify_text!r}) -> CAPTURING")
                    self.state = "CAPTURING"
                    self.session = DictationSession(self.vad, self.whisper, started_at=now)
                    self.preroll.clear()
                else:
                    print(f"[{time.strftime('%H:%M:%S')}] wake word (score={score:.3f}) REJECTED by verification (heard: {verify_text!r}) -- staying IDLE")
            elif start_fired and in_cooldown:
                print(f"[{time.strftime('%H:%M:%S')}] wake word ignored -- inside post-stop cooldown (score={score:.3f})")
        elif self.state == "CAPTURING":
            if not self.session.in_guard(now) and stop_fired:
                stop_wall_time = time.time()
                print(f"[{time.strftime('%H:%M:%S')}] wake word (post-guard, score={score:.3f}) -> finalize")
                self.session.finalize_on_stop_word(self.tmp_wav)
                text = self.session.full_transcript()
                _report_and_deliver(text, self.session.chunk_log, self.live_deliver, self.orchestrator_target, stop_wall_time)
                self.state = "IDLE"
                self.session = None
                self.cooldown_until = now + POST_STOP_COOLDOWN_S
            elif self.session.silence_exceeds_safety_net():
                stop_wall_time = time.time()
                print(f"[{time.strftime('%H:%M:%S')}] {SILENCE_SAFETY_NET_S}s silence safety net -> finalize")
                self.session.flush_final(self.tmp_wav)
                text = self.session.full_transcript()
                _report_and_deliver(text, self.session.chunk_log, self.live_deliver, self.orchestrator_target, stop_wall_time)
                self.state = "IDLE"
                self.session = None
                self.cooldown_until = None
            else:
                partial = self.session.feed(frame_i16, self.tmp_wav)
                if partial:
                    print(f"[{time.strftime('%H:%M:%S')}] chunk transcribed: {partial!r}")

    def _on_cancel_frame(self, frame_i16: np.ndarray, now: float):
        if now >= self._cancel_deadline:
            print(f"[{time.strftime('%H:%M:%S')}] cancel window closed, not detected -> IDLE")
            self.state = "IDLE"
            return
        score = score_frame_ensemble(self._cancel_ensemble, frame_i16)
        if score >= RECOVERABLE_THRESHOLD:
            print(f"[{time.strftime('%H:%M:%S')}] CANCEL detected (ensemble score={score:.3f}) -> IDLE")
            self._cancel_detected = True
            self.state = "IDLE"


async def cancel_socket_server(controller: LiveController):
    """Handles L4's listen_for_cancel RPC by arming `controller`'s cancel
    window and polling for the result -- the actual detection happens in
    LiveController.on_frame(), fed by the same live audio stream as the
    main loop, per the exclusive-claim design."""
    SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
    SOCKET_PATH.unlink(missing_ok=True)

    async def handle(reader, writer):
        data = await reader.read()
        try:
            req = json.loads(data.decode())
            timeout_s = float(req.get("timeout_s", 2.5))
        except (json.JSONDecodeError, TypeError, ValueError):
            writer.write(json.dumps({"cancelled": False}).encode())
            await writer.drain()
            writer.close()
            return

        armed = controller.arm_cancel(timeout_s)
        if not armed:
            # Not IDLE when the request arrived -- shouldn't happen per the
            # exclusive-claim invariant, but fail closed (available=False on
            # the client side, per cancel_listener.py) rather than silently
            # report "not cancelled" as if the window had genuinely run.
            writer.close()
            return

        deadline = time.time() + timeout_s + 0.5  # small buffer over the controller's own deadline
        while time.time() < deadline and controller.state == "CANCEL_ARMED":
            await asyncio.sleep(0.02)
        writer.write(json.dumps({"cancelled": controller._cancel_detected}).encode())
        await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(handle, path=str(SOCKET_PATH))
    async with server:
        await server.serve_forever()


async def live(whisper: WhisperDaemon, live_deliver: bool = False, orchestrator_target: str | None = None):
    """Real microphone capture, driving the same state machine --simulate
    validated. Prints every state transition and wake-word score above a
    noise floor, per the requirement that first-run confidence comes from
    seeing it react, not from trusting it silently.
    """
    import sounddevice as sd

    controller = LiveController(whisper, live_deliver, orchestrator_target)
    loop = asyncio.get_running_loop()
    frame_queue: asyncio.Queue = asyncio.Queue()

    def audio_callback(indata, frames, time_info, status):
        if status:
            print(f"[audio] status: {status}", file=sys.stderr)
        pcm16 = (indata[:, 0] * 32767).astype(np.int16)
        loop.call_soon_threadsafe(frame_queue.put_nowait, pcm16)

    print(f"[{time.strftime('%H:%M:%S')}] opening microphone ({SAMPLE_RATE}Hz mono, {VAD_FRAME_SAMPLES}-sample frames)")
    stream = sd.InputStream(
        channels=1, samplerate=SAMPLE_RATE, blocksize=VAD_FRAME_SAMPLES, dtype="float32", callback=audio_callback,
    )
    with stream:
        print(f"[{time.strftime('%H:%M:%S')}] listening for \"hey jarvis\" -- Ctrl-C to stop")
        cancel_task = asyncio.create_task(cancel_socket_server(controller))
        try:
            while True:
                frame = await frame_queue.get()
                controller.on_frame(frame, time.time())
        finally:
            cancel_task.cancel()


def _install_clean_shutdown_handler():
    """A plain `kill`/Ctrl-C must actually stop this and stay stopped --
    see com.jarvis.l1wakeword.plist's KeepAlive comment for why (a
    microphone listener that resurrects itself after being deliberately
    killed is a trust problem, not just a reliability one). Exiting 0
    here is what makes launchd's SuccessfulExit:false treat this as a
    clean stop rather than a crash to restart from. sys.exit() raises
    SystemExit, which still unwinds the `with WhisperDaemon(...)` block
    below normally, so whisper-server gets torn down too."""
    def handle(signum, frame):
        print(f"\nreceived signal {signum}, shutting down cleanly", file=sys.stderr)
        sys.exit(0)
    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)


if __name__ == "__main__":
    # Without this, stdout is fully buffered whenever it's not a TTY (e.g.
    # redirected to a log file, or piped) -- print() calls sit unflushed
    # until the buffer fills or the process exits. Found by testing: a full
    # run's worth of state-transition output appeared all at once, only on
    # clean shutdown, instead of live. That's the opposite of the "confidence
    # from watching it react in real time" requirement for Ayman's first run.
    sys.stdout.reconfigure(line_buffering=True)
    _install_clean_shutdown_handler()
    ap = argparse.ArgumentParser()
    ap.add_argument("--simulate", help="drive the state machine over a pre-recorded wav instead of live mic")
    ap.add_argument("--model", default=None, help="whisper.cpp model path override")
    ap.add_argument(
        "--live-deliver", action="store_true",
        help="actually call deliver_transcript() at the end of the dictation (real tmux send-keys into the "
             "orchestrator pane) instead of just printing. For the coordinated end-to-end pipeline test only "
             "-- never the default, so routine testing can't touch a real session by accident.",
    )
    ap.add_argument("--target", default=None, help="orchestrator tmux session name (only used with --live-deliver)")
    args = ap.parse_args()

    kwargs = {"model_path": Path(args.model)} if args.model else {}
    with WhisperDaemon(**kwargs) as whisper:
        if args.simulate:
            simulate(args.simulate, whisper, live_deliver=args.live_deliver, orchestrator_target=args.target)
        else:
            asyncio.run(live(whisper, live_deliver=args.live_deliver, orchestrator_target=args.target))
