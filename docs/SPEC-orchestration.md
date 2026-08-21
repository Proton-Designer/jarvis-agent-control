# Two-tier orchestration — Haiku concierge + Sonnet router

Supersedes `SPEC-L2.5-concierge.md`'s local-model layer, which was
disconnected 2026-08-18 (`05dac86`) and whose models are deleted.

## Why this shape

L2.5's only advantage over a Claude session was speed. A Haiku session
measured ~2s against our local path's 1.4–2.1s. At parity on the one axis
it won on, every other axis favours Haiku: tools, arithmetic, and
conversation memory across turns — which the local model had **none** of,
so "what about the other one?" was meaningless to it.

The split is not about intelligence. It is about **who is allowed to be
slow**:

| Tier | Budget | Job |
|---|---|---|
| **Haiku concierge** | never blocks | Answers Ayman, or hands off and returns. All READ tools. |
| **Sonnet router** | no budget at all | Parses the monologue, matches to teams, delivers. All WRITE tools. |

Sonnet exists so Haiku stays free. A six-minute monologue naming five
teams is five routing decisions; if Haiku made them it would be busy for
seconds. Handing the whole transcript over in one call keeps it free.

**Governing principle: the free layer gets read tools, the slow layer gets
write tools.** Reads are fast and safe. Writes need thinking, and thinking
is slow, and that is fine because nothing waits on it.

## Decisions already made — do not relitigate

- **No ambient/addressed gate.** Ayman's call, 2026-08-18. Verification
  (`verify_wake_trigger`) already rejects false acoustic fires by
  re-transcribing, and nobody says "hey jarvis" mid-sentence by accident,
  so the gate covered only the narrow case of a real wake word followed by
  speech that wasn't for Jarvis. Not worth 238ms on every utterance.
- **Sonnet, not Opus,** for the router. Parse → match → formulate → send
  is well within Sonnet, and Opus is a waste for it.
- **One router, not a pool.** The bottleneck is a single reasoning pass
  over one monologue, which does not parallelize. A pool would fragment
  state ("is it done?" needs one place that knows) and add a routing
  problem of its own. Revisit only if compaction becomes frequent, and
  then partition **by project**, not by load.

---

# Phase 0 — Foundation. Build this first.

Both items below are artifacts of a system built around one routing
brain. Extending them to two tiers without fixing them first reproduces
this project's recurring bug class — **failure reading as success** — in
new places.

## 0.1 Key dispatch state by dictation (HIGH)

`dispatch_state.py` is a **single global slot**.
`mark_dispatch_complete()` matches on `stage == "forwarded"` alone and
never checks *which* dictation — even though `dictation_ref` is already in
the record.

Safe today only because one dictation is handled at a time.
**This architecture breaks that on purpose**: Haiku hands off #2 while
Sonnet still works #1. Then #2's forward overwrites #1's record, whichever
finishes first closes out the wrong one, and Ayman asks "is it done?" and
gets a confident **yes** about work still running.

**Required:** a dict of in-flight dispatches keyed by `dictation_ref` —
same shape `blocked_state.py` already uses keyed by session UUID.
`mark_dispatch_complete()` takes and matches a specific ref. Every spoken
confirmation is scoped to its own dictation, never to "the current one."

## 0.2 Split the MCP tool surface (CRITICAL)

`server.py` exposes all six tools on **one** server: `list_sessions`,
`session_activity`, `spend` (reads) and `report_dispatch_stage`,
`confirm_plan`, `deliver_batch` (writes).

So "Haiku gets read tools" **cannot be a prompt instruction.** If Haiku
connects to that server, `deliver_batch` is callable regardless of what
its instructions say. This project's standing rule is that safety lives at
the transport layer, not the prompt layer — the same reasoning behind the
`/config` and `/model` hard-blocks in `slash_guard.py`.

**Required:** two tool surfaces. A read-only one Haiku connects to, the
full one Sonnet connects to. Haiku must be *structurally incapable* of
dispatching, not merely instructed not to.

Existing guarantee to preserve: `slash_guard`'s hard-blocks and the
read-only/interactive view classification are enforced *inside*
`transport.deliver()`, independent of caller. That holds — **as long as
nobody adds a second, unguarded delivery path "because it's simpler."**

