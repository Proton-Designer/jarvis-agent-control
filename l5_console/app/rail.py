"""
Rail: the narrow-pane density (SPEC-TUI.md §3). Status lines + recent
activity, always-visible, steals no room. First layout built, per the
build order (§9) -- Console and Signal come after.

Renders JarvisState read through one call (see main.py's `get_state`
import) -- never holds its own copy of truth between polls. Every
section checks staleness.is_stale() against its own polled_at/
expected_interval before deciding how to render, per the contract's
governing rule: a stopped poller must never look identical to a live one.
"""
from __future__ import annotations

import time

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from staleness import is_stale

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "state"))
from models import JarvisState, LIVENESS_RUNNING, LIVENESS_STOPPED, LIVENESS_LOST  # noqa: E402


def _liveness_icon(liveness: str) -> str:
    return {LIVENESS_RUNNING: "●", LIVENESS_STOPPED: "○", LIVENESS_LOST: "✕"}.get(liveness, "?")


def _staleness_suffix(polled_at: float, expected_interval: float) -> str:
    """Appended to any status line whose backing data is stale -- never a
    subtle color change alone (SPEC-TUI.md §7: "better to look broken
    while broken than healthy while stale"). Text that survives a
    no-color terminal and a quick glance both."""
    return " [STALE]" if is_stale(polled_at, expected_interval) else ""


def _wake_line(wake) -> str:
    """The one line in this whole app that carries a real safety
    property (SPEC-TUI.md §7): it reflects state.wake.running, which
    itself only ever reflects a real pgrep of the daemon process --
    never whether a button was clicked, never this widget's own memory
    of what it last did. If the underlying poll is stale, that's shown
    explicitly rather than trusting the last-known running value."""
    if wake.error:
        return f"⚠ wake: error -- {wake.error}"
    if is_stale(wake.polled_at, wake.expected_interval):
        return f"? wake: unknown (data stale, last checked {time.time() - wake.polled_at:.0f}s ago)"
    icon = "●" if wake.running else "○"
    label = "listening" if wake.running else "stopped"
    return f"{icon} wake: {label}"


class RailWake(Static):
    def update_state(self, wake) -> None:
        self.update(_wake_line(wake))


class RailOrchestrator(Static):
    def update_state(self, orch) -> None:
        if orch.error:
            self.update(f"⚠ orchestrator: error -- {orch.error}")
            return
        icon = _liveness_icon(orch.liveness)
        tools = "tools ok" if orch.tools_reachable else "NO TOOLS"
        suffix = _staleness_suffix(orch.polled_at, orch.expected_interval)
        self.update(f"{icon} orchestrator: {orch.liveness} · {tools}{suffix}")


class RailTeams(Static):
    def update_state(self, teams: list, teams_error, polled_at, expected_interval) -> None:
        if teams_error:
            self.update(f"⚠ teams: error -- {teams_error}")
            return
        if not teams:
            self.update("teams: none configured")
            return
        live = sum(1 for t in teams if any(m.liveness == LIVENESS_RUNNING for m in t.members))
        suffix = _staleness_suffix(polled_at, expected_interval)
        lines = [f"teams: {len(teams)} ({live} live){suffix}"]
        for t in teams:
            team_liveness = _team_liveness(t)
            lines.append(f"  {_liveness_icon(team_liveness)} {t.id} ({len(t.members)})")
        self.update("\n".join(lines))


def _team_liveness(team) -> str:
    """Derived from members, never stored -- per the Lead's ruling: a
    team-level liveness field that could disagree with its own members
    is a second source of truth that can drift from the first. running
    if ANY member is running (the team is reachable through at least
    one path); otherwise stopped if any member has resumable history,
    else lost."""
    if not team.members:
        return LIVENESS_LOST
    if any(m.liveness == LIVENESS_RUNNING for m in team.members):
        return LIVENESS_RUNNING
    if any(m.liveness == LIVENESS_STOPPED for m in team.members):
        return LIVENESS_STOPPED
    return LIVENESS_LOST


class RailRuntime(Static):
    """Ambient, not primary (§3) -- deliberately the least prominent
    section in Rail."""

    def update_state(self, runtime) -> None:
        if runtime.error:
            self.update(f"runtime: error -- {runtime.error}")
            return
        models = ", ".join(runtime.models_resident) if runtime.models_resident else "none warm"
        mem = f"{runtime.memory_free_pct:.0f}% free" if runtime.memory_free_pct is not None else "mem ?"
        suffix = _staleness_suffix(runtime.polled_at, runtime.expected_interval)
        spend_suffix = _staleness_suffix(runtime.spend_polled_at, runtime.spend_expected_interval)
        spend = runtime.spend["summary"] if runtime.spend and runtime.spend.get("ok") else "spend ?"
        self.update(f"runtime: {models} · {mem}{suffix}\n  {spend}{spend_suffix}")


class RailUnassigned(Static):
    def update_state(self, unassigned: list) -> None:
        if not unassigned:
            self.update("")
            return
        self.update(f"⚑ {len(unassigned)} unassigned session(s) -- press 'a' to adopt")


class Rail(Widget):
    """Root Rail widget: composes every status line, refreshed on every
    poll. No widget here ever computes its own state -- each just
    formats what JarvisState already reported."""

    DEFAULT_CSS = """
    Rail {
        width: 100%;
        height: 100%;
        padding: 1;
    }
    Rail > Vertical {
        height: auto;
    }
    #rail_activity {
        height: 1fr;
        border-top: solid $panel;
        margin-top: 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            yield RailWake(id="rail_wake")
            yield RailOrchestrator(id="rail_orch")
            yield RailTeams(id="rail_teams")
            yield RailRuntime(id="rail_runtime")
            yield RailUnassigned(id="rail_unassigned")
        with VerticalScroll(id="rail_activity"):
            yield Static("recent activity", id="rail_activity_header")

    def update_state(self, state: JarvisState) -> None:
        self.query_one("#rail_wake", RailWake).update_state(state.wake)
        self.query_one("#rail_orch", RailOrchestrator).update_state(state.orchestrator)
        self.query_one("#rail_teams", RailTeams).update_state(
            state.teams, state.teams_error, state.teams_polled_at, state.teams_expected_interval
        )
        self.query_one("#rail_runtime", RailRuntime).update_state(state.runtime)
        self.query_one("#rail_unassigned", RailUnassigned).update_state(state.unassigned)
