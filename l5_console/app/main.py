"""
Jarvis Console entry point. docs/SPEC-TUI.md §3: one application, three
densities chosen by available space -- Rail (narrow), Console (full
window), Signal (any width, during an active dictation, pre-empts both).

One widget tree that reflows, not three separate apps or screens (the
Lead's explicit instruction): both Rail and Console are mounted at all
times; only one is visible at a time via CSS display toggling on resize.
Signal isn't built yet (build order step 4) -- when it lands, it's a
third mounted widget with its own visibility rule (pre-empts both
regardless of width, during an active dictation), not a fourth "screen."

Render discipline (§6.2, both items structural here, not left to
convention):
- Poller results never get written straight into widget state from a
  background thread. `refresh_state()` runs on Textual's own event loop
  via `set_interval`, calls the synchronous, side-effect-free
  `get_state()`, and pushes the result into widgets on that same tick --
  there is no second thread racing the render pass. This is the lazygit
  v0.64.0 bug (PR #5791) this project has already read about and is
  deliberately not repeating.
- Redraw and poll are the same clock here for v1 (Rail/Console's data
  changes slowly enough that k9s/btop's dual-clock split isn't yet load-
  bearing) -- REFRESH_INTERVAL_S is one number now, split into separate
  sample/redraw clocks only if the Stream view later shows it's needed
  (§6.1's actual point: two clocks is a deliberate choice, not "always
  split them").
"""
from __future__ import annotations

import sys
from pathlib import Path

from textual.app import App, ComposeResult

sys.path.insert(0, str(Path(__file__).parent))
from rail import Rail  # noqa: E402
from console import Console  # noqa: E402

# TEMPORARY: swap for `from state.poller import get_state` (or wherever
# gu2s6tnt lands it) once the real poller exists. See fixtures.py's own
# docstring -- this is the only place that import needs to change.
from fixtures import fixture_state as get_state  # noqa: E402

REFRESH_INTERVAL_S = 1.0

# Below this terminal width, Rail; at or above, Console. A narrow tmux
# side pane is typically 25-40 columns; a dedicated terminal window is
# usually 80+. 60 sits clearly between the two real cases rather than at
# an edge either would commonly hit -- revisit with an actual number if
# Ayman's real pane widths turn out different from this guess.
RAIL_CONSOLE_BREAKPOINT = 60


class JarvisConsole(App):
    TITLE = "Jarvis"
    CSS_PATH = "app.tcss"

    def compose(self) -> ComposeResult:
        yield Rail(id="rail")
        yield Console(id="console")

    def on_mount(self) -> None:
        self._apply_layout()
        self._refresh_state()
        self.set_interval(REFRESH_INTERVAL_S, self._refresh_state)

    def on_resize(self, event) -> None:
        self._apply_layout()

    def _apply_layout(self) -> None:
        wide = self.size.width >= RAIL_CONSOLE_BREAKPOINT
        self.query_one("#rail", Rail).display = not wide
        self.query_one("#console", Console).display = wide

    def _refresh_state(self) -> None:
        # Synchronous call on the app's own event-loop tick -- get_state()
        # is contractually cheap and side-effect-free (§6 negotiation),
        # so this never blocks input. If that stops being true, it's a
        # contract violation to fix at the source, not a reason to move
        # this onto a background thread and reintroduce the lazygit bug.
        #
        # Both widgets update every tick regardless of which is currently
        # visible -- simpler than tracking display state here, and it
        # means a resize never reveals a layout showing stale content
        # from before it became visible.
        state = get_state()
        self.query_one("#rail", Rail).update_state(state)
        self.query_one("#console", Console).update_state(state)


if __name__ == "__main__":
    JarvisConsole().run()
