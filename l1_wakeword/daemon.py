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
sys.path.insert(0, str(Path(__file__).parent.parent / "l4_controller"))

from listener import load_model, FRAME_SAMPLES as WAKEWORD_FRAME_SAMPLES, SAMPLE_RATE  # noqa: E402
from vad_chunker import SileroVAD, StreamingChunker, FRAME_SAMPLES as VAD_FRAME_SAMPLES  # noqa: E402
from whisper_daemon import WhisperDaemon  # noqa: E402
from session_vocab import build_prompt  # noqa: E402
from hallucination_filter import filter_transcript  # noqa: E402
from latency_log import log_event  # noqa: E402

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
# 2.7s, not the 50s this used to be. Ayman's decision, 2026-08-20, and
# it changes what the primary end-of-dictation signal IS.
#
# The stop phrase ("that's it") was designed as the reliable way to end a
# dictation, and 50s existed purely as a net so a thinking pause could
# never cut him off. That is correct for "tell the gateway team to run
# the integration tests -- that's it". It is badly wrong for saying
# hello: a two-word greeting followed by a two-second pause left him
# waiting FIFTY SECONDS for a reply, which is not a quirk, it is the
# feature failing on its easiest case.
#
# So silence is now the ordinary way a dictation ends, and the stop
# phrase becomes the way to end one INSTANTLY rather than the only way
# to end one at all.
#
# The thinking pause it used to protect is now protected explicitly
# instead of implicitly -- see HOLD_PHRASE_VARIANTS. Saying "hold up"
# buys back the full 50s, on demand, for exactly the turn that needs it.
# That is strictly better than a blanket 50s: it costs a moment of
# speech when you actually need to think, and costs nothing the rest of
# the time.
SILENCE_SAFETY_NET_S = 2.7

# Saying any of these buys back SILENCE_HOLD_S of quiet, once.
#
# Matched only at the END of a chunk that closed on silence -- the same
# discipline as STOP_PHRASE_VARIANTS, and for the same reason: "wait for
# the deploy to finish" must not trigger a hold just because it contains
# the word.
#
# FAILURE DIRECTION, deliberately asymmetric: "tell it to wait" DOES end
# with "wait" and will extend when it shouldn't. That costs a pause, and
# he can always say "that's it" to end immediately. The opposite error --
# failing to extend when he genuinely is thinking -- cuts him off
# mid-thought and sends half an instruction to an agent. So this fails
# toward waiting, every time.
HOLD_PHRASE_VARIANTS = {
    "hold up", "hold on", "wait", "wait a sec", "wait a second",
    "let me think", "one sec", "one second", "give me a second",
    "hang on", "just a sec", "just a second",
}
SILENCE_HOLD_S = 50.0

sys.path.insert(0, str(Path(__file__).parent.parent))
from jarvis_paths import jarvis_home  # noqa: E402

SOCKET_PATH = jarvis_home() / "l1.sock"
# The l5_console TUI's Signal view + always-visible meter (SPEC-TUI.md
# §3) read this -- state (IDLE/CAPTURING/CANCEL_ARMED) and a real-time
# audio level, the one thing in the whole console that reports what the
# microphone is actually receiving rather than what the system believes.
# Written only from live() (a real mic session), never --simulate --
# there's no real audio level to report for a pre-recorded file, and a
# stale/absent file during simulate testing is the correct signal, not a
# gap to paper over. The console gates on JarvisState.wake.running (the
# authoritative pgrep-based signal) before trusting anything in this
# file at all -- this file adds detail once the process is already known
# alive, it is never the thing that decides "is it alive."
WAKE_STATE_PATH = jarvis_home() / "wake_state.json"
WAKE_STATE_WRITE_INTERVAL_S = 0.1  # ~10Hz -- smooth enough to read as "live," far below the ~31Hz frame rate to keep file-write I/O light

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


def match_hold_phrase(text: str) -> bool:
    """Does `text` END with a hold phrase? Callers must only ask this of
    a chunk that closed on SILENCE, same as match_stop_phrase.

    Deliberately does NOT strip the phrase from the transcript the way
    match_stop_phrase does. "that's it" is pure punctuation and removing
    it is safe; a hold phrase is not -- "tell it to wait" would become
    "tell it to", which corrupts a real instruction into a meaningless
    one. A stray "hold up" left in the text is noise the router can
    ignore; a truncated instruction is not recoverable."""
    normalized = _normalize_for_stop_match(text)
    return any(
        normalized == v or normalized.endswith(" " + v)
        for v in HOLD_PHRASE_VARIANTS
    )


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


