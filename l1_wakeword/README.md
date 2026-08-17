# L1 — wake-word listener

Continuous "Hey Jarvis" detection via [openWakeWord](https://github.com/dscripka/openWakeWord).

## Why openWakeWord, not Porcupine

Porcupine (Picovoice) was the originally-planned engine. Checked live before
installing anything: Picovoice discontinued their Free Tier, existing Free
Tier AccessKeys stop authenticating June 30 2026, and there is no
non-commercial tier planned (their own stated reason: "focusing on our core
business, enterprise deployments"). The SDK will not run at all without a
valid key. That is a dead option for a personal project, not a
soon-to-expire one — by the time this was checked (Aug 2026) it already was.

openWakeWord is Apache-licensed, fully local, no account/key/network call,
and ships a pretrained `hey_jarvis_v0.1` model out of the box — matches our
wake word without custom training. One license note: the *pretrained model
weights* (not the code) are CC-BY-NC-SA 4.0 due to training-data provenance.
Fine for this non-commercial single-user tool; revisit if that ever changes.

## Setup

```
uv venv .venv --python 3.13
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python fetch_models.py     # downloads hey_jarvis + preprocessor models into ./models
```

## Usage

```
.venv/bin/python listener.py                    # live mic, prints one JSON line per detection
.venv/bin/python listener.py --file clip.wav     # offline scoring of a 16kHz mono wav
```

## Measured false-trigger behavior (synthetic `say` audio, small sample — real-voice validation still pending)

The model requires the *cadence* of "hey [name]", not just the word "Jarvis":

- Saying "Jarvis" alone, mid-sentence, with no "hey" — **no detection**, tested
  across 5 varied phrases including ones that talk *about* the Jarvis system
  ("I think Jarvis needs a better wake word detector"). This matters because
  Ayman is expected to reference the system by name during long dictations.
- "Hey Jarvis" fires reliably (0.87–0.999 confidence) and the model **does
  re-fire on a second "hey jarvis" later in the same continuous stream**,
  with score dropping back below threshold in between — supports the
  toggle start/stop design (same keyword twice), pending real debounce logic
  in the state machine that consumes these events.
- **Real false-positive found:** "Hey Travis, can you check on that?" fires
  the hey_jarvis model at 0.82–0.99 confidence. The model is keying on the
  "hey + name" phonetic pattern more loosely than just "Jarvis" specifically.
  This is a genuine risk, not a theoretical one — needs to be part of the
  real-voice validation pass, and if it holds up, is a reason to reconsider
  same-keyword toggle vs. a second distinct stop phrase.

Every wake word/melspectrogram/embedding model is `*.onnx` and gitignored —
`fetch_models.py` is how to reproduce `./models` from a fresh checkout.