## 0.3 Serialize speech (HIGH)

`say_feedback.speak()` is a bare `subprocess.Popen(["say", ...])` — fire
and forget, no serialization. Safe when one thing spoke. Once Haiku,
Sonnet, and every team lead can speak, two `say` processes overlap into
one garbled sentence — **worse than silence, because Ayman can't tell it's
a bug.**

Found independently by both engineers, and it is the same problem
voicemode solves with its single-writer "conch" socket.

**Required:** one speech queue that every `speak()` call funnels through,
spoken in priority-then-submission order. Also serialize deliveries **per
target session** — `tmux send-keys` is not atomic against a concurrent
writer, so a `/btw` racing a queued instruction can interleave keystrokes
into one pane. That is payload corruption, not an ordering nuance.

---

# Phase 1 — The tiers

## 1.1 Instant acknowledgement (no model)

Between "that's it" and Haiku's ~2s reply there must be **no silence**.
A canned local phrase or tone fires at ~0ms, before Haiku is invoked.

Templated, not generated — there is no local model any more and this must
not become a reason to reintroduce one. Session names come from `tmux`, so
it can say something real without any inference.

## 1.2 Haiku concierge

- **Read-only MCP surface** (0.2). Conversation memory across turns.
- **Hard 1–2s client-side timeout**, then fall back to forwarding raw and
  unclassified. Fails toward dispatch, the direction ruled everywhere
  here.
- **Must not block the capture loop.** The concierge runs in-process today
  and blocks `daemon.py`. That was fine at 232ms against a local model. A
  network call with real tail latency stalling the microphone means Ayman
  cannot distinguish "not listening" from "heard me and ignoring me" —
  which defeats the one job this tier has.

**Read tools must return `models.py`'s existing shape** — `polled_at`,
`expected_interval`, `error` — not bare strings, so Haiku computes
"unknown" from `error is not None` instead of guessing. The
anti-fabrication rule is unchanged and absolute: **the model may phrase a
fact, never source one.**

Haiku's read tools hit the **same state layer the console uses**
(`l5_console/state/`), never a second poller. One source, many consumers —
this removes the two-pollers-disagree case by construction rather than
resolving it afterward.

### Conversation memory — what carries

**Carries:** recent referents ("it", "that one") within a bounded TTL, and
Ayman's own stated preferences as literal grounding for a later question.

**Never carries:** team or task state. Always re-poll. Recalling status
from memory is sourcing a fact.

**Staleness:** past the TTL, do not silently resolve "it" — ask which
session. Same hold-and-ask rule `SPEC-blockers.md` §5.4 already uses. This
is the anti-fabrication rule applied to *identity*: the model does not get
to guess **which session** either.

**Scope grounding to the open thread, not general recency.** A preference
stated for one task must not silently reapply to an unrelated later
dispatch just because it was recent.

## 1.3 Sonnet router

- **Full MCP surface.** No latency budget.
- **Team registry persisted to disk** (small JSON, same shape as
  `blocked_state.json`) — **not** conversation-memory-only, or a
  compaction wipes it silently and Sonnet mis-routes without knowing.
- **The registry has NO status/activity field.** Identity and routing
  only. Then "registry says idle, live poll says busy" cannot happen — not
  by convention, by construction. Liveness is always polled.

## 1.4 The handoff seam

`deliver_transcript()` today speaks "Got it, working on it" *before*
confirming delivery, but **does** check `result.ok` afterward and speaks
an explicit failure if it didn't land. A failed handoff is never silent.

"Hands off in ~20ms and returns" could easily be built as pure
fire-and-forget, which would make a failed handoff **silent by
construction on day one** — the exact worst outcome this project keeps
finding.

**Required:** the ~20ms budget is for a **queue-write ack**, not full
delivery. Haiku's handoff tool synchronously confirms the enqueue
succeeded; only Sonnet's reasoning is async. The "speak an explicit
failure if the handoff itself didn't land" behaviour is preserved exactly.

## 1.5 `/btw`

