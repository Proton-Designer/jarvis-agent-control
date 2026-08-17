#!/usr/bin/env python3
"""Streaming chunk segmentation for long dictations, via Silero VAD (ONNX).

Role, per the architecture pivot: this VAD does NOT decide when Ayman is
done talking (that's the "Jarvis" toggle, in L1). It only decides where to
cut a long, continuous dictation into chunks small enough to transcribe
incrementally while he keeps talking. A wrong cut here just means an
awkward chunk boundary, not a clipped command -- much lower-stakes tuning
than end-of-utterance detection would be.

Model is the Silero VAD ONNX file openWakeWord already downloads as a
dependency (../l1_wakeword/models/silero_vad.onnx) -- reused rather than
fetched separately, and run on the same onnxruntime already installed
for L1's wake-word detection.
"""
import argparse
import json
import logging
import wave
from pathlib import Path

import numpy as np
import onnxruntime as ort

log = logging.getLogger(__name__)

VAD_MODEL_PATH = Path(__file__).parent.parent / "l1_wakeword" / "models" / "silero_vad.onnx"
SAMPLE_RATE = 16000
FRAME_SAMPLES = 512  # Silero VAD's recommended window at 16kHz
SPEECH_THRESHOLD = 0.5
SILENCE_MS_TO_CUT = 700  # pause length that ends a chunk (mid-dictation boundary, not end-of-speech)
MAX_CHUNK_MS = 30_000  # hard cap so one long unbroken sentence still gets cut for transcription

# Minimum count of speech-classified frames ANY chunk must contain before
# it's allowed to reach Whisper -- not just trimmed remainders. Applies
# structurally, inside _cut() itself, so every code path that can emit a
# chunk (silence-triggered cut, maxlen cut, flush, trimmed-tail) goes
# through the same gate rather than each caller having to remember to
# check. Exists because a chunk can satisfy every duration/silence-run
# condition while being almost entirely non-speech -- a pause containing a
# cough or keyboard clatter that VAD marginally misclassified, a chunk at
# the edge of a long silence -- and Whisper does not return empty on
# low-speech audio, it hallucinates (confirmed directly: "TV Gelderland
# 2021" out of what should have been near-silence). A hallucinated
# sentence reaching L3 becomes a routed instruction with no signal that
# Ayman never said it -- worth being deliberately conservative about.
MIN_CHUNK_MS = 500  # don't emit a chunk shorter than this (avoids cutting on a stray blip)
MIN_CHUNK_SAMPLES = int(MIN_CHUNK_MS / 1000 * SAMPLE_RATE)
MIN_SPEECH_FRAMES_PER_CHUNK = 5  # ~160ms of actual speech, not just duration


class SileroVAD:
    def __init__(self, model_path: Path = VAD_MODEL_PATH):
        self.sess = ort.InferenceSession(str(model_path))
        self.reset()

    def reset(self):
        self._h = np.zeros((2, 1, 64), dtype=np.float32)
        self._c = np.zeros((2, 1, 64), dtype=np.float32)

    def speech_prob(self, frame_i16: np.ndarray) -> float:
        """frame_i16: int16 PCM, exactly FRAME_SAMPLES long."""
        audio = (frame_i16.astype(np.float32) / 32768.0)[None, :]
        out, self._h, self._c = self.sess.run(
            None,
            {
                "input": audio,
                "sr": np.array(SAMPLE_RATE, dtype=np.int64),
                "h": self._h,
                "c": self._c,
            },
        )
        return float(out[0, 0])


