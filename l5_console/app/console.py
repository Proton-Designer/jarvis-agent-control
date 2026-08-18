"""
Console: the full-window density (SPEC-TUI.md §3). Wake / orchestrator /
runtime down the left; stream and teams on the right. Build order step 3
-- same JarvisState as Rail, fuller detail because there's room for it.

Visual pass against docs/console-design-studies.html (the Lead's
"the mockup is the spec" instruction, 2026-08-18): panel captions live
in the border via Textual's native border_title, not hand-drawn -- the
mockup's absolutely-positioned `.cap` span is a web-CSS way to fake what
a terminal's own box-drawing border can do directly. Status text uses
Rich Text objects with the mockup's own palette (format_helpers.COLOR_*,
copied from its CSS variables) for the dim-label/bright-value/semantic-
status hierarchy -- Text objects render through Rich's protocol
directly, never through Static's markup parser, so this doesn't touch
or weaken the markup=False safety property PlainStatic exists for
(widgets.py) -- that property is about literal strings like "[STALE]",
not about pre-built Rich renderables.
"""
from __future__ import annotations

import asyncio
import time

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widget import Widget
from textual.widgets import Button
from widgets import PlainStatic, Footer

from staleness import is_stale
from stream import StreamReader
from format_helpers import (
    liveness_icon, liveness_color, team_liveness, compact_model_name,
    COLOR_OK, COLOR_WARN, COLOR_ERR, COLOR_ACCENT, COLOR_DIM, COLOR_INK,
)
from meter import Meter
import wake_control
from engine_flow import CreateRoleScreen, AttachRoleScreen

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "state"))
from models import JarvisState, LIVENESS_RUNNING, LIVENESS_STOPPED, LIVENESS_LOST  # noqa: E402
import engine_roles  # noqa: E402

CONSOLE_ACTIVITY_MAX_LINES = 200


def _staleness_note(polled_at: float, expected_interval: float) -> str:
    return " [STALE -- data has stopped updating]" if is_stale(polled_at, expected_interval) else ""


