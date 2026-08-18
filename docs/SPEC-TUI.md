# Jarvis Console — TUI specification

The control surface for Jarvis. Replaces the current flow of memorised
commands across three terminals.

Companion documents:
- `docs/TUI-FRAMEWORK-RESEARCH.md` — framework evaluation, the Textual
  decision and its reasoning, `tmux -CC` feasibility, prior-art findings.
- `SPEC-L2.5-concierge.md` — the conversational layer this sits alongside.
- Design studies: three candidate layouts, rendered.

---

## 1. Why this exists

Today Jarvis is driven by remembered commands in separate terminals:
`start.sh`, a daemon invocation with flags, a tmux session started by hand,
and `tmux attach` to see what happened. Nothing shows whether the
microphone is live, whether the orchestrator has its tools, or which
sessions Jarvis can actually reach.

Every failure we found in cold-start testing — orchestrator not running,
MCP prompt unanswered, tools missing — presented as *silence*. The console
exists to make system state visible rather than inferred.

---

## 2. Framework

**Textual (Python).** Decided; reasoning recorded in
`docs/TUI-FRAMEWORK-RESEARCH.md`.

The deciding factor is the backend boundary: Textual imports
`providers.py`, `pane_state.py` and `orchestrator_has_tools()` directly as
function calls. Ratatui and Bubble Tea are better engineered for this
workload but both require building and versioning an IPC layer to a Python
backend that already exists and works.

Accepted risk: Textualize wound down as a funded company (May 2025);
Textual is now community-maintained by a small team. Mitigation: the UI
layer is the cheapest thing in this system to port, and the backend never
moves.

---

## 3. Layout

**One application, three densities, chosen by available space.** Not three
apps. Textual's responsive layout handles this natively.

| Width | Layout | Character |
|-------|--------|-----------|
| Narrow (tmux pane) | **Rail** | Status lines + recent activity. Always-visible, steals no room. |
| Full window | **Console** | Wake / orchestrator / runtime down the left; stream and teams on the right. |
| Any, during dictation | **Signal** | State block and live meter take over regardless of size. |

The dictation view pre-empting the others is deliberate: while you are
talking, the only question that matters is whether it is still hearing you.

### Sections

1. **Wake** — one control, `start` ⇄ `stop`. Never both.
2. **Orchestrator** — set up / reconnect / active.
3. **Agents / Teams** — every team, its members, and what they are doing.
4. **Stream** — live log of wake scores, state transitions, routing.
5. **Runtime** — model warmth, memory, spend. Ambient, not primary.

### The live audio meter is required

Take it from the Signal study into every layout. Every other element
reports what the system *believes*; the meter reports what the microphone
is actually receiving. It is the only element that distinguishes "not
hearing you" from "hearing you and doing nothing."

### Signal's PIPELINE / HEARD SO FAR panels — deliberately deferred

The design study's Signal mockup (`docs/console-design-studies.html`)
also shows a per-stage pipeline visualization and a live partial
transcript. Neither is built, and this is a deliberate decision, not a
gap the mockup and the build silently disagree about (the Lead's ruling,
2026-08-18):

- The data doesn't exist. Nothing in `JarvisState` or daemon.py's
  `wake_state.json` tracks per-stage pipeline position or streams partial
  transcript text — building the instrumentation to back this would be
  real daemon-side work justified by a design drawn before anyone had
  used the console.
- The meter is the load-bearing element, and it already exists. It
  answers the question that actually recurs — "is it still hearing me"
  — where the pipeline/transcript panels answer questions nobody has
  asked yet.
- **Let Ayman use Signal with just the meter first, and ask whether he
  wants more**, rather than build ahead of that answer. If the meter
  alone answers the question in practice, the deferred panels were
  decoration.

---

## 4. Teams

### 4.1 The model

**The orchestrator routes to teams, not sessions.**

A team is a set of Claude Code sessions sharing a working directory. One
member is the **inbox** — the session that receives instructions. Team
internals are opaque to Jarvis: it delivers to an inbox and stops caring
what happens next.

There is no role taxonomy. No "lead", no "engineer". Those concepts do no
work in the data model and encoding them would force every user into one
team structure. The only thing the system needs is *which member
receives*.

Consequence: a lead-plus-engineers team, two peers with no hierarchy, a
solo agent, and structures nobody has imagined all work with zero extra
design.

### 4.2 Identity

