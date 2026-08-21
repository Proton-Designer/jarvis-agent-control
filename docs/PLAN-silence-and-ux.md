# Plan — the silence bug, and Ayman's UX list

Written 2026-08-20 after four failed fix attempts and a three-way
independent investigation.

Status: `TODO` · `IN PROGRESS` · `BUILT` · `VERIFIED` (canary + observed live) · `DONE`

---

## 0. Root cause — converged, proven three ways

Three investigators reached the same mechanism independently.

**Ayman says "how are you doing". The concierge answers correctly within
3 seconds. He hears nothing for ~3 minutes, then a merged blob of stale
replies.**

The concierge is not at fault. Its transcript shows `Read` → `jarvis_say`
every time. The speech path is not broken either — it works once
triggered.

**The flusher was running code from before the fix.**

```
concierge MCP server (server_readonly.py) started   16:04:31
return_queue.py fix written                         16:07:36
```

Python binds a module on first import and never re-reads it. Nothing
calls `importlib.reload()`. So that server has run the pre-fix flush gate
for its entire life — and so has the router's server, started 15:12:21.

### Why it took exactly ~3 minutes

The old gate was `dispatch_state.any_forwarded()`.
`DISPATCH_ABANDONED_AFTER_S = 180.0`. The gate did not clear because
Ayman stopped talking; it cleared because an unrelated abandonment timer
aged the stuck record out. Engineer 1 measured **174.93s** between
enqueue and flush — a match to the constant, not a coincidence.

### Proof it was old code, not a coincidence

Opus Lead 3's evidence is the one that settles it. `abandoned_at` is
written only by `_heal_and_maybe_write()`, reachable only via
`any_forwarded()`:

```
abandoned_at  1787260076.223391
flushed       1787260076.225597   → +2.21 ms
abandoned_at  1787260886.737196
flushed       1787260886.739281   → +2.09 ms
```

Two milliseconds, twice. That is one synchronous call chain, not two
processes coinciding. **The new gate never imports `dispatch_state` at
all.**

### And the gate was unsatisfiable, not merely wrong

`mark_dispatch_complete()` lives in `tools_write.py`, which
`server_readonly.py` deliberately never imports. The concierge has **no
reachable code path** that can close a dispatch. Every conversational
utterance opened one that only the 180s heal could ever close.

### What was disproved

My guess that "some other process flushed it" is **false**. The console
imports `return_queue` but only calls `pending()`, which never starts a
worker; canaries are isolated; the console started after the first flush.
One process, old code, start to finish.

---

## 1. Fixes, in order of what Ayman still feels

### R1 — Restart the servers and OBSERVE the fix work · Lead

Restarting is **not** the fix. It is satisfying a precondition the design
silently requires and never states. Nobody has yet seen `d22d206` execute
against a live utterance.

Done when `latency_log` shows `enqueued → flushed` at ~2.5s, not 180s.
Until that line exists, fix #4 is unverified, not proven.

### R2 — A direct answer must not be batched · Lead — **BUILT**, awaiting live drive

`KINDS` was closed: `blocked_question | error | completion`. There was no
kind meaning *"I am answering the question you just asked."* So the
concierge's only expressible option routed a live conversational reply
into a queue built for asynchronous agent completions.

The harm is already in the log, independent of the stale process:

```
16:21:26  "Still good. Good. I'm here."
          → two answers to two separate askings, merged
15:48:49  "Hey Ayman. Concierge is ready. Hey Ayman, I'm listening. …"
          → four greetings from four exchanges, one blob
```

Restarting fixes the silence. It does not fix this. **Even with the new
code running perfectly, a direct answer waits `SETTLE_S` and is eligible
to merge with an unrelated completion.** Wrong by design, not by bug.

#### What it cost, measured before the fix

Engineer 1, 2026-08-21, from `latency_log.jsonl` and `say_log.jsonl`:
**18 of 19** conversational replies were typed `completion`. Reply
latency therefore ran ~5s from dictation end — the settle delay plus the
flush poll — against `instant_ack`'s `ACK_FALLBACK_AFTER_S = 3.0`. The
ack fired **7 times against 1 suppression**, an 87.5% rate on a mechanism
built to be rare. That is the "Okay, one sec before every single reply"
Ayman reported, and it was a symptom of this item, not of the ack.

