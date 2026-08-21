# Conversation mode — design

Ayman, 2026-08-21. Approved: Approach A (five-state turn-taking machine)
with speaker verification in the first build.

## 0. The problem, stated exactly

Today every single utterance costs a wake word. `IDLE --"hey jarvis"-->
CAPTURING --2.7s silence--> send --> IDLE`. That is correct for issuing
one instruction and wrong for having a conversation: a three-turn
exchange costs three wake words, and saying "hey Jarvis" before every
reply is not something a person does when talking to someone.

The naive fix — stop returning to IDLE — is wrong for three reasons this
design has to answer rather than inherit.

**It makes Jarvis talk to itself.** Nothing in the codebase coordinates
speaking with listening. `say_feedback` does not expose whether the
Kokoro worker is playing, and `daemon.py` never asks. That is safe today
only by accident: the mic is IDLE while Jarvis replies, and the only
thing listening is the wake model, which does not fire on "Doing well,
ready to help." Keep the session open and Whisper transcribes Jarvis's
own voice as the next turn, forever.

**It taxes every other subsystem.** `return_queue.ready_to_flush()` reads
`wake_state.json` and treats `CAPTURING` as "he is mid-sentence, do not
interrupt." If `CAPTURING` becomes the resting state, every agent
completion is held to `MAX_HOLD_S = 12.0`, every time. A feature meant to
feel faster would make the rest of the system measurably slower.

**It removes the only guard on dispatch.** `"hey jarvis"` is not just a
trigger, it is an authorisation boundary: audio not preceded by it cannot
reach an agent. An always-open mic means a remark to someone else in the
room can be classified as an instruction and delivered to a session with
write access.

## 1. Shape of the solution

Turn-taking, not "don't close the session."

The conversation is a sequence of **turns**. A turn ends the way it does
today (2.7s silence, or `"that's it"`). What changes is what happens
after: instead of returning to IDLE, Jarvis answers and then **opens the
floor** for 8 seconds. Speak inside it and you have another turn with no
wake word. Stay quiet and the conversation is over, silently.

That is how conversations actually end between people — not with a
closing phrase, but by nobody taking the next turn.

### 1.1 The load-bearing detail

**The floor opens when Jarvis stops SPEAKING, not when the turn is
sent.**

If it opened at send time, a dispatch would eat it: you say "tell gateway
to run the integration tests", the concierge takes ~2.8s to answer, and
the floor would be a third gone before you heard a word. Opening it at
end-of-speech means the 8 seconds are always 8 seconds of *your* time.

It also composes correctly with the slow path. A dispatch gets a fast
"passing that on", the floor opens, and you can immediately add "also
tell billing to deploy" while the router is still thinking. The router's
eventual completion arrives later as a batched `completion`, which is
exactly what that class is for.

## 2. States

Three today (`IDLE`, `CAPTURING`, `CANCEL_ARMED`). Five after.

| State | Meaning | Mic |
|-------|---------|-----|
| `IDLE` | Waiting for the wake word. Unchanged. | wake model only |
| `CAPTURING` | Buffering a turn. **Semantics unchanged** — still means "he is mid-sentence." | full |
| `REPLYING` | Turn sent. Jarvis is thinking or speaking. | barge-in only |
| `FLOOR_OPEN` | Jarvis has finished. 8s to take the next turn with no wake word. | full |
| `CANCEL_ARMED` | Unchanged. | cancel ensemble |

`CAPTURING` keeping its exact current meaning is deliberate and is what
keeps `return_queue`'s gate correct with no change to that module.

### 2.1 Transitions

```
IDLE          --wake verified-->                      CAPTURING
CAPTURING     --2.7s silence (10s if no word yet)-->  send turn -> REPLYING
CAPTURING     --"that's it" WITH content-->           send turn, closing=True -> REPLYING
CAPTURING     --"that's it" ALONE-->                  IDLE   (no send, no reply)
REPLYING      --Jarvis speech ends, closing-->        IDLE
REPLYING      --Jarvis speech ends-->                 FLOOR_OPEN
REPLYING      --REPLY_WAIT_MAX_S with no speech-->    FLOOR_OPEN   (fail toward open)
FLOOR_OPEN    --speech onset, speaker verified-->     CAPTURING
FLOOR_OPEN    --Jarvis speaks again-->                REPLYING     (floor resets after)
FLOOR_OPEN    --FLOOR_S elapsed-->                    IDLE         (silent close)
```

