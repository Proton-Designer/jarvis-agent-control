"""
Stream ingestion (SPEC-TUI.md §3's Stream section, §4's assignment:
"you already print all of this -- this is routing it into a widget").

Reads ~/.jarvis/latency_log.jsonl -- the same append-only event log
l1_wakeword/daemon.py and l2_5_concierge/concierge.py already write to
for every dictation (classification decisions, routing outcomes, retry/
retention events). This is the one existing channel that already carries
"state transitions and routing" as structured, cross-process-readable
data, so Stream reads it rather than trying to capture another process's
stdout -- that works regardless of whether the daemon was started from
this console or by hand in a terminal, which the wake control has to
support either way (§7: reflects reality, not who started it).

NOT YET IN THIS LOG: per-frame wake-word SCORES (currently print-only in
daemon.py's on_frame/simulate loops, never logged). Stream can show every
classification/routing/retention decision today; showing scores too
would need a small daemon.py addition (a log_event call at the existing
score-threshold-crossed print sites) -- flagged as a real, clearly-scoped
follow-up, not silently treated as done.

Tailing, not re-reading: tracks a byte offset so repeated calls only
return genuinely new lines, matching the "cheap, called every render"
constraint the rest of this app's polling already follows.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

LATENCY_LOG_PATH = Path.home() / ".jarvis" / "latency_log.jsonl"

# Which events are worth a Stream line, and how to phrase each -- a
# curated subset, not "log every field of every event." latency_log.jsonl
# also carries pure-timing telemetry (concierge_phrase_model_call
# elapsed_ms, etc.) that's real but not "state transitions and routing"
# in the sense this panel is for; those stay in the log file for anyone
# who wants to grep it, they just don't clutter this live view.
_EVENT_FORMATTERS = {
    "concierge_classified": lambda e: f"classified {e.get('label')} ({e.get('tier')})",
    "concierge_fast_path_done": lambda e: (
        f"{e.get('label')} -> " + ("forwarded" if e.get("forwarded") else "answered locally")
    ),
    "concierge_chat_suppressed": lambda e: (
        "CHAT suppressed, " + ("kept" if e.get("retain") else "DISCARDED (not addressed)")
    ),
    "l1_dictation_end": lambda e: f"dictation ended ({e.get('chars')} chars)",
    "l1_concierge_round_trip": lambda e: (
        f"round trip: {e.get('label')} in {e.get('end_to_end_s')}s"
        + ("" if e.get("retained", True) else " [discarded]")
    ),
    "handoff_no_tools": lambda e: "⚠ orchestrator has no tools -- dispatch blocked",
    "pointer_delivered": lambda e: (
        f"delivered to {e.get('target')}" if e.get("ok") else f"⚠ delivery FAILED to {e.get('target')}"
    ),
}


class StreamReader:
    def __init__(self, path: Path = LATENCY_LOG_PATH):
        self._path = path
        self._offset = 0
        # Skip everything that already existed on first read -- Stream
        # shows what happens from when the console opens, not a replay
        # of the entire session's history, which could be thousands of
        # lines by the time someone opens the console mid-session.
        if self._path.exists():
            self._offset = self._path.stat().st_size

    def poll(self) -> list[str]:
        """Returns newly-formatted lines since the last call, oldest
        first. Empty list on any read failure (file missing, permission
        error, torn write) -- Stream degrading to "nothing new" is
        acceptable (it's ambient log context, not a safety signal the
        way wake/orchestrator state is); the file itself is untouched
        either way."""
        if not self._path.exists():
            return []
        try:
            with self._path.open("r") as f:
                f.seek(self._offset)
                new_data = f.read()
                self._offset = f.tell()
        except OSError:
            return []

        lines = []
        for raw_line in new_data.splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            formatter = _EVENT_FORMATTERS.get(event.get("event"))
            if formatter is None:
                continue
            ts = time.strftime("%H:%M:%S", time.localtime(event.get("timestamp", time.time())))
            try:
                lines.append(f"[{ts}] {formatter(event)}")
            except Exception:
                continue  # a malformed event shouldn't take down the whole stream
        return lines
