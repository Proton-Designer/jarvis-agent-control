"""
Console: the full-window density (SPEC-TUI.md §3). Wake / orchestrator /
runtime down the left; stream and teams on the right. Build order step 3
-- same JarvisState as Rail, fuller detail because there's room for it.
"""
from __future__ import annotations

import time

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widget import Widget
from textual.widgets import Button
from widgets import PlainStatic

from staleness import is_stale
from stream import StreamReader
from format_helpers import liveness_icon, team_liveness
import wake_control

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "state"))
from models import JarvisState, LIVENESS_RUNNING  # noqa: E402

CONSOLE_ACTIVITY_MAX_LINES = 200


def _staleness_note(polled_at: float, expected_interval: float) -> str:
    return " [STALE -- data has stopped updating]" if is_stale(polled_at, expected_interval) else ""


class WakePanel(Widget):
    """The one interactive control with a real safety property (§7).
    The button only ever triggers wake_control.start()/stop() -- the
    label and icon it shows are driven exclusively by
    JarvisState.wake.running from the next poll, never by which button
    was clicked or this widget's own memory of the click. A click that
    fails to actually start/stop the process shows as an unchanged
    status line on the next poll, not a lie."""

    DEFAULT_CSS = """
    WakePanel { height: auto; border: solid $panel; padding: 1; margin-bottom: 1; }
    WakePanel #wake_status { margin-bottom: 1; }
    """

    def compose(self) -> ComposeResult:
        yield PlainStatic("", id="wake_status")
        yield PlainStatic("", id="wake_action_result")
        yield Button("start", id="wake_button", variant="success")

    def update_state(self, wake) -> None:
        status = self.query_one("#wake_status", PlainStatic)
        button = self.query_one("#wake_button", Button)
        if wake.error:
            status.update(f"⚠ wake: error -- {wake.error}")
            button.disabled = True
            return
        if is_stale(wake.polled_at, wake.expected_interval):
            status.update(f"? wake: unknown (data stale, last checked {time.time() - wake.polled_at:.0f}s ago)")
            button.disabled = True
            return
        button.disabled = False
        if wake.running:
            status.update("● wake: listening")
            button.label = "stop"
            button.variant = "error"
        else:
            status.update("○ wake: stopped")
            button.label = "start"
            button.variant = "success"

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "wake_button":
            return
        result_line = self.query_one("#wake_action_result", PlainStatic)
        # Read the button's CURRENT label to decide the action -- not a
        # separately-tracked "is it running" flag on this widget, since
        # that would be exactly the kind of intent-not-reality state §7
        # rules out. The label itself was set from the last real poll.
        if self.query_one("#wake_button", Button).label == "start":
            ok, msg = wake_control.start()
        else:
            ok, msg = wake_control.stop()
        result_line.update(("✓ " if ok else "⚠ ") + msg)


class OrchestratorPanel(PlainStatic):
    DEFAULT_CSS = "OrchestratorPanel { height: auto; border: solid $panel; padding: 1; margin-bottom: 1; }"

    def update_state(self, orch) -> None:
        if orch.error:
            self.update(f"⚠ orchestrator: error -- {orch.error}")
            return
        icon = liveness_icon(orch.liveness)
        tools = "tools reachable" if orch.tools_reachable else "NO TOOLS -- check MCP prompt"
        sid = orch.session_id or "no session"
        self.update(
            f"{icon} orchestrator: {orch.liveness}\n"
            f"  {tools}\n"
            f"  session: {sid}"
            f"{_staleness_note(orch.polled_at, orch.expected_interval)}"
        )


class RuntimePanel(PlainStatic):
    """Ambient, not primary (§3) -- least visually prominent panel."""

    DEFAULT_CSS = "RuntimePanel { height: auto; border: solid $panel; padding: 1; margin-bottom: 1; color: $text-muted; }"

    def update_state(self, runtime) -> None:
        if runtime.error:
            self.update(f"runtime: error -- {runtime.error}")
            return
        models = ", ".join(runtime.models_resident) if runtime.models_resident else "none warm"
        mem = f"{runtime.memory_free_pct:.0f}% free" if runtime.memory_free_pct is not None else "mem ?"
        spend = runtime.spend["summary"] if runtime.spend and runtime.spend.get("ok") else "spend unavailable"
        self.update(
            f"runtime\n"
            f"  models resident: {models}\n"
            f"  memory: {mem}{_staleness_note(runtime.polled_at, runtime.expected_interval)}\n"
            f"  spend: {spend}{_staleness_note(runtime.spend_polled_at, runtime.spend_expected_interval)}"
        )


