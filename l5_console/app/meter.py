"""
The live audio meter (SPEC-TUI.md §3, the element the Lead named as the
one they care about most). "Every other element reports what the system
believes; the meter reports what the microphone is actually receiving.
It is the only element that distinguishes 'not hearing you' from
'hearing you and doing nothing.'"

Required in every layout (Rail, Console, Signal), not just Signal's
dictation takeover -- this module is the one bar-rendering
implementation all three share, at different sizes, so they can't
render the same level differently.
"""
from __future__ import annotations

from widgets import PlainStatic

METER_CHARS = "▁▂▃▄▅▆▇█"


def render_bar(level: float, width: int) -> str:
    """A `width`-character bar using 1/8-block Unicode chars for
    sub-character resolution -- smoother-looking than plain on/off
    blocks at the terminal widths this app actually runs at."""
    level = max(0.0, min(1.0, level))
    filled_eighths = round(level * width * 8)
    full_chars, remainder = divmod(filled_eighths, 8)
    full_chars = min(full_chars, width)
    bar = METER_CHARS[-1] * full_chars
    if full_chars < width and remainder > 0:
        bar += METER_CHARS[remainder - 1]
    bar = bar.ljust(width, " ")
    return bar


class Meter(PlainStatic):
    """Self-contained: given a WakeState (or None), decides what to
    render, including the "not hearing anything" / "no data" / "stale"
    cases explicitly -- never lets an absent or stale reading render as
    if it were a real, current zero level."""

    DEFAULT_CSS = "Meter { height: 1; }"

    def __init__(self, *args, width: int = 20, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._bar_width = width

    def update_meter(self, wake_running: bool, wake_state) -> None:
        if not wake_running:
            self.update("mic  " + "·" * self._bar_width + "  (not listening)")
            return
        if wake_state is None:
            self.update("mic  " + "?" * self._bar_width + "  (no data)")
            return
        if wake_state.stale:
            self.update("mic  " + "?" * self._bar_width + "  (STALE -- meter has stopped updating)")
            return
        bar = render_bar(wake_state.level, self._bar_width)
        label = {"IDLE": "listening", "CAPTURING": "dictating", "CANCEL_ARMED": "cancel window"}.get(
            wake_state.state, wake_state.state
        )
        self.update(f"mic  {bar}  {label}")