| Property | Source | Durability |
|----------|--------|------------|
| Team identity | working directory | stable; also the unique key |
| Membership | discovered from live sessions in that directory | recomputed every poll |
| Member identity | Claude session UUID | survives restart; on disk under `~/.claude/projects/<encoded-path>/<uuid>.jsonl` |
| tmux session name | current binding only | **not** identity |

PIDs are never identity. tmux names are a binding that changes; the UUID
is what makes reconnect real rather than "start something new in the same
folder."

### 4.3 Registry

`~/Jarvis/teams.json`. Stores only what cannot be discovered:

```json
{
  "id": "api",
  "aliases": ["the api project", "api", "api gateway"],
  "root": "/Users/aymanmohammed/work/api",
  "inbox": "claude-api-lead",
  "members": [
    { "tmux": "claude-api-lead", "claude_session": "fe0563eb-…" },
    { "tmux": "claude-api-eng1", "claude_session": "a91c2f70-…" }
  ]
}
```

Discovered every poll, never stored as truth: liveness, what each member
is doing, whether the inbox is reachable.

**The registry records intent. Liveness is always polled, never
remembered.** A team marked live in a file that is actually dead is the
failure this project has produced repeatedly.

### 4.4 Liveness states — three, not two

| State | Meaning | Action |
|-------|---------|--------|
| **● running** | live now | route to it |
| **○ stopped** | not running, session history on disk | **Reconnect** — resumes the exact conversation |
| **lost** | history absent from disk | nothing to resume; must start fresh |

"Stopped" rather than "gone": not running is the normal case and is fully
recoverable. "Lost" is reserved for when that is genuinely untrue.

The icon carries liveness. The text beside it carries current activity
(`idle` / `busy`). No redundancy between them.

A legend sits beneath the panel. It is not decoration — the distinction
between stopped and lost determines whether a Reconnect action exists.

### 4.5 Addressing an individual member

Supported, with two rules:

1. **The inbox is the default and always wins on an ambiguous reference.**
   Reaching a specific member requires naming them explicitly. Never
   inferred, never a fallback.
2. **Log it; do not interrupt the inbox.** Direct instructions append to
   `<root>/.jarvis/direct.log`. The inbox can read it if it ever matters.

Rationale for (2), and the revision behind it: an earlier draft notified
the inbox on every direct instruction, to preserve coordination. That is
wrong for the common case — a colour change or a small fix does not affect
the inbox's model of the project, and routing it through costs a turn to
deliver information that changes nothing. Logging preserves the record
without the cost.

---

## 5. Flows

### 5.1 Adding a team — one flow, one fork, two questions

**Step 1 — which kind.**
- *Adopt agents already running* — live sessions grouped by directory.
- *Start a fresh team* — create them.

**Step 2 — who receives instructions.** Identical screen either way.
Lists candidates with **model** and **each session's own self-written
summary**, so the choice is informed. Nothing about roles is inferred.

**Step 3 — what do you call it.** Spoken aliases.

Never asked again.

### 5.2 Starting a fresh team

Additionally needs: target directory, number of agents, and the model each
runs (`claude --model opus` / `--model sonnet`).

**Must pre-answer the MCP trust prompt** by seeding `~/.claude.json`
before launch. Confirmed live: an unanswered prompt yields a session that
*looks* healthy, reasons correctly, and has no tools — with no audible
signal. Setup that leaves the user to discover this in a terminal has
failed its purpose.

No terminal work at any point.

### 5.3 Reconnecting

For each stopped member: `tmux new-session -c <root>` running
`claude --resume <uuid>`. Verify each came up. Report anything that
didn't — never report success for a partial restore.

### 5.4 Orchestrator

Same three states and the same actions. The orchestrator is a singleton,
not a team, and lives in `~/Jarvis`.

---

## 6. State layer

One function answering *what is true right now*. The UI renders it; it
never renders from memory of what it last did.

Reports: orchestrator (state, session id, tools reachable), teams (members,
liveness, activity, inbox reachable), wake daemon (running, mic active),
runtime (models resident, memory, spend), unassigned sessions.

**Unassigned sessions are shown deliberately** — a running session Jarvis
cannot reach is how you discover something to adopt, and assigning it
should be one keystroke.

### 6.1 Two clocks, not one

From k9s and btop: **sample rate and redraw rate are separate deliberate
choices.** btop's 2000ms default was chosen for graph sample-smoothing,
not CPU savings.

