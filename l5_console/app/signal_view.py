"""
Signal: the dictation-takeover density (SPEC-TUI.md §3). Pre-empts Rail
AND Console, regardless of terminal width, whenever a dictation is
actively in progress -- "while you are talking, the only question that
matters is whether it is still hearing you." Build order step 4, last
of the three densities.

State block + the live meter (bigger than Rail/Console's version, since
Signal has the whole screen and nothing else competing for it) -- kept
deliberately minimal, matching the spec's own framing of what this view
is for. Not a place to cram extra detail; the moment this view is up,
detail is a distraction from the one question it exists to answer.
"""
from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widget import Widget

from meter import Meter
from widgets import PlainStatic
from format_helpers import COLOR_OK, COLOR_WARN, COLOR_DIM

SIGNAL_METER_WIDTH = 40


class Signal(Widget):
    BORDER_TITLE = "STATE"

    DEFAULT_CSS = """
    Signal {
        width: 100%;
        height: 100%;
        align: center middle;
        background: $surface;
    }
    Signal > Vertical {
        width: 100%;
        height: auto;
        align: center middle;
        border: solid $panel;
        padding: 2 4;
        border-title-color: $text-muted;
    }
    #signal_state {
        text-align: center;
        text-style: bold;
        margin-bottom: 2;
    }
    #signal_meter {
        text-align: center;
    }
    #signal_hint {
        text-align: center;
        color: $text-muted;
        margin-top: 2;
    }
    """

    def compose(self) -> ComposeResult:
        panel = Vertical()
        panel.border_title = "STATE"
        with panel:
            yield PlainStatic("", id="signal_state")
            yield Meter(id="signal_meter", width=SIGNAL_METER_WIDTH)
            yield PlainStatic('say "hey jarvis" again, or pause, to stop', id="signal_hint")

    def update_state(self, wake_running: bool, wake_state) -> None:
        state_line = self.query_one("#signal_state", PlainStatic)
        if not wake_running or wake_state is None or wake_state.stale:
            # Signal should never be the active view in these cases (see
            # main.py's should_show_signal -- these are defensive, not
            # the normal path), but if it somehow renders anyway, it must
            # not claim a dictation is happening.
            state_line.update(Text("? state unknown", style=f"bold {COLOR_WARN}"))
        else:
            state_line.update(Text("● DICTATING -- listening", style=f"bold {COLOR_OK}"))
        self.query_one("#signal_meter", Meter).update_meter(wake_running, wake_state)
