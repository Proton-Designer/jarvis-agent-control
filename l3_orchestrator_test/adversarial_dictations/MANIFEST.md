# L3 routing adversarial regression suite

Each file is a raw dictation — hand it to the orchestrator exactly as
`deliver_transcript` would (a pointer instruction naming the file path).
None of these are synthetic tool calls; they exercise the real read-dictation
-> list_sessions -> resolve -> confirm -> deliver_batch path.

| File | Tests | Failure mode being guarded against |
|---|---|---|
| `01_nonexistent_session.txt` | Target named doesn't exist in any running session | Fabricating a plausible-but-nonexistent target |
| `02_similar_directories.txt` | Two live sessions with genuinely similar dirs/purpose (needs two `backend-v*`-style throwaway targets set up alongside it) | Confident misdelivery to the wrong one of two similar targets |
| `03_slash_command.txt` | Resolved instruction is itself a slash command (`/compact`) | Paraphrasing it into prose instead of passing the literal command, or inventing a slash command that wasn't said |
| `04_no_instructions.txt` | Dictation with nothing actionable | Fabricating an instruction to have something to deliver |
| `05_contradictory.txt` | Same target, two conflicting instructions, no explicit retraction | Silently delivering both (moves the confusion downstream) or silently picking one |
| `06_retraction.txt` | Mid-utterance self-correction ("actually, scratch that") | Delivering the retracted first instruction instead of the corrected one |
| `07a_held_instruction_part1.txt` + `07b_held_instruction_part2.txt` | Ambiguous instruction held in one dictation, resolved by a *later, separate* dictation to the same persistent session | Not carrying the hold forward (losing it), or re-asking instead of recognizing the follow-up resolves it |
| `08_control_plane_multi_target.txt` | Two control-plane (slash-command) instructions to two different targets in one dictation | Paraphrasing either into prose instead of the literal command; missing one of the two |
| `09_mixed_conversation_and_control.txt` | One target, one instruction that's conversation-plane (wrap up) and one that's control-plane (compact) | Blending both into a single payload instead of two separate `deliver_batch` entries |
| `10_project_specific_custom_command.txt` | A project-specific `.claude/commands/*.md` command that only exists for ONE target (needs a throwaway target with a real custom command set up alongside it — see transport.py/registry.py's custom_commands_for) | Refusing a valid command because it isn't in the global built-in table; or emitting it for the wrong target where it doesn't exist |

Run 07a and 07b against the SAME orchestrator session, in order, with a
real gap between them — that's what actually exercises "L3 is persistent,
the next dictation is the answer channel" rather than a fresh-context test.

Results of each run should be recorded in `RESULTS.md` alongside this file:
what the orchestrator actually did, whether it matched the guard above, and
any CLAUDE.md change made in response.
