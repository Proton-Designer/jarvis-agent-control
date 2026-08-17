"""
Production-path latency instrumentation.

The real end-to-end run (2026-08-17) was measured by hand -- manual
timestamps typed into chat as events were observed -- and the Lead
correctly flagged that the reported 155.1s included real test overhead
(manual capture-pane verification, ~68s of it) that doesn't exist in
production. This module replaces eyeballed timestamps with a structured
log of the actual production path: handoff_received -> confirm_spoken ->
cancel_window_closed -> last_send_issued. Verification/observation time is
never part of this log -- it isn't a production event.

No correlation ID: by design, the orchestrator handles one dictation
fully (through confirm_plan and deliver_batch) before the next one
starts, so timestamp ordering alone identifies which events belong to the
same run. Revisit if that assumption ever stops holding (e.g. overlapping
dictations).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

LATENCY_LOG_PATH = Path.home() / ".jarvis" / "latency_log.jsonl"


def log_event(event: str, **fields) -> None:
    LATENCY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {"event": event, "timestamp": time.time(), **fields}
    with LATENCY_LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")
