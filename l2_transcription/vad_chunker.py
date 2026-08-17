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


def chunk_pcm(pcm_i16: np.ndarray, vad: SileroVAD | None = None):
    """Yield (start_sample, end_sample, audio_i16) for each chunk in a full recording.

    Designed to be called the same way on a growing live buffer as on a
    complete file -- the caller decides when a suffix of the stream is
    "final" (dictation ended); this function just finds cut points.
    """
    vad = vad or SileroVAD()
    frame_samples = FRAME_SAMPLES
    silence_frames_to_cut = int(SILENCE_MS_TO_CUT / 1000 * SAMPLE_RATE / frame_samples)
    min_chunk_samples = int(MIN_CHUNK_MS / 1000 * SAMPLE_RATE)
    max_chunk_samples = int(MAX_CHUNK_MS / 1000 * SAMPLE_RATE)

    n_frames = len(pcm_i16) // frame_samples
    chunk_start = 0
    silence_run = 0
    saw_speech = False

    for i in range(n_frames):
        frame = pcm_i16[i * frame_samples : (i + 1) * frame_samples]
        prob = vad.speech_prob(frame)
        pos = (i + 1) * frame_samples
        is_speech = prob >= SPEECH_THRESHOLD
        if is_speech:
            saw_speech = True
            silence_run = 0
        else:
            silence_run += 1

        chunk_len = pos - chunk_start
        should_cut_on_silence = saw_speech and silence_run >= silence_frames_to_cut and chunk_len >= min_chunk_samples
        should_cut_on_maxlen = chunk_len >= max_chunk_samples

        if should_cut_on_silence or should_cut_on_maxlen:
            yield chunk_start, pos, pcm_i16[chunk_start:pos]
            chunk_start = pos
            silence_run = 0
            saw_speech = False

    if chunk_start < len(pcm_i16) and saw_speech:
        yield chunk_start, len(pcm_i16), pcm_i16[chunk_start:]


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
