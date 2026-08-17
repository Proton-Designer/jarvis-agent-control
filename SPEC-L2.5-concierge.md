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