class StreamingChunker:
    """Incremental cut-point finder: push exactly one new FRAME_SAMPLES-long
    frame at a time, get back a completed chunk's audio the moment a cut is
    decided, or None otherwise.

    This exists because the VAD is *stateful* (Silero carries LSTM h/c
    across calls) -- re-running it over a re-grown buffer from index 0 on
    every new frame, the way an earlier version of this daemon did, feeds
    already-seen audio through the model again on top of state that has
    already advanced past it. That's not just wasteful, it's wrong: the
    LSTM state and the audio position silently drift out of sync, and the
    model's output stops meaning what the code assumes it means. Advance
    the VAD exactly once per frame, ever, and this class is where that
    invariant is enforced.
    """

    def __init__(self, vad: SileroVAD | None = None):
        self.vad = vad or SileroVAD()
        self._silence_frames_to_cut = int(SILENCE_MS_TO_CUT / 1000 * SAMPLE_RATE / FRAME_SAMPLES)
        self._min_chunk_samples = int(MIN_CHUNK_MS / 1000 * SAMPLE_RATE)
        self._max_chunk_samples = int(MAX_CHUNK_MS / 1000 * SAMPLE_RATE)
        self._buf: list[np.ndarray] = []
        self._frame_is_speech: list[bool] = []  # parallel to _buf, one bool per frame
        self._silence_run = 0
        self._saw_speech = False
        # Set by _cut() whenever it emits a chunk (None otherwise/between
        # emissions) -- lets a caller distinguish "closed because Ayman
        # paused" from "closed because it hit the 30s hard cap mid-sentence".
        # Added for the transcript-based stop phrase (see daemon.py): a
        # maxlen cut can land anywhere, including mid-word, so a chunk that
        # happens to end with the stop phrase's words for that reason alone
        # is not a real stop -- only a silence-triggered cut means Ayman
        # actually paused there.
        self.last_cut_reason: str | None = None

    @property
    def silence_duration_s(self) -> float:
        """How long since speech was last seen -- exposed so a caller can
        implement a longer safety-net timeout (e.g. L1's 50s no-talk
        auto-close) on top of this chunker's much shorter mid-dictation
        cut threshold, without re-deriving VAD state itself."""
        return self._silence_run * FRAME_SAMPLES / SAMPLE_RATE

    def push(self, frame_i16: np.ndarray) -> np.ndarray | None:
        assert len(frame_i16) == FRAME_SAMPLES
        prob = self.vad.speech_prob(frame_i16)  # the ONE place speech_prob is called per frame
        self._buf.append(frame_i16)
        is_speech = prob >= SPEECH_THRESHOLD
        self._frame_is_speech.append(is_speech)
        if is_speech:
            self._saw_speech = True
            self._silence_run = 0
        else:
            self._silence_run += 1

        chunk_len = len(self._buf) * FRAME_SAMPLES
        should_cut_on_silence = (
            self._saw_speech and self._silence_run >= self._silence_frames_to_cut and chunk_len >= self._min_chunk_samples
        )
        should_cut_on_maxlen = chunk_len >= self._max_chunk_samples
        if should_cut_on_silence:
            return self._cut(reason="silence")
        if should_cut_on_maxlen:
            return self._cut(reason="maxlen")
        return None

    def flush(self) -> np.ndarray | None:
        """Call at dictation end: returns any buffered speech as a final chunk."""
        if self._buf and self._saw_speech:
            return self._cut(reason="flush")
        self._reset_buffer()
        return None

    def _reset_buffer(self):
        self._buf = []
        self._frame_is_speech = []
        self._silence_run = 0
        self._saw_speech = False

    def _cut(self, reason: str) -> np.ndarray | None:
        audio = np.concatenate(self._buf)
        speech_flags = self._frame_is_speech
        self._reset_buffer()
        result = self._gate(audio, speech_flags, source="chunk")
        self.last_cut_reason = reason if result is not None else None
        return result

    @staticmethod
    def _gate(audio: np.ndarray, speech_flags: list, source: str) -> np.ndarray | None:
        """The one place a chunk is allowed to leave this class. Every
        emission path (silence-cut, maxlen-cut, flush, trimmed-tail) routes
        through here so the min-speech-content requirement is a structural
        guarantee, not a convention each caller has to apply -- same
        reasoning that made "advance the VAD once per frame" a class
        invariant instead of something call sites could get wrong."""
        if len(audio) < MIN_CHUNK_SAMPLES:
            log.info("vad_chunker: dropped %s chunk (%.2fs, too short)", source, len(audio) / SAMPLE_RATE)
            return None
        speech_frames = sum(speech_flags)
        if speech_frames < MIN_SPEECH_FRAMES_PER_CHUNK:
            log.warning(
                "vad_chunker: dropped %s chunk (%.2fs, only %d speech frames) -- would risk Whisper hallucination on low-speech audio",
                source, len(audio) / SAMPLE_RATE, speech_frames,
            )
            return None
        return audio


def chunk_pcm(pcm_i16: np.ndarray, vad: SileroVAD | None = None):
    """Yield (start_sample, end_sample, audio_i16) for a complete, already-
    recorded file. Used by the offline CLI / tests below -- for the live
    daemon, use StreamingChunker.push() directly, one frame at a time.

    Note: if the gate in StreamingChunker._gate drops a chunk (too little
    speech content), this generator's chunk_start bookkeeping doesn't
    advance past it, so a subsequent real chunk's reported start_sec can
    be earlier than where its audio actually begins. Cosmetic only --
    daemon.py drives StreamingChunker directly and doesn't use these
    offsets, so production behavior is unaffected. Not fixed here because
    it'd add real complexity to a debug/reporting-only path.
    """
    chunker = StreamingChunker(vad)
    n_frames = len(pcm_i16) // FRAME_SAMPLES
    pos = 0
    chunk_start = 0
    for i in range(n_frames):
        frame = pcm_i16[i * FRAME_SAMPLES : (i + 1) * FRAME_SAMPLES]
        pos = (i + 1) * FRAME_SAMPLES
        audio = chunker.push(frame)
        if audio is not None:
            yield chunk_start, pos, audio
            chunk_start = pos
    tail = chunker.flush()
    if tail is not None:
        yield chunk_start, len(pcm_i16), tail


def run_file(path: str):
    with wave.open(path, "rb") as wf:
        assert wf.getframerate() == SAMPLE_RATE, f"expected {SAMPLE_RATE}Hz, got {wf.getframerate()}"
        assert wf.getnchannels() == 1, "expected mono"
        pcm_i16 = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)

    vad = SileroVAD()
    for start, end, _audio in chunk_pcm(pcm_i16, vad):
        print(json.dumps({
            "event": "chunk",
            "start_sec": round(start / SAMPLE_RATE, 2),
            "end_sec": round(end / SAMPLE_RATE, 2),
            "duration_sec": round((end - start) / SAMPLE_RATE, 2),
        }))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="score a 16kHz mono wav and print chunk boundaries")
    args = ap.parse_args()
    run_file(args.file)
