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

## whisper_daemon.py — persistent transcription

Wraps `whisper-server` (built-in to the whisper.cpp Homebrew formula) as a
context manager: starts it once, keeps the model resident, and calls its
HTTP `/inference` endpoint per chunk instead of shelling out to
`whisper-cli` per chunk. Cold subprocess-per-call was measured earlier at
~1.1s/utterance (process + Metal backend init + model load, on top of the
model's own ~184ms load + ~460ms encode for q5_0). Warm, over HTTP to an
already-running server: **measured ~0.50s/request**, consistently, across
5 back-to-back calls — matches the predicted savings.

Confirmed empirically (not assumed from docs) that `/inference` accepts a
per-request `prompt` form field, separate from the server's startup
`--prompt` flag. This is what makes re-injecting the runtime vocabulary
(live tmux session names + "Jarvis") on every chunk possible without
restarting the server — the whole point of the chunked-transcription
redesign after the architecture pivot.

```
../l1_wakeword/.venv/bin/python whisper_daemon.py --file clip.wav --prompt "Nightwatch, MyKhutbah, Ship Check, Jarvis"
```

Defaults to `~/.whisper-models/ggml-large-v3-turbo-q5_0.bin` (the model
pick from the benchmark). Clean shutdown verified — no orphaned
`whisper-server` process after the context manager exits.

## Not yet built

The piece that ties L1's toggle events + this chunker + this daemon into
one running loop, and the client for gu2s6tnt's `deliver_transcript` /
`listen_for_cancel` handoff. Next.
