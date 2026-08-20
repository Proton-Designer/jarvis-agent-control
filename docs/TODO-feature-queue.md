# Feature queue

Ayman's list, 2026-08-20. Worked **one at a time**, except where two items
are genuinely independent and can be split between the two engineers.

Status: `TODO` · `IN PROGRESS` · `BUILT` · `VERIFIED` (canary + driven live) · `DONE` (pushed)

---

## Order and assignment

| # | Feature | Owner | Status |
|---|---------|-------|--------|
| 1 | Kokoro-82M TTS, British male voice | Engineer 1 | IN PROGRESS |
| 2 | Small items (3, independent) | Engineer 2 | IN PROGRESS |
| 3 | Speech batching | Lead | TODO — blocked on #1 |
| 4 | Pending-speech panel | — | TODO — blocked on #3 |
| 5 | Answering a blocked agent by voice | — | TODO — last, largest |

**Why this order.** #1 and #2 touch disjoint files, so they run together.
#3 (batching) and #1 (Kokoro) both rewrite parts of `say_feedback.py`, so
they must not run at the same time — batching waits. #4 renders #3's
queue, so it needs #3's API to exist. #5 is the largest and depends on
nothing else being in flight.

---

## 1. Kokoro-82M TTS — Engineer 1

Replace macOS `say` entirely.

Engineer 1 researched this: Kokoro-82M (Apache 2.0), 82M params, runs on
CPU, real-time factor ~0.003 on Apple Silicon (30s of audio in ~379ms),
24kHz, 54 voices. Lowest integration risk of the five candidates, and it
slots behind `say_feedback.speak()`'s existing subprocess call.

**Voice: British male.** Ayman said "Daniel" — that is a *macOS* voice
name and Kokoro has its own set (`bm_*` for British male). Engineer 1
must confirm which British male voices Kokoro actually ships and pick the
closest, then say which one it chose. Do not assume a voice called
"daniel" exists.

Requirements:
- Fully replaces `say`; `say` remains only as a fallback if Kokoro fails
  to load, and that fallback must be **audible or logged**, never silent.
- Must not regress the priority queue, the refusal-never-queued rule, or
  `JARVIS_MUTE`.
- First-token latency matters more than throughput — the ack exists to
  kill silence, so measure and report it.

## 2. Small items — Engineer 2

Three independent, low-risk gaps.

- **`identity_verified_at` never rendered** (Class C) — computed at
  `teams.py:264`, shown nowhere. `SPEC-teams.md` §2 names the
  false-confident-active case directly.
- **Effort never collected and never rendered** — no effort step exists
  in team creation, so it is always None. Add the step, then render it.
- **One-keystroke adopt** — `SPEC-TUI.md` §6 wants assigning a known
  unassigned session to be one keystroke; today `[a]` opens the whole
  wizard.

## 3. Speech batching — Lead

`SPEC-orchestration.md` §2.3: collect → speak at end of dictation →
batch → settle-delay, in priority-tier order.

Built already: the tiers (`say_feedback.py`'s PriorityQueue), and
`jarvis_say`'s typed kinds with unknown kinds refused rather than
downgraded. Missing: any batching at all. Three agents finishing together
is three interruptions.

Hard rule that survives unchanged: **refusals are never queued.**

## 4. Pending-speech panel

`SPEC-orchestration.md` Phase 3. Queued speech needs its own surface —
explicitly NOT Stream, which is documented as ambient and best-effort,
while queued speech is actionable. Glancing at the screen mid-dictation
must never make the batch afterwards a surprise.

## 5. Answering a blocked agent by voice

`SPEC-blockers.md` §5 + the `ANSWER` intent class. Today the escalation
is one-way: Jarvis says an agent is stuck, and there is no voice route
back.

Needs: a pending-question queue, `ANSWER` classification (only available
while something is pending, which removes most ambiguity for free),
routing back to the *specific* session that asked, and hold-and-ask when
two are blocked and the answer doesn't say which.

Failure direction, from the spec: an ANSWER misread as DISPATCH is loud
and recoverable; a DISPATCH misread as ANSWER is silent and types an
instruction into a question prompt. **When uncertain, prefer DISPATCH.**

---

## Standing requirements for every item

1. Canary asserting **both directions**, not just the happy path.
2. **Driven live**, not only unit-tested — every Class C bug this project
   has found was invisible to a passing test.
3. Ships **with its render** in the same change if it produces anything
   Ayman is meant to act on.
4. Tests clean up their own state, and never touch `~/Jarvis` real state.