class TeamsPanel(PlainStatic):
    """Fuller than Rail's condensed count -- every member, its activity,
    and which one is the inbox (SPEC-TUI.md §4.5: the inbox is the
    default and always wins on an ambiguous reference, so it's always
    shown, not just implied)."""

    DEFAULT_CSS = "TeamsPanel { height: 1fr; border: solid $panel; padding: 1; }"

    def update_state(self, teams: list, teams_error, unassigned: list, polled_at, expected_interval) -> None:
        if teams_error:
            self.update(f"⚠ teams: error -- {teams_error}")
            return
        lines = [f"teams{_staleness_note(polled_at, expected_interval)}"]
        if not teams:
            lines.append("  none configured")
        for t in teams:
            lines.append(f"\n  {liveness_icon(team_liveness(t))} {t.id}  ({t.root})")
            for m in t.members:
                inbox_marker = " [inbox]" if m.is_inbox else ""
                activity = f" -- {m.activity}" if m.activity else ""
                binding = m.tmux or "(not bound)"
                lines.append(f"    {liveness_icon(m.liveness)} {binding}{inbox_marker}{activity}")
        if unassigned:
            lines.append(f"\n  ⚑ {len(unassigned)} unassigned session(s):")
            for u in unassigned:
                lines.append(f"    {u.tmux}  ({u.working_dir})")
        self.update("\n".join(lines))


class StreamPanel(Widget):
    """Live log of wake scores, state transitions, routing (§3) -- see
    stream.py's own docstring for what's actually in the feed today and
    what would need new daemon.py instrumentation (per-frame wake
    scores, not yet logged anywhere)."""

    DEFAULT_CSS = "StreamPanel { height: 1fr; border: solid $panel; padding: 1; margin-bottom: 1; }"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._stream = StreamReader()
        self._lines: list[str] = []

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="stream_scroll"):
            yield PlainStatic("stream", id="stream_body")

    def poll_stream(self) -> None:
        new_lines = self._stream.poll()
        if not new_lines:
            return
        self._lines.extend(new_lines)
        self._lines = self._lines[-CONSOLE_ACTIVITY_MAX_LINES:]
        self.query_one("#stream_body", PlainStatic).update("\n".join(self._lines))
        self.query_one("#stream_scroll", VerticalScroll).scroll_end(animate=False)


class Console(Widget):
    """Root Console widget. Two columns: left = wake/orchestrator/
    runtime (control + ambient), right = stream/teams (what's actually
    happening) -- §3's explicit layout."""

    DEFAULT_CSS = """
    Console { width: 100%; height: 100%; padding: 1; }
    Console > Horizontal { height: 100%; }
    #console_left { width: 34; height: 100%; }
    #console_right { width: 1fr; height: 100%; }
    """

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical(id="console_left"):
                yield WakePanel(id="console_wake")
                yield OrchestratorPanel(id="console_orch")
                yield RuntimePanel(id="console_runtime")
            with Vertical(id="console_right"):
                yield StreamPanel(id="console_stream")
                yield TeamsPanel(id="console_teams")

    def update_state(self, state: JarvisState) -> None:
        self.query_one("#console_wake", WakePanel).update_state(state.wake)
        self.query_one("#console_orch", OrchestratorPanel).update_state(state.orchestrator)
        self.query_one("#console_runtime", RuntimePanel).update_state(state.runtime)
        self.query_one("#console_teams", TeamsPanel).update_state(
            state.teams, state.teams_error, state.unassigned, state.teams_polled_at, state.teams_expected_interval
        )
        self.query_one("#console_stream", StreamPanel).poll_stream()
