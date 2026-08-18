"""
Reads ~/.jarvis/wake_state.json -- daemon.py's own fast-clock status
file (state + real-time audio level), written only during a real live()
mic session, throttled to ~10Hz on the daemon side.

This is the SECOND, genuinely-needed clock the k9s dual-clock principle
(SPEC-TUI.md §6.1) predicted but v1 didn't build yet: JarvisState's
~1s poll is far too slow for a meter to read as "live." main.py polls
THIS module on its own faster interval, independent of the main state
refresh.

Deliberately NOT part of the JarvisState contract with gu2s6tnt's layer
-- this is L1-owned, daemon-side, real-time data with a completely
different shape and cadence than anything in models.py, and reading a
small JSON file directly is cheap enough that routing it through the
state layer's poller would only add latency for no benefit.

Never the authority on whether the daemon is alive. JarvisState.wake.running
(the real pgrep-based poll) is that authority -- this file can go stale
(daemon crashed mid-write, holds an old timestamp) exactly like any other
polled data, so callers must check staleness here too, same discipline
as everything else, and must gate on wake.running being true before
trusting this file's content at all: a wedged/stale wake_state.json with
wake.running=False must never be read as "still listening."
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from dataclasses import dataclass

WAKE_STATE_PATH = Path.home() / ".jarvis" / "wake_state.json"

# How long a wake_state.json write is trusted before being treated as
# stale -- daemon.py writes at ~10Hz (WAKE_STATE_WRITE_INTERVAL_S=0.1 on
# its side), so a few missed intervals is real margin without missing a
# genuinely wedged/dead write loop.
STALE_AFTER_S = 1.0


@dataclass
class WakeState:
    state: str  # "IDLE" | "CAPTURING" | "CANCEL_ARMED"
    level: float  # 0.0-1.0, RMS-normalized
    stale: bool


def read_wake_state() -> WakeState | None:
    """None on any read failure (file missing -- e.g. daemon never
    started live(), or --simulate is what's running) or malformed
    content -- callers must treat None as "no data," never as level 0
    (level 0 is a real, meaningful reading: the mic is open and hearing
    silence; None means there's nothing to read at all)."""
    try:
        raw = WAKE_STATE_PATH.read_text()
        data = json.loads(raw)
        state = data["state"]
        level = float(data["level"])
        updated_at = float(data["updated_at"])
    except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None
    return WakeState(state=state, level=level, stale=(time.time() - updated_at) > STALE_AFTER_S)
