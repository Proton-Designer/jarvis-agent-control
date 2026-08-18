# Jarvis — voice-controlled Claude Code orchestrator

Lets Ayman drive multiple parallel Claude Code sessions by voice — one
continuous dictation, up to 5-6 minutes, split and routed to whichever
sessions are actually running. Full spec: `voice_orchestrator_context.txt`.

```
"Hey Jarvis" (wake word)
  -> L1: local wake-word detection (openWakeWord)
  -> L2: local VAD chunking + Whisper transcription, streamed to a file
  -> L3: a Claude Code orchestrator session reads the dictation,
         resolves targets, confirms the plan, calls L4
  -> L4: MCP server — live session discovery, spoken plan confirmation
         + cancel window, gated tmux delivery to target sessions
```

**Verified end to end** (2026-08-17, simulated audio via `daemon.py
--simulate`, no microphone): a synthesized 70s multi-instruction dictation
went through wake-word-equivalent stop detection → VAD/Whisper
transcription → the real `deliver_transcript` file-and-pointer handoff →
a live L3 orchestrator (fresh process, no prior conversation memory) →
plan/confirm → `deliver_batch` → all 4 resolved instructions landed
correctly in 3 real throwaway target sessions, including a control-plane
command Whisper had rendered oddly ("run/compact" instead of "/compact")
that L3 still correctly recognized and emitted as a literal `/compact`.
The orchestrator also correctly picked up an unrelated undelivered-
instruction record left over from an earlier test run, reasoned that this
dictation didn't resolve it, and correctly declined to auto-redeliver it.

