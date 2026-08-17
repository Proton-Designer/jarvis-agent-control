#!/usr/bin/env python3
"""L1 wake-word listener: continuous "hey jarvis" detection via openWakeWord.

Two modes:
  --file <wav>   offline scoring of a single 16kHz mono wav (for validation /
                  false-trigger measurement against recorded audio)
  (default)      live microphone streaming, prints a detection event per hit

Detection events are printed as JSON lines to stdout so a caller (or a human
watching logs) can consume them without parsing free text.
"""
import argparse
import json
import sys
import time
import wave
from pathlib import Path

import numpy as np
from openwakeword.model import Model

MODELS_DIR = Path(__file__).parent / "models"
FRAME_SAMPLES = 1280  # openWakeWord expects 16kHz mono, ~80ms frames
SAMPLE_RATE = 16000
DEFAULT_THRESHOLD = 0.5


def load_model():
    return Model(
        wakeword_models=[str(MODELS_DIR / "hey_jarvis_v0.1.onnx")],
        melspec_model_path=str(MODELS_DIR / "melspectrogram.onnx"),
        embedding_model_path=str(MODELS_DIR / "embedding_model.onnx"),
        inference_framework="onnx",
    )


def score_frames(model, pcm16: np.ndarray, threshold: float):
    """Yield (frame_index, score) for every frame that crosses threshold."""
    n_frames = len(pcm16) // FRAME_SAMPLES
    for i in range(n_frames):
        frame = pcm16[i * FRAME_SAMPLES : (i + 1) * FRAME_SAMPLES]
        prediction = model.predict(frame)
        score = prediction["hey_jarvis_v0.1"]
        if score >= threshold:
            yield i, score


def run_file(path: str, threshold: float):
    with wave.open(path, "rb") as wf:
        assert wf.getframerate() == SAMPLE_RATE, f"expected {SAMPLE_RATE}Hz, got {wf.getframerate()}"
        assert wf.getnchannels() == 1, "expected mono"
        pcm16 = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)

    model = load_model()
    hits = list(score_frames(model, pcm16, threshold))
    for frame_idx, score in hits:
        t = frame_idx * FRAME_SAMPLES / SAMPLE_RATE
        print(json.dumps({"event": "detection", "file": path, "t_sec": round(t, 2), "score": round(float(score), 3)}))
    if not hits:
        print(json.dumps({"event": "no_detection", "file": path}))
    return len(hits)


def run_live(threshold: float):
    import sounddevice as sd

    model = load_model()
    print(json.dumps({"event": "listening", "threshold": threshold}), file=sys.stderr)

    def callback(indata, frames, time_info, status):
        pcm16 = (indata[:, 0] * 32767).astype(np.int16)
        prediction = model.predict(pcm16)
        score = prediction["hey_jarvis_v0.1"]
        if score >= threshold:
            print(json.dumps({"event": "detection", "t": time.time(), "score": round(float(score), 3)}), flush=True)

    with sd.InputStream(channels=1, samplerate=SAMPLE_RATE, blocksize=FRAME_SAMPLES, dtype="float32", callback=callback):
        while True:
            time.sleep(0.1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="score a single 16kHz mono wav instead of live mic")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    args = ap.parse_args()

    if args.file:
        run_file(args.file, args.threshold)
    else:
        run_live(args.threshold)
