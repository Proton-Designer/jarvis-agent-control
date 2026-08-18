# The Engine section — roles, sessions, and the console UI

Ayman's spec, 2026-08-18. The console's "Orchestrator" section becomes
**ENGINE**, because it now holds more than one agent type.

```
ENGINE
├── CONCIERGE     ← the fast front layer (Haiku)
└── ORCHESTRATOR  ← the router (Sonnet)
```

## Hard requirement: no typed input, anywhere

Every action is buttons or selection from a list. No text fields, no
terminal input, for any step of any flow — **including naming**, which
offers a generated default plus a pick-your-own path that must also be
selectable rather than typed where at all possible. The current
`setup_flow.py` uses `Input` widgets; that pattern does not carry over.

---

## 0. Directory layout — both roles live under ~/Jarvis/

Ayman's spec says the attach picker shows "all existing sessions inside
the jarvis directory ... because they would be located inside the same
directory." They were not: the concierge home was first created as
`~/Jarvis-concierge/`, a sibling. Restructured 2026-08-18 to match the
spec's assumption:

```
~/Jarvis/
├── CLAUDE.md          shared, NEUTRAL — both subdirs inherit it
├── teams.json         team registry
├── engine.json        the two role slots
├── concierge/         CLAUDE.md + .mcp.json -> server_readonly.py
└── orchestrator/      CLAUDE.md + .mcp.json -> server.py
```

**The parent CLAUDE.md must stay role-neutral.** Both subdirectories
inherit it, so anything role-specific there contradicts one of them. The
router's original 272-line CLAUDE.md moved down into `orchestrator/`
unchanged; the parent was rewritten to hold only what is true for both.
Verified live: a concierge started in `~/Jarvis/concierge/` correctly
reports its own role and its three tools.

**Attach-picker scope: sessions whose cwd is under `~/Jarvis/`.** Not
system-wide. A system-wide picker would list every Claude session on the
machine — team leads in unrelated projects included — which is precisely
the confusion "inside the jarvis directory" was written to avoid. Team
sessions live in their own project directories and are enumerated
separately by `list_teams()`.

## 1. The role flag

Every session in the Jarvis directory carries exactly one flag:

    "concierge" | "orchestrator" | "unused"

**The flag is the single source of truth for attachment**, and it is what
conflict detection reads. Set on creation, updated on attach, and reset to
`unused` on removal.

**It is persisted by us, not by the session.** It must survive the session
dying, the laptop restarting, and the console being killed and reopened.
A session that is attached-but-dead still reads as attached.

**Roles record intent; liveness is always polled.** Same rule as the team
registry — the role record must have no status field of its own, so
"attached" and "running" can never disagree by construction.

To support the Activate button (§5), each role record must persist enough
to *relaunch* the session, not merely identify it: **name, model, effort,
working directory, tmux session name.**

---

## 2. Create a session for a role

Four steps, each a selection:

1. **Model** — Haiku / Sonnet / Opus
2. **Effort** — low / medium / high / xhigh / max.
   Default **medium** for concierge, **high** for orchestrator. Mark the
   default as recommended.
3. **Name** — default `Concierge 1`, `Orchestrator 1`, auto-incrementing
   against existing sessions of that role (`Concierge 2`, and so on), or
   the user supplies their own.
4. **Spawn** a tmux session with those settings, flagged with the role
   being created for.

Launch must follow the verified form in `SPEC-orchestration.md` §1.6 —
`--mcp-config` + `--strict-mcp-config`, and read tools pre-approved. A
concierge launched without `--strict-mcp-config` inherits Gmail, Drive and
Calendar, which voids the read-only split entirely.

**Concierge points at `server_readonly.py`. Orchestrator points at
`server.py`.** That is the whole security boundary; do not let a UI flow
attach the wrong one.

---

## 3. Attach an existing session

Opens a list of **all** sessions in the Jarvis directory — both roles'
sessions appear, since they share a directory — **sorted most-recent
first**, scrollable.

- If the chosen session's flag is **already another role**, show a plain
  prompt: *"That session is already attached as the Concierge."* Do not
  silently steal it.
- If the flag is **`unused`**, offer an **optional rename** (default: its
  existing name), then on confirm set the flag to the new role.

---

## 4. Remove

A single button. Detaches the session from the role and sets its flag to
`unused`. Does **not** kill the session, and does not require choosing a
replacement first.

---

## 5. Swap and Activate

- **Swap:** change the session in a role for another, through the same
  selection flow as §3.
- **Activate:** shown when a role has an attached session that is **not
  currently running** — previously attached, never removed, but not
  detected alive. Pressing it relaunches *that same session* from the
  persisted name/model/effort/cwd until it is live again.

---

## 6. Start-button preconditions

**Which button:** `#wake_button` in the console's WakePanel — the existing
start/stop control that launches the wake daemon. There was real
confusion here worth recording: the SPACE key is stop-only by an explicit
ruling (accidentally stopping is recoverable; accidentally starting opens
the microphone without intent), but the BUTTON does start. §6 gates the
button, not the key.

Gating it is right because starting the daemon opens the microphone, and
voice input with no live concierge or no live router has nowhere to go —
the user would speak into a system that silently cannot act.

If either role has no live session, **Start must refuse and explain in
plain language** which role is missing and what to do:

> *"The Orchestrator has no session attached. Attach one before starting."*
> *"The Concierge session isn't running. Press Activate to bring it back."*

Name the specific role and the specific action. Never a generic failure,
never a silent no-op.

---

## 7. What each subsection shows

Per role, at a glance:

- The attached session's **name**, model, and effort — never a session
  UUID.
- Its live status: **active** or **inactive**.
- The right buttons for its current state: nothing attached → Create /
  Attach. Attached and live → Swap / Remove. Attached and dead →
  **Activate** / Swap / Remove.

---

## Verification

Same bar as everything else. In particular, prove:

- The flag survives console restart AND session death.
- Attaching an already-attached session is refused with the correct role
  named.
- Activate genuinely revives the same session with the same model+effort.
- Start refuses with the correct message for each missing-role case.
- A concierge created through the UI really does get `server_readonly.py`
  and `--strict-mcp-config` — assert on the actual spawned command line,
  not on the intent.
