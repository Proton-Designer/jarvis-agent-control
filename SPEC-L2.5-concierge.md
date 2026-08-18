# L2.5 — The Concierge

## Why

Jarvis today has one path: transcript → orchestrator → reasoning → speech.
Every spoken word sits downstream of a full Claude reasoning turn, so the
measured loop is ~90s end to end, essentially all of it orchestrator
thinking (transport is ~20ms).

That is fine for dispatch and fatal for conversation. These are two jobs
with opposite latency budgets:

| Job | Budget | Why |
|-----|--------|-----|
| Dispatch — "tell the API gateway to run its tests" | 30–90s acceptable | The work itself takes minutes |
| Conversation — "what's it working on?" | **< 1s** | Past ~2s it stops feeling like a being |

Both currently share one path, so conversation inherits dispatch latency.
L2.5 fixes that by owning the conversation and treating L3 as something it
consults, not something the user waits on.

## Shape

```
voice → L1 wake → L2 transcribe → L2.5 CONCIERGE ──┬─→ answers directly (most turns, ~300ms)
                                                    └─→ dispatches to L3 (slow turns)
```

L2.5 sits between transcription and the orchestrator. It is the voice of
the system. L3 becomes a specialist it calls for hard routing.

Constraint, unchanged: **no third-party API keys.** Ollama (already
installed, `qwen2.5` already pulled), Whisper, and macOS `say` are all
local and free.

## Intent classes

The concierge classifies every transcript into exactly one:

| Class | Examples | Handling | Latency |
|-------|----------|----------|---------|
| `CONTROL` | "cancel", "never mind", "repeat that", "stop" | Deterministic, no model | < 50ms |
| `QUERY` | "what's running?", "what's the gateway doing?", "how much have I spent?" | Deterministic data + local phrasing | < 500ms |
| `CHAT` | conversational, no action implied | Local model | < 800ms |
| `DISPATCH` | "tell X to do Y", any instruction for an agent | Forward to L3 | unchanged |
| `UNSURE` | anything else | **Forward to L3** | unchanged |

### The CHAT speech gate

A CHAT-classified transcript is not answered unconditionally. It is
answered when `assess_addressed()` does **not** verdict `AMBIENT` — i.e.
on `ADDRESSED` or `UNSURE`, and never on the imperative short-circuit
(`verdict=None`). CHAT never forwards to L3 either way.

The first build of this suppressed CHAT entirely, keying on the label.
That was wrong, and it failed live on 2026-08-18: Ayman said *"What's
up? How's it going?"* straight at the microphone, the system ran
`assess_addressed()`, got `ADDRESSED` in 624ms, and stayed silent —
because the verdict was only ever consumed by the retention decision.
The right answer was computed and discarded.

The label answers *"is this small talk?"*. The gate needed *"was this
said to me?"* — which is what `assess_addressed()` already answered.

**Two consumers, two thresholds, one model call.** `assess_addressed()`
returns three-way evidence and each consumer sets its own bar from its
own cost asymmetry:

| Decision | Bar | Why |
|---|---|---|
| Retention (discard) | only on `AMBIENT` | A wrong discard is irreversible — no artifact left to diagnose from |
| Speech (stay silent) | only on `AMBIENT` | A wrong sentence is one recoverable utterance; a wrong silence is indistinguishable from a crash |

They coincide on `AMBIENT` and diverge in what the default is on either
side of it. `UNSURE` must speak: *"Hello. How are you doing?"* — Ayman's
own example — verdicts `UNSURE` every time, and a gate requiring
`ADDRESSED` would have stayed mute on the exact sentence this exists to
answer.

**What still protects the original concern.** Reaching the gate at all
required `daemon.py`'s `verify_wake_trigger()` to re-transcribe the
trigger audio and positively confirm *"hey jarvis"* was spoken — a
rejected trigger never enters CAPTURING, so no transcript exists. False
acoustic fires are filtered upstream. `AMBIENT` then catches the
residual case verification cannot: the name really was said, but *about*
Jarvis rather than *to* it.

Guarded by `l2_5_concierge/chat_gate_canary.py`, which asserts on
direction (spoke / stayed silent), never on wording.

### CHAT gets facts

