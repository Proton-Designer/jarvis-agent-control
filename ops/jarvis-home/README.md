# Runtime configuration for `~/Jarvis/`

Reference copies of what the two Engine sessions actually run with. The
live files are in `~/Jarvis/`, which is personal state and deliberately
not a git repo; these are the versioned record.

```
~/Jarvis/
├── CLAUDE.md                          <- CLAUDE.md
├── concierge/
│   ├── CLAUDE.md                      <- concierge/CLAUDE.md
│   ├── .mcp.json                      <- concierge/mcp.json
│   └── .claude/settings.local.json    <- concierge/settings.local.json
└── orchestrator/
    ├── CLAUDE.md                      <- orchestrator/CLAUDE.md
    ├── .mcp.json                      <- orchestrator/mcp.json
    └── .claude/settings.local.json    <- orchestrator/settings.local.json
```

**Note the flattened names.** `.gitignore` ignores `.claude/`, so anything
stored under that path here is silently untracked — which is how the
`settings.local.json` files, the ones that encode *which tools each role
may use*, went unversioned without anyone noticing. They are the security
boundary in file form; they belong in history. Stored flat and mapped
above rather than mirroring the runtime layout exactly.

## Launching

Both roles, from `SPEC-orchestration.md` §1.6 and the `--disallowedTools`
finding:

```
# concierge
claude --model haiku --effort medium --permission-mode acceptEdits \
       --mcp-config ~/Jarvis/concierge/.mcp.json --strict-mcp-config \
       --disallowedTools Bash Write Edit NotebookEdit Agent WebFetch WebSearch

# orchestrator
claude --model sonnet --effort high --permission-mode acceptEdits \
       --mcp-config ~/Jarvis/orchestrator/.mcp.json --strict-mcp-config
```

`--strict-mcp-config` bounds which MCP *servers* are visible. It does
**not** touch Claude Code's built-ins, which is why the concierge also
needs `--disallowedTools`: without it the session has `Bash`, and a
session with `Bash` can run `tmux send-keys` and bypass the entire
read-only MCP split.
