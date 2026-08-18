# Agent Teams — directories, leads, and project context

Companion to `SPEC-engine-roles.md`. The Engine section manages two fixed
roles in one known directory. Teams are the harder case: **many teams,
anywhere on disk, each with several members and one lead.**

Consolidated from both engineers' brainstorm passes plus Ayman's own
corrections, 2026-08-18.

---

## 0. What is already true

Worth stating first, because a lot of this exists and building it twice
would be the worse outcome.

- **The lead role already exists.** It is called `inbox` — a pointer on
  the team (`entry["inbox"] = <tmux name>`), with `is_inbox` *computed*
  per member, never stored. Pointer-on-team is the right shape and it is
  already the shape: a single pointer structurally cannot name two leads,
  where a per-member flag can.
- **Routing already works without any of this.** `Team.id` and
  `Team.aliases` are authored by Ayman. He says "gateway", the team is
  called gateway. That is the real routing key: always present, never
  stale, written by the person doing the routing.
- Liveness, adoption, fresh creation, and the 3-state computation all
  exist and work.

**Captured project context is therefore an enrichment, not a dependency.**
If capture fails entirely, routing still works. That bounds the risk of
everything in §3.

---

## 1. Directory — the genuinely new problem

Engine roles live in one fixed directory. Teams live anywhere, so
**directory selection is the first step of the flow**, before model,
effort, or name.

### 1.1 The picker, without typed input

Three tiers, in order:

1. **Directories of live sessions not yet in a team.**
   `discover_teams_and_unassigned()`'s unassigned list already is exactly
   this. Zero new state work, and it covers the dominant case — you
   already started an agent there.
2. **Recent roots** — every distinct root that has appeared in
   `teams.json`.
3. **A button-driven walker** (subdirectories + `..` + confirm) for the
   genuinely novel directory.

**The naming text-field exception does NOT extend to paths.** That
exception was granted narrowly because a name is authored content with no
bounded set. A directory almost always has a bounded, enumerable set via
(1) and (2). A second `Input` erodes the rule for a case that does not
need it.

**Resolve every path at the moment of capture** (`Path(...).resolve()`).
Tiers (1) and (2) come from already-resolved sources — the kernel reports
real paths, not the symlink you typed — but tier (3) is a new entry point
for the exact `/tmp` → `/private/tmp` bug already fixed once in the fresh-
creation path.

### 1.2 Validate the directory at pick time, not after

**Two teams can register the same root today.** `create_team()` checks
only that the team *id* is unique — there is no check on `root` at all,
and none on `aliases` either. Neither team's liveness breaks, so both look
healthy; the damage appears only when a voice instruction has two
candidates and no tiebreak.

Add uniqueness checks on root and alias (case-insensitive, matching
however the router normalises), and **refuse at the moment the directory
is chosen** — the same "don't silently steal" discipline
`attach_role()` already applies. Discovering the conflict after the rest
of the flow completes is a worse experience and harder to retrofit.

### 1.3 Relocate

`teams.json`'s `root` is a static string; a live session's path updates
automatically. **Rename a project directory and a genuinely-running agent
reads as STOPPED or LOST**, because `member_liveness()` matches by exact
string equality.

**No matching improvement fixes this** — it is inherent to identifying by
path. It needs an explicit **Relocate** action: update `root` for a team
whose member is verifiably the same `claude_session` at a new path,
without forcing a full re-registration.

### 1.4 Subdirectory sessions

A session whose cwd is *below* a team's root does not match that team and
shows as unassigned. **Keep exact-match** as the rule and let Ayman
explicitly adopt such a session as another member. Containment-based
matching is a much larger invariant to get right, reopens §1.2's
ambiguity one level worse, and was not asked for.

---

## 2. The lead

Rename `inbox` → **lead** in UI vocabulary only. **Do not add a parallel
field** — two sources of truth that can disagree is the bug class this
project keeps finding.

Three fixes the existing pointer needs:

1. **Key it on `claude_session`, not tmux name.** tmux names are reusable
   after a kill, so the pointer is one recycled name away from silently
   naming an unrelated future session. Resolve tmux via the same
   `member_liveness()` lookup used for everything else.
2. **Validate the pointer is a member of that team.** Nothing currently
   stops pointing at another team's agent.
3. **Refuse a team with zero members**, and render "live members, no
   lead" as its own distinct state — today it is indistinguishable from a
   fully dead team.

**Swapping** is a picker over that team's own members. No directory
involved, no external universe — it is re-pointing one field.

**Swap is never automatic and never mid-dispatch.** `deliver_batch()`
resolves the target once at delivery time; a swap during that window is a
genuinely rare race rather than a structural gap, so this is a product
constraint rather than a lock: swapping is a deliberate action taken
between dictations, consistent with the no-auto-anything discipline.