def default_deliver(text: str, orchestrator_target: str | None = None, live_deliver: bool = False, wake_score: float | None = None):
    """Production hookup. Delivers each finished dictation to whichever
    session Ayman attached to the CONCIERGE role in the console.

    This is the join the whole two-tier design was for. The chain is now:

        voice -> L1 (wake + whisper)
              -> CONCIERGE (Haiku, read tools + jarvis_say + handoff_to_router)
              -> ROUTER    (Sonnet, write tools, dispatches to teams)
              -> teams

    The concierge either answers Ayman itself (~1-2s) or calls
    handoff_to_router and returns immediately. It cannot dispatch: its MCP
    surface has no deliver_batch and tools_write is not in its process.

    TARGET COMES FROM engine.json, NOT FROM A CONSTANT. The old
    DEFAULT_ORCHESTRATOR_TARGET ("claude-orchestrator") was a hardcoded
    name that had to match a session someone remembered to start with
    exactly that name. Now the console owns it: whatever Ayman attached
    is what receives his voice, and swapping the concierge in the UI
    changes where his words go with no code change and no restart.

    `orchestrator_target`, if passed, still wins -- it is how the CLI and
    the canaries drive a throwaway target without touching the real
    engine. Production passes None.

    FAILS CLOSED AND AUDIBLY, which is the point rather than a detail.
    If no concierge is attached, or the attached one isn't running, Ayman
    just spoke a full instruction into a system with nowhere to put it.
    He finds out NOW, out loud, instead of discovering later that nothing
    happened -- the failure class this project has found in six different
    places and ruled against every time.

    Note the console's own start gate (SPEC-engine-roles.md §6) already
    refuses to open the microphone when either role is missing, so this
    should rarely fire. "Should rarely fire" is not "cannot fire": the
    role can die between pressing start and finishing a sentence, and a
    guard that only holds when an earlier guard held is not a guard.
    """
    from say_feedback import speak, PRIORITY_HIGH  # noqa: E402

    # INSTANT ACK, before anything else -- SPEC-orchestration.md §1.1.
    # Templated, zero inference, ~0ms, so there is never silence between
    # "that's it" and a reply that takes 1-2s to come back from a network
    # call. It states RECEIPT only and never an outcome.
    #
    # Restored 2026-08-18: I dropped this when I rewrote default_deliver()
    # to target the concierge role, replacing the whole function body and
    # losing ue6rruxg's §1.1 work with it. instant_ack_canary.py caught it
    # immediately, which is exactly what it was written for -- the ack is
    # invisible when present and inaudible when missing, so nothing else
    # would have noticed until Ayman sat through the silence himself.
    #
    # Fires only on a real dispatch (live_deliver), same gate as before,
    # so smoke tests and dry runs stay silent.
    if live_deliver:
        try:
            sys.path.insert(0, str(Path(__file__).parent.parent / "l4_controller"))
            from instant_ack import speak_instant_ack  # noqa: E402
            speak_instant_ack(text)
        except Exception as e:
            # Never let the courtesy layer break the dispatch it precedes.
            log_event("instant_ack_failed", error=str(e))

    # Dry runs stop HERE, before the concierge is ever resolved. My bug,
    # found 2026-08-20 by instant_ack_canary: this early-return used to
    # sit BELOW the lookup, so a live_deliver=False smoke test still
    # resolved the concierge role and, finding none, SPOKE -- "No
    # concierge is attached, so I couldn't send that anywhere" -- at
    # PRIORITY_HIGH. The canary's very first property is that a dry run
    # produces no speech at all, and it was violated by a path that only
    # runs when there is nothing to say it about.
    #
    # It stayed hidden because it needs BOTH conditions at once: a dry
    # run AND no attached concierge. On this machine a concierge was
    # always attached, so the branch never fired; it took the engine
    # registry becoming properly test-isolated for the empty case to
    # exist at all.
    #
    # The HIGH priority is what made it worse than a stray line: it
    # jumps the speech queue, so on a real dictation that error would
    # overtake the instant ack and be the first thing Ayman hears --
    # inverting the one ordering guarantee this whole layer exists to
    # provide.
    #
    # Resolving a target we have already decided not to use was never
    # meaningful work; doing it before the gate was the mistake.
    if not live_deliver:
        print(f"(live_deliver=False: NOT forwarding -- would send: {text!r})")
        return {"label": "FORWARDED", "forwarded": False, "delivery": None, "retain": True}

    if orchestrator_target is None:
        sys.path.insert(0, str(Path(__file__).parent.parent / "l5_console" / "state"))
        try:
            import engine_roles
            record = engine_roles.get_role_record("concierge")
            liveness = engine_roles.role_liveness("concierge")
        except Exception as e:
            log_event("l1_target_lookup_failed", error=str(e))
            print(f"CONCIERGE LOOKUP FAILED: {e!r} -- nothing sent")
            speak("I couldn't find the concierge, so nothing was sent.", priority=PRIORITY_HIGH)
            return {"label": "NO_TARGET", "forwarded": False, "delivery": None, "retain": True}

        if not record:
            log_event("l1_no_concierge_attached")
            print("NO CONCIERGE ATTACHED -- nothing sent")
            speak("No concierge is attached, so I couldn't send that anywhere.", priority=PRIORITY_HIGH)
            return {"label": "NO_TARGET", "forwarded": False, "delivery": None, "retain": True}

        # `running` (bool), not a "state" string -- role_liveness()'s
        # actual shape is {attached, running, liveness, record,
        # tools_reachable}. I guessed "state"/"RUNNING" here first and it
        # read as not-running for a session that was genuinely up. It
        # failed CLOSED, which is the correct direction for a guess to
        # fail, but it is still a guess: read the contract, don't infer it.
        if not liveness.get("running"):
            log_event("l1_concierge_not_running", liveness=liveness.get("liveness"))
            print(f"CONCIERGE NOT RUNNING (liveness={liveness.get('liveness')}) -- nothing sent")
            speak("The concierge isn't running, so I couldn't send that anywhere.", priority=PRIORITY_HIGH)
            return {"label": "NO_TARGET", "forwarded": False, "delivery": None, "retain": True}

        target = record["tmux"]
    else:
        target = orchestrator_target

    sys.path.insert(0, str(Path(__file__).parent.parent / "l4_controller"))
    from l2_l3_handoff import deliver_transcript  # noqa: E402
    from transport import TmuxTransport  # noqa: E402

    delivery = deliver_transcript(text, TmuxTransport(), orchestrator_target=target)
    # "forwarded" means actually delivered, never merely attempted --
    # deliver_transcript returns None on a real failure, and reporting
    # that as success is precisely the silence-read-as-success failure.
    delivered = delivery is not None and getattr(delivery, "ok", False)
    log_event("l1_to_concierge", forwarded=delivered, target=target, chars=len(text))
    return {"label": "FORWARDED", "forwarded": delivered, "delivery": delivery, "retain": True}


