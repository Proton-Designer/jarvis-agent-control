# Runtime configuration for `~/Jarvis/`

Reference copies of what the two Engine sessions actually run with. The
live files are in `~/Jarvis/`, which is personal state and deliberately
not a git repo; these are the versioned record.

```
~/Jarvis/
├── CLAUDE.md                                   <- CLAUDE.md
├── concierge.md                                <- concierge.md
├── orchestrator.md                             <- orchestrator.md
├── concierge/
│   ├── .mcp.json                               <- concierge/mcp.json
│   └── .claude/settings.local.json             <- concierge/settings.local.json
└── orchestrator/
    ├── .mcp.json                               <- orchestrator/mcp.json
    └── .claude/settings.local.json             <- orchestrator/settings.local.json
```

**Both roles now launch with `~/Jarvis` itself as their working
directory** — there are no per-role subdirectories to `cd` into any more.
`CLAUDE.md` loads automatically from that cwd and routes: each role is
sent exactly one boot message ("You are the Concierge…" / "You are the
Orchestrator…"), and `CLAUDE.md` tells it to read `concierge.md` or
`orchestrator.md` accordingly — both sitting right next to it. The two
subdirectories still exist, but only to hold each role's `.mcp.json` and
`.claude/settings.local.json` — nothing else lives there any more, and
nothing role-specific is loaded from cwd by being in them.

**Note the flattened names.** `.gitignore` ignores `.claude/`, so anything
stored under that path here is silently untracked — which is how the
`settings.local.json` files went unversioned without anyone noticing.
Stored flat and mapped above rather than mirroring the runtime layout
exactly.

**The per-role `settings.local.json` files are no longer the security
boundary, and are no longer loaded at all.** Claude Code reads
`.claude/settings.local.json` relative to the working directory, and the
working directory is now `~/Jarvis` for both roles — so
`~/Jarvis/concierge/.claude/settings.local.json` is read by nothing.
Editing it to change what the concierge may do would appear to work and
change nothing, which is the worst possible property for a permission
file to have. They are kept here only as the historical record of what
the boundary used to be.

The boundary now travels on the **command line**, in
`--allowedTools` / `--disallowedTools` / `--mcp-config` /
`--strict-mcp-config`, built by `engine_roles._role_cage_args()` and
shared by both the launch and the revive path. That is deliberate: a cage
that lives beside the working directory silently stops applying the
moment the working directory moves, and it moved.

`~/Jarvis/.claude/settings.local.json` does now exist and is read by both
roles, but it carries only `enabledMcpjsonServers` — MCP *trust*, not
permission. It lists `jarvis-l4` (the write surface) alongside the
read-only one, and that is safe for a specific reason worth stating:
trust is not availability. The concierge's `--mcp-config` declares only
`jarvis-l4-readonly` and `claude-peers`, and `--strict-mcp-config` means
nothing outside that file loads regardless of what is trusted.

Verified live against the running, already-once-revived concierge on
2026-08-20 — asked for every tool it has, it answered with exactly nine:

```
list_sessions, session_activity, spend, jarvis_say, handoff_to_router,
check_messages, list_peers, send_message, set_summary
```

No write tools, no Bash, after both the directory restructure and a
revival.

## Launching

Both roles, from `SPEC-orchestration.md` §1.6 and the `--disallowedTools`
finding, run with cwd `~/Jarvis` and each role's own `.mcp.json`:

```
# concierge — cwd ~/Jarvis
claude --model haiku --effort medium --permission-mode acceptEdits \
       --mcp-config ~/Jarvis/concierge/.mcp.json --strict-mcp-config \
       --disallowedTools Bash Write Edit NotebookEdit Agent WebFetch WebSearch \
       --session-id <uuid minted for this role, see engine.json>

# orchestrator — cwd ~/Jarvis
claude --model sonnet --effort high --permission-mode acceptEdits \
       --mcp-config ~/Jarvis/orchestrator/.mcp.json --strict-mcp-config \
       --session-id <uuid minted for this role, see engine.json>
```

`--strict-mcp-config` bounds which MCP *servers* are visible. It does
**not** touch Claude Code's built-ins, which is why the concierge also
needs `--disallowedTools`: without it the session has `Bash`, and a
session with `Bash` can run `tmux send-keys` and bypass the entire
read-only MCP split.

**Each role's Claude conversation UUID is minted by us, not discovered
afterwards.** We generate the UUID ourselves and pass it in with
`--session-id` at launch, rather than reading it back out of Claude Code
after the fact. This is why Activate can bring back the *exact same*
conversation after a kill: reviving a session means relaunching with the
same minted UUID, and it resumes knowing everything it was told before —
verified live, a session was killed and revived and still knew a fact it
had been told beforehand.

## `engine.json`

Stores what was **decided** at launch time for each role: tmux session
name, the minted conversation UUID, display name, model, effort, and
which role it is. It never stores anything **observed** — there is no
"running" or "busy" field here. Liveness is always computed fresh from
`list_sessions` / `session_activity` at read time, never read back out of
this file. That split is deliberate: a stale observed field sitting next
to a decided one is exactly the class of bug ("registry says idle, live
poll says busy") this whole redesign exists to remove by construction,
not by convention.
