# Pane-state classifier: false-refusal-rate measurement

2026-08-17, Claude Code v2.1.233, real throwaway target sessions (no
synthetic pane text).

## Result

**0 false refusals in 20 samples (0%)**, once measured with a methodology
that matches how Claude Code actually behaves. My first attempt reported
50% and was wrong — not because the classifier is unreliable, but because
my test's assumptions about ground truth were. Both flawed assumptions are
worth recording since they're real, non-obvious behaviors of this Claude
Code version that matter beyond this measurement.

## First attempt: wrong, and why

Initial run assumed "busy" = the full wall-clock duration of any bash tool
call, and that state resets to "ready" a fixed short delay after. 6 of 16
ready-expected samples came back as false refusals under that assumption.
On inspection, every one was the test's ground truth being wrong, not the
classifier:

1. **`/cost` (and by extension `/usage`, `/status`, `/config`) opens a
   persistent full-screen tabbed dashboard view with no input prompt at
   all.** It does not auto-return to the normal chat input — it requires
   an explicit Escape to dismiss. The classifier correctly reported
   UNKNOWN (fail-closed refuse) for every sample taken while this view was
   open; my test wrongly expected READY. This is the RIGHT behavior — the
   pane genuinely could not accept a normal instruction at that point —
   but it surfaces a real gap, see "New finding" below.

2. **Bash commands can get silently auto-backgrounded**, after which the
   status bar shows "1 shell" (or similar) while the main input box
   returns to a normal empty/ghost-suggested prompt — genuinely available
   for new input, not busy, even though the original tool call hasn't
   finished. My test assumed the tool call's full duration was "busy";
   in reality the foreground input can go READY well before that.
   Verified this is actually safe to act on: injected an unrelated
   instruction while a background shell was still running and Claude Code
   handled it correctly — processed the new instruction as its own turn,
   then separately reported the background task's completion when it
   finished. No corruption, no dropped output, no interleaving mess.

## Second attempt: methodology fixed, clean result

20 samples over a real session cycling short foreground commands (no
backgrounding, no dashboard-opening commands) and idle gaps of varying
length. Every sample's classification was checked against the pane's
actual content by direct inspection (not against a timing assumption).

- 16/20 correctly classified READY, including at least one genuine
  ghost-text case (dim autosuggest sitting in the box) — confirms the
  earlier SGR-dim fix holds up outside the original test-session run.
- 4/20 correctly classified BUSY — each one inspected directly and
  confirmed to show a real, live spinner line (e.g. "✢ Hyperspacing…
  (3s · ↓ 72 tokens)"), including one case 3+ seconds after a trivial
  `echo` command was submitted — Claude Code's own post-tool
  "thinking"/summary phase genuinely runs that long sometimes, so this
  was correctly caught as busy, not a flicker or a bug.
- 0 mismatches.

## New finding, not yet acted on

A control-plane command that opens a persistent full-screen view
(`/cost`, likely `/usage`/`/status`/`/config` too — not individually
verified) leaves the target pane stuck in that view. Nothing currently
dismisses it. If `deliver_batch` ever sends one of these as a
control-plane instruction, the target session's input line becomes
unavailable until a human (or some future mechanism) presses Escape.
`known_slash_commands.json` currently allows these through the slash
guard with no special handling.

Options, not decided:
1. Exclude commands that are known to open a persistent view from the
   control-plane mapping table in CLAUDE.md (treat them as
   not-safe-to-send unsupervised) until dismissal is handled.
2. Have the transport send a trailing Escape after specific commands
   known to open a view.
3. Leave as-is and accept that these commands, if ever routed, will
   require a human to notice and dismiss the view later — no worse than
   today, but also not something this suite currently tests for.

Flagging for a decision rather than picking one.
