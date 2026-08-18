"""
Jarvis Console entry point. docs/SPEC-TUI.md §3: one application, three
densities chosen by available space -- Rail (narrow), Console (full
window), Signal (any width, during an active dictation, pre-empts both).

One widget tree that reflows, not three separate apps or screens (the
Lead's explicit instruction): Rail, Console, and Signal are all mounted
at all times; exactly one is visible via CSS display toggling, decided
by _apply_layout() below.

Two clocks, not one -- SPEC-TUI.md §6.1's dual-clock principle, actually
needed now rather than deferred: REFRESH_INTERVAL_S (1s) drives
JarvisState (teams/orchestrator/runtime -- data that changes slowly),
METER_REFRESH_INTERVAL_S (0.1s) drives the live audio meter + Signal's
pre-emption decision from daemon.py's wake_state.json. A meter updating
once a second doesn't read as "live"; JarvisState polled 10x/sec would
be needless load for data that doesn't change that fast. Splitting them
is the deliberate choice the spec asked for, not "always split
everything."

Render discipline (§6.2, both items structural here, not left to
convention):
- Poller results never get written straight into widget state from a
  background thread. Both `_refresh_state` and `_refresh_meter` run on
  Textual's own event loop via `set_interval`, call a synchronous,
  side-effect-free read, and push the result into widgets on that same
  tick -- there is no second thread racing the render pass. This is the
  lazygit v0.64.0 bug (PR #5791) this project has already read about and
  is deliberately not repeating.
- wake_state.py's read is a plain, cheap file read (see its own
  docstring) -- safe to call at 10Hz from the main thread for the same
  reason get_state() is safe to call at 1Hz.
"""
from __future__ import annotations

import sys
from pathlib import Path

from textual.app import App, ComposeResult

sys.path.insert(0, str(Path(__file__).parent))
from rail import Rail  # noqa: E402
from console import Console  # noqa: E402
from signal_view import Signal  # noqa: E402
from wake_state import read_wake_state  # noqa: E402
from setup_flow import SetupScreen  # noqa: E402
from reconnect_flow import ReconnectScreen  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent.parent / "state"))
import api as state  # noqa: E402

REFRESH_INTERVAL_S = 1.0
METER_REFRESH_INTERVAL_S = 0.1

# Below this terminal width, Rail; at or above, Console. A narrow tmux
# side pane is typically 25-40 columns; a dedicated terminal window is
# usually 80+. 60 sits clearly between the two real cases rather than at
# an edge either would commonly hit -- revisit with an actual number if
# Ayman's real pane widths turn out different from this guess.
RAIL_CONSOLE_BREAKPOINT = 60


class JarvisConsole(App):
    TITLE = "Jarvis"
    CSS_PATH = "app.tcss"
    BINDINGS = [
        ("a", "add_team", "Add team"),
        ("r", "reconnect", "Reconnect"),
    ]

    def action_add_team(self) -> None:
        self.push_screen(SetupScreen())

    def action_reconnect(self) -> None:
        self.push_screen(ReconnectScreen())

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Cached from the two clocks, read by _apply_layout() to decide
        # Signal's pre-emption -- not re-derived per layout check, since
        # that would mean either clock racing to read live daemon/state
        # data from inside a layout decision instead of from its own tick.
        self._wake_running = False
        self._dictating = False

    def compose(self) -> ComposeResult:
        yield Rail(id="rail")
        yield Console(id="console")
        yield Signal(id="signal")

    def on_mount(self) -> None:
        state.start()  # idempotent; launches the state layer's background poller threads once
        self._apply_layout()
        self._refresh_state()
        self._refresh_meter()
        self.set_interval(REFRESH_INTERVAL_S, self._refresh_state)
        self.set_interval(METER_REFRESH_INTERVAL_S, self._refresh_meter)

    def on_unmount(self) -> None:
        state.stop()

    def on_resize(self, event) -> None:
        self._apply_layout()

    def _apply_layout(self) -> None:
        # Signal pre-empts Rail/Console regardless of width -- SPEC-TUI.md
        # §3: "while you are talking, the only question that matters is
        # whether it is still hearing you." Everything else follows the
        # normal width breakpoint.
        rail = self.query_one("#rail", Rail)
        console = self.query_one("#console", Console)
        signal = self.query_one("#signal", Signal)
        if self._dictating:
            signal.display = True
            rail.display = False
            console.display = False
        else:
            wide = self.size.width >= RAIL_CONSOLE_BREAKPOINT
            signal.display = False
            rail.display = not wide
            console.display = wide

    def _refresh_state(self) -> None:
        # Synchronous call on the app's own event-loop tick -- get_state()
        # is contractually cheap and side-effect-free (§6 negotiation),
        # so this never blocks input. If that stops being true, it's a
        # contract violation to fix at the source, not a reason to move
        # this onto a background thread and reintroduce the lazygit bug.
        #
        # All three widgets update every tick regardless of which is
        # currently visible -- simpler than tracking display state here,
        # and it means switching layouts never briefly shows stale
        # content from before a widget became visible.
        current = state.get_state()
        self._wake_running = current.wake.running
        self.query_one("#rail", Rail).update_state(current)
        self.query_one("#console", Console).update_state(current)

    def _refresh_meter(self) -> None:
        wake_state = read_wake_state()
        # Signal is "dictating" only when wake.running is true (the
        # authoritative signal, from the slow clock) AND the fast-clock
        # wake_state file agrees CAPTURING is the current state AND that
        # reading isn't stale -- all three, not any one alone, so a
        # stale/wedged wake_state.json can never trigger Signal on its
        # own, and a genuinely dead daemon can't either even if its last
        # wake_state.json write happened to say CAPTURING.
        self._dictating = bool(
            self._wake_running and wake_state is not None and not wake_state.stale
            and wake_state.state == "CAPTURING"
        )
        self.query_one("#rail", Rail).update_meter(wake_state)
        self.query_one("#console", Console).update_meter(wake_state)
        self.query_one("#signal", Signal).update_state(self._wake_running, wake_state)
        self._apply_layout()


if __name__ == "__main__":
    JarvisConsole().run()