Ayman's three ending rules, as specified, map onto rows 2-4 and 8.

### 2.2 Constants

```python
FLOOR_S           = 8.0    # Ayman's number
REPLY_WAIT_MAX_S  = 10.0   # REPLYING cannot hang if Jarvis never speaks
```

`SILENCE_SAFETY_NET_S = 2.7`, `SILENCE_HOLD_S = 50.0` and
`STOP_PHRASE_VARIANTS` are unchanged and keep working inside a turn.

`SILENCE_OPENING_GRACE_S = 10.0` applies **only to wake-initiated turns**.
A floor-initiated turn begins because speech was already detected, so
there is no "he has not started yet" window to protect. Applying it there
would hold an open mic for 10s after any stray sound.

## 3. Knowing when Jarvis stopped speaking

The daemon (L1) and the speech worker are **different processes** —
`jarvis_say` runs inside an MCP server, not the daemon. L1 cannot observe
it directly.

`say_feedback`'s worker writes `~/.jarvis/speaking_state.json`:

```json
{"speaking": true, "started_at": 1787326033.4, "updated_at": 1787326034.1}
```

Same pattern and same discipline as `wake_state.json`: written at ~10Hz
while playing, and **treated as not-speaking if `updated_at` is older
than 1.0s**. A speech worker that dies mid-utterance must not strand the
daemon in `REPLYING` forever — the identical failure Opus Lead 3 found in
`return_queue`'s CAPTURING gate, and it must not be re-introduced one
layer up.

`REPLY_WAIT_MAX_S` is the second, unconditional backstop: if nothing has
been spoken at all within 10s, the floor opens anyway. This matters
because there is a known live bug where a dispatch produces **no speech
at all** — waiting on an end-of-speech that never comes would silently
kill the conversation.

Both mechanisms fail toward opening the floor. Never toward hanging.

## 4. Speaker verification

Approved for the first build. It is not primarily "only listen to Ayman"
— it is what lets Jarvis **ignore itself**, and that is what makes
barge-in possible on laptop speakers.

### 4.1 Device detection is NOT part of this design

An earlier draft proposed detecting the macOS output device and running
full-duplex on headphones, half-duplex on speakers. **Rejected.** The
speaker filter rejects Jarvis's voice whether it arrives through the air
or not at all; on headphones it simply never has anything to reject. One
mechanism covers both days, and the device-detection subsystem — with its
polling, its caching, and its own staleness bugs — does not get built.

### 4.2 Model

ONNX speaker-embedding model in `l1_wakeword/models/`, fetched by the
existing `fetch_models.py`.

This is forced, not chosen: the whole L1 stack is `onnxruntime` and there
is **no torch in `l1_wakeword/.venv`**. That rules out Resemblyzer and
points at a WeSpeaker / ECAPA-TDNN ONNX export — 192-256 dim embedding
from 16kHz mono, a few ms on CPU, feeding off the same `frame_i16` stream
Silero VAD already consumes. Exact model pinned during implementation
against a measured accept/reject margin, the same way Kokoro's voice was.

### 4.3 It gates turn STARTS, not words

The single most important decision here.

A per-chunk filter that drops mid-sentence audio would delete half an
instruction and send the rest — this project's signature failure, and
strictly worse than the problem it solves.

So: **speaker verification decides whether a turn may START.** Once
`CAPTURING` begins, every chunk is kept. Stray room conversation can
never open a turn; a stray word during your sentence is harmless.

One narrow exception: **Jarvis-matched chunks are dropped inside
`CAPTURING` too**, because barging in overlaps the two voices. That
exception is bounded to a known fingerprint, not a general filter.

### 4.4 Two prints, not one

`~/.jarvis/voiceprint.json`

```json
{"version": 1, "model": "<name>", "dim": 192, "created_at": "...",
 "ayman": {"centroid": [...], "n_windows": 42, "accept_threshold": 0.62},
 "jarvis": {"centroid": [...], "voice": "bm_lewis", "reject_threshold": 0.55}}
```