The threshold was deliberately **not** touched. 3.0s is correct; the 5s
was the defect. Lowering it would have hidden the cause and made the ack
fire on a path about to become fast.

#### The fix

`answer` joins `KINDS` at `PRIORITY_HIGH`, absent from `return_queue`'s
`_TIER` so it can never be batched, and the concierge prompt now teaches
the distinction at all three points where it decides what to say:
`answer` for a reply Ayman is waiting on, `completion` only for news he
did not ask for, and **`answer` when both look like they fit** — a report
spoken a moment early costs nothing, a reply held costs the reason the
layer exists.

Canary asserts both directions, including the one that actually bit: an
answer arriving while a completion is already queued must overtake it and
leave it queued, not join it into one blob.

**Not done until driven live** — a prompt change reaches nothing until
the role session is relaunched (§4). Verified means `latency_log` shows
`kind="answer"`, `batched=false`, and `instant_ack_suppressed`.

### R3 — One flusher, enforced across processes · Engineer 1

`_ensure_worker()` starts a thread in whatever process calls `enqueue()`.
`jarvis_say` is on **both** surfaces, so both MCP servers can hold a
worker over one shared file, with only a per-process `threading.RLock`
between them. `flush_now()` does read → speak → re-read → write, so two
workers can speak the same batch twice or interleave and drop items.

Not the cause of today's bug. Live in the code right now.

### R4 — The new gate trusts a possibly-dead daemon · Engineer 1

`ready_to_flush()` reads `wake_state.json` and trusts `state ==
"CAPTURING"` with no staleness check. Every other reader in this codebase
refuses to do that. A crash mid-capture leaves `CAPTURING` on disk
permanently, and every batchable message then waits the full
`MAX_HOLD_S`, forever, with nothing saying why.

The backstop prevents silence — which is what it is for — but the gate is
one line from being a permanent tax with no diagnosis.

### R5 — Surface "this role is older than your code" · Engineer 2

The console showed every role green throughout. Nothing anywhere said
"running code from before your last fix." That invisibility cost four fix
attempts and three engineers an afternoon.

Coarse by design: compare the MCP server child's start time against the
newest mtime in `l4_controller/`. Over-flagging is the safe direction — a
false "might be stale" costs one no-op Activate; a false negative costs
an afternoon. Advisory tier, below blocked/prompt_pending. Activate
already respawns the server (confirmed: it is a child of the claude
process), so the fix action exists.

---

## 2. Ayman's UX list · Engineer 2

### U1 — The mic meter shows `?????` on every start

`meter.py:57-67` renders `?` for both "no data" and "stale". Right after
pressing start, no data is **expected** — the daemon has not written
`wake_state.json` yet. Rendering the same alarm glyph for "starting
normally" and "the meter has died" makes the real signal meaningless.

### U2 — Buttons take seconds with no feedback

Show a pressed/working state immediately, then optimise what is slow.
Some of these do real work (spawning sessions); the fix is honest
feedback first, speed second.

### U3 — Button text stays highlighted after clicking

Textual focus styling persisting after the action. Cosmetic, cheap.

---

## 3. Design question · Opus Lead 3

**Inject the transcript directly instead of a file pointer?**

Today the daemon writes `~/.jarvis/dictations/<ts>.txt` and sends a
pointer. Ayman asks whether to paste the text itself.

Evaluate, do not implement: token cost of inlining versus a pointer; loss
of message-bar features; whether temp files are a real burden or a real
audit trail; readability of the pane; what happens to a long dictation;
whether the pointer's session-list preamble is doing work the text alone
would not. Come back with a recommendation, not a survey.

---

## 4. Standing rule this incident earns

**A fix to code an MCP server imports is not live until that server
restarts.** Editing the file changes nothing for a running process. Any
fix in `l4_controller/` must state how it reaches the running roles, or
it is a change that has not happened yet.