- Cheap checks (tmux list, pgrep): ~1s
- Expensive checks (pane capture, spend): slower, backed off
- Redraw: its own floor, independent of both

A single interval driving everything is the naive implementation and it is
wrong.

### 6.2 Polling discipline

- **Never cache intent as truth.** Poll reality on every render.
- **Do not write poller results straight into widget state.** lazygit
  shipped a documented bug (v0.64.0, PR #5791) where concurrent background
  loaders painted into views unsynchronised against the render pass,
  producing partial frames.
- Use terminal **synchronized output** (`CSI ?2026h` / `2026l`) for atomic
  frame painting. A terminal protocol, not a framework feature; Ghostty
  supports it.
- Data collection must never block input.

### 6.3 `tmux -CC` — deferred, not rejected

Investigated and feasible. Control mode carries the SGR detail the
ghost-text discriminator depends on (verified empirically, two ways).

But `%output` is a raw write-byte stream, not a resolved screen — Claude
Code repaints via cursor positioning, so reconstructing screen state would
require a full VT state machine. **Not a drop-in replacement.**

The workable design, when we build it: treat `%output` as a **content-free
wake signal**, then request `capture-pane -e -p` over the same persistent
connection and feed the **unmodified** classifier. Event-triggered rather
than timer-triggered, no subprocess spawn per check. Idle panes emit zero
spontaneous bytes, so it eliminates polling exactly where it is wasted.

**Requirement if ever built: timer polling stays as a permanent fallback,
never a migration path.** A persistent connection can die silently, after
which every pane looks unchanged forever and the UI shows stale state while
appearing healthy — the same shape as the toolless orchestrator and the
fail-open cancel socket. Heartbeat it; on drop, fall back and **make the
degraded state visible**. Absence of events must never be
indistinguishable from absence of change.

Deferred deliberately: current polling is fine at this scale. Build it in
the state layer when the console needs it. Do not retrofit L4.

---

## 7. Safety

The console inherits the project's governing rules. Restated because the
UI is where they become visible or invisible:

- **The wake control reflects reality, not intent.** It shows whether the
  daemon process is alive, never whether the button was pressed.
- **`space` stops listening, always. It never starts.** Not a toggle,
  deliberately (the Lead's ruling, 2026-08-18): accidentally stopping is
  fully recoverable — press start again — but accidentally starting opens
  the microphone without intent, the one direction this whole project
  refuses. A toggle cannot make that distinction; a stop-only key can,
  which is what makes it a trustworthy panic key — nothing to check about
  current state before pressing it. Starting requires the button (or a
  distinct, deliberate key), never `space`.
- **Quitting the console does not stop the daemon**, and must never do so
  silently while the mic is confirmed running. They are separate processes
  by design — the daemon is meant to survive the console closing under
  normal operation (e.g. a LaunchAgent-managed listener) — so a closed
  window must not read as "the microphone stopped." Warn explicitly and
  require an affirmative "quit anyway" rather than auto-stopping the
  daemon on quit: killing an in-progress dictation because the window
  closed would be its own surprise, in the same family as the button
  claiming success before a poll confirms it.
- **Fail loudly and legibly.** Every failure path says what happened and
  what to do. An unhelpful error at first run is worse than a slow one.
- **Never silently succeed partially.** A partial reconnect reports what
  did not come back.
- **The console never bypasses existing gates.** It shells out to verified
  tools — `start.sh`, `pane_status.py`, the preflight check — rather than
  reimplementing their logic. Every reimplementation is a second thing that
  can drift from reality.

---

## 8. Out of scope

- Replacing or altering the wake-word, transcription, classification or
  delivery layers.
- Cross-platform support. The delivery layer is built on tmux, which does
  not exist on Windows; that is a rewrite of L4, not a port, and it is not
  a UI decision.
- A packaged `.app` bundle. Running from a terminal is expected, and it
  may also give a more stable microphone permission grant than a Python
  path that moves on upgrade.
- Round-robin or load-balanced inboxes.

---

## 9. Build order

1. **State layer** — teams registry, discovery, liveness, reconnect,
   orchestrator lifecycle. Correct and tested before any UI exists.
2. **Rail** — narrowest layout, smallest surface, immediately useful.
3. **Console** — full-window layout over the same state.
4. **Signal + meter** — dictation view and live audio meter.
5. **Setup flows** — adopt, create, reconnect.

The state layer is the product. The layouts are views onto it.