**Latency**: the audio-to-pointer step is fast — isolated measurement of
`deliver_transcript` against a real ready target: **20 milliseconds**
(file write + one tmux send-keys call). The full integration run took
roughly two minutes end to end, and essentially all of that is
orchestrator reasoning time, not audio/transport overhead — a deliberate
cost of choosing Claude over a local model for routing (see "Auto-mode
safety posture"). An early estimate of a ~14s "pointer sent → visible"
gap during the live run turned out to be the tester's own reaction time,
not a real delay — worth recording as a killed false lead, not just the
real numbers. Structured latency events (handoff_received →
pointer_delivered → confirm_spoken → cancel_window_closed →
last_send_issued) are logged to `~/.jarvis/latency_log.jsonl` for any
future breakdown.

Layer READMEs have the real detail and the dated findings behind each
design decision — this file is the map, install steps, and the safety
model, not a duplicate of them:
- `l1_wakeword/README.md` — wake-word engine choice, thresholds, the
  three-bug state-machine hardening pass
- `l2_transcription/README.md` — VAD chunking, whisper-server wrapper,
  hallucination defense
- `l4_controller/` — no single README yet; see `FALSE_REFUSAL_MEASUREMENT.md`,
  `QUEUEING_INVESTIGATION.md`, and
  `l3_orchestrator_test/adversarial_dictations/RESULTS.md` for the
  equivalent dated-findings record

## Install

Each layer has its own Python environment, managed with `uv` rather than
plain `pip` — see "Why uv, not pip" below before reaching for pip
directly on this machine.

```
# L1 + L2 (share one venv — see l2_transcription/README.md for why)
cd l1_wakeword
uv venv .venv --python 3.13
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python fetch_models.py

# L4
cd ../l4_controller
uv venv .venv
uv pip install --python .venv/bin/python3 mcp
```

L3 needs no install — it's a normal Claude Code session with an
`.mcp.json` pointing at L4's `server.py`. See `l3_orchestrator_test/` for
a working example config and `CLAUDE.md` (the actual routing-behavior
spec L3 runs on — treat edits to it like code, not prose: it's L4's real
interface, and every rule in it exists because a specific adversarial
test failed without it).

### Configuring a target session

A "target" is just a tmux session running Claude Code — nothing extra to
install per project. Two things make it addressable:

1. **Live discovery is the default and requires no configuration.**
   `list_sessions` (an L4 MCP tool) enumerates real, currently-running
   tmux sessions via `tmux list-sessions` and enriches each with its
   working directory AND its project-specific custom slash commands
   (`.claude/commands/*.md` at that cwd). L3 resolves loose voice
   references ("the API one") against this list at request time —
   nothing is hardcoded, and nothing ever gets routed to a session that
   isn't actually running, or a command that doesn't actually exist for
   that target.
2. **`l4_controller/sessions.json` is an optional alias override**, empty
   by default. Only needed if Ayman wants a spoken nickname that doesn't
   match the session's real tmux name. Do not seed it with real project
   names as a shortcut — that defeats live discovery and is exactly the
   hardcoded-registry design this replaced.

`l4_controller/test_targets.json` is a separate, unrelated file —
integration-testing scaffolding only (see "Auto-mode safety posture"
below), always empty outside an active test run.

## Why uv, not pip

Plain `pip install` in this machine's Homebrew Python 3.14 hits a real
bug: `pip`'s `truststore`-based SSL context creation calls
`platform.mac_ver()`, which returns an empty string in this environment,
and `truststore` crashes trying to parse it — breaking `pip install` for
*anything*, even from a local wheel, since session creation happens
before pip checks whether it actually needs the network. Not investigated
further since it's an unrelated macOS/Python packaging bug, not a Jarvis
issue — `uv` has its own resolver and doesn't hit this path. Use `uv venv`
+ `uv pip install --python <venv>/bin/python3 <package>` for every layer's
setup; don't fight the pip bug, route around it.

## Auto-mode safety posture — read this before running anything live

**Target sessions run in Claude Code's auto mode, which removes
tool-permission prompts entirely.** Verified directly: `rm` on a real file
executed with zero confirmation under auto mode. This is not a
theoretical risk — it's the actual, measured behavior, and it changes
which controls in this system are load-bearing.

**The spoken cancel window is the only human-in-the-loop control left in
the system for auto-mode targets.** Not a nice-to-have on top of
permission prompts — the replacement for them. Everything downstream of
that window (`confirm_plan` speaking the plan, `deliver_batch` speaking
every refusal and a final summary) exists because there is no other
backstop.

Consequences this codebase enforces, not just documents:

- **The cancel trigger is a re-detection of "Hey Jarvis" during the
  confirm window** — not a separate "cancel" keyword. openWakeWord ships
  no pretrained model for one, and a custom-trained one would carry an
  unmeasured false-negative rate on exactly the control that can't afford
  one. "Hey Jarvis" already has a measured false-positive rate (see L1's
  README). The confirm-window trigger is architecturally exclusive with
  L1's other two meanings for "Hey Jarvis" (start / stop dictation) — see
  `l1_wakeword/daemon.py`'s state machine.
- **Missing the cancel socket fails CLOSED, not open.** An earlier version
  of `cancel_listener.py` treated a down/missing L1 socket as "not
  cancelled," which is the *permissive* outcome — delivery would proceed
  with no real cancel window and no indication it was ever a real one.
  Fixed: `available=False` is spoken explicitly ("Cancel unavailable."),
  and `deliver_batch` refuses delivery to any target that isn't
  explicitly marked as a throwaway test target (`test_targets.json`,
  empty by default) while the socket is down. This is the one rule in
  the codebase applied everywhere: **safety controls fail closed and
  audibly; convenience features fail open and quietly.**
- **Pane-state gating before every delivery.** `deliver_batch` never
  types into a target blind — it captures the pane, classifies it
  (ready / busy / permission-prompt / persistent-view / unknown), and
  only injects into READY (plus one narrow, evidence-backed exception,
  see below). A real bug here (ghost/autosuggest text in the input box
  visually indistinguishable from Ayman's own unsubmitted typing, unless
  you read the ANSI dim/faint attribute rather than cursor position — see
  `pane_state.py`) was found and fixed by testing against a live pane, not
  by inspection. Measured false-refusal rate after the fix: 0/20 on a
  clean methodology run — see `FALSE_REFUSAL_MEASUREMENT.md`.
- **`/compact` may be sent into a BUSY pane rather than refused**,
  trusting Claude Code's own mid-turn message queueing — the one, narrow,
  exact-by-name exception to the rule above. Verified reliable at
  multiple points in a busy window, in order, no swallowing, AND (the
  finding that actually justified this) queued actionable instructions
  are genuinely performed when a merged turn resolves, not just
  acknowledged — verified with independently-checked file artifacts, not
  model self-report, up to 3 instructions merged into one turn with no
  degradation. See `QUEUEING_INVESTIGATION.md`. By name, not by category
  ("any command that touches conversation state") — a category rule
  would silently extend to a future untested command. `held.json`'s
  undelivered-instruction deferral stays as a fallback for anything this
  doesn't cover, not deleted.
- **Control-plane (slash-command) safety is two-axis, not one.**
  `known_slash_commands.json` classifies every command on (a) what kind
  of view it opens — `none` (runs inline), `readonly` (opens a display-
  only view, captured/dismissed/read back automatically, see
  `/cost`/`/usage`/`/status`), or hard-blocked as interactive
  (`/config`, `/model` — confirmed live that injected keystrokes inside
  a selectable-list view are UI INPUT, not text: a stray send toggled a
  real, global Claude Code setting during testing, caught, fixed,
  disclosed, and turned into this policy) — and (b) reversibility,
  independent of view type: `/clear` renders inline like `/compact` but
  is irreversible (wipes the conversation, vs. `/compact`'s
  non-destructive summarize), so it's blocked by default regardless of
  its view classification (`JARVIS_ALLOW_CLEAR=1` re-enables it; no such
  override exists for the interactive-picker blocks, since that's a
  hazard axis, not a risk-Ayman-can-knowingly-accept axis). **Anything
  not explicitly classified defaults to refused** — the same rule that
  already applied to partial/unrecognized slash fragments now applies to
  whole unverified commands, project-specific or not (see
  `registry.py`'s per-target `.claude/commands/*.md` discovery). **This
  happened a second time (2026-08-18), reproduced independently while
  characterising `/config`'s view shape for the blocked-view self-heal
  feature described below:** an Escape that only cleared `/config`'s
  search filter (its footer said "Esc to clear," not "Esc to close" —
  the mistake was not reading it) left the view open and focused, and a
  follow-up test keystroke landed as filter text, with the next Enter
  toggling `autoCompactEnabled` off in the real, global
  `~/.claude/settings.json` — caught, restored, disclosed immediately.
  Two occurrences of the identical hazard, months apart in code age and
  hours apart in wall-clock, is why the rule is not "be careful," it's
  **characterising a blocklisted or unidentified view is capture-only —
  open it, capture the pane, classify it, stop.** No dismissal attempt,
  no follow-up command, nothing beyond what opened it. If it needs
  closing, that's a deliberate human action informed by actually reading
  what the footer says, never folded into an automated probing sequence.
- **Partial/unrecognized slash commands are refused, not typed.**
  Confirmed live that a truncated payload like `/comp` doesn't fail
  safely — Claude Code's own completion overlay auto-submits whatever it
  has highlighted (`/compact`) instead of the literal typed text. A
  malformed payload reaching the transport could therefore execute a
  *different* command than the one resolved. `slash_guard.py` refuses any
  `/`-prefixed payload that isn't an exact match.
- **`JARVIS_MUTE=1` silences audio, never the cancel window.** A real
  product feature (Ayman on a call, in a meeting), not test scaffolding —
  `speak_with_cancel_window`'s control flow is identical under mute; only
  the `say` subprocess is skipped, and every call (muted or not) is
  logged with an explicit `muted` field to `~/.jarvis/say_log.jsonl` so a
  clean muted test run is never mistaken for evidence the audio path
  works. **Set it via a server's declared `env` in `.mcp.json`
  (`l3_orchestrator_test/.mcp.json`), not by exporting it in a shell.**
  Found live: `export`ing it in one shell has no effect on an
  already-running orchestrator process, which inherited its environment
  from whenever its tmux session was created — a mute "confirmed" that
  way is not actually in effect for the process that matters, and there
  is no way to verify from outside that it is. A declared `env` value is
  read fresh on every process spawn and is verifiable by reading the
  config file; ambient shell state is neither. `l3_orchestrator_test`
  ships muted by default for exactly this reason — audio for a real
  session (like Ayman's live-voice test) is an explicit, considered
  unmute, not something inherited from whatever shell happened to start
  it.
- **Every pane-state pattern is scraped from a UI that changes without
  notice, and a broken detector can go silent.** Found the hard way,
  2026-08-17, within the same session: a Claude Code point release
  (v2.1.234) grew `/cost`/`/usage`'s view tall enough that its footer
  marker scrolled off an ordinary 80x24 pane, silently breaking
  PERSISTENT_VIEW detection — every read-back timed out with the view
  left open, and nothing surfaced it until someone happened to be
  testing that exact path. Investigating it surfaced a second, more
  serious gap in the same pass: PERMISSION_PROMPT — the detector guarding
  the single most dangerous interaction in this system, unauthorized
  keystrokes landing on a live approval prompt — had never actually been
  validated against a real per-tool-call approval prompt at all, only
  against a differently-shaped onboarding dialog captured once, months
  earlier. The real prompt was silently misclassified as PERSISTENT_VIEW.
  Harmless that particular day only by coincidence (both states currently
  refuse delivery the same way, and Escape happens to be documented as
  safe-cancel on both) — not a property either state was designed to
  have. `l4_controller/pane_state_canary.py` exists so this class of
  regression is a thirty-second command after any Claude Code update, not
  a discovery made by luck weeks later: it live-drives a real throwaway
  session through every known state (including a real manual-mode
  approval prompt, declined, with an explicit assertion that nothing was
  actually written) and asserts the classifier still agrees with reality.
- **Held-instruction expiry (the dual-clock design in `CLAUDE.md`'s
  "Held instructions" section — `surfaced_and_unresolved` reaching 2, or
  60 minutes wall-clock, whichever comes first) is not a cleanup path for
  test artifacts, and must never be relied on as one.** A surfacing means
  the hold is spoken aloud to Ayman — the clock exists to answer "has he
  had a real chance to resolve this and declined," which is exactly why
  it can't also be the thing that quietly removes a fake instruction
  before he hears it. Confirmed the hard way (2026-08-17): an accidental
  live delivery from a CLI smoke test (see the shared-git-index and
  default-target-resolves-to-a-live-session lessons in this same section)
  produced a real, spoken-aloud held entry — "still holding: tell
  shipcheck to redeploy the api gateway" — for an instruction Ayman never
  gave, about a session that never existed. Self-expiry would eventually
  have cleared it, but only after Ayman heard it at least once as if it
  were a real, currently-pending decision. **Tests and test artifacts
  clear their own runtime state explicitly** — delete/reset
  `held.json` (or whatever file the mistake touched) as part of noticing
  the mistake, don't wait for or rely on the expiry clock to do it.
- **No unattended microphone capture, ever.** Live-mic testing only
  happens while Ayman is actively present and watching, and stops when
  watching stops — this is a standing rule, not a per-incident decision
  (it followed an incident where a *different* component's test produced
  unexpected audible TTS output on Ayman's own machine; the fix there was
  the mute feature above, and the same "don't do unattended things that
  affect Ayman's environment" principle was extended to the microphone).
  `l1_wakeword/daemon.py`'s live-mic branch is a deliberate placeholder
  until that go-ahead happens.

**Broader lesson from three separate incidents on this project** (a
partial slash fragment resolving to the wrong command via the completion
overlay; a settings-picker view treating injected text as a UI toggle
instead of prose; and — checked, not assumed — that queued messages
still get correctly acted on): **the transport's core assumption — that
send-keys produces characters in an input box — only holds when the
target is genuinely at an input line.** Every place that gate gets
relaxed or a new interaction shape gets added, re-derive from evidence
whether the assumption still holds; don't extend a working pattern to a
new case by analogy alone.

**Second, related lesson, same underlying principle in a third context:**
a safety-relevant setting that depends on ambient shell state is not
actually a control — L1's "Hey Jarvis" state-machine invariant and L4's
command classification table are both *verifiable by reading*; a mute
flag `export`ed into one shell and assumed to reach an already-running
process in a different one is not, and it fails silently (a "confirmed"
mute can simply not be in effect, with nothing available to check that
from outside). Prefer declared configuration a process carries with it —
an `env` entry in `.mcp.json`, not a shell export — for anything where
"is this actually on" needs to be answerable by reading a file rather
than trusting a report.

**Third, related lesson, from the two pane-state regressions above:**
**a position-independent marker beats a position-dependent one, and a
shared marker isn't a positive signature.** The `/cost` fix moved from a
footer that can scroll out of a short pane to a tab-bar row that
renders at a fixed position regardless of content height — that
generalizes to every pattern this codebase keys on, and it's a cheap
review question to ask of any new one. The PERMISSION_PROMPT fix is the
sharper version of the same lesson: PERSISTENT_VIEW's original "esc to
cancel" marker wasn't wrong because of *where* it sat on screen, it was
wrong because that footer is a shared UI convention, not something
unique to a read-only view — so it silently caught states it was never
meant to describe, in both directions, at different times. Every pattern
in `pane_state.py` should describe something structurally unique to the
state it names, checked positively, not "whatever's left after the
other checks didn't match" and not "a phrase that happens to appear
there today."

**Fourth: a test that cannot fail when the world changes is not
protecting you from the world changing.** The instinct after finding the
PERSISTENT_VIEW regression was to fixture the exact pane text that had
just been captured and assert the parser handles it — a fine unit test
of parsing logic, and a false sense of coverage for anything else. A
golden fixture of the *old* PERMISSION_PROMPT shape (the onboarding
dialog) would have passed on every run, forever, while the real
per-tool-call prompt stayed misclassified as PERSISTENT_VIEW the whole
time — canary green, gate wrong, which is worse than no canary at all
because it produces false confidence instead of an honest unknown.
`pane_state_canary.py` live-drives a real Claude Code pane into each
state on every run for exactly this reason; if you add a fixture-based
check anywhere in this codebase, label it explicitly as a parser test,
never as coverage for "does this still match the real UI."

## Contributing — shared working tree

Multiple engineers (and their agents) work in this same checkout at the
same time, not separate clones. That means `git`'s staging area is
**shared state**, not per-person — anything anyone `git add`s sits in one
index until someone commits, and it doesn't matter who runs the commit.
Confirmed the hard way (2026-08-17): a bare `git commit` swept up another
engineer's staged-but-uncommitted files under an unrelated commit
message. Caught before anything was pushed and split cleanly, but the
next one might not be caught.

Two rules, not suggestions:

- **Always commit with an explicit pathspec — `git commit <paths> -m
  "..."`, never a bare `git commit`, `git commit -a`, or `git add .`.**
  Committing specific paths only touches those paths, regardless of what
  anyone else has staged in the shared index. This removes the hazard
  structurally; it doesn't depend on anyone remembering to check `git
  status` first.
- **No destructive git operations in this tree, ever — no `reset --hard`,
  `checkout .`, `clean -fd`, or `stash` that could touch another
  engineer's files.** A messy commit is recoverable; those aren't — they
  silently delete someone else's uncommitted work with no way to get it
  back. `reset --soft` (rewinds commits, keeps everything staged) is
  fine. If you think you need anything stronger, stop and ask first.

## What isn't built yet

- Live-mic end-to-end integration test (blocked on Ayman's explicit
  go-ahead + presence, not a technical blocker)
- `/help`'s view classification (readonly vs interactive) — currently
  blocked pending that check, see `known_slash_commands.json`
- `l4_controller`'s own README (this file covers it at the system level;
  a layer-level one following L1/L2's format would be useful)
- A full L3-orchestrator-level run of the project-specific custom-command
  scenario (verified at the transport level directly; loose-voice-
  reference resolution of a custom command hasn't been run live — see
  scenario 10 in `l3_orchestrator_test/adversarial_dictations/`)
