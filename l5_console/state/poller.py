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

**On a failed check: the previous good data is kept, `error` is set, and
`polled_at` is NOT advanced.** A failure isn't fresher data, it's a
confirmation the check itself is broken right now -- leaving polled_at at
the last real success means staleness (polled_at + expected_interval,
computed by the consumer) grows naturally across repeated failures rather
than looking falsely fresh.
"""

from __future__ import annotations

import threading
import time
from dataclasses import replace

import orchestrator as orchestrator_mod
import runtime as runtime_mod
import teams as teams_mod
import wake as wake_mod
from models import (
    LIVENESS_LOST,
    JarvisState,
    OrchestratorState,
    RuntimeState,
    WakeDaemonState,
)

ORCHESTRATOR_INTERVAL_S = 1.0
WAKE_INTERVAL_S = 1.0
TEAMS_INTERVAL_S = 3.0  # effective cadence once per-member activity is added; membership/liveness alone would be ~1s
RUNTIME_INTERVAL_S = 2.5
SPEND_INTERVAL_S = 45.0


def _initial_state() -> JarvisState:
    """Never-polled-yet placeholder -- error is set explicitly rather
    than leaving fields at a misleadingly "normal-looking" default, so a
    consumer that calls get_state() before start() has had a chance to
    complete a first pass sees "not yet polled", not a false "running"."""
    return JarvisState(
        orchestrator=OrchestratorState(
            polled_at=0.0, expected_interval=ORCHESTRATOR_INTERVAL_S, error="not yet polled",
            liveness=LIVENESS_LOST, session_id=None, tools_reachable=False,
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
            self._loop_orchestrator,
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

    def _loop_orchestrator(self) -> None:
        while not self._stop.is_set():
            try:
                info = orchestrator_mod.compute_orchestrator_state()
                new = OrchestratorState(
                    polled_at=time.time(), expected_interval=ORCHESTRATOR_INTERVAL_S, error=None, **info,
                )
            except Exception as e:  # noqa: BLE001 -- must never take this thread down
                with self._lock:
                    old = self._state.orchestrator
                new = replace(old, error=str(e))
            with self._lock:
                self._state.orchestrator = new
            self._stop.wait(ORCHESTRATOR_INTERVAL_S)

    def _loop_teams(self) -> None:
        while not self._stop.is_set():
            try:
                teams_list, unassigned_list = teams_mod.discover_teams_and_unassigned()
                with self._lock:
                    self._state.teams = teams_list
                    self._state.unassigned = unassigned_list
                    self._state.teams_polled_at = time.time()
                    self._state.teams_expected_interval = TEAMS_INTERVAL_S
                    self._state.teams_error = None
            except Exception as e:  # noqa: BLE001
                with self._lock:
                    self._state.teams_error = str(e)
            self._stop.wait(TEAMS_INTERVAL_S)

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
                new = replace(old, error=str(e))
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
                    polled_at=time.time() if error is None else old.polled_at,
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
                            old, spend_expected_interval=SPEND_INTERVAL_S,
                            spend_error="orchestrator /cost check did not return a parseable result",
                        )
            except Exception as e:  # noqa: BLE001
                with self._lock:
                    old = self._state.runtime
                    self._state.runtime = replace(
                        old, spend_expected_interval=SPEND_INTERVAL_S, spend_error=str(e),
                    )
            self._stop.wait(SPEND_INTERVAL_S)
