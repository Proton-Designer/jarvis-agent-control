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
class OrchestratorState:
    polled_at: float
    expected_interval: float  # the real cadence this section is polled at -- consumer's staleness threshold is a multiple of this, not a hardcoded guess
    error: str | None
    liveness: str  # LIVENESS_RUNNING | LIVENESS_STOPPED | LIVENESS_LOST
    session_id: str | None
    tools_reachable: bool  # orchestrator_has_tools() -- see l4_controller/l2_l3_handoff.py


@dataclass
class TeamMember:
    tmux: str | None  # current tmux binding; None when not currently running
    claude_session: str  # stable identity -- Claude Code's own session UUID, never PID/tmux name
    liveness: str
    activity: str | None  # only meaningful when liveness == LIVENESS_RUNNING
    is_inbox: bool


@dataclass
class Team:
    id: str
    aliases: list[str]
    root: str
    inbox_reachable: bool
    members: list[TeamMember] = field(default_factory=list)


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
    working_dir: str


@dataclass
class JarvisState:
    orchestrator: OrchestratorState
    teams: list[Team]
    teams_polled_at: float
    teams_expected_interval: float
    teams_error: str | None
    wake: WakeDaemonState
    runtime: RuntimeState
    unassigned: list[UnassignedSession] = field(default_factory=list)
