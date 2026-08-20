"""
The two-clock background poller (SPEC-TUI.md SS6.1) and get_state() --
the whole state-layer/console-app contract in one call.

**get_state() is cheap and side-effect-free by construction.** It only
ever returns a reference to an internally-maintained snapshot; it never
does real work itself. If it ever did, render rate would silently become
poll rate and the whole dual-clock design collapses -- this was an
explicit hard requirement from the Lead during the contract negotiation
with ue6rruxg (l5_console/app/), not a style preference.

Each section runs on its own background thread, its own interval, and
never blocks another section's update -- an expensive spend() round-trip
stalling does not stall orchestrator/wake liveness, which is exactly the
point of separating the clocks per SS6.1.

**On a failed check: the previous good DATA is kept and `error` is set,
but `polled_at` is still advanced on every attempt, success or failure.**
`polled_at` answers "is this loop alive and attempting", not "did the
last attempt succeed" -- `error` is the signal for the latter. Conflating
them was a real bug (found live 2026-08-18, Ayman's own console): a check
that had never once succeeded left its polled_at at the initial 0.0
forever, so staleness computed as ~56 years old, and the UI showed a
healthy loop that was failing every attempt as "STALE" -- indistinguishable
from a loop that had silently died. Advancing polled_at unconditionally
and leaving `error` to carry the failure keeps those two questions
separate.
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "l4_controller"))
import blocked_state  # noqa: E402
import dispatch_state as dispatch_state_mod  # noqa: E402
import say_feedback  # noqa: E402

import engine_roles as engine_roles_mod
import runtime as runtime_mod
import teams as teams_mod
import wake as wake_mod
import wake_signal as wake_signal_mod
from models import (
    EngineState,
    JarvisState,
    RoleSlot,
    RuntimeState,
    WakeDaemonState,
)

ENGINE_INTERVAL_S = 1.0
WAKE_INTERVAL_S = 1.0
TEAMS_INTERVAL_S = 3.0  # effective cadence once per-member activity is added; membership/liveness alone would be ~1s
RUNTIME_INTERVAL_S = 2.5
SPEND_INTERVAL_S = 45.0

# SPEC-blockers.md SS5.3: "a session that blocks and unblocks itself
# within seconds is not worth reporting." No spec-mandated number --
# 5s is a judgment call, easy to retune, chosen to comfortably cover a
# session answering its own question via a quick follow-up tool call
# without holding real blocks silent for long.
BLOCKED_SETTLE_DELAY_S = 5.0


def _empty_role_slot() -> RoleSlot:
    return RoleSlot(
        attached=False, name=None, model=None, effort=None, tmux=None, working_dir=None,
        claude_session=None, liveness=None, tools_reachable=False,
    )


def _initial_state() -> JarvisState:
    """Never-polled-yet placeholder -- error is set explicitly rather
    than leaving fields at a misleadingly "normal-looking" default, so a
    consumer that calls get_state() before start() has had a chance to
    complete a first pass sees "not yet polled", not a false "running"."""
    return JarvisState(
        engine=EngineState(
            polled_at=0.0, expected_interval=ENGINE_INTERVAL_S, error="not yet polled",
            concierge=_empty_role_slot(), orchestrator=_empty_role_slot(),
        ),
        teams=[], teams_polled_at=0.0, teams_expected_interval=TEAMS_INTERVAL_S, teams_error="not yet polled",
        wake=WakeDaemonState(
            polled_at=0.0, expected_interval=WAKE_INTERVAL_S, error="not yet polled", running=False,
        ),
        runtime=RuntimeState(
            polled_at=0.0, expected_interval=RUNTIME_INTERVAL_S, error="not yet polled",
            models_resident=[], memory_free_pct=None,
            spend_polled_at=0.0, spend_expected_interval=SPEND_INTERVAL_S, spend_error="not yet polled", spend=None,
        ),
        unassigned=[],
    )


class Poller:
    def __init__(self):
        self._lock = threading.Lock()
        self._state = _initial_state()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        loops = [
            self._loop_engine,
            self._loop_teams,
            self._loop_wake,
            self._loop_runtime,
            self._loop_spend,
        ]
        self._threads = [threading.Thread(target=loop, daemon=True) for loop in loops]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()

    def get_state(self) -> JarvisState:
        with self._lock:
            return self._state

    # --- individual clocks ---

    @staticmethod
    def _role_slot(liveness: dict) -> RoleSlot:
        record = liveness["record"] or {}
        return RoleSlot(
            attached=liveness["attached"],
            name=record.get("name"),
            model=record.get("model"),
            effort=record.get("effort"),
            tmux=record.get("tmux"),
            working_dir=record.get("working_dir"),
            claude_session=record.get("claude_session"),
            liveness=liveness["liveness"],
            tools_reachable=liveness["tools_reachable"],
            cwd_mismatch=liveness.get("cwd_mismatch", False),
            found_cwd=liveness.get("found_cwd"),
        )

    def _loop_engine(self) -> None:
        while not self._stop.is_set():
            try:
                state = engine_roles_mod.get_engine_state()
                new = EngineState(
                    polled_at=time.time(), expected_interval=ENGINE_INTERVAL_S, error=None,
                    concierge=self._role_slot(state["concierge"]),
                    orchestrator=self._role_slot(state["orchestrator"]),
                )
            except Exception as e:  # noqa: BLE001 -- must never take this thread down
                with self._lock:
                    old = self._state.engine
                new = replace(old, polled_at=time.time(), error=str(e))
            with self._lock:
                self._state.engine = new
            self._stop.wait(ENGINE_INTERVAL_S)

    def _loop_teams(self) -> None:
        while not self._stop.is_set():
            try:
                teams_list, unassigned_list = teams_mod.discover_teams_and_unassigned()
                # Expensive per-member tier (SS6.1, SPEC-blockers.md stage
                # 1): folded into this same cadence per TEAMS_INTERVAL_S's
                # own comment, not a separate slower loop. Mutates
                # teams_list's members in place -- detection only, no
                # speaking here (enrich_running_members() never speaks by
                # design; see its docstring).
                teams_mod.enrich_running_members(teams_list)
                with self._lock:
                    self._state.teams = teams_list
                    self._state.unassigned = unassigned_list
                    self._state.teams_polled_at = time.time()
                    self._state.teams_expected_interval = TEAMS_INTERVAL_S
                    self._state.teams_error = None
                self._maybe_escalate_blocked(teams_list)
            except Exception as e:  # noqa: BLE001
                with self._lock:
                    self._state.teams_polled_at = time.time()
                    self._state.teams_expected_interval = TEAMS_INTERVAL_S
                    self._state.teams_error = str(e)
            self._stop.wait(TEAMS_INTERVAL_S)

    def _maybe_escalate_blocked(self, teams_list) -> None:
        """SPEC-blockers.md SS5.3: not immediately on detection. Collects
        every member that has been blocked (and unsurfaced) for at least
        BLOCKED_SETTLE_DELAY_S, and -- only if it is actually safe to
        speak right now -- announces all of them in ONE batched utterance
        naming sessions, never reading the question text aloud (the
        console shows the detail; voice is the notification). Runs every
        tick but is a no-op unless something is both pending and safe --
        so a block detected mid-dictation simply waits here, unspoken,
        until a later tick finds the mic clear, which is what makes this
        "speak at the end of the dictation" rather than "speak on a
        timer" in practice."""
        now = time.time()
        pending = [
            (team, member)
            for team in teams_list
            for member in team.members
            if member.blocked_question is not None
            and not member.blocked_surfaced
            and member.blocked_since is not None
            and (now - member.blocked_since) >= BLOCKED_SETTLE_DELAY_S
        ]
        if not pending or not self._safe_to_speak():
            return

        labels = [self._member_label(team, member) for team, member in pending]
        if len(labels) == 1:
            text = f"{labels[0]} is waiting on a question."
        else:
            text = f"{len(labels)} sessions are waiting on questions: " + ", ".join(labels[:-1]) + f", and {labels[-1]}."
        say_feedback.speak(text)

        for team, member in pending:
            blocked_state.mark_surfaced(member.claude_session)
            with self._lock:
                member.blocked_surfaced = True

    def _safe_to_speak(self) -> bool:
        """Fails closed on anything uncertain. Two independent gates:

        1. The mic. If the wake daemon isn't even running (confirmed via
           the pgrep-based JarvisState.wake.running, not this file's own
           existence), there's no live mic session to corrupt, so it's
           safe regardless of wake_state.json's content -- a
           missing/stale wake_state.json with wake.running=False must
           never itself be read as "unsafe," only as "not listening."
           If the daemon IS running, wake_state.json must exist, be
           fresh, and read IDLE -- CAPTURING or CANCEL_ARMED (or a stale
           read, which per app/wake_state.py's own discipline must never
           be trusted as "quiet, so it's fine") both block speaking.
        2. Nothing in flight. dispatch_state.py's "forwarded" stage is
           written the instant a transcript reaches the router and stays
           that way until deliver_batch() marks it "complete" --
           confirm_plan's spoken plan summary and cancel window both
           happen inside that window, so gating on it also happens to be
           exactly "after the plan summary": the first tick where
           forwarded flips away is already past it. Checks ANY dispatch,
           not just the most recent -- dispatch_state.py is now keyed by
           dictation_ref (SPEC-orchestration.md SS0.1) because more than
           one can be genuinely in flight at once; gating on only the
           latest would miss an earlier dictation still running while a
           later one has already been forwarded too."""
        with self._lock:
            wake_running = self._state.wake.running
        if wake_running:
            signal = wake_signal_mod.read_wake_signal()
            if signal is None or signal.stale or signal.state != "IDLE":
                return False

        if dispatch_state_mod.any_forwarded():
            return False
        return True

    @staticmethod
    def _member_label(team, member) -> str:
        alias = team.aliases[0] if team.aliases else team.id
        if len(team.members) == 1:
            return alias
        return f"{alias}'s {member.tmux}"

    def _loop_wake(self) -> None:
        while not self._stop.is_set():
            try:
                running = wake_mod.is_running()
                new = WakeDaemonState(
                    polled_at=time.time(), expected_interval=WAKE_INTERVAL_S, error=None, running=running,
                )
            except Exception as e:  # noqa: BLE001
                with self._lock:
                    old = self._state.wake
                new = replace(old, polled_at=time.time(), error=str(e))
            with self._lock:
                self._state.wake = new
            self._stop.wait(WAKE_INTERVAL_S)

    def _loop_runtime(self) -> None:
        while not self._stop.is_set():
            models, models_err = runtime_mod.resident_models()
            mem, mem_err = runtime_mod.memory_free_pct()
            error = models_err or mem_err
            with self._lock:
                old = self._state.runtime
                self._state.runtime = replace(
                    old,
                    polled_at=time.time(),
                    expected_interval=RUNTIME_INTERVAL_S,
                    error=error,
                    models_resident=models if models_err is None else old.models_resident,
                    memory_free_pct=mem if mem_err is None else old.memory_free_pct,
                )
            self._stop.wait(RUNTIME_INTERVAL_S)

    def _loop_spend(self) -> None:
        while not self._stop.is_set():
            try:
                result = runtime_mod.orchestrator_spend()
                with self._lock:
                    old = self._state.runtime
                    if result.get("ok"):
                        self._state.runtime = replace(
                            old,
                            spend=result,
                            spend_polled_at=time.time(),
                            spend_expected_interval=SPEND_INTERVAL_S,
                            spend_error=None,
                        )
                    else:
                        # providers.spend()'s failure shape carries no detail
                        # (deliberately, per its own "never fabricate" rule --
                        # ok=False/summary=None/raw=None on any unparseable or
                        # refused check, no invented reason string).
                        self._state.runtime = replace(
                            old, spend_polled_at=time.time(), spend_expected_interval=SPEND_INTERVAL_S,
                            spend_error="orchestrator /cost check did not return a parseable result",
                        )
            except Exception as e:  # noqa: BLE001
                with self._lock:
                    old = self._state.runtime
                    self._state.runtime = replace(
                        old, spend_polled_at=time.time(), spend_expected_interval=SPEND_INTERVAL_S, spend_error=str(e),
                    )
            self._stop.wait(SPEND_INTERVAL_S)
