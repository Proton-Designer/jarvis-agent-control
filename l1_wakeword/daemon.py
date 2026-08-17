#!/usr/bin/env python3
"""The L1+L2 voice-frontend daemon: wake word -> chunked dictation -> handoff.

One process, one mic, one wake-word model, three states:

  IDLE          -- listening for "hey jarvis" at the normal threshold.
                   On detection: -> CAPTURING.
  CAPTURING     -- buffering audio, VAD-chunking it as it arrives, each
                   completed chunk transcribed immediately (whisper-server,
                   warm) with a freshly-rebuilt --prompt from live tmux
                   session names. Ends when a chunk that closed on silence
                   (not the 30s hard cap) has a transcript ending in the
                   stop phrase ("that's it", see STOP_PHRASE_VARIANTS) --
                   matched on TEXT, not acoustics, so none of the wake-word
                   fragility (marginal scores, frame-alignment luck,
                   acoustic name collisions) applies to stopping a
                   dictation. "Hey jarvis" said again mid-dictation does
                   nothing special here -- it's just more content, unless it
                   happens to also end with the stop phrase. ~50s of
                   continuous silence (safety net, not the primary
                   mechanism) also ends it: -> IDLE, transcript assembled
                   and handed to L4 via deliver_transcript().
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
import difflib
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
# Cancel-window-only now (see CANCEL_ARMED below). Used to gate the STOP
# transition too until this session's stop-phrase rework: the acoustic
# "hey jarvis" stop trigger had the same marginal-score/alignment problems
# documented for cancel (a standalone closing "Hey Jarvis." measured 0.348,
# below IDLE_THRESHOLD -- would have missed) -- moot now, stop is matched
# on Whisper's transcript text, not a second acoustic score. Left in place,
# unchanged, because the cancel window still needs it and inherits the
# same "false accept is cheap, false reject is expensive" reasoning: the
# Lead's ruling on the cancel window ("bias toward over-triggering") still
# applies there.
RECOVERABLE_THRESHOLD = 0.3
# Measured, not assumed: 20 diverse synthetic "ordinary conversation"
# sentences (no wake-word-like phrasing) scored a max of 0.002 against this
# model -- zero false positives anywhere near 0.3 for the cancel window.
SILENCE_SAFETY_NET_S = 50.0
SOCKET_PATH = Path.home() / ".jarvis" / "l1.sock"

# --- Stop phrase: matched on TRANSCRIPT TEXT, not acoustics ---
#
# Ayman ends a dictation by pausing and saying "that's it" instead of a
# second "hey jarvis". Deliberately NOT a second acoustic wake-word model:
# every acoustic problem fought this session (marginal-score suppression,
# frame-alignment luck needing a 3-offset ensemble, "Hey Charles"/"Hey
# Travis" scoring indistinguishably from "Hey Jarvis") is a property of
# scoring PROSODIC SHAPE against a small trained model -- none of it
# applies to checking whether Whisper's own transcript, which we already
# produce for every chunk, ends with a specific short phrase.
#
# Matched only against a chunk that closed because the StreamingChunker
# detected a pause (last_cut_reason == "silence"), never a chunk that hit
# the 30s hard cap (last_cut_reason == "maxlen") -- a maxlen cut can land
# anywhere, including mid-sentence, so "ends with the stop phrase" would be
# coincidence, not Ayman actually stopping. And matched only at the END of
# the chunk's transcript: "that's it for the gateway, now mobile..." does
# not match, and is inherently safe regardless -- more speech in the same
# chunk means the VAD never saw the pause that would let this fire early.
STOP_PHRASE_VARIANTS = {"that's it", "thats it", "that is it"}


def _normalize_for_stop_match(text: str) -> str:
    """lowercase, drop punctuation (keep apostrophes so "that's" survives
    intact), collapse whitespace. For matching only -- the transcript kept
    in DictationSession retains its original punctuation/casing."""
    text = text.lower()
    text = re.sub(r"[^\w\s']", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def match_stop_phrase(text: str) -> tuple[bool, str]:
    """Does `text` END with an accepted stop-phrase variant? Returns
    (matched, remainder) -- remainder is `text` with the matched trailing
    words removed, word-count-based on the ORIGINAL text (not the
    normalized one) so the kept remainder keeps its real punctuation and
    casing. Not byte-precise -- good enough for a dictation transcript
    with "that's it" stripped off the end, not a general-purpose parser.
    Every match (and the raw transcript it matched against) gets logged by
    the caller into chunk_log, per the Lead's ask, so which spoken/rendered
    forms actually occur is something we learn from data, not guesswork."""
    normalized = _normalize_for_stop_match(text)
    for variant in STOP_PHRASE_VARIANTS:
        if normalized == variant:
            return True, ""
        if normalized.endswith(" " + variant):
            n_words_to_strip = len(variant.split())
            words = text.split()
            remainder = " ".join(words[:-n_words_to_strip]).strip()
            return True, remainder
    return False, text


# How close a silence-closed chunk's trailing words need to be to an
# accepted variant to flag as a NEAR-miss in the chunk log -- diagnostic
# only, this never gates the actual stop transition or changes control
# flow. Exists for the real risk the Lead flagged: Ayman's actual voice
# could render "that's it" outside STOP_PHRASE_VARIANTS (a trailing
# "...that's it, thanks", a run-together "thassit") and the failure mode
# for that would otherwise be silent -- "it didn't stop, no idea why."
# This surfaces it in ~/.jarvis/dictations/*.chunks.json immediately
# instead. Threshold picked to catch obviously-close variants without
# flagging every unrelated silence-closed chunk; not empirically tuned --
# no real near-miss data exists yet, expect to revisit once it does.
STOP_PHRASE_NEAR_MISS_SIMILARITY = 0.55
_NEAR_MISS_WINDOW_WORDS = max(len(v.split()) for v in STOP_PHRASE_VARIANTS) + 2


def stop_phrase_near_miss(text: str) -> bool:
    """Diagnostic only -- call after match_stop_phrase() already returned
    False. Two distinct near-miss shapes, both real risks on real speech:

    1. The variant appears verbatim but NOT at the very end (e.g. Ayman
       adds a trailing word Whisper picks up too -- "...that's it,
       thanks."). Caught by plain substring containment in a wider
       trailing window; a ratio comparison against the same wide window
       would dilute below threshold on the extra words and miss this.
    2. The variant doesn't appear verbatim anywhere -- Whisper mangled it
       (a run-together "thassit"). Caught by a fuzzy ratio against a
       window sized to the variant itself, not the wider window, so extra
       trailing words can't dilute this one either."""
    normalized = _normalize_for_stop_match(text)
    words = normalized.split()
    if not words:
        return False
    wide_tail = " ".join(words[-_NEAR_MISS_WINDOW_WORDS:])
    for variant in STOP_PHRASE_VARIANTS:
        if variant in wide_tail:
            return True
        narrow_tail = " ".join(words[-len(variant.split()):])
        if difflib.SequenceMatcher(None, narrow_tail, variant).ratio() >= STOP_PHRASE_NEAR_MISS_SIMILARITY:
            return True
    return False


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


def default_deliver(text: str, orchestrator_target: str | None = None, live_deliver: bool = False):
    """Production hookup -- routes every finished dictation through the
    L2.5 concierge (classify -> answer locally, or forward unchanged)
    instead of calling deliver_transcript directly. `live_deliver` only
    gates the concierge's OWN forwarding decision for a DISPATCH/UNSURE
    classification -- CONTROL/QUERY/CHAT get classified and answered
    locally regardless, since answering locally never touches a real
    orchestrator session (it can still speak, gated separately by
    JARVIS_MUTE, same as everything else that calls say_feedback.speak).
    See l2_5_concierge/concierge.py's own --live-deliver flag and the
    incident that motivated it: a smoke test without this flag delivered
    fake test text into a real live orchestrator session by accident."""
    sys.path.insert(0, str(Path(__file__).parent.parent / "l2_5_concierge"))
    from concierge import handle_transcript, DEFAULT_ORCHESTRATOR_TARGET  # noqa
    target = orchestrator_target or DEFAULT_ORCHESTRATOR_TARGET
    return handle_transcript(text, orchestrator_target=target, live_deliver=live_deliver)


class DictationSession:
    """Owns the state for one CAPTURING episode: rolling VAD chunker +
    accumulated transcript text."""

    def __init__(self, vad: SileroVAD, whisper: WhisperDaemon):
        self.whisper = whisper
        self.chunks_transcribed: list[str] = []
        self.chunk_log: list[dict] = []  # one record per Whisper call -- see _transcribe_and_append
        self._chunker = StreamingChunker(vad)
        self._total_samples_fed = 0

    def silence_exceeds_safety_net(self) -> bool:
        return self._chunker.silence_duration_s >= SILENCE_SAFETY_NET_S

    @property
    def last_cut_reason(self) -> str | None:
        """Why the most recent chunk (if any) was emitted -- "silence" or
        "maxlen"/"flush". Only meaningful immediately after feed() returns
        non-None text; see StreamingChunker.last_cut_reason."""
        return self._chunker.last_cut_reason

    def _transcribe_and_append(self, audio: np.ndarray, wav_tmp_path: Path, source: str) -> str | None:
        """The one place a chunk's audio becomes text in the assembled
        transcript. Every caller (feed/flush_final)
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
            "stop_phrase_matched": False,  # overwritten by strip_stop_phrase() if this chunk ended the dictation
            "stop_phrase_near_miss": False,  # overwritten by mark_stop_phrase_near_miss() -- diagnostic only
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

    def strip_stop_phrase(self, matched_chunk_text: str, remainder: str):
        """Called when the just-transcribed chunk (already appended by
        feed()/_transcribe_and_append) is confirmed to end the dictation --
        replaces that chunk's contribution to the transcript with
        `remainder` (the same text, stop phrase removed) so "that's it"
        itself never reaches L3. If the chunk was nothing but the stop
        phrase, remainder is empty and the chunk contributes nothing.

        No audio trimming needed here, unlike the old acoustic stop word:
        the match only ever fires on a chunk that already closed on a real
        pause (see StreamingChunker.last_cut_reason), so there's no
        "no-pause, real content stuck to the stop word" ambiguity to
        resolve at the audio layer -- the whole chunk's transcript,
        stop phrase included, was always going to be one unit, and
        word-count stripping the phrase back out of that same text handles
        the no-pause-before-"that's it" case for free (e.g. "...also
        redeploy that's it" -> "...also redeploy") without a separate
        heuristic."""
        assert self.chunks_transcribed and self.chunks_transcribed[-1] == matched_chunk_text, (
            "strip_stop_phrase must be called immediately after the matching feed() call, "
            "before anything else touches chunks_transcribed"
        )
        self.chunks_transcribed.pop()
        if remainder:
            self.chunks_transcribed.append(remainder)
        self.chunk_log[-1]["kept_transcript"] = remainder or None
        self.chunk_log[-1]["stop_phrase_matched"] = True

    def mark_stop_phrase_near_miss(self):
        """Diagnostic only, see stop_phrase_near_miss() -- flags the just-
        appended chunk's log record so a rendering-variant miss on real
        speech is visible in the chunk log immediately, instead of only
        being inferable later from "it didn't stop, no idea why."""
        if self.chunk_log:
            self.chunk_log[-1]["stop_phrase_near_miss"] = True

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
    """Always routes through the L2.5 concierge now, regardless of
    live_deliver -- CONTROL/QUERY/CHAT get classified and answered on
    every run (including plain --simulate testing with no flags), since
    none of that touches a real orchestrator session. live_deliver only
    reaches the concierge's own DISPATCH/UNSURE forwarding gate (see
    default_deliver / concierge.handle_transcript's --live-deliver
    semantics) -- there is no separate short-circuit here any more."""
    print(f"FULL TRANSCRIPT: {text!r}")
    chunk_log_path = _write_chunk_log(chunk_log)
    print(f"chunk log ({len(chunk_log)} chunks): {chunk_log_path}")
    result = default_deliver(text, orchestrator_target=orchestrator_target, live_deliver=live_deliver)
    handoff_wall_time = time.time()
    print(f"concierge result: {result!r}")
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
        # Fed every frame regardless of IDLE/CAPTURING -- see LiveController
        # .on_frame's identical comment for why (verify_wake_trigger needs a
        # real rolling window even for a quick second dictation).
        preroll.append(frame)

        if state == "IDLE":
            score = wake_model.predict(frame)["hey_jarvis_v0.1"]
            if score >= IDLE_THRESHOLD:
                accepted, verify_text = verify_wake_trigger(whisper, preroll, tmp_wav)
                if accepted:
                    print(f"[{t:.2f}s] wake word (score={score:.3f}) verified ({verify_text!r}) -> CAPTURING")
                    state = "CAPTURING"
                    session = DictationSession(vad, whisper)
                    preroll.clear()
                else:
                    print(f"[{t:.2f}s] wake word (score={score:.3f}) REJECTED by verification (heard: {verify_text!r}) -- staying IDLE")
        elif state == "CAPTURING":
            # No acoustic model runs during CAPTURING any more -- the stop
            # trigger is transcript text, checked below on whatever feed()
            # just produced, not a second wake-word score.
            partial = session.feed(frame, tmp_wav)
            if partial is not None:
                matched, remainder = (
                    match_stop_phrase(partial) if session.last_cut_reason == "silence" else (False, partial)
                )
                if matched:
                    stop_wall_time = time.time()  # real wall-clock: whisper/deliver calls take real time even in --simulate
                    print(f"[{t:.2f}s] stop phrase matched in chunk transcript {partial!r} -> finalize (remainder={remainder!r})")
                    session.strip_stop_phrase(partial, remainder)
                    text = session.full_transcript()
                    _report_and_deliver(text, session.chunk_log, live_deliver, orchestrator_target, stop_wall_time)
                    state = "IDLE"
                    session = None
                else:
                    print(f"[{t:.2f}s] chunk transcribed: {partial!r}")
                    if session.last_cut_reason == "silence" and stop_phrase_near_miss(partial):
                        print(f"[{t:.2f}s] *** possible stop-phrase near-miss (not matched): {partial!r} ***")
                        session.mark_stop_phrase_near_miss()
            elif session.silence_exceeds_safety_net():
                stop_wall_time = time.time()
                print(f"[{t:.2f}s] {SILENCE_SAFETY_NET_S}s silence safety net -> finalize (no stop phrase heard)")
                session.flush_final(tmp_wav)  # no stop phrase to strip -- transcribe everything buffered
                text = session.full_transcript()
                _report_and_deliver(text, session.chunk_log, live_deliver, orchestrator_target, stop_wall_time)
                state = "IDLE"
                session = None

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
    validated (chunking, hallucination filtering, stop-phrase matching)
    applies unchanged here. What's new here is only the frame source and
    the CANCEL_ARMED branch --simulate never exercised.
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
        # Fed every frame regardless of IDLE/CAPTURING (not just when we're
        # about to check the wake-word score) so verify_wake_trigger always
        # has a full rolling window of real context to hand Whisper, even
        # if a new dictation starts within VERIFY_BUFFER_S of the last one
        # ending -- an empty/thin preroll fails verification closed (see
        # verify_wake_trigger), which would otherwise make a quick second
        # "Hey Jarvis" right after "that's it" spuriously get rejected.
        self.preroll.append(frame_i16)

        if self.state == "IDLE":
            score = self.wake_model.predict(frame_i16)["hey_jarvis_v0.1"]
            if score >= IDLE_THRESHOLD:
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
                    self.session = DictationSession(self.vad, self.whisper)
                    self.preroll.clear()
                else:
                    print(f"[{time.strftime('%H:%M:%S')}] wake word (score={score:.3f}) REJECTED by verification (heard: {verify_text!r}) -- staying IDLE")
        elif self.state == "CAPTURING":
            # No acoustic model runs during CAPTURING any more -- the stop
            # trigger is transcript text, checked below on whatever feed()
            # just produced, not a second wake-word score.
            partial = self.session.feed(frame_i16, self.tmp_wav)
            if partial is not None:
                matched, remainder = (
                    match_stop_phrase(partial) if self.session.last_cut_reason == "silence" else (False, partial)
                )
                if matched:
                    stop_wall_time = time.time()
                    print(f"[{time.strftime('%H:%M:%S')}] stop phrase matched in chunk transcript {partial!r} -> finalize (remainder={remainder!r})")
                    self.session.strip_stop_phrase(partial, remainder)
                    text = self.session.full_transcript()
                    _report_and_deliver(text, self.session.chunk_log, self.live_deliver, self.orchestrator_target, stop_wall_time)
                    self.state = "IDLE"
                    self.session = None
                else:
                    print(f"[{time.strftime('%H:%M:%S')}] chunk transcribed: {partial!r}")
                    if self.session.last_cut_reason == "silence" and stop_phrase_near_miss(partial):
                        print(f"[{time.strftime('%H:%M:%S')}] *** possible stop-phrase near-miss (not matched): {partial!r} ***")
                        self.session.mark_stop_phrase_near_miss()
            elif self.session.silence_exceeds_safety_net():
                stop_wall_time = time.time()
                print(f"[{time.strftime('%H:%M:%S')}] {SILENCE_SAFETY_NET_S}s silence safety net -> finalize")
                self.session.flush_final(self.tmp_wav)
                text = self.session.full_transcript()
                _report_and_deliver(text, self.session.chunk_log, self.live_deliver, self.orchestrator_target, stop_wall_time)
                self.state = "IDLE"
                self.session = None

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
    overflow_count = 0

    def audio_callback(indata, frames, time_info, status):
        # This callback runs on PortAudio's own real-time thread, not the
        # asyncio loop -- it must never block, so it does exactly two things:
        # convert the frame and hand it off via call_soon_threadsafe (non-
        # blocking, doesn't wait on the loop even if the loop is itself
        # blocked inside on_frame()'s verification call). No locks, no I/O,
        # no shared state with loop-side code beyond this one handoff. This
        # is what makes the OS-level input buffer not a real overflow risk
        # during the ~0.5s verification block -- the callback keeps firing
        # on schedule regardless of what the loop is doing.
        #
        # input_overflow is the direct, measurable check on that claim
        # rather than trusting the reasoning: if PortAudio ever couldn't
        # hand off audio before the next block was ready, it sets this
        # flag. Logged explicitly (not just the raw status object) and
        # counted, so "zero overflows across a session with repeated
        # triggers" is a real, checkable pass rather than absence of
        # evidence.
        nonlocal overflow_count
        if status.input_overflow:
            overflow_count += 1
            print(f"[{time.strftime('%H:%M:%S')}] *** INPUT OVERFLOW #{overflow_count} *** (audio may have been dropped)", file=sys.stderr)
        elif status:
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
            print(f"[{time.strftime('%H:%M:%S')}] input overflow count this session: {overflow_count}")


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
        help="let the L2.5 concierge actually forward a DISPATCH/UNSURE-classified dictation (real tmux "
             "send-keys into the orchestrator pane) instead of just printing what it would send. Every "
             "dictation is classified and CONTROL/QUERY/CHAT turns are answered locally regardless of this "
             "flag -- it only gates real delivery to a live orchestrator session. Never the default, so "
             "routine testing (including plain --simulate) can't touch a real session by accident.",
    )
    ap.add_argument("--target", default=None, help="orchestrator tmux session name (only used with --live-deliver)")
    args = ap.parse_args()

    kwargs = {"model_path": Path(args.model)} if args.model else {}
    with WhisperDaemon(**kwargs) as whisper:
        if args.simulate:
            simulate(args.simulate, whisper, live_deliver=args.live_deliver, orchestrator_target=args.target)
        else:
            asyncio.run(live(whisper, live_deliver=args.live_deliver, orchestrator_target=args.target))
