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
from textual.widget import Widget
from widgets import PlainStatic

from staleness import is_stale
from stream import StreamReader
from format_helpers import liveness_icon, team_liveness
from meter import Meter

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "state"))
from models import JarvisState, LIVENESS_RUNNING  # noqa: E402

RAIL_ACTIVITY_MAX_LINES = 30


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


class RailWake(PlainStatic):
    def update_state(self, wake) -> None:
        self.update(_wake_line(wake))


class RailOrchestrator(PlainStatic):
    def update_state(self, orch) -> None:
        if orch.error:
            self.update(f"⚠ orchestrator: error -- {orch.error}")
            return
        icon = liveness_icon(orch.liveness)
        tools = "tools ok" if orch.tools_reachable else "NO TOOLS"
        suffix = _staleness_suffix(orch.polled_at, orch.expected_interval)
        self.update(f"{icon} orchestrator: {orch.liveness} · {tools}{suffix}")


class RailTeams(PlainStatic):
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
            lines.append(f"  {liveness_icon(team_liveness(t))} {t.id} ({len(t.members)})")
        self.update("\n".join(lines))


class RailRuntime(PlainStatic):
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


class RailUnassigned(PlainStatic):
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

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._stream = StreamReader()
        self._activity_lines: list[str] = []
        self._wake_running = False  # cached from the last slow-clock update_state(); see update_meter()

    def compose(self) -> ComposeResult:
        with Vertical():
            yield RailWake(id="rail_wake")
            yield Meter(id="rail_meter", width=16)
            yield RailOrchestrator(id="rail_orch")
            yield RailTeams(id="rail_teams")
            yield RailRuntime(id="rail_runtime")
            yield RailUnassigned(id="rail_unassigned")
        with VerticalScroll(id="rail_activity"):
            yield PlainStatic("", id="rail_activity_body")

    def update_state(self, state: JarvisState) -> None:
        self.query_one("#rail_wake", RailWake).update_state(state.wake)
        # Only trust wake.running as a real "is it alive" signal when the
        # reading itself is neither errored nor stale -- an error/stale
        # wake reading must never let the meter claim it's showing live
        # data (matches WakePanel's identical guard in console.py).
        self._wake_running = bool(
            state.wake.running
            and not state.wake.error
            and not is_stale(state.wake.polled_at, state.wake.expected_interval)
        )
        self.query_one("#rail_orch", RailOrchestrator).update_state(state.orchestrator)
        self.query_one("#rail_teams", RailTeams).update_state(
            state.teams, state.teams_error, state.teams_polled_at, state.teams_expected_interval
        )
        self.query_one("#rail_runtime", RailRuntime).update_state(state.runtime)
        self.query_one("#rail_unassigned", RailUnassigned).update_state(state.unassigned)
        self._update_activity()

    def update_meter(self, wake_state) -> None:
        """Called on main.py's separate, faster clock (SPEC-TUI.md §6.1's
        dual-clock principle -- the meter needs to look live, JarvisState's
        ~1s poll doesn't). wake_running comes from the last slow-clock
        update_state(), not re-derived here -- at most ~1s stale on "is it
        running at all," which is the JarvisState poll's own native
        cadence anyway, not a new staleness this introduces."""
        self.query_one("#rail_meter", Meter).update_meter(self._wake_running, wake_state)

    def _update_activity(self) -> None:
        new_lines = self._stream.poll()
        if not new_lines:
            return
        self._activity_lines.extend(new_lines)
        self._activity_lines = self._activity_lines[-RAIL_ACTIVITY_MAX_LINES:]
        body = self.query_one("#rail_activity_body", PlainStatic)
        body.update("\n".join(self._activity_lines))
        self.query_one("#rail_activity", VerticalScroll).scroll_end(animate=False)
