# Claude Code's own message queueing: investigation

2026-08-17, Claude Code v2.1.233, real throwaway target sessions. Question:
does the target app's own mid-turn message queueing (discovered
incidentally during the full-pipeline integration test) make the
scenario-09 busy-deferral workaround (held.json) unnecessary — i.e. can
`deliver_batch` just send a control-plane command into a busy pane and
trust it to queue correctly, instead of refusing and deferring?

## Method

Directly injected text into a real target via `tmux send-keys` while it
was genuinely busy (a `sleep N` bash tool call in flight, spinner visibly
active) — bypassing L4's own pane-state gate, since the point was to
observe Claude Code's behavior in isolation, not the gate's. Tested at
three points in the busy window (~0.5s in, mid-turn, ~5.5s into a 6s
command) and with three payload types: prose, `/compact`, and `/cost`.

## Findings

**1. Queueing is reliable for prose and for commands that touch
conversation state (tested: `/compact`).** Every injection, at every
timing point tested, was accepted and visibly queued
("Press up to edit queued messages"), never swallowed, never corrupted.
`/compact` specifically: queued during a busy `sleep 10`, ran correctly
as its own turn once the busy work finished, in the position it was
queued — order preserved, output correct ("Compacted (ctrl+o to see full
summary)").

**2. Multiple queued PROSE messages get merged into ONE continuation
turn, not delivered as separate distinct turns.** Injected two prose
messages ("test message A", "test message B") during one busy window;
both were accepted individually as separate `❯` lines, but when the busy
turn ended, the model's single response acknowledged BOTH together: "I
also received both mid-turn messages — ... No action was requested in
either." This is fine for informational content but is a real nuance: if
two queued instructions were each meant to be individually acted on in
sequence (not just both read), merging into one response could lose that
distinctness. Not tested: whether an actionable (not just informational)
queued message gets properly acted on when merged this way — worth a
follow-up if this path is used for actionable instructions, not just the
informational content tested here.

**3. Read-only, non-conversation commands (tested: `/cost`) do NOT
queue at all — they execute immediately, bypassing busy state
entirely.** Injected `/cost` during a busy window; it opened its
dashboard view essentially instantly, independent of whatever prose was
already queued. This makes sense in retrospect: `/cost` (and by pattern
likely `/usage`/`/status`) are local CLI client actions that don't need
the model's turn to be free at all, unlike `/compact` (which operates on
the actual conversation) or prose (which needs the model). **So "does
queueing work the same for slash commands as for prose" has a real,
non-uniform answer: it depends on whether the command touches
conversation state.** This doesn't create a hazard for `/cost` itself
(nothing to corrupt in a read-only view), but it does mean don't assume
uniform queueing behavior across all control-plane commands without
checking which category a given one falls into.

**4. No state found where mid-turn text was swallowed rather than
queued**, across all timings and payload types tested.

## Recommendation

**Relax the BUSY refusal specifically for scenario 09's shape**
(conversation-plane instruction immediately followed by a control-plane
command that touches conversation state — currently only `/compact` is in
that category, since `/config`/`/model` are blocked outright and
`/clear` is blocked by default) — **but don't delete the held.json
deferral path.** Evidence supports trusting the queue for this specific,
narrow case: `/compact` reliably queued and ran correctly, in order, at
every timing tested, including in the real full-pipeline integration test
earlier today (where it happened by accident, via a pane-state-gate
timing gap, not by design, and still worked). Keep held.json's
undelivered-instruction lifecycle as a fallback for whatever this
evidence doesn't cover (a target that never becomes idle within some
bound, an edge case not tested here) rather than removing it — this is
"delivery no longer usually needs deferral," not "deferral can never be
needed."

**Don't relax BUSY for PERMISSION_PROMPT or UNKNOWN/real-typed-content.**
This investigation is only about the BUSY state specifically (model
actively working, spinner visible) — typing into an actual y/n modal or
over Ayman's own in-progress typing is a different hazard class entirely
that queueing behavior says nothing about.

**Not implemented yet** — reporting findings first, per the standing
instruction not to remove/change safety-relevant behavior before showing
results.