`PHRASE_SYSTEM_CHAT` used to be told it had no state access at all,
which made a Jarvis that could not answer *"how's it going"* with
anything real. It now receives the same code-computed fact string QUERY
does (`concierge._chat_facts()` — session list, dispatch status). The
anti-fabrication rule (requirement 2) is unchanged: it was never *"the
model must know nothing"*, it is *"the model must not be the SOURCE of a
fact."*

CHAT is also the **one** path where a transcript reaches a model prompt,
via `phrase_chat_reply()` — deliberately its own function, so
`phrase_answer()` (QUERY) stays structurally incapable of seeing one and
asserts `kind == "QUERY"`. A reply to a greeting has to have heard the
greeting; the alternative, tried first, was a status readout wearing a
conversation's clothes. See that function's docstring for the three
upstream gates that make it safe and the residual risk it does not
close.

A provider failure must produce *"unknown"*, never *"nothing"* — an
empty session list and an unreachable tmux look identical from the
model's side, and reporting the second as the first is this project's
recurring silence-read-as-success failure.

This table is v1's five, as implemented today (`l2_5_concierge/classifier.py`).
A sixth class, `ANSWER`, is planned for a later stage of blocked-session
handling — see `docs/SPEC-blockers.md` SS5.1: a transcript answering a
currently-pending question from a blocked session, routed back to that
session rather than classified as a new instruction. Not yet built —
stage 1 of blocked-session detection (surface + escalate by voice) is
live; the auto-answer path that needs `ANSWER` is deliberately unbuilt.

### Governing rule: classification fails toward DISPATCH

A dispatch misclassified as chat means **the instruction never happens and
nobody is told** — silent loss, the failure class this project has
repeatedly found and repeatedly ruled against. A query misclassified as
dispatch is merely slow, which is exactly what we have today.

So: when confidence is low, forward. Never answer conversationally if
there is any chance an instruction was intended. This is the same
asymmetry that governs the rest of the system (see the start-fails-closed
/ stop-fails-open rule in `l1_wakeword/README.md`).

## Hard requirements

1. **Deterministic before model.** "What sessions are running?" is
   `tmux list-sessions`, not an inference. The model may phrase an answer;
   it must never be the source of a fact.

2. **The local model must not fabricate state.** It answers only from
   facts passed to it in the prompt. No fact available → say so, or
   forward to L3. A hallucinated session state is worse than a slow one:
   it is confidently wrong about the user's real work.

3. **All speech routes through `say_feedback.speak()`.** Not a second TTS
   path. `JARVIS_MUTE` must govern the concierge exactly as it governs
   everything else, and every utterance must land in `say_log.jsonl`.

4. **Dispatch-in-flight state.** The concierge knows when L3 is working
   and can answer "is it done yet?" / "what's it doing?" without waiting.
   This is most of the felt improvement: the user is never standing in
   silence wondering whether the system heard them.

5. **Never blocks on L3.** Forwarding to the orchestrator is fire-and-
   continue. The concierge stays responsive while L3 thinks.

## Latency budget

End of speech → first audio out:

| Stage | Target |
|-------|--------|
| Whisper (already measured) | ~500ms |
| Classification | < 150ms |
| Deterministic answer | < 50ms |
| Local model (when used) | < 400ms |
| `say` startup | ~100ms |
| **Total, fast path** | **< 800ms** |

Measure it; do not estimate it. Log to `~/.jarvis/latency_log.jsonl`
alongside the existing dispatch instrumentation.

## Out of scope for v1

- Barge-in (interrupting speech mid-sentence). Wanted, needs audio duplex
  work, separate phase.
- Replacing or altering the wake-word flow.
- Any change to L3 routing logic or L4 delivery gates.
- Continuous listening without a wake word.

## Cheap wins, shipped alongside

Independent of the concierge, all small:

1. **Voice quality.** `say` defaults to a robotic voice. macOS ships
   premium voices — evaluate and pick one. Largest perceived-quality gain
   per unit of work anywhere in this project.
2. **Incremental plan narration.** L3 currently reasons about every
   instruction, then speaks. Narrate each resolution as it lands.
3. **Kill a round-trip.** Pre-inject the live session list into the
   pointer so L3 does not spend a full model turn on `list_sessions`.

## Open question for the build

Is the classifier a local model call, or keyword/regex first with the
model as fallback? Regex is faster and predictable but brittle on natural
speech. Recommendation: try tiered — high-confidence keyword match wins
immediately, everything else goes to the local model, and the model is
told to prefer DISPATCH when unsure. Measure the classification accuracy
against real transcripts before committing.
