"""
Kokoro-82M TTS backend (docs/TODO-feature-queue.md #1): replaces macOS
`say` as the primary voice. Lazy singleton, loaded once per process and
kept warm for that process's lifetime -- every process that imports
say_feedback.py (each MCP server, daemon.py, team-lead callers) gets its
own instance, which is fine at Kokoro's size (unlike a heavier model,
duplicating it per process is cheap -- see the research this build is
based on).

Model files (~354MB total: kokoro-v1.0.onnx ~325MB + voices-v1.0.bin
~28MB, all 54 voices bundled in the one voices file) live in ./models/,
gitignored same as l1_wakeword/models/. ONE-TIME download via
fetch_kokoro_models.py -- run once after creating the venv. NEVER
fetched at runtime: if the files are missing, load() raises rather than
reaching out to the network mid-conversation. The caller (say_feedback.py)
is what turns that into an audible/logged fallback to `say` -- never a
silent one.

VOICE: bm_george -- British male, Kokoro's own Grade-C tier (tied-best
among its four bm_* voices; bm_daniel and bm_lewis both grade lower, D
and D+ respectively -- see hexgrad/Kokoro-82M's own VOICES.md). Chosen
over the equally-graded bm_fable for CHARACTER, not just the grade:
independent voice reviews describe bm_george as calm/professional,
suited to tutorials and explainers, where bm_fable is graded for
storytelling/narration -- the former is the register an assistant's
status/confirmation speech actually wants. Do NOT assume "bm_daniel" is
the match for Ayman's requested "Daniel" just because the string
matches -- it's Kokoro's WORST-graded British male voice of the four,
found by actually checking rather than assuming (the Lead's explicit
instruction). Samples of all four were generated and sent to Ayman for
his own ear-check; he can overrule this recommendation.

COLD-START, MEASURED LIVE (2026-08-20, this machine -- Apple M4 Pro, 24GB
unified memory): model load ~390ms, but the FIRST real synthesis call
after loading costs an EXTRA ~1.5s on top of that (ONNX Runtime's own
session/graph warmup -- not model-load, not phonemization: every call
after the first lands at ~270-300ms regardless of which voice).
~1.9s total cold time-to-first-audio in a freshly-started process.
Warm, per-utterance synthesis for a short Jarvis-length phrase
("Got it, working on it.") measures ~270-300ms across all four
candidate voices -- essentially voice-independent, and this is the real
"first-token" number that matters once warm, since Kokoro returns a
whole utterance's audio in one batch rather than streaming token by
token.

warm() exists specifically so the ~1.9s cold cost lands at explicit,
opt-in PROCESS STARTUP -- invisible, same reasoning daemon.py's old
local-model warmup comment already used -- rather than on whatever the
first REAL queued utterance happens to be. Without it, a process whose
first-ever spoken thing is the instant ack would make that ack itself
pay the full cold-start cost, undermining the entire reason the ack
exists (killing the silence between "that's it" and a reply).
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import wave
from pathlib import Path

import numpy as np

MODELS_DIR = Path(__file__).parent / "models"
MODEL_PATH = MODELS_DIR / "kokoro-v1.0.onnx"
VOICES_PATH = MODELS_DIR / "voices-v1.0.bin"

DEFAULT_VOICE = "bm_george"
DEFAULT_LANG = "en-gb"

_lock = threading.Lock()
_kokoro = None  # lazy singleton -- the loaded Kokoro instance, one per process
_load_failed_detail: str | None = None  # sticky: a load failure won't fix itself between calls


class KokoroUnavailable(Exception):
    """Raised when Kokoro cannot be used right now -- missing model
    files, an import/load failure, or a prior load already failed in
    this process. The caller's job (say_feedback.py) is to catch this
    and fall back to `say`, audibly/logged -- never silently."""


def _load():
    global _kokoro, _load_failed_detail
    if _kokoro is not None:
        return _kokoro
    if _load_failed_detail is not None:
        # Sticky: retrying a missing-file or import error on every single
        # speak() call would make every fallback pay the failed-load cost
        # again for nothing. A process restart is what re-attempts this,
        # same as any other load-once resource. Per-call SYNTHESIS
        # failures (model loaded fine, this one call errored) are NOT
        # sticky -- only a LOAD failure disables Kokoro for the rest of
        # the process's life; see synthesize()'s own docstring.
        raise KokoroUnavailable(_load_failed_detail)
    with _lock:
        if _kokoro is not None:
            return _kokoro
        if _load_failed_detail is not None:
            raise KokoroUnavailable(_load_failed_detail)
        if not MODEL_PATH.exists() or not VOICES_PATH.exists():
            _load_failed_detail = (
                f"Kokoro model files not found at {MODELS_DIR} -- run "
                "l4_controller/fetch_kokoro_models.py once (~354MB, one-time, "
                "never fetched at runtime)"
            )
            raise KokoroUnavailable(_load_failed_detail)
        try:
            from kokoro_onnx import Kokoro
            _kokoro = Kokoro(str(MODEL_PATH), str(VOICES_PATH))
        except Exception as e:  # noqa: BLE001
            _load_failed_detail = f"Kokoro failed to load: {e}"
            raise KokoroUnavailable(_load_failed_detail) from e
        return _kokoro


def synthesize(text: str, voice: str = DEFAULT_VOICE, lang: str = DEFAULT_LANG):
    """Returns (samples: float32 mono ndarray, sample_rate: int). Raises
    KokoroUnavailable on ANY failure -- load or per-call synthesis --
    never returns silence. A synthesis-only failure (model already
    loaded, this specific call errored) does NOT poison future calls;
    only a load failure (see _load()) is sticky."""
    kokoro = _load()
    try:
        samples, sample_rate = kokoro.create(text, voice=voice, lang=lang)
    except Exception as e:  # noqa: BLE001
        raise KokoroUnavailable(f"Kokoro synthesis failed: {e}") from e
    return samples, sample_rate


def synthesize_to_wav(text: str, voice: str = DEFAULT_VOICE, lang: str = DEFAULT_LANG, out_path: Path | None = None) -> Path:
    """Synthesizes and writes a temp (or given) 16-bit PCM wav, for a
    caller that plays it back via a real audio player (afplay) rather
    than handling raw samples itself. Caller owns cleanup of the temp
    file when out_path is None."""
    samples, sample_rate = synthesize(text, voice=voice, lang=lang)
    if out_path is None:
        fd, tmp_name = tempfile.mkstemp(suffix=".wav", prefix="jarvis_kokoro_")
        os.close(fd)
        out_path = Path(tmp_name)
    samples16 = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(str(out_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(samples16.tobytes())
    return out_path


def warm() -> dict:
    """Loads the model AND runs one throwaway synthesis call, so the
    measured ~1.5s ONNX Runtime session-warmup cost (see module
    docstring) lands here -- at explicit, opt-in process startup -- not
    on whatever the first REAL queued utterance happens to be. Idempotent:
    a no-op returning ok=True if already warm, and a cheap no-op
    returning the same failure detail if a load already failed. Never
    raises -- a caller doing best-effort startup warming doesn't need
    its own try/except; unavailability is reported, not an exception to
    remember to catch. Returns {"ok", "load_ms", "warmup_ms", "detail"}."""
    if _kokoro is not None:
        return {"ok": True, "load_ms": 0.0, "warmup_ms": 0.0, "detail": "already warm"}
    if _load_failed_detail is not None:
        return {"ok": False, "load_ms": 0.0, "warmup_ms": 0.0, "detail": _load_failed_detail}

    t0 = time.monotonic()
    try:
        _load()
    except KokoroUnavailable as e:
        return {"ok": False, "load_ms": 0.0, "warmup_ms": 0.0, "detail": str(e)}
    load_ms = (time.monotonic() - t0) * 1000

    t0 = time.monotonic()
    try:
        synthesize("Warming up.")
    except KokoroUnavailable as e:
        # Loaded fine but the warmup synthesis call itself failed --
        # unusual, but not fatal, and NOT a load failure: leave _kokoro
        # set. A real speak() call will attempt synthesis fresh and fall
        # back per-call if it errors too, same as any other synthesis
        # failure. This function never disables Kokoro on a warmup-only
        # failure.
        return {"ok": False, "load_ms": round(load_ms, 1), "warmup_ms": 0.0, "detail": f"loaded but warmup synthesis failed: {e}"}
    warmup_ms = (time.monotonic() - t0) * 1000
    return {"ok": True, "load_ms": round(load_ms, 1), "warmup_ms": round(warmup_ms, 1), "detail": "warm"}