Verified real. Its help string: *"Use /btw to ask a quick side question
without interrupting Claude's current work."* Not wired anywhere in this
repo — it is a CLI-native feature.

To send one to a BUSY target it must be added to
`BUSY_TOLERANT_COMMANDS` (`transport.py:68`, currently `{"/compact"}`).
**Confirm its view type first** via `slash_guard.py`'s existing
discipline — verify it is non-interactive before allowlisting it, not
after.

---

## 1.6 How the concierge session is actually launched

Found by standing it up live, 2026-08-18. Two of these are non-obvious
and each one silently breaks the tier if missed.

```
claude --model haiku \
       --permission-mode acceptEdits \
       --mcp-config ~/Jarvis-concierge/.mcp.json \
       --strict-mcp-config
```

**`--strict-mcp-config` is mandatory, not tidiness.** Without it the
session inherits every user-level MCP server — Gmail, Drive, Calendar,
Chrome, Playwright, terminal-mcp — 100+ tools. Two consequences, both
observed:

1. **The read-only split becomes meaningless.** All the work in §0.2 to
   make the concierge structurally incapable of acting is void if the same
   session can read the user's email. The guarantee is only as narrow as
   the narrowest surface actually attached.
2. **Tool resolution degrades.** With that many tools loaded, the model
   emitted a call for a tool not in its schema (`bash`), got
   `No such tool available`, and then **fabricated an explanation** —
   "I'm unable to call the tool due to permission restrictions" — when
   `/mcp` showed `jarvis-l4-readonly ✔ connected · 3 tools` and the tools
   were pre-approved. Nothing was restricted. It invented a cause for its
   own failure.

   That last part is the thing to remember about this tier: **Haiku will
   narrate a plausible reason for a failure it does not understand.** It
   is why §1.2's grounding rules are written as hard prohibitions rather
   than guidance, and why a concierge answer must never be the only
   evidence that something is or isn't true.

**Read tools must be pre-approved** via `.claude/settings.local.json`:

```json
{"permissions": {"allow": [
  "mcp__jarvis-l4-readonly__list_sessions",
  "mcp__jarvis-l4-readonly__session_activity",
  "mcp__jarvis-l4-readonly__spend"
]}}
```

Otherwise every single read raises an approval dialog and the concierge
blocks — which defeats the one job this tier has. A settings file is used
rather than clicking "don't ask again" so the configuration is explicit,
reviewable, and reproducible instead of hidden session state.

Safe because the surface is read-only *by construction* (§0.2), not
because we chose to trust it.

**First launch in a fresh directory raises an MCP trust prompt.** Setup,
not runtime — `SPEC-TUI.md` §5.2 already covers this. It must be answered
by a human, once, per directory.

## 1.7 Measured concierge latency

Live, Haiku 4.5, Claude Code 2.1.234, from Claude Code's own turn timer:

| Query | Tools | Time |
|---|---|---|
| "Nice one" | none | **1s** |
| "How are you doing today?" | none | **2s** |
| "What sessions are up?" | `list_sessions` | **3–4s** |
| "How much have I spent?" | `spend` | **7s** |

**The "always ~2s" premise holds only for tool-free replies.** A
tool-grounded answer costs 3–7s, and `spend` is the worst because it
drives `/cost` on another session. Anything that must feel instant —
the acknowledgement in §1.1 — cannot depend on this tier at all, which is
exactly why that ack is templated and fires before the concierge is
invoked.

Reply quality was good without further prompting, including for speech:

> *"Two up — orchestrator and concierge."*
> *"Sixty-four cents this session, seventeen percent of your limit used."*

Note it wrote the numbers out for the ear unprompted.

---

# Phase 2 — The return channel

Without this, Sonnet finishes 30 seconds later with nobody listening. It
is not optional; it is the other half of an architecture where the front
layer never waits. Same tool `SPEC-blockers.md` §5 needs for escalation —
one build, two purposes.

## 2.1 `jarvis_say()` takes a typed class, not free prose

`answer` / `completion` / `blocked_question` / `error`. Free prose from
any agent at any time is a spam channel built into Ayman's attention, and
untyped messages cannot be batched or prioritized.

