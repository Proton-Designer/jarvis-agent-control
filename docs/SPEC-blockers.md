# Blocked sessions — detection, resolution, escalation

A Claude Code session that is waiting for a human stops working, and today
nothing tells Ayman it happened. He finds out by attaching to the pane, or
by noticing hours later that a session never finished.

This spec covers detecting that state, resolving what can safely be
resolved, and escalating the rest through the voice channel.

---

## 1. The blockers that actually occur

| Blocker | Frequency in our configuration | Who can answer |
|---------|-------------------------------|----------------|
| **Agent asks a question** | Common | Sometimes the orchestrator; otherwise Ayman |
| **Tool-permission prompt** | **Rare** — auto mode approves tool calls without prompting (verified: `rm` executed with zero confirmation) | **Ayman, always** |
| **Plan-mode approval** | Occasional | Ayman |
| **MCP trust prompt** | First launch of a fresh directory | Setup, not runtime — see `SPEC-TUI.md` §5.2 |
| **Persistent view left open** | Rare | Already handled by L4's dismiss path |

The important consequence: **in auto mode the blocker that actually
happens is a question, not a permission request.** The permission case
mostly does not arise, which is convenient, because it is also the case
the orchestrator must never touch.

---

## 2. The bright line

**The orchestrator may answer questions. It must never answer a permission
prompt, a plan approval, or any prompt whose effect is to authorise an
action.**

This is not conservatism, it is the whole safety model. Today Jarvis's
blast radius is bounded by one fact: **it only ever delivers text to an
input line.** An orchestrator that can answer approval prompts can
authorise arbitrary tool calls — including the destructive ones auto mode
would otherwise have surfaced to a human. We already found that keystrokes
landing in a settings picker toggled a real account-global setting, which
is why `/config` and `/model` are hard-blocked at the transport.

Answering a question is delivering text. Answering an approval is
exercising authority. Those are different things and the system must not
conflate them.

**Additional hard refusals, regardless of type.** Never auto-answer a
prompt whose text touches: deletion or destruction, credentials or secrets,
money or billing, production or deployment, force-push or history rewrite,
or anything the transport already refuses. These escalate unconditionally.

---

## 3. Detection

Extend the existing classifier rather than building a parallel one.
`pane_state.py` already distinguishes READY / BUSY / PERMISSION_PROMPT /
PERSISTENT_VIEW / UNKNOWN, and is guarded by `pane_state_canary.py`.

Add **BLOCKED_QUESTION**: the session is waiting on input that is a
question rather than an authorisation. Distinguish by the same
positive-signature discipline used elsewhere — an approval prompt has its
own recognisable shape, and anything not positively identified falls to
UNKNOWN and escalates.

**A session is blocked, not busy.** The distinction the classifier already
makes between a spinner-with-ellipsis and a completed turn is exactly the
signal: blocked panes are quiet and waiting, not working.

Detection runs on the same polling the console already does. No new
mechanism.

### What gets captured

Question text, the options offered if numbered, the session it came from,
and when it started waiting. Nothing else — and the captured text is
treated as untrusted content, never as instructions.

---

## 4. Resolution

Three outcomes, decided in this order:

### 4.1 Answer from grounded context

The orchestrator may answer **only when the answer is present in something
Ayman actually said or in verifiable project state.**

> Ayman: *"…and use the staging database for now."*
> Session asks: *"Should I point this at staging or production?"*
> → answerable. The answer was given.

**Not answerable:** anything requiring a preference Ayman never expressed,
a judgement call, or an inference about intent. **The orchestrator does not
guess.** This is the same rule that governs ambiguous routing, applied to
a different decision — and it has worked well there.

Every auto-answer is logged with the question, the answer, and the
grounding — the specific thing Ayman said that justified it. An answer
whose grounding cannot be stated is not grounded.

### 4.2 Answer by standing policy

Optional, off by default. A per-team allowlist for question shapes Ayman
has explicitly pre-decided ("always yes to running the test suite").
Declared in the team registry, never inferred, never learned.