class WakePanel(Widget):
    """The one interactive control with a real safety property (§7).

    Two separate lines, deliberately never merged into one:
    - `#wake_status` is the ONLY thing that ever renders "listening" /
      "stopped" -- driven exclusively by JarvisState.wake.running from
      the next real poll, never by which button was clicked, never by
      wake_control's return value, never by this widget's own memory of
      what it last did. Stated explicitly so a later refactor can't
      quietly merge this with the action-result line below and
      reintroduce exactly the bug §7 rules out: rendering "stopped"
      before a poll has actually confirmed the process is gone.
    - `#wake_action_result` is an action LOG line -- "did the click
      succeed at triggering start/stop," which is a different, weaker
      claim than "is it running now." wake_control.stop() itself waits
      for real process death before returning (see its docstring), so by
      the time this line updates the process really is gone or the
      failure is real -- but this line is still just a report of one
      past action, not a substitute for #wake_status."""

    BORDER_TITLE = "WAKE"

    DEFAULT_CSS = """
    WakePanel { height: auto; border: solid $panel; padding: 1; margin-bottom: 1; border-title-color: $text-muted; }
    WakePanel #wake_status { margin-bottom: 1; }
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._wake_running = False  # cached from the last slow-clock update_state(); see update_meter()

    def compose(self) -> ComposeResult:
        yield PlainStatic("", id="wake_status")
        yield Meter(id="wake_meter", width=24)
        yield PlainStatic("", id="wake_action_result")
        yield Button("start", id="wake_button", variant="success")

    def update_meter(self, wake_state) -> None:
        self.query_one("#wake_meter", Meter).update_meter(self._wake_running, wake_state)

    def update_state(self, wake) -> None:
        # Stated explicitly per the Lead's ruling: this method is the
        # ONLY place "listening"/"stopped" ever gets written to
        # #wake_status, and it only ever does so from `wake.running` on
        # THIS poll -- never from wake_control.stop()'s return value,
        # never carried over from a previous call. A later refactor that
        # tries to "optimistically" set status.update("stopped") right
        # after a successful stop() call would reintroduce the exact bug
        # this rule exists to prevent: rendering "stopped" before a poll
        # has actually confirmed the process is gone.
        #
        # self._wake_running (read by update_meter() on the separate fast
        # clock) defaults to False here and is only set True in the fully
        # -trusted branch below -- an error or stale wake reading must
        # never let the meter claim it's showing live data.
        self._wake_running = False
        status = self.query_one("#wake_status", PlainStatic)
        button = self.query_one("#wake_button", Button)
        if wake.error:
            status.update(Text(f"⚠ error -- {wake.error}", style=COLOR_ERR))
            button.disabled = True
            return
        if is_stale(wake.polled_at, wake.expected_interval):
            status.update(Text(f"? unknown (data stale, last checked {time.time() - wake.polled_at:.0f}s ago)", style=COLOR_WARN))
            button.disabled = True
            return
        button.disabled = False
        if wake.running:
            status.update(Text("● LISTENING", style=f"bold {COLOR_OK}"))
            button.label = "stop"
            button.variant = "error"
            self._wake_running = True
        else:
            status.update(Text("○ stopped", style=COLOR_DIM))
            button.label = "start"
            button.variant = "success"

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "wake_button":
            return
        result_line = self.query_one("#wake_action_result", PlainStatic)
        button = self.query_one("#wake_button", Button)
        # Read the button's CURRENT label to decide the action -- not a
        # separately-tracked "is it running" flag on this widget, since
        # that would be exactly the kind of intent-not-reality state §7
        # rules out. The label itself was set from the last real poll.
        #
        # Both branches disable the button and show a transitional
        # "...ing" message immediately, symmetrically -- the click was
        # real and something IS happening, so saying nothing for up to
        # a full REFRESH_INTERVAL_S (the Lead's finding: it read as
        # broken, not slow) is the bug, not the poll-only-render
        # principle itself. #wake_status is still untouched here and
        # still only ever changes from a real poll in update_state() --
        # a transitional state is not an optimistic render of the
        # outcome, it's an honest description of "a click just
        # happened." The app-level poll burst kicked off below shrinks
        # how long that transitional state is visible; update_state()
        # re-enables the button and corrects the label the moment a
        # real poll lands either way.
        if button.label == "start":
            # SPEC-engine-roles.md SS6: starting the daemon opens the
            # microphone, and voice input with no live concierge/router
            # has nowhere to go -- refuse BEFORE arming the mic, and show
            # engine_roles.start_precondition()'s own detail verbatim
            # (never paraphrased into something generic; the whole point
            # is Ayman is told which role is missing and what to press).
            # Pure query, no side effects, so checking it costs nothing
            # even on the common case where it passes. This does not
            # touch the SPACE-key ruling -- that key is stop-only and
            # unaffected; this gate is on the BUTTON only.
            precondition = engine_roles.start_precondition()
            if not precondition["ok"]:
                result_line.update(Text(f"⚠ {precondition['detail']}", style=COLOR_ERR))
                return
            if hasattr(self.app, "start_wake_poll_burst"):
                self.app.start_wake_poll_burst()
            button.disabled = True
            button.label = "starting..."
            result_line.update(Text("starting...", style=COLOR_DIM))
            ok, msg = wake_control.start()
            result_line.update(Text(("✓ " if ok else "⚠ ") + msg, style=COLOR_OK if ok else COLOR_ERR))
            if not ok:
                button.disabled = False
                button.label = "start"
            return

        # Stop is the case that matters most (§7: the mic must actually
        # close, not just receive a signal) -- wake_control.stop() blocks
        # (as a coroutine, not the UI thread) until it has verified real
        # process death or genuinely exhausted SIGTERM+SIGKILL, which can
        # take a few seconds. Disable the button for that window so a
        # second click can't race the same stop() call -- update_state()
        # will correctly re-enable it once the next real poll confirms
        # wake.running is false, same path as any other state change.
        button.disabled = True
        button.label = "stopping..."
        result_line.update(Text("stopping...", style=COLOR_DIM))
        ok, msg = await wake_control.stop()
        result_line.update(Text(("✓ " if ok else "⚠ ") + msg, style=COLOR_OK if ok else COLOR_ERR))


class EnginePanel(Widget):
    """ENGINE (SPEC-engine-roles.md): CONCIERGE and ORCHESTRATOR, one
    role slot each. Both live inside this single bordered panel rather
    than two separate top-level panels -- the spec's own tree

        ENGINE
        +-- CONCIERGE
        +-- ORCHESTRATOR

    is nesting, and a second nested border per role would be visual noise
    at this width (#console_left is 34 columns); a caption line inside
    one outer panel reads as the same hierarchy without it.

    Buttons: a FIXED set of 5 per role, always mounted, visibility
    toggled via `.display` in update_state() -- not remove()/mount()
    cycles. This is a "which of a small known set is visible" state
    machine (SPEC-engine-roles.md SS7's exact matrix), the same shape
    WakePanel already handles by toggling one button's label/variant;
    here there are more buttons instead of one relabeled button, but the
    principle -- render only ever reflects the last real poll, never an
    optimistic guess -- is identical.
    """

    BORDER_TITLE = "ENGINE"

    DEFAULT_CSS = """
    EnginePanel { height: auto; border: solid $panel; padding: 1; margin-bottom: 1; border-title-color: $text-muted; }
    EnginePanel .role_caption { margin-top: 1; }
    EnginePanel .role_caption.first { margin-top: 0; }
    EnginePanel Button { width: 100%; margin-top: 1; }
    """

    def compose(self) -> ComposeResult:
        for i, role in enumerate(engine_roles.ROLES):
            classes = "role_caption first" if i == 0 else "role_caption"
            yield PlainStatic(role.upper(), id=f"{role}_caption", classes=classes)
            yield PlainStatic("", id=f"{role}_info")
            yield PlainStatic("", id=f"{role}_result")
            # success/default -- unattached, offering to acquire a session
            yield Button("Create", id=f"{role}_create", variant="success")
            yield Button("Attach", id=f"{role}_attach", variant="success")
            # attached-but-dead: bring it back before anything else
            yield Button("Activate", id=f"{role}_activate", variant="warning")
            # attached (either liveness): change or let go of it
            yield Button("Swap", id=f"{role}_swap")
            yield Button("Remove", id=f"{role}_remove", variant="error")

    def _role_widgets(self, role: str) -> dict[str, Widget]:
        return {
            "info": self.query_one(f"#{role}_info", PlainStatic),
            "result": self.query_one(f"#{role}_result", PlainStatic),
            "create": self.query_one(f"#{role}_create", Button),
            "attach": self.query_one(f"#{role}_attach", Button),
            "activate": self.query_one(f"#{role}_activate", Button),
            "swap": self.query_one(f"#{role}_swap", Button),
            "remove": self.query_one(f"#{role}_remove", Button),
        }

    def update_state(self, engine) -> None:
        note = _staleness_note(engine.polled_at, engine.expected_interval)
        for role in engine_roles.ROLES:
            w = self._role_widgets(role)
            if engine.error:
                w["info"].update(Text(f"⚠ error -- {engine.error}", style=COLOR_ERR))
                for key in ("create", "attach", "activate", "swap", "remove"):
                    w[key].display = False
                continue

            slot = getattr(engine, role)
            if not slot.attached:
                w["info"].update(Text("— no session attached —" + note, style=COLOR_DIM if not note else COLOR_WARN))
                w["create"].display = True
                w["attach"].display = True
                w["activate"].display = False
                w["swap"].display = False
                w["remove"].display = False
                continue

            live = slot.liveness == LIVENESS_RUNNING
            text = Text()
            text.append(f"{slot.name}\n", style=f"bold {COLOR_INK}")
            text.append(f"{compact_model_name(slot.model)} · {slot.effort}\n", style=COLOR_DIM)
            status_word = "active" if live else "inactive"
            status_color = COLOR_OK if live else COLOR_DIM
            text.append(f"{liveness_icon(slot.liveness)} {status_word}", style=status_color)
            if live and not slot.tools_reachable:
                text.append("  ⚠ no tools", style=COLOR_WARN)
            if note:
                text.append(note, style=COLOR_WARN)
            w["info"].update(text)

            w["create"].display = False
            w["attach"].display = False
            w["activate"].display = not live
            w["swap"].display = True
            w["remove"].display = True

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        for role in engine_roles.ROLES:
            if button_id == f"{role}_create":
                self.app.push_screen(CreateRoleScreen(role))
                return
            if button_id in (f"{role}_attach", f"{role}_swap"):
                self.app.push_screen(AttachRoleScreen(role))
                return
            if button_id == f"{role}_remove":
                await self._do_remove(role)
                return
            if button_id == f"{role}_activate":
                await self._do_activate(role)
                return

    async def _do_remove(self, role: str) -> None:
        w = self._role_widgets(role)
        w["remove"].disabled = True
        w["result"].update(Text("removing...", style=COLOR_DIM))
        result = await asyncio.to_thread(engine_roles.remove_role, role)
        w["result"].update(Text(("✓ " if result["ok"] else "⚠ ") + result["detail"], style=COLOR_OK if result["ok"] else COLOR_ERR))
        w["remove"].disabled = False

    async def _do_activate(self, role: str) -> None:
        # Relaunch + wait_for_ready can take 10-15s (a real tmux launch
        # and a real Claude Code cold start, not an instant call) -- both
        # engine_roles.activate_role() and remove_role() are plain
        # blocking functions (subprocess.run/time.sleep polling
        # internally, not asyncio-native), so both run in a thread rather
        # than freezing the whole UI event loop for their duration.
        w = self._role_widgets(role)
        w["activate"].disabled = True
        w["result"].update(Text("activating...", style=COLOR_DIM))
        result = await asyncio.to_thread(engine_roles.activate_role, role)
        w["result"].update(Text(("✓ " if result["ok"] else "⚠ ") + result["detail"], style=COLOR_OK if result["ok"] else COLOR_ERR))
        w["activate"].disabled = False


class RuntimePanel(PlainStatic):
    """Ambient, not primary (§3) -- least visually prominent panel."""

    BORDER_TITLE = "RUNTIME"
    DEFAULT_CSS = "RuntimePanel { height: auto; border: solid $panel; padding: 1; margin-bottom: 1; border-title-color: $text-muted; }"

    def update_state(self, runtime) -> None:
        if runtime.error:
            self.update(Text(f"error -- {runtime.error}", style=COLOR_ERR))
            return
        models = ", ".join(runtime.models_resident) if runtime.models_resident else "none warm"
        mem = f"{runtime.memory_free_pct:.0f}% free" if runtime.memory_free_pct is not None else "mem ?"
        spend = runtime.spend["summary"] if runtime.spend and runtime.spend.get("ok") else "spend unavailable"
        text = Text()
        text.append("  models: ", style=COLOR_DIM)
        text.append(f"{models}\n", style=COLOR_INK)
        text.append("  memory: ", style=COLOR_DIM)
        text.append(mem, style=COLOR_INK)
        text.append(_staleness_note(runtime.polled_at, runtime.expected_interval) + "\n", style=COLOR_WARN)
        text.append("  spend:  ", style=COLOR_DIM)
        text.append(spend, style=COLOR_INK)
        text.append(_staleness_note(runtime.spend_polled_at, runtime.spend_expected_interval), style=COLOR_WARN)
        self.update(text)


class TeamsPanel(PlainStatic):
    """Fuller than Rail's condensed count -- every member, its activity,
    and which one is the inbox (SPEC-TUI.md §4.5: the inbox is the
    default and always wins on an ambiguous reference, so it's always
    shown, not just implied). Real aligned columns via a borderless Rich
    Table -- label left, status right, path dim -- not one joined
    prose string, and never wraps a path: display_path is already
    collapsed/truncated at the source (l5_console/state), this panel
    only renders it."""

    BORDER_TITLE = "AGENTS / TEAMS"
    DEFAULT_CSS = "TeamsPanel { height: 1fr; border: solid $panel; padding: 1; border-title-color: $text-muted; }"

    def update_state(self, teams: list, teams_error, unassigned: list, polled_at, expected_interval) -> None:
        if teams_error:
            self.update(Text(f"⚠ error -- {teams_error}", style=COLOR_ERR))
            return

        table = Table.grid(padding=(0, 1, 0, 0), expand=True)
        table.add_column(no_wrap=True)  # icon + name
        table.add_column(ratio=1, no_wrap=True, overflow="ellipsis")  # activity / member count
        table.add_column(no_wrap=True, justify="right")  # status / path

        note = _staleness_note(polled_at, expected_interval)
        if note:
            table.add_row(Text(note.strip(), style=COLOR_WARN), "", "")

        if not teams:
            table.add_row(Text("none configured", style=COLOR_DIM), "", "")

        for t in teams:
            tl = team_liveness(t)
            table.add_row(
                Text(f"{liveness_icon(tl)} {t.id}", style=f"bold {COLOR_ACCENT}"),
                Text(f"{len(t.members)} agent(s)", style=COLOR_DIM),
                Text(t.display_path or t.root, style=COLOR_DIM),
            )
            for m in t.members:
                name = m.tmux or "(not bound)"
                if m.is_inbox:
                    name += " ← inbox"
                activity = m.activity or ("idle" if m.liveness == LIVENESS_RUNNING else "")
                status_style = {
                    LIVENESS_RUNNING: COLOR_OK if not m.activity else COLOR_WARN,
                    LIVENESS_STOPPED: COLOR_DIM,
                    LIVENESS_LOST: COLOR_ERR,
                }.get(m.liveness, COLOR_DIM)
                table.add_row(
                    Text(f"  {liveness_icon(m.liveness)} {name}", style=liveness_color(m.liveness)),
                    Text(activity, style=COLOR_DIM),
                    Text(m.liveness, style=status_style),
                )

        if unassigned:
            table.add_row("", "", "")
            table.add_row(Text(f"⚑ {len(unassigned)} unassigned", style=COLOR_WARN), "", "")
            for u in unassigned:
                table.add_row(Text(f"  {u.tmux}", style=COLOR_INK), "", Text(u.display_path or u.working_dir, style=COLOR_DIM))

        # The legend the Lead flagged as load-bearing: stopped vs lost is
        # what decides whether Reconnect can do anything for a member.
        legend = Text()
        legend.append("● ", style=COLOR_OK)
        legend.append("running   ", style=COLOR_DIM)
        legend.append("○ ", style=COLOR_DIM)
        legend.append("stopped -- reconnectable   ", style=COLOR_DIM)
        legend.append("✕ ", style=COLOR_ERR)
        legend.append("lost -- no saved history", style=COLOR_DIM)

        grid = Table.grid(padding=(1, 0, 0, 0))
        grid.add_row(table)
        grid.add_row(legend)
        self.update(grid)


class StreamPanel(Widget):
    """Live log of wake scores, state transitions, routing (§3) -- see
    stream.py's own docstring for what's actually in the feed today and
    what would need new daemon.py instrumentation (per-frame wake
    scores, not yet logged anywhere)."""

    BORDER_TITLE = "STREAM"
    DEFAULT_CSS = "StreamPanel { height: 1fr; border: solid $panel; padding: 1; margin-bottom: 1; border-title-color: $text-muted; }"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._stream = StreamReader()
        self._lines: list[str] = []

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="stream_scroll"):
            yield PlainStatic("", id="stream_body")

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
    Console > Horizontal { height: 1fr; }
    #console_left { width: 34; height: 100%; }
    #console_right { width: 1fr; height: 100%; }
    """

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical(id="console_left"):
                yield WakePanel(id="console_wake")
                yield EnginePanel(id="console_engine")
                yield RuntimePanel(id="console_runtime")
            with Vertical(id="console_right"):
                yield StreamPanel(id="console_stream")
                yield TeamsPanel(id="console_teams")
        yield Footer()

    def update_state(self, state: JarvisState) -> None:
        self.query_one("#console_wake", WakePanel).update_state(state.wake)
        self.query_one("#console_engine", EnginePanel).update_state(state.engine)
        self.query_one("#console_runtime", RuntimePanel).update_state(state.runtime)
        self.query_one("#console_teams", TeamsPanel).update_state(
            state.teams, state.teams_error, state.unassigned, state.teams_polled_at, state.teams_expected_interval
        )
        self.query_one("#console_stream", StreamPanel).poll_stream()

    def update_meter(self, wake_state) -> None:
        self.query_one("#console_wake", WakePanel).update_meter(wake_state)
