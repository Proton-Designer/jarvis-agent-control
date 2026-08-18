"""
Staleness is a rendering decision, computed here from the timing facts
the state layer reports (polled_at, expected_interval) -- never a
hardcoded threshold per section. See l5_console/state/models.py's
docstring and docs/SPEC-TUI.md §7: a snapshot that stopped updating must
never render identically to one that's current.

STALE_MULTIPLIER is the one number this module owns: how many missed
poll cycles before something is flagged stale, not per-section timing.
3x gives real margin for normal jitter (a slightly slow tmux call, one
skipped tick) while still catching a poller that's actually stopped --
a poller that's died doesn't come back on the next cycle, so by the 3rd
missed interval the distinction is real, not noise.
"""
from __future__ import annotations

import time

STALE_MULTIPLIER = 3.0


def is_stale(polled_at: float, expected_interval: float, now: float | None = None) -> bool:
    now = now if now is not None else time.time()
    return (now - polled_at) > (STALE_MULTIPLIER * expected_interval)