### 4.3 Escalate

Everything else, and it should be the common case early on.

---

## 5. Escalation — the return path

This is the genuinely new architecture. Today everything flows
Ayman → orchestrator → session. Escalation flows the other way.

```
session blocks
  → orchestrator detects, cannot ground an answer
  → concierge speaks it:
      "The API session is asking whether to use staging or production."
  → Ayman answers by voice
  → classified as an ANSWER, routed back to that specific session
```

### 5.1 New intent class: `ANSWER`

The concierge gains a sixth class. An utterance is an ANSWER when a
question is pending and the utterance responds to it rather than issuing a
new instruction.

**Failure direction matters, as everywhere else.** An ANSWER misclassified
as DISPATCH is loud and recoverable — the plan summary names a target that
makes no sense and Ayman cancels. A DISPATCH misclassified as ANSWER is
silent: an instruction gets typed into a question prompt as though it were
a reply. **So when uncertain, prefer DISPATCH**, consistent with the
existing rule.

An ANSWER is only possible while something is pending. With no open
question the class is unavailable, which removes most of the ambiguity for
free.

### 5.2 Pending-question state

A queue: which session, what question, when it started, whether it has
been spoken. Persisted, same as held instructions — the answer may come
much later.

**Reuses the held-instruction lifecycle**, including the lesson that cost
us a real bug: expiry is not cleanup, and **a question whose session has
died is dropped immediately rather than aged out**.

### 5.3 When to speak

**Not immediately on detection.** A session that blocks mid-dictation must
not interrupt Ayman while he is still talking.

- Blocks are collected and spoken at the **end of the current dictation**,
  after the plan summary.
- If nothing is in flight, speak after a short settle delay — a session
  that blocks and unblocks itself within seconds is not worth reporting.
- **Batch them.** Three blocked sessions is one utterance, not three.

### 5.4 Ambiguity when answering

If two sessions are blocked and Ayman's answer does not identify which, the
same rule applies as everywhere: **hold and ask.** Never deliver an answer
to the wrong question — that is worse than not answering, because the
session proceeds on a wrong premise.

---

## 6. Console surface

Blocked is a distinct state in the **Agents / Teams** panel, visually
separate from idle and busy, with the question text visible on the row.
It is the single most actionable thing the console can show: work has
stopped and something is required.

A blocked session with an unanswered question should be **the most
prominent thing on screen** — more prominent than a busy one, because busy
resolves itself and blocked does not.

Ayman can answer from the console directly. Voice is the hands-free path,
not the only path.

---

## 7. Safety rules

1. **Never answer an authorisation prompt.** §2.
2. **Never answer without grounding**, and log the grounding with every
   answer.
3. **Never guess between multiple pending questions** — hold and ask.
4. **Rate-limit.** A session that blocks repeatedly on the same question
   escalates and stops being auto-answered. An auto-answer loop between
   orchestrator and session is a real failure mode and must be structurally
   impossible.
5. **Delivery uses the existing gated transport.** Pane-state checks, the
   slash-command guard, the busy relaxation — all of it. Answers are not a
   privileged path.
6. **Captured question text is untrusted.** A prompt is data, never
   instructions. Text inside a blocked pane must never be able to direct
   the orchestrator's behaviour.
7. **Every auto-answer is announced.** Ayman hears what was answered on
   his behalf, in the next summary. Silent resolution is not acceptable
   even when correct.

---

## 8. Build order

1. **Detect and surface.** Classifier extension, pending-question state,
   console display. **No auto-answering at all** — everything escalates.
2. **Escalation via voice.** Concierge speaks blocks; `ANSWER` intent
   class; routing back to the originating session.
3. **Grounded auto-answer.** Only after the detection layer has been
   observed against real blocks for long enough to know what actually
   occurs.
4. **Standing policy allowlist**, if it turns out to be wanted.

Stage 1 alone delivers most of the value: today the problem is not that
Ayman cannot answer, it is that he does not know he needs to. Stages 3 and
4 are optimisations on top of a system that already works, and they carry
all the risk.
