#!/bin/bash
# The documented off switch. A plain `kill`/Ctrl-C also works now
# (daemon.py catches the signal and exits cleanly, and KeepAlive's
# SuccessfulExit:false only restarts on a crash) -- this script does
# that AND fully unloads the LaunchAgent (bootout unregisters the job),
# so it won't come back on the next login/reboot until reloaded either.
set -euo pipefail
PLIST="$HOME/Library/LaunchAgents/com.jarvis.l1wakeword.plist"

if [ ! -f "$PLIST" ]; then
    echo "Not installed ($PLIST doesn't exist) -- nothing to stop."
    exit 0
fi

launchctl bootout "gui/$(id -u)" "$PLIST" 2>&1 || echo "(already stopped)"
echo "Jarvis wake-word listener stopped. It will not restart on its own."
echo "To start it again: launchctl bootstrap gui/\$(id -u) $PLIST"
