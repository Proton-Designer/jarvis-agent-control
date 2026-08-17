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
import wave
from pathlib import Path

import numpy as np
import onnxruntime as ort

VAD_MODEL_PATH = Path(__file__).parent.parent / "l1_wakeword" / "models" / "silero_vad.onnx"
SAMPLE_RATE = 16000
FRAME_SAMPLES = 512  # Silero VAD's recommended window at 16kHz
SPEECH_THRESHOLD = 0.5
SILENCE_MS_TO_CUT = 700  # pause length that ends a chunk (mid-dictation boundary, not end-of-speech)
MIN_CHUNK_MS = 500  # don't emit a chunk shorter than this (avoids cutting on a stray blip)
MAX_CHUNK_MS = 30_000  # hard cap so one long unbroken sentence still gets cut for transcription


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
        if should_cut_on_silence or should_cut_on_maxlen:
            return self._cut()
        return None

    # Minimum count of speech-classified frames a trimmed remainder must
    # contain before it's worth sending to Whisper at all. Exists because a
    # remainder can pass the duration check (min_chunk_samples) while being
    # almost entirely silence -- e.g. a paused-before-stop case, where the
    # pending buffer is [long pause][short wake word] and trimming a fixed
    # tail off the end leaves mostly the pause behind. Whisper hallucinates
    # on near-silent input rather than returning nothing (confirmed by
    # testing: got back "TV Gelderland 2021" from what should have been an
    # empty remainder) -- so silence has to be filtered out explicitly,
    # not left to the model to notice there's nothing to transcribe.
    _MIN_SPEECH_FRAMES_IN_REMAINDER = 5  # ~160ms of actual speech

    def flush(self) -> np.ndarray | None:
        """Call at dictation end: returns any buffered speech as a final chunk."""
        if self._buf and self._saw_speech:
            return self._cut()
        self._reset_buffer()
        return None

    def pop_trimming_tail(self, trim_s: float) -> np.ndarray | None:
        """Call when a stop wake word fires mid-buffer: removes the last
        `trim_s` seconds (the wake word's own acoustic span) and returns
        whatever real content remains before it, or None if nothing does.

        Exists because discarding the whole pending buffer on stop is only
        correct when there was a pause before the stop word -- the VAD
        will already have cut real content into its own chunk by then. If
        Ayman says the stop word with no pause, the pending buffer *is*
        the real content plus the stop word stuck together with nothing to
        tell them apart except roughly how long "hey jarvis" itself takes
        to say. Confirmed by testing: without this, a whole no-pause
        dictation ("...also tell Ship Check to redeploy, Hey Jarvis") was
        silently lost in full, not just the stop phrase.

        This is a fixed-duration heuristic, not a precise cut -- it can't
        know exactly where the wake word starts, only estimate from
        typical duration. Real-voice validation should confirm the trim
        window against how Ayman actually says it, not just synthetic
        `say` timing.
        """
        if not self._buf:
            self._reset_buffer()
            return None
        audio = np.concatenate(self._buf)
        speech_flags = self._frame_is_speech
        self._reset_buffer()

        trim_samples = int(trim_s * SAMPLE_RATE)
        keep_samples = max(0, len(audio) - trim_samples)
        remainder = audio[:keep_samples]
        if len(remainder) < self._min_chunk_samples:
            return None

        keep_frames = keep_samples // FRAME_SAMPLES
        if sum(speech_flags[:keep_frames]) < self._MIN_SPEECH_FRAMES_IN_REMAINDER:
            return None  # mostly/entirely silence -- would just feed Whisper hallucination bait
        return remainder

    def _reset_buffer(self):
        self._buf = []
        self._frame_is_speech = []
        self._silence_run = 0
        self._saw_speech = False

    def _cut(self) -> np.ndarray:
        audio = np.concatenate(self._buf)
        self._reset_buffer()
        return audio


def chunk_pcm(pcm_i16: np.ndarray, vad: SileroVAD | None = None):
    """Yield (start_sample, end_sample, audio_i16) for a complete, already-
    recorded file. Used by the offline CLI / tests below -- for the live
    daemon, use StreamingChunker.push() directly, one frame at a time.
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
