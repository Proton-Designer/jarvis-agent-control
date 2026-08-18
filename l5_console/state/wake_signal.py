"""
Reads ~/.jarvis/wake_state.json to answer one question for escalation
gating (docs/SPEC-blockers.md SS5.3): is it safe to speak right now?

Deliberately a separate, minimal copy of the read logic in
l5_console/app/wake_state.py (ue6rruxg's console-meter reader) rather than
a cross-layer import -- l5_console/state/ must not depend on
l5_console/app/, the ownership boundary negotiated for this project. Same
file, same shape, same staleness discipline; two small readers for two
different consumers is cheaper than the coupling a shared import would
create.

NEVER the sole authority on whether the daemon is alive -- pair with
JarvisState.wake.running (the pgrep-based poll) exactly as app/wake_state.py
documents. A missing file, malformed content, stale timestamp, or any
state other than IDLE all fail closed here: unsafe to speak, never treated
as "quiet, so it must be fine."
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

WAKE_STATE_PATH = Path.home() / ".jarvis" / "wake_state.json"
STALE_AFTER_S = 1.0


@dataclass
class WakeSignal:
    state: str  # "IDLE" | "CAPTURING" | "CANCEL_ARMED"
    stale: bool


def read_wake_signal() -> WakeSignal | None:
    """None on any read failure -- callers must treat None as unsafe to
    speak, never as IDLE."""
    try:
        raw = WAKE_STATE_PATH.read_text()
        data = json.loads(raw)
        state = data["state"]
        updated_at = float(data["updated_at"])
    except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None
    return WakeSignal(state=state, stale=(time.time() - updated_at) > STALE_AFTER_S)
