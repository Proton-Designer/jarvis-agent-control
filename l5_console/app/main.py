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
JarvisState (teams/engine/runtime -- data that changes slowly),
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
from textual.widgets import Button

sys.path.insert(0, str(Path(__file__).parent))
from rail import Rail  # noqa: E402
from console import Console  # noqa: E402
from signal_view import Signal  # noqa: E402
from wake_state import read_wake_state  # noqa: E402
from setup_flow import SetupScreen  # noqa: E402
from reconnect_flow import ReconnectScreen  # noqa: E402
from revive_flow import ReviveScreen  # noqa: E402
from team_flow import TeamManageScreen  # noqa: E402
from quick_adopt_flow import QuickAdoptScreen  # noqa: E402
from quit_warning import QuitWarningScreen  # noqa: E402
import wake_control  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent.parent / "state"))
import api as state  # noqa: E402

REFRESH_INTERVAL_S = 1.0
METER_REFRESH_INTERVAL_S = 0.1

# After a start/stop click, poll JarvisState this fast instead of at
# REFRESH_INTERVAL_S for a few seconds -- the dual-clock design already
# permits a short deviation from the steady 1s cadence (SPEC-TUI.md
# §6.1), and this is exactly the case it's for: a wake_button click is
# a real action with a real answer coming, and making the person wait
# up to a full second to see it confirmed read as broken, not slow (the
# Lead's finding from Ayman's live test). This does not change what
# gets rendered or when -- update_state() still only ever renders from
# a real poll, never optimistically -- it only shrinks how long the
# transitional "starting.../stopping..." state in console.py's
# WakePanel stays up before that real poll lands.
WAKE_POLL_BURST_INTERVAL_S = 0.2
WAKE_POLL_BURST_DURATION_S = 3.0

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
        ("u", "quick_adopt", "Adopt a session"),
        ("r", "reconnect", "Reconnect"),
        ("v", "revive_everything", "Revive everything"),
        ("t", "manage_teams", "Manage teams"),
        ("q", "quit", "Quit"),
        ("space", "panic_stop_wake", "Stop listening"),
    ]

    def action_add_team(self) -> None:
        self.push_screen(SetupScreen())

    def action_quick_adopt(self) -> None:
        # SPEC-TUI.md §6 / TODO-feature-queue.md item 3: assigning a
        # KNOWN unassigned session should be one keystroke, distinct from
        # [a]'s full deliberate-setup wizard -- see quick_adopt_flow.py's
        # own docstring for why this is a separate screen rather than a
        # fast-path branch bolted onto SetupScreen.
        self.push_screen(QuickAdoptScreen())

    def action_reconnect(self) -> None:
        self.push_screen(ReconnectScreen())

    def action_revive_everything(self) -> None:
        self.push_screen(ReviveScreen())

    def action_manage_teams(self) -> None:
        self.push_screen(TeamManageScreen())

    async def action_panic_stop_wake(self) -> None:
        # space is a panic key, not a toggle -- it only ever stops,
        # never starts (the Lead's explicit ruling, 2026-08-18):
        # accidentally stopping is fully recoverable (press start
        # again), accidentally starting opens the microphone without
        # intent, which is the direction this whole project refuses. A
        # toggle can't make that distinction; a stop-only key can, and
        # that's what makes it trustworthy as a panic key -- Ayman never
        # has to know or check current state before pressing it.
        #
        # self._wake_running is the same App-level cache _apply_layout()
        # already trusts for the dictating gate -- not re-derived here.
        if self._panic_stop_in_progress or not self._wake_running:
            return
        self._panic_stop_in_progress = True
        self.start_wake_poll_burst()
        try:
            await wake_control.stop()
        finally:
            self._panic_stop_in_progress = False

    async def action_quit(self) -> None:
        # The Lead's finding: quitting the console does not stop
        # daemon.py -- they're separate processes by design (the daemon
        # is meant to survive the console closing under normal
        # operation, e.g. a LaunchAgent-managed listener). Silently
        # leaving a live mic behind a closed window is exactly the trust
        # failure the visible-indicator work exists to prevent, so warn
        # explicitly rather than exit straight through -- the Lead's own
        # preference over auto-stopping, since killing an in-progress
        # dictation because the window closed would be its own surprise.
        if self._wake_running:
            self.push_screen(QuitWarningScreen())
            return
        self.exit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        # docs/PLAN-silence-and-ux.md U3: a clicked button keeps Textual's
        # :focus styling (a visible highlight) until something else takes
        # focus -- which for a button that stays mounted after its action
        # (WakePanel's start/stop toggle is the one Ayman actually clicks
        # repeatedly; EnginePanel's buttons the same way) reads as "this
        # is still doing something" indefinitely, long after the real
        # action finished.
        #
        # App-level, not per-widget: every Button.Pressed handler in this
        # app (WakePanel/EnginePanel/every modal screen) is defined
        # CLOSER to the widget than the App, and none of them call
        # event.stop() (checked), so Textual's message bubbling delivers
        # the event to each of those FIRST and to this handler LAST --
        # one line here covers every button in the app instead of adding
        # a blur() call to each individual on_button_pressed. Safe even
        # for a button that's about to be removed/disabled/hidden by its
        # own handler (already run by the time this fires): blurring an
        # about-to-vanish widget is a harmless no-op, not a race.
        event.button.blur()

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        # Defense in depth for the setup/reconnect focus bug (see
        # setup_flow.py's docstring): focusing the right widget on every
        # step mount fixes every path that currently loses focus, but
        # focus is a runtime property -- a future step, a widget that
        # can't take focus, an unmount race, any of those puts a
        # keystroke back in front of these global bindings with nothing
        # capturing it as text. A modal should own the keyboard while
        # it's open, structurally, not by relying on nothing ever
        # slipping through the per-widget fix. len(screen_stack) > 1
        # means a SetupScreen/ReconnectScreen is pushed on top of the
        # base Rail/Console/Signal screen.
        if action in ("add_team", "quick_adopt", "reconnect", "revive_everything", "manage_teams") and len(self.screen_stack) > 1:
            return False
        return True

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Cached from the two clocks, read by _apply_layout() to decide
        # Signal's pre-emption -- not re-derived per layout check, since
        # that would mean either clock racing to read live daemon/state
        # data from inside a layout decision instead of from its own tick.
        self._wake_running = False
        self._dictating = False
        self._normal_refresh_timer = None
        self._burst_refresh_timer = None
        self._burst_end_timer = None
        self._panic_stop_in_progress = False

    def compose(self) -> ComposeResult:
        yield Rail(id="rail")
        yield Console(id="console")
        yield Signal(id="signal")

    def on_mount(self) -> None:
        state.start()  # idempotent; launches the state layer's background poller threads once
        self._apply_layout()
        self._refresh_state()
        self._refresh_meter()
        self._normal_refresh_timer = self.set_interval(REFRESH_INTERVAL_S, self._refresh_state)
        self.set_interval(METER_REFRESH_INTERVAL_S, self._refresh_meter)

    def start_wake_poll_burst(self) -> None:
        # Called by console.py's WakePanel right after a start/stop
        # click. Stops the steady 1s timer and replaces it with a fast
        # one for a bounded window, then replaces it back -- deliberately
        # stop-and-recreate, not pause()/resume(): verified live that a
        # Timer paused for longer than its own interval does not resume
        # ticking at all (its next-fire target is already in the past by
        # the time resume() runs, and it never catches up or reschedules
        # from there) -- a real Textual Timer edge case, not just theory.
        # A freshly created set_interval() always fires on schedule from
        # its own creation time, which sidesteps that case entirely.
        if self._burst_refresh_timer is not None:
            self._burst_end_timer.stop()
            self._burst_refresh_timer.stop()
        else:
            self._normal_refresh_timer.stop()
        self._burst_refresh_timer = self.set_interval(WAKE_POLL_BURST_INTERVAL_S, self._refresh_state)
        self._burst_end_timer = self.set_timer(WAKE_POLL_BURST_DURATION_S, self._end_wake_poll_burst)

    def _end_wake_poll_burst(self) -> None:
        if self._burst_refresh_timer is not None:
            self._burst_refresh_timer.stop()
            self._burst_refresh_timer = None
        self._normal_refresh_timer = self.set_interval(REFRESH_INTERVAL_S, self._refresh_state)

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
            # Defense in depth, not just the one CSS bug it was found
            # from (Signal's inner Vertical auto-sizing to 0 while
            # hidden -- fixed in signal_view.py): never trust that
            # `display = True` alone means Signal actually has something
            # on screen. Confirm a non-zero laid-out size before hiding
            # Rail/Console, so any future way Signal could fail to
            # render self-heals instead of producing a blank screen --
            # "if the new view can't render, keep the old one." Signal
            # was display:none last tick, so its size here is stale
            # pre-layout on the very first tick this flips true; that
            # tick falls through to the normal branch below and keeps
            # whichever of Rail/Console already fits, and the next
            # meter tick (0.1s later) picks up the fresh, confirmed
            # size and switches over -- a small delay, never a blank
            # frame.
            if signal.size.width > 0 and signal.size.height > 0:
                rail.display = False
                console.display = False
                return
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
