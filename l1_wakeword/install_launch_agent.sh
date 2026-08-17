#!/bin/bash
# Installs the LaunchAgent plist -- does NOT load/start it.
#
# Per the standing rule established this session (no unattended
# microphone capture, ever): a LaunchAgent with RunAtLoad+KeepAlive that
# opens the mic is exactly the case that rule exists for. This script
# copies the plist into place so it's ready, and prints the load command
# rather than running it. The load happens for the first time during the
# live-mic session, with Ayman present and knowing it's happening --
# that's also the session that answers the open TCC-identity question
# (see README), which should be resolved before this runs unattended
# across reboots anyway.
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p logs
PLIST_SRC="$(pwd)/com.jarvis.l1wakeword.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.jarvis.l1wakeword.plist"

cp "$PLIST_SRC" "$PLIST_DST"
echo "Installed: $PLIST_DST"
echo
echo "NOT loaded. To start it (only during a watched session, with the mic"
echo "legitimately in use, per the standing rule):"
echo
echo "  launchctl bootstrap gui/\$(id -u) $PLIST_DST"
echo
echo "To stop it later: kill <pid>, or ./stop_wakeword.sh to also fully"
echo "unload it. See README's 'How to turn this off' section."
