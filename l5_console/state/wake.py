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

import re
import subprocess

DAEMON_PROCESS_PATTERN = "l1_wakeword/daemon.py"

# `pgrep -f` matches this pattern as a plain substring anywhere in the
# process's full, flattened command line -- including inside a
# `python -c "<script>"` argument's own text. Found live (2026-08-18): a
# throwaway `python -c "...# l1_wakeword/daemon.py placeholder...\ntime.
# sleep(600)"` test fixture (ue6rruxg's, for an unrelated repro, no mic
# involved at all) matched and was momentarily read as the real daemon.
# That string is also genuinely common in this codebase's own comments
# and docstrings (this file's own module docstring has it), so a bare
# substring match is fragile against any Python process that happens to
# mention it in passing, not just adversarial ones. run_daemon.sh always
# launches the real daemon as `<python> <abs-path>/l1_wakeword/daemon.py`
# -- daemon.py as its OWN trailing argument, not embedded inside a larger
# string -- so require that literal invocation shape and reject anything
# where the match only appears inside a `-c` argument's body.
_REAL_INVOCATION = re.compile(r"(?:^|\s)\S*python\S*\s+\S*l1_wakeword/daemon\.py(?:\s|$)")


def _looks_like_real_daemon(cmdline: str) -> bool:
    if " -c " in cmdline or cmdline.rstrip().endswith(" -c"):
        return False
    return bool(_REAL_INVOCATION.search(cmdline))


def is_running() -> bool:
    """Uses `pgrep -f` for just the candidate PIDs (one per line,
    unambiguous -- PIDs are numeric, never contain embedded newlines),
    then `ps -p <pid>` per PID for that specific process's own command
    line. Deliberately not `pgrep -fl`: a multi-line `-c "<script>"`
    command line comes back from pgrep with its own embedded newlines,
    which breaks a naive "one line per process" parse and can misalign
    which command line belongs to which match -- confirmed live while
    testing this fix. Fetching each PID's command line independently
    via ps avoids that ambiguity entirely."""
    pids = subprocess.run(["pgrep", "-f", DAEMON_PROCESS_PATTERN], capture_output=True, text=True)
    if pids.returncode != 0:
        return False
    for pid in pids.stdout.split():
        cmd = subprocess.run(["ps", "-o", "command=", "-p", pid], capture_output=True, text=True)
        if cmd.returncode == 0 and _looks_like_real_daemon(cmd.stdout):
            return True
    return False
