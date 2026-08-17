#!/usr/bin/env python3
"""Persistent whisper.cpp transcription via whisper-server, kept warm.

Cold subprocess-per-chunk (whisper-cli) was measured earlier at ~1.1s per
utterance because it reloads the model and reinits the Metal/BLAS backend
every call: whisper.cpp's own timers showed only ~184ms load + ~460ms
encode of that as real inference (q5_0), the rest is one-time process
startup. On a multi-minute dictation cut into many chunks, that overhead
would be paid dozens of times. whisper-server keeps the model resident in
one process; this wrapper starts it once and calls its HTTP API per chunk.

Confirmed empirically that whisper-server's /inference endpoint accepts a
per-request `prompt` form field, distinct from its startup --prompt flag --
this is what makes runtime vocabulary re-injection per chunk possible.
Measured warm per-request latency: ~0.50s (vs. ~1.1s cold), consistent
with the load+encode breakdown above.
"""
import argparse
import atexit
import subprocess
import time
from pathlib import Path

import requests

DEFAULT_MODEL = Path.home() / ".whisper-models" / "ggml-large-v3-turbo-q5_0.bin"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8811
STARTUP_TIMEOUT_S = 15


class WhisperDaemon:
    """Owns a warm whisper-server subprocess. Use as a context manager."""

    def __init__(self, model_path: Path = DEFAULT_MODEL, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
        self.model_path = Path(model_path)
        self.host = host
        self.port = port
        self.base_url = f"http://{host}:{port}"
        self._proc: subprocess.Popen | None = None

    def start(self):
        if not self.model_path.exists():
            raise FileNotFoundError(f"model not found: {self.model_path}")
        self._proc = subprocess.Popen(
            ["whisper-server", "-m", str(self.model_path), "--host", self.host, "--port", str(self.port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        atexit.register(self.stop)
        self._wait_ready()
        return self

    def _wait_ready(self):
        deadline = time.time() + STARTUP_TIMEOUT_S
        while time.time() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError("whisper-server exited during startup")
            try:
                if requests.get(self.base_url, timeout=1).status_code == 200:
                    return
            except requests.exceptions.ConnectionError:
                pass
            time.sleep(0.2)
        raise TimeoutError(f"whisper-server not ready after {STARTUP_TIMEOUT_S}s")

    def stop(self):
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None

    def transcribe(self, wav_path: str, prompt: str = "") -> str:
        with open(wav_path, "rb") as f:
            resp = requests.post(
                f"{self.base_url}/inference",
                files={"file": f},
                data={"prompt": prompt, "response_format": "text"},
                timeout=30,
            )
        resp.raise_for_status()
        return resp.text.strip()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--prompt", default="")
    ap.add_argument("--model", default=str(DEFAULT_MODEL))
    args = ap.parse_args()

    with WhisperDaemon(model_path=Path(args.model)) as daemon:
        t0 = time.time()
        text = daemon.transcribe(args.file, prompt=args.prompt)
        elapsed = time.time() - t0
        print(f"[{elapsed:.3f}s] {text}")
