"""
Verifies JARVIS_MUTE suppresses audio without disabling the cancel window
-- the property that matters, since a mute that also silences the control
is the same failure class as the cancel_listener fail-open bug.

Timing against a real (absent) socket isn't a valid proxy for "was the
window armed": listen_for_cancel legitimately fast-fails when the socket
doesn't exist (no reason to block for the full timeout just to discover
that), so a fast return means "L1 isn't up," not "mute skipped the
window." The actual property under test is: was listen_for_cancel called
at all, with the correct timeout, regardless of MUTE. Verified by
monkeypatching cancel_listener.listen_for_cancel to record the call
instead of timing wall-clock elapsed time.

Run with: JARVIS_MUTE=1 python3 test_mute_mode.py
"""

from __future__ import annotations

import json
import os

assert os.environ.get("JARVIS_MUTE") == "1", "run this with JARVIS_MUTE=1 set"

import cancel_listener  # noqa: E402
import say_feedback  # noqa: E402  (import after asserting MUTE env is set)

say_feedback.SAY_LOG_PATH.unlink(missing_ok=True)

calls = []
_real_listen_for_cancel = cancel_listener.listen_for_cancel


def _spy_listen_for_cancel(timeout_s, *args, **kwargs):
    calls.append(timeout_s)
    return cancel_listener.CancelResult(cancelled=False, available=True)


say_feedback.listen_for_cancel = _spy_listen_for_cancel

timeout_s = 2.5
result = say_feedback.speak_with_cancel_window("test summary", timeout_s)

assert calls == [timeout_s], (
    f"expected listen_for_cancel called once with timeout_s={timeout_s}, got calls={calls} -- "
    "mute must not skip arming the cancel window"
)
print(f"PASS: cancel window armed under mute (listen_for_cancel called with timeout_s={timeout_s})")

assert result["cancelled"] is False and result["available"] is True

log_lines = say_feedback.SAY_LOG_PATH.read_text().strip().splitlines()
assert log_lines, "expected at least one log entry"
entries = [json.loads(line) for line in log_lines]
assert all(e["muted"] is True for e in entries), "every log entry under JARVIS_MUTE=1 must have muted=true"
assert any("test summary" in e["text"] for e in entries), "expected the spoken text to be logged verbatim"
print(f"PASS: {len(entries)} log entries, all marked muted=true, exact text preserved")

print("ALL PASS")
