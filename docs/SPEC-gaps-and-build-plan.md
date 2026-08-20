# Gaps and build plan

Everything known to be missing, and the order to build it in.

Assembled 2026-08-20, after the ENGINE layer landed and all test data was
wiped for a clean slate.

**Every gap in §1 was verified against code, not recalled.** That matters
here specifically: several things remembered as "built" this session turned
out to be half-built when actually checked, and the half that was missing
was always the same half — the part Ayman can see.

---

## 0. The classification, and why it is not decoration

| Class | Meaning |
|-------|---------|
| **A** | Specced, not built — nothing implements it |
| **B** | Built, not reachable — code exists, nothing calls it |
| **C** | **Built, not surfaced** — it works, and the result never reaches Ayman |
| **D** | Built, partial — handles some cases, silently not others |

**C is this project's signature failure**, and it is invisible by
construction: nothing errors, nothing looks broken, the value is simply
computed and dropped on the way out. A plain "what's missing" list finds
none of them, because none of them are missing.

Confirmed instances, all found by looking rather than reasoning:

- the CHAT verdict computed, logged, then discarded by a guard keyed on
  the wrong field — Jarvis stayed silent when spoken to
- `[orchestrator]` eaten by Rich markup — two already-claimed sessions
  rendered as free
- the instant ack deleted in a rewrite — inaudible when missing
- `cwd_mismatch` computed and rendered nowhere until wired this morning
- a row whose icon said "no saved history" about a session it had just
  found alive

---

## 1. Verified gaps

### 1.1 Blocked agents are spoken but never shown — **C**

The most consequential gap open right now.

| Piece | State | Evidence |
|-------|-------|----------|
| Pane classification | built | `l4_controller/pane_state.py:27,208` — `BLOCKED_QUESTION` |
| Episode tracking | built | `l4_controller/blocked_state.py` |
| Member fields | built | `l5_console/state/models.py:98-100` |
| Voice escalation | built | `l5_console/state/poller.py:187` `_maybe_escalate_blocked()` |
| **Console render** | **absent** | `grep blocked l5_console/app/*.py` → no matches |

So a session stopped and waiting on a human is **spoken aloud** and then
renders as an ordinary running agent. `SPEC-blockers.md` §6 requires the
opposite: a blocked agent should be *the most prominent thing on screen*,
because busy resolves itself and blocked does not.

The data is already in the model. This is a render, not a feature.

### 1.2 Return-channel batching — **A** (partial: priorities exist)

`SPEC-orchestration.md` §2.3 specifies collect → speak at end of dictation
→ batch → settle-delay, with priority tiers spoken in tier order.

Built: the tiers. `say_feedback.py:105` is a real `queue.PriorityQueue`
serviced by one worker, and `tools_voice.py:67` maps kinds to priorities
with unknown kinds **refused rather than downgraded**.

Not built: any batching at all. Every item is spoken as it reaches the
front of the queue. Three agents finishing together is three utterances,
where the spec says one. Nor is the queue persisted, so "what did I miss"
cannot be answered.

### 1.3 No pending-utterance surface — **A**

`SPEC-orchestration.md` Phase 3 requires queued speech to have its own
console surface, explicitly NOT Stream (which is documented as ambient and
best-effort; queued speech is actionable). `grep -n "pending"
l5_console/app/console.py` → no matches.

Consequence: glancing at the screen mid-dictation tells you nothing about
what is about to be said, so the batch afterwards is always a surprise.

### 1.4 `ANSWER` intent class — **A**

`SPEC-L2.5-concierge.md:116` and `SPEC-blockers.md` §5.1. When a session
is blocked on a question and Ayman answers by voice, that answer must
route back to the *specific* session that asked, rather than being
classified as a new instruction.

Without it, the escalation path is one-way: Jarvis can tell him a session
is stuck, and he has no voice route to unstick it.

### 1.5 Blocked stages 2–4 — **A**

`SPEC-blockers.md` §8 build order. Stage 1 (detect/surface) is partial per
§1.1. Stages 2 (escalation routing), 3 (grounded auto-answer) and 4
(standing-policy allowlist) are unbuilt. Stages 3 and 4 carry all the risk
and should stay unbuilt until the detection layer has run against real
blocks for a while.

### 1.6 Visible terminal windows per directory — **A**, never specced

Ayman's requirement, 2026-08-20: project agents should be visible by
default, one window per directory, with a background option. The
concierge and orchestrator stay invisible.

Everything launches `tmux new-session -d` today — detached, no window ever
opened.

Grouping falls out of existing structure: a team **is** a directory
(`Team.root`), so "one window per directory" and "one window per team" are
the same rule, and a solo agent is a one-member team.

Needs: a `visible` flag in the registry, a platform call to open a
terminal attached to the session, reuse of an already-open window, and
window restoration on revival.

### 1.7 No revive-everything — **A**, never specced

A restart kills every tmux session. `engine.json`, `teams.json` and all
transcripts survive, so nothing is lost — but recovery is per-item:
Activate for each role, `[r]` then pick for each team.