class DictationSession:
    """Owns the state for one CAPTURING episode: rolling VAD chunker +
    accumulated transcript text."""

    def __init__(self, vad: SileroVAD, whisper: WhisperDaemon, wake_score: float | None = None):
        self.whisper = whisper
        # float(...) matters, not cosmetic: openWakeWord's predict() returns
        # numpy.float32, and json.dumps (via log_event) can't serialize that
        # -- found by testing, crashed the NOT_ADDRESSED discard-event log.
        self.wake_score = float(wake_score) if wake_score is not None else None
        self.chunks_transcribed: list[str] = []
        self.chunk_log: list[dict] = []  # one record per Whisper call -- see _transcribe_and_append
        self._chunker = StreamingChunker(vad)
        self._total_samples_fed = 0
        self._hold_active = False

    def note_hold_phrase(self) -> None:
        """Arms the long window for the NEXT silence only."""
        self._hold_active = True

    @property
    def silence_limit_s(self) -> float:
        return SILENCE_HOLD_S if self._hold_active else SILENCE_SAFETY_NET_S

    def clear_hold(self) -> None:
        """Consumed when speech resumes, so the hold is genuinely
        TEMPORARY -- one thinking pause, not a mode the dictation stays
        in. Saying "hold up" again buys another."""
        self._hold_active = False

    def silence_exceeds_safety_net(self) -> bool:
        return self._chunker.silence_duration_s >= self.silence_limit_s

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
        whisper_t0 = time.monotonic()
        raw_text = self.whisper.transcribe(str(wav_tmp_path), prompt=prompt_used)
        whisper_ms = (time.monotonic() - whisper_t0) * 1000
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
            "whisper_ms": round(whisper_ms, 1),  # for the L2.5 latency budget's "Whisper" row -- the LAST chunk's value is what's on the critical path to first audio out
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


