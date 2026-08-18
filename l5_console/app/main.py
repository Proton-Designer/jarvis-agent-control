"""
Jarvis Console entry point. docs/SPEC-TUI.md §3: one application, three
densities chosen by available space -- Rail (narrow), Console (full
window), Signal (any width, during an active dictation, pre-empts both).

Only Rail is built (build order step 2, §9). Console and Signal are
stubs -- the width-reflow skeleton exists now so adding them later is a
matter of filling in a branch, not restructuring the app.

Render discipline (§6.2, both items structural here, not left to
convention):
- Poller results never get written straight into widget state from a
  background thread. `refresh_state()` runs on Textual's own event loop
  via `set_interval`, calls the synchronous, side-effect-free
  `get_state()`, and pushes the result into widgets on that same tick --
  there is no second thread racing the render pass. This is the lazygit
  v0.64.0 bug (PR #5791) this project has already read about and is
  deliberately not repeating.
- Redraw and poll are the same clock here for v1 (Rail's data changes
  slowly enough that k9s/btop's dual-clock split isn't yet load-bearing)
  -- REFRESH_INTERVAL_S is one number now, split into separate
  sample/redraw clocks only if Console's live Stream view later shows
  it's needed (§6.1's actual point: two clocks is a deliberate choice,
  not "always split them").
"""
from __future__ import annotations

import sys
from pathlib import Path

from textual.app import App, ComposeResult

sys.path.insert(0, str(Path(__file__).parent))
from rail import Rail  # noqa: E402

# TEMPORARY: swap for `from state.poller import get_state` (or wherever
# gu2s6tnt lands it) once the real poller exists. See fixtures.py's own
# docstring -- this is the only place that import needs to change.
from fixtures import fixture_state as get_state  # noqa: E402

REFRESH_INTERVAL_S = 1.0


class JarvisConsole(App):
    TITLE = "Jarvis"
    CSS_PATH = "app.tcss"

    def compose(self) -> ComposeResult:
        yield Rail(id="rail")

    def on_mount(self) -> None:
        self._refresh_state()
        self.set_interval(REFRESH_INTERVAL_S, self._refresh_state)

    def _refresh_state(self) -> None:
        # Synchronous call on the app's own event-loop tick -- get_state()
        # is contractually cheap and side-effect-free (§6 negotiation),
        # so this never blocks input. If that stops being true, it's a
        # contract violation to fix at the source, not a reason to move
        # this onto a background thread and reintroduce the lazygit bug.
        state = get_state()
        self.query_one("#rail", Rail).update_state(state)


if __name__ == "__main__":
    JarvisConsole().run()
