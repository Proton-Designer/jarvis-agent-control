#!/bin/bash
# Launch the Jarvis console (L5). Standalone -- does NOT start the wake
# daemon, and never touches the microphone. Same deliberate separation as
# start.sh: opening the mic is always its own explicit step.
#
# Absolute paths throughout so this works from any cwd and so the process
# is identifiable in `ps` (the same lesson that produced run_daemon.sh's
# $(pwd) fix -- a bare relative path made the daemon invisible to pgrep
# and wake.running read false forever while it was working fine).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
exec "$HERE/l5_console/app/.venv/bin/python3" "$HERE/l5_console/app/main.py"