Decision per candidate chunk:

```
sim_j = cos(emb, jarvis.centroid)
sim_a = cos(emb, ayman.centroid)

if sim_j >= reject_threshold and sim_j > sim_a:   drop   (echo)
elif sim_a >= accept_threshold:                    accept
else:                                              drop   (other speaker)
```

Barge-in — starting a turn while Jarvis is mid-sentence — requires a
**positive** Ayman match at a raised threshold. Ordinary floor turns only
require "not obviously someone else." Proof is demanded exactly where the
cost of being wrong is a feedback loop.

### 4.5 The voice-change trap

`jarvis.centroid` is a fingerprint of a **specific Kokoro voice**. Ayman
changed `bm_george -> bm_lewis` today. A voice change silently
invalidates echo rejection.

So the print records the voice name, and on startup the daemon compares
it against the configured Kokoro voice. On mismatch it **refuses
full-duplex, falls back to half-duplex (mic deafened while Jarvis
speaks), and says so out loud.** Degraded and audible, never silently
broken. Re-enrolling Jarvis's half is automatic and needs no human — it
is synthesis, not recording.

### 4.6 Onset latency, and never losing the first word

Verification needs roughly a second of audio; a turn starts the instant
VAD detects speech. Those cannot both be true unless buffering is
explicit.

**Capture starts at VAD onset, unconditionally.** Audio buffers from the
first frame while verification runs on the leading ~1s. If it verifies,
the buffered audio is already there and nothing was lost. If it does not,
the buffer is discarded and the state returns to `FLOOR_OPEN` with the
remaining time intact.

This is the only ordering that cannot truncate him. Verifying first and
capturing second would clip the first word of every turn, which is the
§4.3 failure in a different costume.

**Utterances too short to verify are ACCEPTED.** "Yes", "no", "stop" may
not carry enough signal for a reliable embedding. During `FLOOR_OPEN` the
overwhelmingly likely speaker is the person who was just talking, so a
sub-threshold-length chunk fails toward him. The floor being open at all
is the guard; the model is not asked to carry a decision it cannot make.

The one place this does not apply: barge-in during `REPLYING` still
demands a positive match regardless of length. A short chunk that cannot
be verified there is more likely Jarvis's own tail than a barge-in.

### 4.7 Reading the configured voice

The daemon must know which Kokoro voice is configured in order to detect
the §4.5 mismatch. That is L1 reading an L4 value. It reads the voice
**name only**, at startup, and treats an unreadable value as a mismatch
(refuse full-duplex, say so) rather than as a match.

## 5. Enrollment

One command, `l1_wakeword/enroll_voice.py`, which:

1. Prints the paragraph in §5.1 and waits for Enter.
2. Records until Ayman stops, showing a live level meter (reuses the
   existing meter path).
3. Rejects the take and asks again if it is too short, too quiet, or
   clipped. Better to re-read than to build the system's identity check
   on a bad recording.
4. Windows the audio (3s windows, 1.5s hop), embeds each, takes the mean
   as the centroid.
5. **Calibrates the threshold rather than hardcoding it** — computes
   intra-speaker similarity across his own windows and sets
   `accept_threshold` below that spread.
6. Synthesises the same paragraph with Kokoro at the configured voice,
   embeds it identically, stores it as the Jarvis print.
7. Reports the measured margin between the two. **If the margin is thin,
   it says so and refuses to enable full-duplex** — a number Ayman sees,
   not a promise.

### 5.1 The paragraph

Written for this system specifically, not generic phonetic filler. It
covers every English phoneme, and it deliberately contains the exact
operational phrases and vocabulary the daemon has to match on, so the
print is strongest where it is used most. Read at a normal pace in a
normal voice — about ninety seconds.

