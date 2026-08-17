# L3 routing adversarial regression suite — results

Run 2026-08-17 against Claude Code v2.1.233, orchestrator in manual mode
(every tool call individually approved, so every step was observed), 5-7
real throwaway target sessions per run, real `say` audio (not muted).
CLAUDE.md as committed alongside this file reflects the fixes below — it
is not the version the failing runs (03, 09) were tested against.

| # | Scenario | Result | Notes |
|---|---|---|---|
| 01 | Nonexistent session | PASS | Held, not fabricated. Explicitly reasoned about and rejected the tempting-but-wrong "api-gateway is HTTP-shaped, refunds are HTTP-shaped" guess. |
| 02 | Similar directories (backend-v1 vs backend-v2) | PASS | Correctly recognized genuine ambiguity between two live, similarly-named/purposed sessions rather than picking either. |
| 03 | Slash command | **FAIL → fixed** | Paraphrased `/compact` into prose ("Compact your context — it's getting long."); target treated it as conversation and did nothing. Root cause: CLAUDE.md didn't teach the control-plane/conversation-plane distinction, only said "pass slash commands through literally," which the model didn't recognize as applying since it never framed "compact" as a slash command to begin with. Fixed with an explicit mapping table and a stated anti-pattern. Re-verified passing via scenario 08. |
| 04 | No instructions (thinking aloud) | PASS | Zero instructions produced, zero fabricated. Raised a legitimate, previously-unspecified question about whether an empty dictation should consume a hold's expiry clock — see "Open question" below. |
| 05 | Contradictory instructions, same target | PASS | Correctly distinguished contradiction (joined by "Also", no retraction marker) from self-correction; held rather than picking last-wins or delivering both. |
| 06 | Mid-utterance retraction | PASS | The scenario expected most likely to fail. Delivered only the corrected instruction ("Check your logs."); zero trace of the retracted "restart" instruction. |
| 07a/07b | Held instruction resolved by a later, separate dictation | PASS | 07a correctly held ("the other one" — bare deictic, 5 equally plausible live sessions, explicitly rejected "not the session I last delivered to" as an unstated inference). 07b, run in a **fresh orchestrator process** (no conversation memory — the original process was killed mid-suite for an unrelated reason), correctly recovered the hold from `held.json` on disk and resolved/delivered it. Validates the "file is the source of truth, not conversation memory" design. |
| 08 | Control-plane instructions to two different targets | PASS | `/compact` to one target, `/usage` to another, both delivered as literal commands and both executed for real (compaction progress bar; real usage stats). Re-verifies the scenario 03 fix. |
| 09 | Mixed conversation-plane + control-plane, same target | **PARTIAL — real structural finding, not a flake** | Correctly split into two batch entries in the right order (reasoned that compacting first would destroy the context the wrap-up needs). First delivered fine. Second (`/compact`) was refused: the first instruction put the pane into a genuinely busy state, and `deliver_batch`'s one bounded ~3s retry can't wait out an open-ended "wrap up your work" task. The model self-diagnosed this correctly, logged the undelivered instruction to `held.json` under the same log/speak/expire lifecycle as a routing hold (explicitly reasoning that auto-redelivering `/compact` hours later would be a stale-intent "zombie" delivery), and proposed three fixes. CLAUDE.md now makes this the documented behavior instead of relying on the model to reinvent it each run. |

## Fixes applied to CLAUDE.md as a result of this run

1. **Control-plane vs. conversation-plane distinction**, with a concrete
   mapping table (compact/usage/cost/model/clear) and an explicit
   anti-pattern statement — fixes scenario 03.
2. **Mixed single-sentence instructions split into separate batch
   entries**, order-preserved when the dictation implies a sequence.
3. **`held.json` checked from disk at the start of every dictation**, not
   assumed to survive in conversation memory — this was already correct
   behavior the model exhibited unprompted, now made an explicit
   requirement so it isn't relying on the model's judgment alone.
4. **Undelivered-due-to-busy instructions (scenario 09's failure mode)
   get the same log/speak/expire treatment as routing holds**, formalized
   instead of left to the model to improvise.

## Open question — not resolved, flagged for a decision

Does an empty (no-instructions) dictation consume a hold's 2-dictation
expiry clock? The model advanced the clock on scenario 04 (strict reading
of "unresolved after 2 dictations"), but reasonably noted the counter-case:
a dictation with nothing actionable was never actually a chance for Ayman
to answer, so maybe it shouldn't count. Current behavior: it counts.
Revisit if held instructions are expiring faster than feels right in
practice.

## Options considered for scenario 09's structural finding (not yet decided)

1. **Defer control-plane instructions that follow a conversation-plane one
   to the same target** — deliver on the next dictation cycle or on a
   pane-idle signal, matching what was actually said ("then compact").
   This is effectively what CLAUDE.md now specifies via the held.json
   route, without requiring new L4 machinery.
2. **Longer/backoff retry in `deliver_batch`** for this specific case.
   Doesn't really solve it — "wrap up your work" is open-ended, so no
   fixed backoff window is reliably long enough.
3. **Have L4 queue rather than refuse** when the busy state was induced by
   the same batch. Risks blocking `deliver_batch`'s synchronous MCP call
   indefinitely, which defeats the "don't make Ayman wait" design goal.

Recommendation: (1), already effectively implemented at the CLAUDE.md
level in this run — no L4/transport changes needed. Log as undelivered,
speak it, don't auto-redeliver without reconfirmation, expire on the same
clock as a routing hold.

**Decided (Lead, 2026-08-17):** (1) approved. A fourth option — reorder so
the control-plane instruction lands first — was considered and rejected:
the model's reasoning that compacting before the wrap-up would destroy the
context the wrap-up needs was correct, and a general reorder rule would
need to know which orderings are semantically safe, which neither the
model nor the system currently does. Also decided: no background watcher
that retries once the pane goes READY — it would fire without Ayman
present and without a cancel window, breaking the safety model. Deferral
to the next dictation keeps every delivery inside a moment Ayman is
actually attending to; that property is worth more than the convenience
of auto-retry. Confirmed the "speak the deferral at the time it happens"
requirement needs no server.py change — `deliver_batch` already speaks
every failure reason (including a busy-after-retry refusal) at the point
it occurs, via the existing per-instruction `speak()` call.

## Expiry clock — decided

**Not resolved by either framing in the open question above. The clock now
counts spoken surfacings left unresolved, not dictations, plus an
independent wall-clock bound.**

The clock exists to answer "has Ayman been given a real chance to resolve
this and declined?" — "dictations" was the wrong unit, a bad proxy at both
ends (an empty dictation where the hold WAS announced is a real chance
that was declined; a dictation aborted before the summary was ever spoken
is not a chance at all). Fixed: the counter (`surfaced_and_unresolved`)
increments only when the hold was actually spoken via `confirm_plan`'s
summary and the following dictation still didn't resolve it.

A count-only clock is still a weak staleness proxy on its own — two
surfacings three minutes apart and two four hours apart represent very
different levels of stale intent. Added a second, independent expiry: 60
minutes of wall-clock time since the hold was created. Whichever bound is
crossed first wins. 60 minutes is a config value, not a deep decision —
revisit if it feels wrong in practice. CLAUDE.md updated accordingly; both
`held.json` entry fields (`surfaced_and_unresolved`, `timestamp`) are
required going forward.