def _frame_level(frame_i16: np.ndarray) -> float:
    """RMS level, normalized to roughly 0.0-1.0 -- a meter tracking peak
    amplitude alone spikes on transients and reads jumpy; RMS is the
    standard "how loud does this actually sound" measure most real audio
    meters use. int16 full-scale is 32768; speech rarely drives RMS
    anywhere near that, so this will mostly read as a modest fraction
    even during normal talking -- expected, not a bug, same way a real
    VU meter doesn't sit near full scale for ordinary speech."""
    if frame_i16.size == 0:
        return 0.0
    rms = float(np.sqrt(np.mean(frame_i16.astype(np.float64) ** 2)))
    return min(1.0, rms / 32768.0)


def _write_wake_state_file(state: str, level: float) -> None:
    """Best-effort -- a failure here (disk full, permissions) must never
    take down the wake-word loop over a UI convenience file. Throttled by
    the caller (WAKE_STATE_WRITE_INTERVAL_S), not here -- this function
    always writes when called."""
    try:
        WAKE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        WAKE_STATE_PATH.write_text(json.dumps({
            "state": state, "level": round(level, 4), "updated_at": time.time(),
        }))
    except OSError:
        pass


def _clear_wake_state_file() -> None:
    """Called once, on a CLEAN exit from live()'s loop only -- a hard
    kill (SIGKILL, crash) can never reach this, which is expected and
    fine: wake_state.py's reader already treats a stale `updated_at` as
    unknown on its own (see its STALE_AFTER_S check), independent of
    whether this ever runs. This is only closing the gap on the clean-
    shutdown path specifically -- without it, the last real reading
    (possibly CAPTURING) would keep rendering as live, current data for
    up to STALE_AFTER_S after a clean stop, instead of immediately
    reading as "no data" the moment the file is gone. Deleting rather
    than writing a new "stopped" state value reuses read_wake_state()'s
    already-correct, already-tested missing-file handling instead of
    teaching every downstream consumer (meter.py, main.py's dictating
    gate) a fourth state value that means the same thing "no data"
    already means."""
    try:
        WAKE_STATE_PATH.unlink(missing_ok=True)
    except OSError:
        pass


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


