# Engine sessions — stable identity, a real cage, and revival

How the Concierge and Orchestrator are launched, identified, caged, and
brought back after a reboot.

Companion documents: `SPEC-engine-roles.md` (the console surface —
create/attach/swap/remove/activate), `SPEC-orchestration.md` (what the two
roles are *for*), `SPEC-blockers.md` §2 (the bright line this cage exists
to hold).

---

## 1. The problem this replaces

A role's Claude conversation was **discovered** after launch rather than
assigned, and the discovered value was then trusted forever. Both records
were wrong when we looked, on 2026-08-19:

```
concierge      claude_session = 51f72701-…   transcript last written Aug 18 16:16
               its tmux session created                        Aug 18 16:19
orchestrator   claude_session = null
```

The concierge was pinned to a conversation that stopped being written
three minutes **before its own session existed**, and the process inside
that session had been launched fresh (`claude --model haiku …`, no
`--resume`), so the recorded id could not possibly be the conversation it
was running.

That field is what **Activate** resumes from. So a dead concierge would
have come back wearing a stranger's memory — silently, looking perfectly
healthy — and a dead orchestrator would have refused to come back at all.
The orchestrator's failure was at least honest; the concierge's is this
project's recurring shape, a wrong value indistinguishable from a right
one.

**Ayman's own diagnosis was different and worth recording**: he saw 13
conversations in the concierge's directory and concluded a new
conversation was being created per transcription. That was not the
mechanism — all 13 are development probes (`say hi`, `List your tool
names`, `Run the shell command 'whoami'`), and a real dictation is
delivered with `tmux send-keys` into the pane that is already open, which
continues that pane's conversation. But the conclusion he drew from it was
correct: nothing tied a role to one durable thread. Right answer, wrong
route, and the right answer is what mattered.

---

## 2. Identity is assigned, never discovered

`claude` accepts `--session-id <uuid>`. We generate the UUID and pass it
in, so the registry is authoritative by construction rather than by
successful observation.

Verified against real sessions, 2026-08-19:

```
claude --session-id 11111111-…-555555555555 …
  → transcript written to exactly 11111111-…-555555555555.jsonl

claude --resume 11111111-…-555555555555 …
  → same file, 48,967 → 54,922 bytes. No second file, no fork.
```

So the id survives revival. `--fork-session` is the flag that would mint a
new one on resume; **we never pass it**, and that is a rule, not a
default we are relying on.

This deletes the whole discover-then-drift class of bug. There is nothing
to heal on a poll because there is nothing that can go stale: we did not
learn the id, we chose it.

`-n/--name` sets the display name shown in the prompt box and in
`/resume`, so the name Ayman picks in the console is the name he sees when
he lists conversations by hand. Those two lists agreeing is not cosmetic —
their disagreement is what started this whole investigation.

---

## 3. The cage, and what it cost to measure

The Concierge must never be able to authorise anything or deliver to a
session directly. Its only route onward is `handoff_to_router`, which
takes no target.

**Denying Bash/Write/Edit/Agent is not enough.** Measured, not assumed —
the real tool list of a concierge caged that way still contained:

| Tool | Why it is an escape |
|------|---------------------|
| `Workflow` | spawns subagents; `Agent` being denied is irrelevant if this is not |
| `CronCreate` | schedules execution for later |
| `RemoteTrigger`, `Monitor` | out-of-band execution paths |
| `TaskCreate`/`TaskUpdate`/`TaskStop` | task orchestration |

The verified deny list closes all of them. What survives is read-only
(`Read`, `Glob`, `Grep`, `TaskGet`, `TaskList`), plus peers messaging and
the role's own five tools.

### 3.1 `--strict-mcp-config` is load-bearing

Ayman asked for sessions launched through his `claude-peer` alias, which
is `--dangerously-skip-permissions --dangerously-load-development-channels
server:claude-peers`. The permissions half was declined for the concierge
by his own decision ("give the concierge all the roles/permissions it
needs"), but the peers half raised a subtler problem.

`--strict-mcp-config` blocks the development-channels route to
claude-peers. Dropping it to get peers back is where the cost hid — measured:

```
with    --strict-mcp-config:   9 MCP tools, 2 servers
without --strict-mcp-config:  91 MCP tools, 7 servers
```

Those seven include `claude_ai_Gmail` (send mail, trash threads),
`claude_ai_Google_Drive` (read, share, trash), and **`terminal-mcp`, whose
`terminal_send_keys` is arbitrary keystroke injection into a terminal** —
precisely the capability that denying `Bash` exists to prevent. Trading
strict-mode for a messaging mesh would have handed the caged layer a
better weapon than the one we took away.

### 3.2 The resolution

`claude-peers` turned out to be an ordinary stdio MCP server, not a
special channel:

```json
"claude-peers": {"type":"stdio","command":"bun",
                 "args":["/Users/aymanmohammed/claude-peers-mcp/server.ts"]}
```

So it is declared in each role's **own** `.mcp.json`, and
`--strict-mcp-config` stays. Verified: exactly 9 tools — four peers, five
role — and none of the other six servers.

Both halves of what Ayman asked for, with none of what he did not.

---

## 4. Launch

Working directory is `~/Jarvis` for **both** roles. The subdirectories now
hold only `.mcp.json`. This is safe *because* identity is assigned: when
the id was discovered, two roles sharing a directory would have made
"the newest conversation here" ambiguous.

The cage travels on the command line — `--mcp-config`,
`--strict-mcp-config`, `--disallowedTools` — and not in a
`.claude/settings.local.json` beside the working directory. Moving both
roles into one shared directory would otherwise have silently merged their
permission files into one, which is the same class of failure as every
other entry in this document: a boundary that quietly stops existing while
everything still appears to work.

### 4.1 Boot

After the session reaches READY — never before — exactly one message is
sent:

> You are the Concierge. Get context about your scope.

`~/Jarvis/CLAUDE.md` routes on that sentence to `concierge.md` or
`orchestrator.md`. A session that does not know which it is must say so
rather than guess.

---

## 5. What the registry stores

`~/Jarvis/engine.json`, one record per role.

**Stored — things that were decided:** tmux session name, session UUID,
display name, model, effort, which role it is attached to.

**Never stored — things that are observed:** whether it is running.
`role_liveness()` computes that fresh on every read, and storing it
instead is exactly the stale-field bug this document exists to remove.
A flag that says "attached" is a decision. A flag that says "running" is
a claim about the world, and the world changes without telling us.

### 5.1 Reboot

tmux does not survive a restart; `engine.json` does. So after a reboot a
role reads as **attached, not running**, with its history intact — the
console shows it stopped with an Activate button, and Activate resumes
that exact conversation.

Activate rebuilds the **identical** flag set, cage included. A revived
concierge coming back with fewer restrictions than a freshly created one
is the failure this sentence exists to prevent.
