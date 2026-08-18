"""
Wake daemon liveness (SPEC-TUI.md SS3.1, SS7): the safety-critical field
the console's wake control reflects. "It shows whether the daemon
process is alive, never whether the button was pressed" -- reality, not
intent, same rule as everything else in this project.

mic_active (IDLE/CAPTURING/CANCEL_ARMED) is deliberately not here yet --
nothing external exposes daemon.py's internal state today. ue6rruxg is
adding a status-file signal for that alongside Signal+meter (build step
4); this module gets a `state` field then, additive to WakeDaemonState.
"""

from __future__ import annotations

import subprocess

DAEMON_PROCESS_PATTERN = "l1_wakeword/daemon.py"


def is_running() -> bool:
    result = subprocess.run(["pgrep", "-f", DAEMON_PROCESS_PATTERN], capture_output=True)
    return result.returncode == 0