def _report_and_deliver(
    text: str, chunk_log: list[dict], live_deliver: bool, orchestrator_target: str | None,
    stop_wall_time: float, wake_score: float | None = None,
):
    """Hands the finished transcript to default_deliver(), which since
    2026-08-18 forwards it straight to the orchestrator -- no local
    classification step in between.

    The chunk log is still written AFTER default_deliver() returns and
    only if it reports retain=True. That ordering was the fix for "a
    false trigger persists a transcription of a private conversation to
    disk permanently" -- discarded means never written, not
    written-then-deleted. NOTE that nothing currently returns
    retain=False: the local NOT_ADDRESSED check was the only producer of
    it and it is disconnected, so every dictation is now written. The
    ordering is kept deliberately so that whichever layer owns the
    addressed check next inherits a working discard path rather than
    having to rebuild one. Printing the transcript to
    this process's own console is unaffected either way -- during any
    session where this daemon is running, Ayman (or whoever's watching,
    per the standing no-unattended-mic rule) is already present in the
    room the audio came from, so this isn't a new disclosure the way a
    durable file would be."""
    print(f"FULL TRANSCRIPT: {text!r}")
    last_whisper_ms = chunk_log[-1]["whisper_ms"] if chunk_log else None
    log_event("l1_dictation_end", chars=len(text), last_chunk_whisper_ms=last_whisper_ms)
    result = default_deliver(text, orchestrator_target=orchestrator_target, live_deliver=live_deliver, wake_score=wake_score)

    if result.get("retain", True):
        chunk_log_path = _write_chunk_log(chunk_log)
        print(f"chunk log ({len(chunk_log)} chunks): {chunk_log_path}")
    else:
        print(f"NOT_ADDRESSED (high confidence): discarding transcript, chunk log NOT written ({len(chunk_log)} chunks dropped)")

    handoff_wall_time = time.time()
    end_to_end_s = handoff_wall_time - stop_wall_time
    # Was a raw `{result!r}` dict dump, which Ayman reasonably read as
    # "it sent my transcription to an agent" on a turn where
    # forwarded=False and nothing had been sent anywhere (2026-08-18).
    # The routing was correct and the console said the opposite; a
    # terminal line that requires parsing a Python dict to learn whether
    # your words left the machine is not an acceptable answer to the
    # only question this line exists to answer. State the outcome first,
    # in words, and keep the dict behind it for debugging.
    if result.get("forwarded"):
        outcome = f"FORWARDED to {orchestrator_target}"
    elif result.get("response"):
        outcome = f"answered locally, nothing sent to any agent: {result['response']!r}"
    else:
        outcome = "no response, nothing sent to any agent"
    print(f"routing: {result.get('label')} -> {outcome}")
    print(f"  (raw: {result!r})")
    print(f"stop-word-to-handoff-return wall-clock: {end_to_end_s:.3f}s")
    log_event(
        "l1_concierge_round_trip", label=result.get("label"), spoken=bool(result.get("response")),
        forwarded=result.get("forwarded", False), retained=result.get("retain", True),
        end_to_end_s=round(end_to_end_s, 3),
    )


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
                    log_event("wake_start_verified", score=round(float(score), 3))
                    state = "CAPTURING"
                    session = DictationSession(vad, whisper, wake_score=score)
                    preroll.clear()
                else:
                    print(f"[{t:.2f}s] wake word (score={score:.3f}) REJECTED by verification (heard: {verify_text!r}) -- staying IDLE")
                    # Never log verify_text here -- it's ambient audio that
                    # failed verification, exactly the content NOT_ADDRESSED
                    # (l2_5_concierge/classifier.py) exists to avoid
                    # persisting. The score is diagnostic (this is the data
                    # the acoustic-collision investigation was built on);
                    # what was actually said is not.
                    log_event("wake_start_rejected", score=round(float(score), 3))
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
                    _report_and_deliver(text, session.chunk_log, live_deliver, orchestrator_target, stop_wall_time, wake_score=session.wake_score)
                    state = "IDLE"
                    # Mirrors LiveController._to_idle() -- see its docstring
                    # for the stale-buffer replay this prevents. Kept in sync
                    # deliberately: --simulate exists to reproduce production
                    # behaviour over a recorded wav, and a simulate path that
                    # can't reproduce the bug production has is a test that
                    # protects nothing.
                    wake_model.reset()
                    session = None
                else:
                    print(f"[{t:.2f}s] chunk transcribed: {partial!r}")
                    # Speech arrived, so any previous hold is spent -- the
                    # extension covers ONE thinking pause, not the rest of
                    # the dictation.
                    session.clear_hold()
                    if session.last_cut_reason == "silence" and match_hold_phrase(partial):
                        session.note_hold_phrase()
                        print(f"[{t:.2f}s] hold phrase heard -> silence window extended to {SILENCE_HOLD_S}s for this pause")
                    if session.last_cut_reason == "silence" and stop_phrase_near_miss(partial):
                        print(f"[{t:.2f}s] *** possible stop-phrase near-miss (not matched): {partial!r} ***")
                        session.mark_stop_phrase_near_miss()
            elif session.silence_exceeds_safety_net():
                stop_wall_time = time.time()
                print(f"[{t:.2f}s] {session.silence_limit_s}s silence -> finalize (no stop phrase heard)")
                session.flush_final(tmp_wav)  # no stop phrase to strip -- transcribe everything buffered
                text = session.full_transcript()
                _report_and_deliver(text, session.chunk_log, live_deliver, orchestrator_target, stop_wall_time, wake_score=session.wake_score)
                state = "IDLE"
                wake_model.reset()  # see LiveController._to_idle()
                session = None

    if state == "CAPTURING" and session:
        session.flush_final(tmp_wav)
        text = session.full_transcript()
        print(f"[end of file] FULL TRANSCRIPT (file ended mid-dictation, no stop word or safety-net timeout reached): {text!r}")
        _report_and_deliver(text, session.chunk_log, live_deliver, orchestrator_target, time.time(), wake_score=session.wake_score)

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

    def _to_idle(self) -> None:
        """Return to IDLE and CLEAR THE WAKE MODEL'S BUFFERS.

        The reset is the whole point, and it is not optional. predict()
        is only ever called in the IDLE branch below -- during CAPTURING
        the acoustic model deliberately doesn't run (the stop trigger is
        transcript text). openWakeWord keeps internal audio-feature and
        prediction buffers across calls and assumes a CONTINUOUS stream;
        skipping it for the length of a dictation leaves those buffers
        still holding the audio from just before CAPTURING began -- which
        is the "Hey Jarvis" that started it.

        So the first predict() after returning to IDLE re-scored the
        ORIGINAL wake word and fired again, immediately, over and over as
        the stale audio aged out. Found live (2026-08-18, Ayman's own
        test), and the logs name the cause outright -- the rejected
        scores are IDENTICAL to the accepted one that started the
        dictation:

            12:17:55 wake word (score=0.824) verified ('Hey Jarvis')
            12:18:11 wake word (score=0.824) REJECTED (heard: 'jaa')
            12:18:12 wake word (score=0.824) REJECTED (heard: "That's it.")

        Same 0.824 three times; likewise 0.735 and 0.927 in the other two
        runs. A fresh acoustic event cannot reproduce a previous score to
        three decimal places -- that is a replay, not a detection.

        Nothing unsafe ever happened, because verify_wake_trigger()
        re-transcribes and rejected 100% of them -- start-fails-closed
        doing exactly its job. But every spurious fire spent a ~470ms
        blocking Whisper call, and on_frame() stalls frame consumption
        for its duration, so a burst of these is the daemon spending most
        of its time re-rejecting its own echo -- and a genuine "Hey
        Jarvis" landing in that window is delayed or missed outright.
        That is the real cost: not a false accept, a DEAF period right
        after every dictation.

        reset() is documented as "may not be efficient when called too
        frequently" -- irrelevant here, it runs once per dictation, not
        per frame."""
        self.state = "IDLE"
        self.wake_model.reset()

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
                    log_event("wake_start_verified", score=round(float(score), 3))
                    self.state = "CAPTURING"
                    self.session = DictationSession(self.vad, self.whisper, wake_score=score)
                    self.preroll.clear()
                else:
                    print(f"[{time.strftime('%H:%M:%S')}] wake word (score={score:.3f}) REJECTED by verification (heard: {verify_text!r}) -- staying IDLE")
                    # Never log verify_text -- see the matching comment in
                    # simulate()'s IDLE branch for why.
                    log_event("wake_start_rejected", score=round(float(score), 3))
        elif self.state == "CAPTURING":
            # No acoustic model runs during CAPTURING any more -- the stop
            # trigger is transcript text, checked below on whatever feed()
            # just produced, not a second wake-word score.
            partial = self.session.feed(frame_i16, self.tmp_wav)
            if partial is not None:
                matched, remainder = (
                    match_stop_phrase(partial) if self.session.last_cut_reason == "silence" else (False, partial)
                )
                # Speech arrived: spend any armed hold before deciding
                # whether this chunk arms a new one.
                self.session.clear_hold()
                if self.session.last_cut_reason == "silence" and match_hold_phrase(partial):
                    self.session.note_hold_phrase()
                    print(f"[{time.strftime('%H:%M:%S')}] hold phrase heard in {partial!r} -> silence window extended to {SILENCE_HOLD_S}s for this pause")
                if matched:
                    stop_wall_time = time.time()
                    print(f"[{time.strftime('%H:%M:%S')}] stop phrase matched in chunk transcript {partial!r} -> finalize (remainder={remainder!r})")
                    self.session.strip_stop_phrase(partial, remainder)
                    text = self.session.full_transcript()
                    _report_and_deliver(text, self.session.chunk_log, self.live_deliver, self.orchestrator_target, stop_wall_time, wake_score=self.session.wake_score)
                    self._to_idle()
                    self.session = None
                else:
                    print(f"[{time.strftime('%H:%M:%S')}] chunk transcribed: {partial!r}")
                    if self.session.last_cut_reason == "silence" and stop_phrase_near_miss(partial):
                        print(f"[{time.strftime('%H:%M:%S')}] *** possible stop-phrase near-miss (not matched): {partial!r} ***")
                        self.session.mark_stop_phrase_near_miss()
            elif self.session.silence_exceeds_safety_net():
                stop_wall_time = time.time()
                print(f"[{time.strftime('%H:%M:%S')}] {self.session.silence_limit_s}s silence -> finalize")
                self.session.flush_final(self.tmp_wav)
                text = self.session.full_transcript()
                _report_and_deliver(text, self.session.chunk_log, self.live_deliver, self.orchestrator_target, stop_wall_time, wake_score=self.session.wake_score)
                self._to_idle()
                self.session = None

    def _on_cancel_frame(self, frame_i16: np.ndarray, now: float):
        if now >= self._cancel_deadline:
            print(f"[{time.strftime('%H:%M:%S')}] cancel window closed, not detected -> IDLE")
            log_event("cancel_window_closed", detected=False)
            self._to_idle()
            return
        score = score_frame_ensemble(self._cancel_ensemble, frame_i16)
        if score >= RECOVERABLE_THRESHOLD:
            print(f"[{time.strftime('%H:%M:%S')}] CANCEL detected (ensemble score={score:.3f}) -> IDLE")
            log_event("cancel_detected", score=round(float(score), 3))
            self._cancel_detected = True
            self._to_idle()


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
        last_wake_state_write = 0.0
        try:
            while True:
                frame = await frame_queue.get()
                controller.on_frame(frame, time.time())
                # Throttled to WAKE_STATE_WRITE_INTERVAL_S, not every
                # frame (~31Hz) -- smooth enough for a live meter, far
                # less file-write I/O. Level computed from every frame
                # regardless of whether this tick writes, so a throttled
                # write still reflects the CURRENT frame, not a stale one
                # from several frames ago.
                now = time.time()
                if now - last_wake_state_write >= WAKE_STATE_WRITE_INTERVAL_S:
                    _write_wake_state_file(controller.state, _frame_level(frame))
                    last_wake_state_write = now
        finally:
            cancel_task.cancel()
            _clear_wake_state_file()
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

    # Warm Kokoro once at startup, not on the first real thing this
    # process speaks -- which can be the instant ack (default_deliver(),
    # speak_instant_ack()) or a failure message from this same function,
    # either of which exists specifically to avoid silence. Measured live
    # (kokoro_tts.py's docstring): the first synthesis call after a fresh
    # model load costs an extra ~1.5s (ONNX Runtime's own session
    # warmup) on top of ~390ms model load -- ~1.9s total, which would
    # otherwise land on that first spoken thing instead of here. This is
    # the exact same "startup cost is invisible, mid-conversation cost is
    # not" reasoning this file used to apply to the old L2.5 local
    # model's warmup before it was disconnected (2026-08-18) -- reused
    # for the thing that actually needs it now. Printed, not silent --
    # same "confidence from watching it react" discipline as every other
    # state transition in this file.
    sys.path.insert(0, str(Path(__file__).parent.parent / "l4_controller"))
    import kokoro_tts  # noqa: E402
    warm_result = kokoro_tts.warm()
    status = "ok" if warm_result["ok"] else f"FAILED ({warm_result['detail']}) -- will fall back to `say` audibly/logged"
    print(f"[{time.strftime('%H:%M:%S')}] Kokoro warm-up: {status} (load={warm_result['load_ms']}ms, warmup={warm_result['warmup_ms']}ms)")

    print(f"[{time.strftime('%H:%M:%S')}] ready")

    kwargs = {"model_path": Path(args.model)} if args.model else {}
    with WhisperDaemon(**kwargs) as whisper:
        if args.simulate:
            simulate(args.simulate, whisper, live_deliver=args.live_deliver, orchestrator_target=args.target)
        else:
            asyncio.run(live(whisper, live_deliver=args.live_deliver, orchestrator_target=args.target))
