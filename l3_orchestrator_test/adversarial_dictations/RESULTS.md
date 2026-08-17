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
| 09 | Mixed conversation-plane + control-plane, same target | **PARTIAL — real structural finding, not a flake** | Correctly split into two batch entries in the right order (reasoned that compacting first would destroy the context the wrap-up needs). First delivered fine. Second (`/compact`) was refused: the first instruction put the pane into a genuinely busy state, and `deliver_batch`'s one bounded ~3s retry can't wait out an open-ended "wrap up your work" task. The model self-diagnosed this correctly, logged the undelivered instruction to `held.json` under the same log/speak/expire lifecycle as a routing hold (explicitly reasoning that auto-redelivering `/compact` hours later would be a stale-intent "zombie" delivery), and proposed three fixes. CLAUDE.md now makes this the documented behavior instead of relying on the model to reinvent it each run. Later re-run through the real full-pipeline integration test (see README): the same race did NOT reproduce that time — Claude Code's own UI queued the mid-turn `/compact` instead of refusing it, see the queueing investigation below. |
| 10 | Project-specific custom command | **PASS — now verified full-path through a live L3 orchestrator** | Two live targets set up (deploy-service with `.claude/commands/deploy-check.md`, and mobile-app with none), so the test actually exercises target discrimination, not just "the only session available." L3 correctly resolved "run the deploy check on the deploy service" to deploy-service specifically, recognized it as control-plane and not in the built-in table, verified it against that target's `custom_commands` (found `/deploy-check`), and emitted the literal command — mobile-app was never touched. Target confirmed to actually invoke the skill ("Skill(/deploy-check)... Successfully loaded skill"). Bonus: this run also exercised held.json's stale-target case for real — an outstanding hold from an earlier session (`/compact` to a data-pipeline session that no longer exists) was correctly identified as now-undeliverable and dropped rather than incorrectly re-surfaced. |

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

## Persistent-view control-plane commands (`/cost`, `/usage`, `/config`, `/model`, `/status`, `/help`)

Characterized all six live (2026-08-17). `/cost`, `/usage`, `/config`,
`/status` all open the same tabbed dashboard modal (or a variant of it);
`/help` opens a separate shortcuts view. All share one property: no `❯`
input prompt while open, dismissed only by Escape, marked by "Esc to
cancel" text with no accompanying "Enter to confirm" (which is what
distinguishes them from a real actionable permission modal — an earlier
version of `pane_state.py`'s patterns matched "esc to cancel" alone and
would have misclassified every one of these as PERMISSION_PROMPT; fixed
to require both phrases together for that state, added a new
PERSISTENT_VIEW state for "esc to cancel" alone).

**Incident during this characterization work, disclosed and fixed
immediately:** testing `/config` then `/model` in sequence, a stray
keystroke sequence sent while the Config view was still open/closing
landed on a highlighted settings row and Enter TOGGLED it — disabled
Ayman's real, global "Auto-compact" preference (not scoped to the
throwaway test session; same account as every other session). Caught by
inspecting the pane afterward, fixed by navigating back in and
re-enabling it, verified `autoCompactEnabled: true` restored. Root cause:
`/model` doesn't exist as a distinct command in this Claude Code version
and fell through into the still-open Config view, and inside an
interactive picker, injected keystrokes are UI INPUT, not text — the
transport's whole design assumes it's typing into an input box, and that
assumption is false inside a picker.

**Policy fix, not just the proximate bug:** classified every control-
plane command by what kind of view (if any) it opens, in
`known_slash_commands.json`:
- `none` — runs inline, always safe (`/compact`, `/clear`).
- `readonly` — persistent view, nothing selectable, safe to send AND
  safe to read back (`/cost`, `/usage`, `/status`).
- Hard-blocked, explicit reason recorded — `/config` and `/model` (the
  entire command, including `/model <name>` forms, until specifically
  re-verified safe with an argument) and `/help` (not yet classified
  readonly vs interactive, blocked pending that).
- **Anything not in the allowed list at all is refused by default** —
  the same rule that already applied to partial/unrecognized slash
  fragments now applies to whole unverified commands, since an
  unrecognized command is exactly what caused this incident.

**Read-only view flow, built and verified live against `/cost`:**
`deliver_batch` -> transport sends the command -> polls for the
PERSISTENT_VIEW state to actually appear (bounded wait, never assumed) ->
captures the plain-text content -> sends Escape -> polls again for a
genuinely empty `❯` prompt before considering the delivery complete
(never chains a send immediately after Escape without that verification)
-> a small per-command parser (`view_parsers.py`) extracts a short
spoken answer from the captured text. Verified end to end: real `/cost`
delivery correctly spoke "$0.66 spent this session, 15 percent of the
session limit used" (numbers from a live capture) and left the pane
verified READY afterward. Parsers return `None` rather than guessing on
an unexpected layout, and the caller speaks an honest fallback
("...on screen but I couldn't parse a clean answer from it") instead of
ever fabricating a figure from a misparse. A failed dismiss is reported
as a delivery failure (in `deliver_batch`'s `failures` list, spoken
explicitly) even though the content was still successfully read, per the
"never treat an uncertain pane state as done" rule everywhere else in
this codebase.

**Per-target custom command discovery**, promoted to a safety control by
the same incident (verifying a command exists before sending it is the
actual fix, not a nice-to-have): `registry.custom_commands_for(cwd)`
scans `<cwd>/.claude/commands/*.md` per target and `list_sessions` now
exposes each session's `custom_commands`. `deliver_batch` refuses any
slash command that's neither in the built-in allowed table nor that
specific target's custom-command list. Verified live end to end: set up
a real target with `.claude/commands/deploy-check.md`, delivered
`/deploy-check` to it, and confirmed the target actually invoked it
("Skill(/deploy-check)... Initializing…") — not just that the guard let
it through. Does not enumerate plugin- or skill-provided commands (no
generic way to inspect those from outside a live Claude Code session);
project-local `.claude/commands/` only.

**Not yet done:** scenario 10 (a full L3 orchestrator run resolving a
loose reference to a project-specific command) — verified at the
transport level only so far, see the table above. The queueing
investigation the Lead requested (whether Claude Code's own message
queueing makes the scenario 09 busy-deferral workaround unnecessary) is
a separate, not-yet-started investigation.
