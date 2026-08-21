"""
State-layer data model. This is the contract agreed with ue6rruxg
(l5_console/app/, the Textual console) before either side wrote code --
see docs/SPEC-TUI.md §6 for the governing design and the peer-chat
negotiation this shape came out of.

Two hard requirements from the Lead, both structural in the types below,
not left to convention:
1. Staleness is per-section (`polled_at` + `expected_interval`), not one
   blanket timestamp -- §6.1's two clocks (cheap vs expensive checks)
   mean different sections are fresh on different schedules.
   `expected_interval` is the real cadence this section is actually
   polled at, exposed as data rather than assumed by the consumer --
   hardcoding it into the TUI's staleness thresholds would drift
   silently the moment a poll interval is tuned, the same class of bug
   `error` exists to prevent one level down. The CONSUMER (the TUI)
   computes "stale" from polled_at + expected_interval + its own
   multiplier; this layer's only job is to report both timing facts
   honestly.
2. A failed check must be structurally distinguishable from "checked
   successfully, found nothing" -- `error: str | None` per section, not
   silent omission. `teams=[]` because tmux is unreachable must never
   look like `teams=[]` because there are genuinely no teams.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Liveness is a shared 3-state vocabulary across the orchestrator and every
# team member (SPEC-TUI.md §4.4, §5.4) -- not two states. "stopped" means
# not running but reconnectable (session history exists on disk); "lost"
# means no history to reconnect to, so no Reconnect action exists at all.
# This distinction is why it's a string enum-of-intent, not a bool.
LIVENESS_RUNNING = "running"
LIVENESS_STOPPED = "stopped"
LIVENESS_LOST = "lost"


@dataclass
class RoleSlot:
    """One ENGINE role's state (SPEC-engine-roles.md): CONCIERGE or
    ORCHESTRATOR. `attached` mirrors engine.json's own persisted intent
    (identity only, no status field there -- see engine_roles.py); every
    other field below except attached/name/model/effort/tmux/working_dir
    is computed FRESH on every poll, never stored, so "attached" and
    "running" can never disagree by construction -- same rule the team
    registry already established."""
    attached: bool
    name: str | None
    model: str | None  # "haiku" | "sonnet" | "opus"
    effort: str | None  # "low" | "medium" | "high" | "xhigh" | "max"
    tmux: str | None
    working_dir: str | None
    claude_session: str | None  # the PERSISTED identity, not re-verified live every poll (same discipline as TeamMember.claude_session)
    liveness: str | None  # LIVENESS_RUNNING | LIVENESS_STOPPED | LIVENESS_LOST -- None only when attached is False
    tools_reachable: bool  # only meaningful when liveness == LIVENESS_RUNNING
    # Added 2026-08-20 (SPEC-engine-roles.md, the cwd-mismatch gap): a
    # live tmux session WAS found under `tmux`, but at a cwd other than
    # `working_dir`. The liveness CONTRACT is unchanged by this -- such a
    # slot still reports liveness STOPPED/LOST, never RUNNING -- so a
    # consumer that only reads `liveness` behaves exactly as before.
    # These two fields exist so a consumer that DOES want to render the
    # difference (EnginePanel/RailEngine, via
    # format_helpers.role_status_phrase()) can, instead of a live session
    # rendering identically to a genuinely-dead one -- the failure this
    # field pair exists to make impossible to miss on screen. Both
    # default False/None so any construction site that predates this
    # (there is none left in this codebase, but a future one is safe by
    # default) behaves as "no mismatch observed."
    cwd_mismatch: bool = False
    found_cwd: str | None = None  # only meaningful when cwd_mismatch is True -- the session's REAL live working_dir, never shown as a raw path in the ENGINE panel (plain language only, per the Lead's ruling); Stream's diagnostic feed is where the actual path belongs
    # The 2026-08-20 stuck-concierge incident (an urgent fix, not part
    # of TODO-feature-queue.md's numbered list): role_liveness() now
    # captures+classifies the pane (read
    # only, no keystroke) for a RUNNING role, same cheap-check cadence as
    # everything else here. prompt_pending is True only when the pane is
    # genuinely PaneState.PERMISSION_PROMPT right now; prompt_preview is
    # a short, truncated, verbatim -- UNTRUSTED, same discipline as
    # TeamMember.blocked_question -- capture of the prompt for display.
    # Both default False/None: only meaningful when liveness ==
    # LIVENESS_RUNNING, same as tools_reachable above.
    prompt_pending: bool = False
    prompt_preview: str | None = None
    # docs/PLAN-silence-and-ux.md R5, the 2026-08-20 incident: this
    # role's MCP server subprocess was forked before the newest change
    # under l4_controller/ -- coarse (any relevant-directory change, not
    # real per-module import tracking) and deliberately biased toward
    # over-flagging, see engine_roles._role_server_stale()'s own
    # docstring for the full reasoning. Only meaningful when liveness ==
    # LIVENESS_RUNNING -- a stopped/lost role has no server to be stale.
    server_stale: bool = False


@dataclass
class EngineState:
    polled_at: float
    expected_interval: float  # the real cadence this section is polled at -- consumer's staleness threshold is a multiple of this, not a hardcoded guess
    error: str | None
    concierge: RoleSlot
    orchestrator: RoleSlot


@dataclass
class TeamMember:
    tmux: str | None  # current tmux binding; None when not currently running
    claude_session: str  # stable identity -- Claude Code's own session UUID, never PID/tmux name
    liveness: str
    activity: str | None  # only meaningful when liveness == LIVENESS_RUNNING
    is_lead: bool  # renamed from is_inbox (SPEC-teams.md SS2) -- vocabulary only, still computed, never stored per-member
    # Reserved for docs/SPEC-blockers.md, not yet populated by anything --
    # blocked is its own distinct member state (a running session quietly
    # waiting on a human, not a variant of idle/busy), so it needs its own
    # fields rather than overloading `activity`. Detection is NOT built
    # yet (pane_state.py has no BLOCKED_QUESTION state); these exist now
    # only so adding that detection later is additive to this contract,
    # not a breaking change to it. All three are None/False until then.
    blocked_question: str | None = None  # the captured question text, untrusted content, never instructions
    blocked_since: float | None = None  # unix timestamp blocking was first observed
    blocked_surfaced: bool = False  # whether this has already been spoken/escalated to Ayman
    # SPEC-teams.md SS6: model is cheap (already fetched at adoption,
    # previously discarded); effort is only ever known for a member THIS
    # app created itself -- verified live (2026-08-18) that /status does
    # not expose it at all, so an adopted (pre-existing) member's effort
    # is permanently None, not a bug to chase.
    model: str | None = None
    effort: str | None = None
    # SPEC-teams.md SS2: the last time member_identity.py actually
    # confirmed this member's claude_session matched at dispatch time --
    # None if never dispatched to. NOT re-verified on a poll (that
    # function is documented adoption/dispatch-time-only, never a polling
    # operation) -- a consumer showing "active" should render "unverified
    # since <this timestamp>" rather than a false-confident live status
    # once it's been a while, rather than this layer guessing a threshold.
    identity_verified_at: float | None = None


@dataclass
class Team:
    id: str
    aliases: list[str]
    root: str  # exact, unmodified registry root -- teams.py matches liveness against this byte-for-byte, never display_path
    has_lead: bool  # SPEC-teams.md SS2: is ANY lead pointer set at all -- independent of reachability, so "no lead, live members" is a distinct, renderable state from "lead unreachable"
    lead_reachable: bool  # renamed from inbox_reachable -- only meaningful when has_lead is True
    members: list[TeamMember] = field(default_factory=list)
    display_path: str = ""  # root with $HOME collapsed to "~" -- cosmetic only, see UnassignedSession.display_path
    # SPEC-teams.md SS3: folded directly into Team by the poller (per
    # ue6rruxg's request, 2026-08-18) rather than a second per-team call
    # path at render time -- every other panel renders from ONE
    # get_state() call per tick, and N teams x N context-file reads per
    # tick would be the first thing to break that. None = never captured.
    # A CLAUDE.md-sourced team's fields are re-read from the live file
    # every poll tick (never cached) so they're never stale by
    # construction, same instinct as everything else "liveness" in this
    # project; an agent-sourced team's fields come from the on-disk
    # capture and only change when Refresh is pressed.
    context_summary: str | None = None
    context_subsystems: list[str] = field(default_factory=list)
    context_tech_stack: list[str] = field(default_factory=list)
    context_captured_at: float | None = None
    context_source: str | None = None  # "agent" | "claude_md" | None
    # SPEC-gaps-and-build-plan.md SS1.6, Ayman's requirement 2026-08-20:
    # project agents are visible (their terminal window open) by default;
    # concierge/orchestrator stay invisible always -- this field exists
    # only on Team, never on RoleSlot, so there is no field to
    # accidentally wire an Engine role's window-opening through. Default
    # True mirrors teams.json entries written before this field existed
    # (teams.py reads `entry.get("visible", True)`), so an old registry
    # entry silently becomes "visible" rather than silently becoming
    # background -- matching the spec's own "visible by default."
    visible: bool = True


@dataclass
class WakeDaemonState:
    polled_at: float
    expected_interval: float
    error: str | None
    running: bool  # process alive -- the safety-critical field, §7: reflects reality, not intent
    # mic_active / IDLE-CAPTURING-CANCEL_ARMED state: deferred to when
    # ue6rruxg adds daemon.py status-file instrumentation (build step 4,
    # Signal+meter). Additive field, not a breaking change to this
    # contract -- see docs/SPEC-TUI.md and the peer-chat negotiation log.


@dataclass
class RuntimeState:
    polled_at: float
    expected_interval: float
    error: str | None  # covers models_resident + memory together
    models_resident: list[str]
    memory_free_pct: float | None
    spend_polled_at: float
    spend_expected_interval: float
    spend_error: str | None  # separate clock -- the explicit expensive/backed-off case, §6.1
    spend: dict | None  # providers.spend()'s shape verbatim: {"ok", "summary", "raw"}, orchestrator session only for v1


@dataclass
class UnassignedSession:
    tmux: str
    claude_session: str | None
    working_dir: str  # exact, unmodified pane_current_path -- use THIS, never display_path, for anything functional (e.g. passed as `root` when adopting), since teams.py matches liveness against it byte-for-byte
    display_path: str  # working_dir with $HOME collapsed to "~" -- cosmetic only, safe to truncate further at render time


@dataclass
class PendingSpeechItem:
    """One item in the return-channel queue (SPEC-orchestration.md SS2.3,
    l4_controller/return_queue.py's contract -- l5_console/state/poller.py
    maps that module's plain dicts to this 1:1, nothing invented). Mirrors
    return_queue.pending()'s shape exactly: kind is "completion" or
    "blocked_question" -- NEVER "error" or "answer". Refusals are never
    queued (the one priority-tier rule that survives unchanged, per the
    spec), and an "answer" is a reply Ayman is waiting on, spoken
    immediately, so it is never pending long enough to render. This panel
    showing an "answer" would itself be the bug."""
    kind: str
    text: str  # what will be spoken, verbatim -- untrusted content, same discipline as TeamMember.blocked_question
    team: str | None
    queued_at: float


@dataclass
class PendingSpeechState:
    """SPEC-orchestration.md Phase 3: "the pending-utterance queue does
    NOT belong in Stream... queued speech is actionable. It needs its own
    surface." Same per-section staleness/error shape as every other
    cheap, ~1s-cadence section (WakeDaemonState is the closest analog) --
    return_queue.pending() is documented as "safe to call from the poller
    every tick; cheap, no I/O beyond one small file read.\""""
    polled_at: float
    expected_interval: float
    error: str | None
    items: list[PendingSpeechItem] = field(default_factory=list)


@dataclass
class JarvisState:
    engine: EngineState
    teams: list[Team]
    teams_polled_at: float
    teams_expected_interval: float
    teams_error: str | None
    wake: WakeDaemonState
    runtime: RuntimeState
    pending_speech: PendingSpeechState
    unassigned: list[UnassignedSession] = field(default_factory=list)