`reconnect_team()` already loops over a team's members, and `activate_role()`
already restores a role with its full cage. Both mechanisms are proven;
what is missing is one loop over both registries.

**The reason to build it is not convenience.** Five manual steps is five
chances to revive four things and not notice the fifth — and a
half-revived system looks healthy.

### 1.8 Teams cwd-mismatch is logged, not data — **C**, scoped deliberately

`teams.member_liveness()` logs a cwd mismatch but does not return it, so
no console surface can show it. `engine_roles.role_liveness()` returns
`cwd_mismatch`/`found_cwd` and renders them.

Left as logging-only on purpose: `member_liveness()` returns a positional
tuple unpacked at two call sites and feeding a `TeamMember` contract, and
there is no consumer yet. Revisit when §1.1's render exists — that is the
consumer.

### 1.10 From Engineer 1's L1–L4 audit (2026-08-20)

Merged from the audit; classifications theirs, rulings mine.

- **Instant-ack ordering** — `daemon.py:305` speaks the ack before the
  concierge preflight at `:341`, so with no concierge attached Ayman
  hears the receipt, then the failure. **Ruled not-a-bug**: the ack
  exists to remove silence on the slowest path and asserts receipt only,
  never outcome. Moving it after the preflight reintroduces the silence
  the layer exists to prevent.
  **But the coverage hole underneath it is real and is being fixed**:
  `instant_ack_canary` passes `orchestrator_target=` explicitly, which
  skips the concierge-lookup branch entirely, so that path has no canary
  at all. That is how a bug shipped there this morning — the dry-run path
  spoke a HIGH-priority failure, which jumps the queue and would have
  overtaken the ack on a real dictation.

- **Blocked escalation has no L4 trigger** — accurate as scoped, but the
  escalator lives in L5 (`poller.py:187`), so this is a layering
  observation rather than a missing feature. Do not build a second one.

- **`latency_log` assumes one dictation at a time** (`:13-17`), which the
  concurrent-dictation work invalidated. No correlation id. Real, logged,
  not blocking.

- **`orchestrator_has_tools()` is not session-scoped** — verifies "some
  server.py is running somewhere" rather than *this* router's
  (`l2_l3_handoff.py:172-177`, which admits it). Real, logged.

- **No COMPACTING / CONTEXT_FULL pane-state signature** — unverified
  whether a mid-compaction pane fails safe. Needs live observation before
  code.

Verified built, reachable AND surfaced: dispatch keying, the tool-surface
split (checked via import graph), speech serialization, member-identity
restart detection, cancel-window fail-closed, and `slash_guard`'s three
hazard classes.

### 1.11 A canary that launches real sessions is load-sensitive

`team_actions_canary` failed twice in one afternoon and passed on
unloaded re-runs. Chasing it found two genuine bugs — `capture-pane`
returning only the visible screen (losing the first line of a long
reply, a real production data-loss path) and a 10s turn-start timeout
measured on an idle machine. Both fixed.

It still timed out once afterwards on a heavily loaded box. It launches
real Claude sessions, so the sensitivity is inherent, and neither fix
claims otherwise. Worth hardening — but the lesson is the one already
recorded above: "just a flake" is where two real bugs were hiding.

### 1.9 Deliberately deferred, not gaps

Recorded so nobody "discovers" them as oversights:

- **`tmux -CC` event-driven polling** — `SPEC-TUI.md` §6.3. Investigated,
  feasible, deferred; timer polling is fine at this scale. If ever built,
  polling stays a permanent fallback, never a migration path.
- **Signal's PIPELINE / HEARD SO FAR panels** — `SPEC-TUI.md` §82.

---

## 2. Build order

Ordered by *consequence when absent*, not by size.

**1 — Blocked render (§1.1).** The data exists and is already spoken; only
the screen is missing. Highest value per line in the list, and it is the
consumer that unblocks §1.8.

**2 — Revive-everything (§1.7).** A loop over two registries using two
proven mechanisms. Turns a five-step recovery with a silent-partial-failure
mode into one action with one verified outcome.

**3 — Visible windows (§1.6).** The only genuinely new capability, and the
only one needing platform-specific code. Independent of 1 and 2.

**4 — Return-channel batching + pending surface (§1.2, §1.3).** One build:
the queue policy and the surface that shows it. Worth doing after real
multi-team use, so the batching rules are shaped by observed traffic rather
than guessed.

**5 — `ANSWER` routing (§1.4), then blocked stages 2–4 (§1.5).** Last
deliberately. Stage 1 has to run against real blocks before auto-answering
anything is safe.

---

## 3. Standing rule this list argues for

Every new member/role state ships **with its render**, in the same change
that computes it. Not after.

Three separate instances in one day — `cwd_mismatch`, the contradictory
icon, and blocked state — were all the same shape: correct computation, no
surface. The canary discipline already says assert behaviour rather than
structure; this extends it to say that for anything Ayman is meant to act
on, *the surface is the behaviour*.