**Restarted leads are already handled** — `member_identity.py` fires at
dispatch time for every registered member, not just Engine roles. The gap
is *display*: a panel showing "lead: active" can be showing an amnesiac
session, because identity is only probed at dispatch. **Do not poll
identity continuously** — `/status` is documented as adoption-time-only
and too intrusive for a poll loop. Show "unverified since <last
dispatch>" rather than a false-confident "active".

---

## 3. Project context

### 3.1 Ask the agent by default; CLAUDE.md is the shortcut

This ordering is **inverted from the first draft**, on Ayman's
correction, and his reasoning is the right one: *"we can't rely on
independent project structure because each project will be structured
differently."*

Any rule we write — read the README, parse the manifest — works on some
projects and returns nothing on others. **An agent reading the directory
is the one method that does not care about structure.** That makes it the
general mechanism; `CLAUDE.md` is a shortcut to skip the turn when it
already answers.

### 3.2 The prompt must be structured, not open

Because capture now runs on every registration rather than rarely, prompt
shape matters more. Open summarisation ("describe this project") produces
different prose every time and is not worth storing.

Bounded form: one sentence on what it does; 2–4 subsystems, one line
each; tech stack; **nothing you cannot verify by reading files.**
Bounding the shape bounds the drift.

### 3.3 What it may be used for

**Routing hints only.** Fed to the router's own reasoning; **never
recited to Ayman as fact.**

Model-generated context renders with a qualifier and a date —
*"(self-described, unverified)"* — and stores `captured_at`.
CLAUDE.md-sourced context needs no qualifier; it is not a fabrication
risk. Read CLAUDE.md **live** at display/routing time rather than caching
it, so it is never stale by construction — the same "liveness is always
polled" instinct applied to a file read.

Capture at registration only. Refresh is a manual button, never a poller.

### 3.4 Storage

The pointer and timestamp live in `teams.json` (which **is** the per-team
config file); the context body lives in its own file so a large capture
cannot bloat the file every poll reads.

**Key it on `team_id`** — the only identifier that survives a lead swap,
a member restart, *and* a directory move, and the only one already
uniqueness-enforced. **Write the team id inside the context file too**,
not just in its path: nothing else reads these files, so a mismatch caused
by a rename or reuse would otherwise never surface.

---

## 4. Identifiers — three keys, three jobs

| Key | Job | Breaks on |
|---|---|---|
| `team_id` | pairing context | nothing (uniqueness-enforced, Ayman-chosen) |
| `claude_session` | **identity** — is this the same agent? | fresh start; survives `--resume` |
| tmux name / cwd | **cheap liveness matching** | rename, reuse |

This identity-versus-matching split is already the real shape of
`TeamMember`. Keep extending it. **Anything that must survive a
rename, restart, or resume keys on `claude_session` or `team_id` — never
on tmux name or root.**

---

## 5. Console — modal, but discoverable

Ayman's decision: Teams management stays **modal**, not inline. The
Engine section has two fixed slots and its buttons still pushed RUNTIME
off-screen; Teams is N teams × M members, where per-row controls would
bury the information the panel exists to show.

His requirement: *"make the instructions on what to press and its function
very clear and very easy to work with and convenient."*

- **The empty state names the action and the key** — not "none
  configured" and nothing else.
- **Action hints live in the panel**, not only the global footer. The
  footer is for global bindings; per-panel actions belong with the panel.
- **Every key is labelled with its effect**, never bare.
- The hint area must hold more than one action without a rewrite, since
  remove and swap-lead are coming.
- A **selected-team highlight** with that team's actions in one compact
  strip — inline's directness without paying for it per member.

Panel changes: the lead gets a real badge and is **always rendered
first** (today ordering is just list order and the lead is a suffix);
model folds into the existing activity column as compact text
(`busy · sonnet`) rather than a fourth column; a per-team context line,
dim and truncated, with the unverified qualifier only where it applies.
`RailTeams` stays ambient and grows none of this.

`setup_flow.py` needs the same rebuild the Engine flows just got — its
`Input` widgets become ListView/Button steps.

---

## 6. Missing entirely — new work, not extensions

- **No remove or delete for teams or members exists at all.** Same rule
  as Engine: **detaching never kills a live session.**
- **`TeamMember` has no model or effort field.** Model is already fetched
  during adoption and then discarded — cheap. Whether `/status` exposes
  effort is unverified; verify live rather than assume, and do not let it
  block the feature.
- No uniqueness checks (§1.2); no zero-member refusal (§2).

---

## 7. Verification

Same bar as everything else, and prove these specifically:

- A directory already used by another team is refused **at pick time**,
  naming the conflicting team.
- Relocate updates a team whose member is verifiably the same
  `claude_session` at a new path, and the team reads RUNNING again after.
- The lead pointer survives a member restart, and is refused when aimed
  at a non-member.
- Removing a team leaves its sessions **alive**.
- Context capture writes the `team_id` inside the file, and a
  model-generated capture renders with its qualifier and date while a
  CLAUDE.md-sourced one does not.
- The empty Teams panel tells you what to press without consulting the
  footer.
