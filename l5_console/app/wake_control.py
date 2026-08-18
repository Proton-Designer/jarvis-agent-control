"""
The wake control's action side (SPEC-TUI.md §3.1, §7). This module only
ever TRIGGERS start/stop -- it never claims to know whether the daemon
is actually running afterward. The displayed state in rail.py/console.py
comes exclusively from JarvisState.wake.running (a real poll, gu2s6tnt's
layer), never from this module's own subprocess handle, precisely
because "the wake control reflects whether the daemon process is alive,
never whether the button was pressed" (§7) -- a console that trusted its
own Popen handle as ground truth would reintroduce the exact bug that
line exists to prevent (this process's belief can be wrong: the daemon
could crash a second after spawning, or already be running from a
terminal this console never touched).

Shells out to the same entrypoint a human would use by hand
(l1_wakeword/run_daemon.sh), not a reimplementation -- §7's "never
bypasses existing gates" applies here directly. Stop sends SIGTERM,
which daemon.py already handles cleanly (see its
_install_clean_shutdown_handler -- exits 0, tears down whisper-server
via its own `with` block) rather than anything console-specific.

Depends on run_daemon.sh's `exec "$PYTHON" daemon.py` (not a plain
invocation) more than it looks like it should -- verified directly,
worth recording since it's non-obvious: a first test harness that ran a
foreground command from a plain bash script (no `exec`) left the process
tree alive minutes after SIGTERM, because bash defers trap/signal
handling until the current foreground command returns on its own. Only
once the test harness matched run_daemon.sh's real `exec` (which
replaces the bash process image with python, so the PID this module
holds becomes daemon.py itself, not a wrapper shell around it) did
SIGTERM reach daemon.py's real handler and `poll()` correctly observe
the exit. If run_daemon.sh ever stops using `exec`, stop() silently
leaves an orphaned process tree and this module has no way to detect
that from the Python side -- flagging the coupling explicitly rather
than letting it be an invisible assumption.

Standing project rule this module exists inside of, not around: no
unattended microphone capture, ever. This code makes the daemon
startable/stoppable by an explicit, watched click in the console --
exactly the sanctioned case (a human pressing a visible button they can
see the console react to), same as running run_daemon.sh by hand in a
terminal. It must never be wired to run automatically (no auto-start on
console launch, no restart-on-crash loop here) -- if that's ever wanted,
it's a LaunchAgent decision requiring Ayman's explicit session-specific
go-ahead per the standing rule, not something this module adds
unilaterally.
"""
from __future__ import annotations

import signal
import subprocess
from pathlib import Path

RUN_DAEMON_SCRIPT = Path(__file__).parent.parent.parent / "l1_wakeword" / "run_daemon.sh"

_process: subprocess.Popen | None = None


def start() -> tuple[bool, str]:
    """Spawns the daemon via its real entrypoint. Returns (ok, message)
    for the caller to surface -- never silently succeeds or fails (§7)."""
    global _process
    if _process is not None and _process.poll() is None:
        return False, "already have a handle to a running daemon process in this console session"
    if not RUN_DAEMON_SCRIPT.exists():
        return False, f"run_daemon.sh not found at {RUN_DAEMON_SCRIPT}"
    try:
        _process = subprocess.Popen(
            [str(RUN_DAEMON_SCRIPT)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError as e:
        return False, f"failed to spawn: {e}"
    return True, f"spawned (pid {_process.pid}) -- confirm via wake.running, not this return value"


def stop() -> tuple[bool, str]:
    """SIGTERM, matching daemon.py's own clean-shutdown handler -- not a
    kill -9, so whisper-server gets torn down and the process exits 0
    rather than looking like a crash to launchd/anything watching it."""
    global _process
    if _process is None or _process.poll() is not None:
        _process = None
        return False, "no daemon process spawned from this console session to stop"
    _process.send_signal(signal.SIGTERM)
    return True, f"sent SIGTERM to pid {_process.pid} -- confirm via wake.running, not this return value"