**`answer` vs `completion` is the load-bearing distinction**, added
2026-08-21 after the original three shipped without it. An `answer` is a
reply to something Ayman said seconds ago; a `completion` is news that
arrived while he was busy. Only the second may be held and grouped. With
no `answer` class the concierge's only expressible option was
`completion`, so every live reply inherited a policy written for
asynchronous agent news — held for the settle delay, eligible to merge
with unrelated news into one blob, and (measured) pushed past the instant
ack's fallback threshold so a filler line preceded nearly every reply.

The rule for anyone adding a fifth class: **the class encodes whether
Ayman is waiting, not what the message is about.** Batching is correct
exactly when he is not.

## 2.2 Identification by grammar, not callsign

Not `"Gateway here —"` on every line; that turns one voice into a phone
tree. Make the team the grammatical subject, the pattern
`SPEC-blockers.md` §5.3 already uses:

> *"The API session is asking whether to use staging or production."*
> *"Gateway finished its tests — three passing."*

One consistent voice, unambiguous source.

## 2.3 One queue, one flush policy

Generalize §5.3's blocked-question policy to **all** return traffic:
collect, speak at the end of the current dictation, batch, settle-delay.
If only blocks batch and completions interrupt, the system feels
inconsistent for no reason Ayman could infer.

**Priority tiers, spoken in tier order not arrival order:**

1. **Refusals — never queued.** `say_feedback.py`'s own docstring already
   treats a delayed refusal as equivalent to a lost instruction. That
   invariant survives this change untouched.
2. Blocked-question escalations
3. Completions
4. Informational

Persist the queue to disk so Haiku can answer "what did I miss" later. No
presence detection in v1.

---

# Phase 3 — Console

- `OrchestratorPanel` becomes actively misleading — it was built when
  "orchestrator" meant one whole brain. Split into a **CONCIERGE** panel
  (Haiku: mostly up/down, since "always free" makes busy/idle meaningless)
  and a renamed **ROUTER** panel (what it already is).
- The pending-utterance queue does **not** belong in Stream — `stream.py`
  is explicitly ambient and best-effort ("degrading to nothing new is
  acceptable… not a safety signal"). Queued speech is *actionable*. It
  needs its own surface, so that glancing at the screen mid-dictation
  never makes the batch afterward a surprise.

---

# Known gaps to close during the build

- **A restarted team lead reads as READY.** A crashed-and-relaunched
  Claude pane is a normal tmux session at a welcome screen — identical,
  from `registry.py`'s live enumeration, to an on-task lead. Textbook
  failure-reads-as-success. Fix: leads self-report identity+task to a
  per-session marker file on startup; diff against last-known.
- **No COMPACTING / CONTEXT_FULL signature** in `pane_state.py`. A
  mid-compaction pane probably reads BUSY, which probably fails safe —
  **unverified**. Verify live and add a positive signature, same process
  used for `BLOCKED_QUESTION`.
- **`orchestrator_has_tools()` matches by command-line substring**, not
  scoped to a session — its own comment flags this. Two MCP-connected
  processes activate it. Scope the check per intended target.
- **Startup order.** Copy `deliver_transcript()`'s existing pattern:
  preflight before the ack, spoken failure if the target isn't up. Do not
  invent a new one.
- **Compaction ownership** is undecided for every layer. Decide it
  explicitly — this team already had one live incident with
  `autoCompactEnabled`.

---

# Verification — the bar for "done"

Ayman asked for this tested and verified end to end. Nothing ships on
"it compiled."

1. **Canaries, in the style already established** (`pane_state_canary.py`,
   `chat_gate_canary.py`, `classify_canary.py`): assert on direction and
   both directions. A canary that only checks the happy path protects
   nothing.
2. **Concurrency proven, not assumed.** Two dictations in flight must be
   shown to complete against their own refs. Two simultaneous
   `jarvis_say()` calls must be shown to produce two clear utterances, not
   one garbled one.
3. **Every failure mode produces an audible or visible signal.** The test
   for each is: *unplug it and confirm Ayman would know.* Silence is never
   an acceptable result of a failure.
4. **Live end-to-end run** with real voice, both tiers, a real dispatch to
   a real team, and a return-channel message — observed, not simulated.
