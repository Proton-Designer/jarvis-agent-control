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

## daemon.py — the state machine

Ties the wake-word listener, the VAD chunker, and the whisper daemon into
one running loop. One process, one mic, three mutually exclusive states:
`IDLE`, `CAPTURING`, `CANCEL_ARMED`.

**Stated invariant, per the Lead's ruling that this needed to be
verifiable by reading, not inferred from code structure:**
**"Hey Jarvis" has exactly one meaning at any moment, determined by
system state — never two.** This is implemented as **exclusive claim**,
not parallel-with-suppression: there is one state variable and one
detector consumer at a time. `CANCEL_ARMED` is only entered via L4's
`listen_for_cancel` RPC, which only happens after a dictation has already
been handed off and the daemon is back in `IDLE` — so a "Hey Jarvis"
during a cancel window cannot also be interpreted as "start a new
dictation," because there is no code path evaluating that interpretation
while `CANCEL_ARMED` is active. Boundary case (a detection arriving right
as the window's timeout expires) resolves toward cancel, per the same
asymmetry as the threshold decision below — the RPC handler reports
whatever the detector said before the timeout fired, not "whichever
happened to run last."

**Two detection thresholds, not one, and why:** `IDLE_THRESHOLD` (0.5) for
starting a dictation, `RECOVERABLE_THRESHOLD` (0.3) for *ending* one
(post-guard stop-toggle) and for the cancel window. Both of the lowered
cases share the same logic: a false positive is cheap (say the wake word
again / re-dictate), a false negative is the failure being guarded
against (a stuck-open mic / an undetected cancel). This isn't
speculative — a real synthetic test clip's closing "Hey Jarvis." peaked
at 0.348, comfortably below 0.5 but above 0.3, meaning the un-lowered
threshold would have missed the stop trigger entirely and run the
dictation to the (also real, separately tested) 50s silence safety net
instead of ending when Ayman actually stopped talking.

**Known limitation, tested and documented rather than silently assumed
away:** when the stop wake word fires, whatever audio is still buffered
(not yet cut into a chunk by the VAD) is discarded, not transcribed —
otherwise "Hey Jarvis" itself shows up verbatim at the end of the
transcript (this happened in testing before the fix). This is correct
when Ayman pauses before the closing phrase, which the VAD will already
have cut as its own chunk boundary by then. It is **not** correct if he
says real content immediately before "Hey Jarvis" with no pause — that
content would be in the same undischarged buffer and lost. The
pause-before-stop case is tested; the no-pause case is not, and belongs
in real-voice validation.

**Bug found and fixed during this build, logged here because it's the
kind of thing worth being able to point to later:** an earlier version
re-ran the VAD chunker over the *entire* accumulated buffer on every new
frame, using the same stateful `SileroVAD` instance whose LSTM hidden
state had already advanced past audio it was now being asked to re-score
from the start. Produced garbage transcripts ("So", "Aw!", "Hey!"
instead of real sentences) — caught by testing against a real
multi-sentence clip, not by inspection. Fixed by extracting
`StreamingChunker` (`../l2_transcription/vad_chunker.py`), which
enforces "the VAD advances exactly once per frame, ever" as a class
invariant rather than a convention callers have to remember. **This bug
was contained entirely to new, previously-unreported daemon.py
integration code — it never touched `whisper-cli`/`whisper-server`
directly, so it does not affect the benchmark numbers, the q5_0 model
pick, or the warm-latency measurements already reported.** Those used
separate, already-tested code paths.

A second, related bug from the same root cause (real-vs-simulated time):
the 3-second start guard used `time.monotonic()` internally, which is
wall-clock time — harmless live, but meaningless in `--simulate` mode,
which processes 10+ seconds of audio in a fraction of a real second, so
the guard never actually lifted during a simulated run. Fixed by making
`DictationSession` take an explicit `now` parameter instead of sampling
the clock itself, so simulated-time and wall-clock-time callers get
identical behavior.

```
.venv/bin/python daemon.py --simulate clip.wav     # drives the full state machine over a pre-recorded file
```

**Not yet live-mic tested, and won't be without Ayman's explicit
go-ahead and presence** — per the standing rule established after an
unrelated incident during this build (a different component's test
produced audible TTS output unexpectedly): no unattended microphone
capture, ever, live-mic testing only while actively watched and stopped
when watching stops. `daemon.py`'s live-mic branch currently just prints
that it isn't wired up and exits — this is deliberate, not an oversight.
The cancel-socket server (`~/.jarvis/l1.sock`) is scaffolded
(`cancel_socket_server()`) but its actual detection logic is a documented
placeholder for the same reason.
