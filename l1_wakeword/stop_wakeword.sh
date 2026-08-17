#!/bin/bash
# The real kill switch. `kill`/`pkill` on the daemon process will NOT
# work as an off switch -- the LaunchAgent's KeepAlive is bare `true`,
# so launchd just restarts it. This is the one command that actually
# stops it and keeps it stopped (bootout unregisters the job; it won't
# come back on the next login/reboot until re-loaded).
set -euo pipefail
PLIST="$HOME/Library/LaunchAgents/com.jarvis.l1wakeword.plist"

if [ ! -f "$PLIST" ]; then
    echo "Not installed ($PLIST doesn't exist) -- nothing to stop."
    exit 0
fi

launchctl bootout "gui/$(id -u)" "$PLIST" 2>&1 || echo "(already stopped)"
echo "Jarvis wake-word listener stopped. It will not restart on its own."
echo "To start it again: launchctl bootstrap gui/\$(id -u) $PLIST"
