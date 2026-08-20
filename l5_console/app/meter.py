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

import time

from rich.text import Text
from widgets import PlainStatic
from format_helpers import COLOR_ACCENT, COLOR_DIM, COLOR_WARN

METER_CHARS = "▁▂▃▄▅▆▇█"

# docs/PLAN-silence-and-ux.md U1: right after pressing start, wake_state
# is None because the daemon hasn't written wake_state.json yet -- that
# is EXPECTED, not an alarm. How long is "expected" before it becomes a
# real problem: the daemon needs to open the mic and complete one write
# cycle (SPEC-TUI.md's ~10Hz), which should land well under a second on
# a healthy machine. 5s is generous headroom above that, not a measured
# cold-start number -- same order of magnitude as main.py's
# WAKE_POLL_BURST_DURATION_S (3.0s, the other "give a just-clicked
# action a few seconds before treating its absence as meaningful"
# constant in this app), rounded up rather than down because a false
# "starting up" that runs a couple seconds long is harmless where a
# false alarm this glyph exists to prevent is not.
STARTUP_GRACE_S = 5.0


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
        # When wake_running last transitioned False->True -- None while
        # not running. This is the ONLY state this widget keeps outside
        # of what it's handed each call; needed because "no data yet" is
        # only honest to call "starting up" for a bounded window after
        # the daemon was actually asked to start, not forever.
        self._wake_running_since: float | None = None

    def update_meter(self, wake_running: bool, wake_state) -> None:
        if not wake_running:
            self._wake_running_since = None
            text = Text("mic  ", style=COLOR_DIM)
            text.append("·" * self._bar_width, style=COLOR_DIM)
            text.append("  (not listening)", style=COLOR_DIM)
            self.update(text)
            return
        if self._wake_running_since is None:
            self._wake_running_since = time.time()
        if wake_state is None:
            # docs/PLAN-silence-and-ux.md U1: meter.py used to render the
            # SAME "?" alarm glyph here as the genuine-stale case below,
            # every single time start was pressed -- no data is EXPECTED
            # immediately after start, so the one glyph meant to signal
            # "something is wrong" fired on every normal startup and
            # taught Ayman to ignore it. Within STARTUP_GRACE_S this is
            # the calm "not listening" treatment (dots, dim) with its own
            # honest label; past it, still-no-data really is anomalous
            # (the daemon should have written something by now) and gets
            # the SAME alarm treatment stale data gets -- the two
            # "something is wrong with the data feed" cases converge
            # once the grace window is over, they just don't share a
            # rendering during the window that's normal for one and not
            # the other.
            if time.time() - self._wake_running_since < STARTUP_GRACE_S:
                text = Text("mic  ", style=COLOR_DIM)
                text.append("·" * self._bar_width, style=COLOR_DIM)
                text.append("  (starting up...)", style=COLOR_DIM)
                self.update(text)
                return
            text = Text("mic  ", style=COLOR_DIM)
            text.append("?" * self._bar_width, style=COLOR_WARN)
            text.append("  (no data)", style=COLOR_WARN)
            self.update(text)
            return
        if wake_state.stale:
            text = Text("mic  ", style=COLOR_DIM)
            text.append("?" * self._bar_width, style=COLOR_WARN)
            text.append("  (STALE -- meter has stopped updating)", style=COLOR_WARN)
            self.update(text)
            return
        bar = render_bar(wake_state.level, self._bar_width)
        label = {"IDLE": "listening", "CAPTURING": "dictating", "CANCEL_ARMED": "cancel window"}.get(
            wake_state.state, wake_state.state
        )
        text = Text("mic  ", style=COLOR_DIM)
        text.append(bar, style=f"bold {COLOR_ACCENT}")
        text.append(f"  {label}", style=COLOR_DIM)
        self.update(text)
