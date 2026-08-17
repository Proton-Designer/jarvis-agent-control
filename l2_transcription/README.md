# L2 — transcription

Runs in the same daemon process as L1 (the wake-word listener) rather than
as a separate service, so it currently reuses `../l1_wakeword/.venv`
(numpy + onnxruntime, already installed there) instead of a second venv.
If L2 grows a dependency L1 doesn't need, split it out then.

## vad_chunker.py — mid-dictation chunk segmentation

**Not** end-of-utterance detection — that's the "Jarvis" toggle (L1). This
decides where to cut a long, continuous dictation into pieces small enough
to transcribe incrementally while Ayman keeps talking. A wrong cut here is
low-stakes: an awkward chunk boundary, not a clipped command.

Uses the Silero VAD ONNX model openWakeWord already downloads
(`../l1_wakeword/models/silero_vad.onnx`) — not a separate model or
dependency, see that layer's README for why.

Validated against a synthetic 3-sentence dictation with explicit pauses
(macOS `say ... [[slnc N]]`): correctly cut into 3 chunks at the pause
boundaries, no false cuts within a sentence. The `MAX_CHUNK_MS` (30s) hard
cap for unbroken speech is implemented but not yet tested against real
30s+ continuous audio — flagging rather than claiming it's verified.

```
../l1_wakeword/.venv/bin/python vad_chunker.py --file clip.wav
```

## whisper_daemon.py — persistent transcription (next)

Not built yet. Requirement carried from the L2 benchmark work: must keep
the whisper.cpp model resident (whisper-server or bindings) rather than
shelling out to `whisper-cli` per chunk — cold-start (process + Metal
backend init + model load) was measured at ~350-400ms on top of whichever
model's own load+encode time, and a long dictation would pay that
repeatedly, once per chunk, if built the naive way.
