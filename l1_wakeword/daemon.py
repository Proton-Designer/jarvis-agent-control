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
import json
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
GUARD_S = 3.0
SILENCE_SAFETY_NET_S = 50.0
SOCKET_PATH = Path.home() / ".jarvis" / "l1.sock"


def default_deliver(text: str):
    """Production hookup -- imports L4's real handoff. Deliberately NOT called
    by --simulate (see below): that would fire a real `tmux send-keys` into
    whatever "claude-orchestrator" session exists, which is not something a
    test run should ever do by accident."""
    sys.path.insert(0, str(Path(__file__).parent.parent / "l4_controller"))
    from l2_l3_handoff import deliver_transcript  # noqa
    from transport import TmuxTransport  # noqa
    return deliver_transcript(text, TmuxTransport())


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
        self._chunker = StreamingChunker(vad)

    def in_guard(self, now: float) -> bool:
        return (now - self.started_at) < GUARD_S

    def silence_exceeds_safety_net(self) -> bool:
        return self._chunker.silence_duration_s >= SILENCE_SAFETY_NET_S

    def feed(self, frame_i16: np.ndarray, wav_tmp_path: Path) -> str | None:
        """Push exactly one frame into the chunker (the VAD advances exactly
        once per frame, ever -- see StreamingChunker's docstring for why
        that invariant matters). Transcribes and returns text the moment a
        chunk boundary is found; otherwise returns None."""
        audio = self._chunker.push(frame_i16)
        if audio is None:
            return None
        _write_wav(wav_tmp_path, audio)
        text = self.whisper.transcribe(str(wav_tmp_path), prompt=build_prompt())
        self.chunks_transcribed.append(text)
        return text

    def flush_final(self, wav_tmp_path: Path):
        """Called after the dictation ends normally (silence safety net) --
        transcribes whatever's still buffered, since there's no wake word
        to strip it out."""
        audio = self._chunker.flush()
        if audio is not None and len(audio) > 0:
            _write_wav(wav_tmp_path, audio)
            self.chunks_transcribed.append(self.whisper.transcribe(str(wav_tmp_path), prompt=build_prompt()))

    def discard_pending(self):
        """Called after the dictation ends via the stop wake word instead --
        the still-buffered audio at that moment IS the "hey jarvis" stop
        phrase (the VAD hasn't cut it into its own chunk yet, since nothing
        after it has triggered a silence-based cut), so it gets dropped
        rather than transcribed. Confirmed by testing: without this, a
        closing "Hey Jarvis" showed up verbatim at the end of the
        transcript handed to L3.

        Known limitation, not silently assumed safe: if Ayman says real
        content immediately before the stop word with no pause between
        them, that content is in the same not-yet-cut buffer and would be
        discarded along with it. The common case (a natural pause before a
        deliberate toggle phrase) is what's been tested; the no-pause case
        hasn't been, and belongs in real-voice validation.
        """
        self._chunker = StreamingChunker(self._chunker.vad)  # drop pending audio; flush() deliberately not called

    def full_transcript(self) -> str:
        return " ".join(t for t in self.chunks_transcribed if t)


def _write_wav(path: Path, pcm_i16: np.ndarray):
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm_i16.tobytes())


def simulate(dictation_wav: str, whisper: WhisperDaemon):
    """Drives the IDLE -> CAPTURING -> IDLE state machine over a pre-recorded
    file, as if it arrived from a live mic frame-by-frame. Uses a print-only
    deliver stub -- this must never touch a real tmux session."""
    wake_model = load_model()
    vad = SileroVAD()
    tmp_wav = Path("/tmp/jarvis_daemon_chunk.wav")

    with wave.open(dictation_wav, "rb") as wf:
        assert wf.getframerate() == SAMPLE_RATE and wf.getnchannels() == 1
        pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)

    state = "IDLE"
    session: DictationSession | None = None
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

        if state == "IDLE":
            if start_fired:
                print(f"[{t:.2f}s] wake word -> CAPTURING")
                state = "CAPTURING"
                session = DictationSession(vad, whisper, started_at=t)
        elif state == "CAPTURING":
            if not session.in_guard(now=t) and stop_fired:
                print(f"[{t:.2f}s] wake word (post-guard, score={score:.3f}) -> finalize")
                session.discard_pending()  # drops the stop phrase itself, not real content -- see docstring
                text = session.full_transcript()
                print(f"[{t:.2f}s] FULL TRANSCRIPT: {text!r}")
                print(f"[{t:.2f}s] (simulate mode: not calling deliver_transcript -- would send to L4 here)")
                state = "IDLE"
                session = None
            elif session.silence_exceeds_safety_net():
                print(f"[{t:.2f}s] {SILENCE_SAFETY_NET_S}s silence safety net -> finalize (no stop word heard)")
                session.flush_final(tmp_wav)  # no stop word to strip -- transcribe everything buffered
                text = session.full_transcript()
                print(f"[{t:.2f}s] FULL TRANSCRIPT: {text!r}")
                print(f"[{t:.2f}s] (simulate mode: not calling deliver_transcript -- would send to L4 here)")
                state = "IDLE"
                session = None
            else:
                partial = session.feed(frame, tmp_wav)
                if partial:
                    print(f"[{t:.2f}s] chunk transcribed: {partial!r}")

    if state == "CAPTURING" and session:
        session.flush_final(tmp_wav)
        text = session.full_transcript()
        print(f"[end of file] FULL TRANSCRIPT (file ended mid-dictation, no stop word or safety-net timeout reached): {text!r}")

    tmp_wav.unlink(missing_ok=True)


async def cancel_socket_server(wake_model, threshold: float = RECOVERABLE_THRESHOLD):
    """Production-only: real implementation needs to read from the SAME live
    mic stream the IDLE/CAPTURING loop owns, arming the lowered-threshold
    detector for the requested window. Not exercised by --simulate --
    there's no live mic in this environment to validate it against, so it's
    scaffolded and documented rather than claimed as tested.
    """
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
        # TODO(live mic): arm `threshold` on the shared stream for timeout_s
        # seconds instead of this placeholder sleep. Not implemented against
        # real audio yet -- see module docstring and README.
        await asyncio.sleep(timeout_s)
        writer.write(json.dumps({"cancelled": False}).encode())
        await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(handle, path=str(SOCKET_PATH))
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--simulate", help="drive the state machine over a pre-recorded wav instead of live mic")
    ap.add_argument("--model", default=None, help="whisper.cpp model path override")
    args = ap.parse_args()

    kwargs = {"model_path": Path(args.model)} if args.model else {}
    with WhisperDaemon(**kwargs) as whisper:
        if args.simulate:
            simulate(args.simulate, whisper)
        else:
            print("Live mic + cancel-socket loop not wired up yet -- use --simulate <wav> for now.", file=sys.stderr)
            sys.exit(1)