> Hey Jarvis, it's Ayman. I'm recording this so you can learn the sound
> of my voice and tell it apart from everyone else's. Let me talk for a
> while so you have plenty to work with.
>
> Here's how a normal day sounds. I might ask you what's running right
> now, or how much I've spent today, or which sessions are still waiting
> on me. Sometimes I'll give you real work: tell the gateway team to run
> the integration tests and report back, or ask billing to deploy the new
> pricing changes to staging before we touch production. Other times I'll
> just think out loud — hold up, wait, let me think about that for a
> second — and then finish the thought once I've got it straight.
>
> When I'm done with something, I'll say that's it, and you should stop
> listening and get out of the way.
>
> Now some odd words, because the more different sounds you hear the
> better you'll know me: orchestrator, concierge, authorization, jeopardy,
> extinguish, mischievous, thoroughly, quixotic, judgment, hemisphere,
> vulnerable, azure, treasure, laughter, sixth, twelfth, strengths.
>
> Zero, one, two, three, four, five, six, seven, eight, nine. The quick
> brown fox jumps over the lazy dog. She sells seashells by the shore,
> and the shells she sells are surely seashells.
>
> Would you check on that for me? Are you sure? What happened there? Why
> did it stop? That's the whole point — I want this to feel like talking
> to a person, not pressing a button and waiting.
>
> Okay. I think that's enough. That's it.

The final "that's it" is intentional: it is the phrase most likely to be
matched at the end of a chunk, so it is worth having a clean sample of it
in his own voice.

## 6. What this changes elsewhere

Everything computed here that Ayman is meant to act on **ships with its
render in the same change** — the standing rule from
`SPEC-gaps-and-build-plan.md` §3.

| Subsystem | Change |
|-----------|--------|
| `wake_state.json` | Gains `REPLYING` / `FLOOR_OPEN`. Console must render them, including a floor countdown. |
| `return_queue` | **No change.** `CAPTURING` still means mid-sentence. A completion arriving during `FLOOR_OPEN` may speak, which re-enters `REPLYING` and resets the floor — the conversation naturally extends. |
| `say_feedback` | Writes `speaking_state.json` from the worker. |
| `instant_ack` | Unchanged. Still a fallback, still cross-process. |
| `CANCEL_ARMED` | May arm from `FLOOR_OPEN` (an idle-equivalent). Never from `CAPTURING` or `REPLYING`. |
| Console | New states, floor countdown, and a voiceprint status line (enrolled / stale voice / not enrolled). |

## 7. Failure directions, decided in advance

Each of these is a case where the two errors are not symmetric, so the
design commits to one.

- **Floor open too long vs. too short** — too short drops him mid-thought
  and costs a wake word; too long leaves a hot mic. 8s, his call, with
  `"that's it"` for instant close.
- **Speaker verification** — false reject deletes his words, false accept
  admits a stray sentence. Gate turn *starts* only (§4.3), so a false
  reject costs a wake word and can never truncate an instruction.
- **Barge-in** — a missed barge-in means talking over Jarvis; a false one
  means Jarvis hears itself. Raised threshold, because the feedback loop
  is unrecoverable and the missed barge-in is not.
- **Stale `speaking_state.json`** — hanging in `REPLYING` kills the
  conversation silently. Fails toward opening the floor, always.
- **Voice change invalidating the Jarvis print** — degrade to half-duplex
  **audibly**, never silently continue with a broken echo filter.

## 8. Testing

Standing requirements from `docs/TODO-feature-queue.md` apply: both
directions on every property, driven live, ships with its render, tests
isolate their own state.

- `conversation_mode_canary.py` — every transition in §2.1 **and its
  negation**: the floor closes at 8s AND stays open at 7.9s; `"that's
  it"` alone sends nothing AND with content sends; `REPLYING` opens the
  floor on stale `speaking_state.json` AND holds on a fresh one.
- `speaker_verify_canary.py` — must assert **both**: Ayman's enrollment
  audio accepts, and Kokoro `bm_lewis` audio rejects. A test that only
  checks acceptance passes just as happily if everything is accepted,
  which is the feedback loop.
- Voice-change test: a `voiceprint.json` naming a different voice refuses
  full-duplex and says so.
- Live drive, both duplex situations: headphones and laptop speakers.

## 9. Deliberately not in this build

- **Continuous streaming with semantic endpointing** (Approach C) — what
  makes turn-taking feel instant rather than timed. Approach A is a
  strict subset, so nothing here is wasted if it is built later.
- **Multi-speaker enrollment** — one enrolled speaker only.
- **Output-device detection** — rejected in §4.1, recorded so nobody
  rediscovers it as an oversight.
