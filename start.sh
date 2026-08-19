#!/bin/bash
# Single startup command for what needs to be up before you run the
# daemon: the two ENGINE roles (concierge + orchestrator), as recorded
# in ~/Jarvis/engine.json.
#
# Deliberately does NOT start the daemon itself (l1_wakeword/daemon.py)
# -- that's a separate, deliberate step you run when you're actually
# ready to dictate, per the standing "no unattended microphone capture"
# rule. This script never touches the microphone.
#
# Idempotent: safe to run again if things are already up -- reports
# current state either way instead of erroring on "already running."
#
# No longer starts Ollama: the local model was disconnected from the
# wiring on 2026-08-18 and nothing in the live path calls it any more.
# No longer starts `claude-orchestrator` either -- that session predates
# the ENGINE split and is not what voice talks to. Voice goes to the
# CONCIERGE role; the concierge hands off to the ORCHESTRATOR role.
#
# Lives in the source repo (versioned, has history), not ~/Jarvis --
# ~/Jarvis holds personal, unversioned state (CLAUDE.md, knowledge/,
# skills/), and this is code, so it belongs with the rest of the code.
# ~/Jarvis/README.md points here.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Checking the ENGINE roles (from ~/Jarvis/engine.json)..."
"$HERE/l5_console/app/.venv/bin/python3" - <<'PY'
import sys
sys.path.insert(0, "/Users/aymanmohammed/Desktop/Jarvis/l5_console/state")
import engine_roles

ok = True
for role in ("concierge", "orchestrator"):
    rec = engine_roles.get_role_record(role)
    if not rec:
        print(f"  {role:<13} NOT ATTACHED -- open the console (./run_console.sh) and attach one.")
        ok = False
        continue
    live = engine_roles.role_liveness(role)
    if not live.get("running"):
        print(f"  {role:<13} attached to {rec['tmux']!r} but NOT RUNNING (liveness={live.get('liveness')})")
        print(f"                open the console and re-create or re-attach it.")
        ok = False
    elif not live.get("tools_reachable"):
        print(f"  {role:<13} running in {rec['tmux']!r} but its MCP tools are NOT REACHABLE.")
        print(f"                usually the trust prompt -- attach and answer it (option 1, not 2).")
        ok = False
    else:
        print(f"  {role:<13} up: {rec['tmux']}  ({rec.get('model')}, effort {rec.get('effort')})")

sys.exit(0 if ok else 1)
PY
ENGINE_OK=$?

echo ""
if [ "$ENGINE_OK" -ne 0 ]; then
    echo "Not ready. Fix the roles above first:"
    echo "  ./run_console.sh      (ENGINE section -- create, attach, or swap)"
    exit 1
fi

echo "Ready. To actually dictate, run the daemon yourself in its own terminal"
echo "tab (separate, deliberate step -- this is the part that opens the mic):"
echo ""
echo "  jarvis-voice"
echo ""
echo "or, without the alias:"
echo "  cd $HERE && l1_wakeword/.venv/bin/python3 l1_wakeword/daemon.py --live-deliver"
echo ""
echo "No --target: the daemon resolves the CONCIERGE role from engine.json"
echo "itself. Passing --target overrides that and sends straight to a raw"
echo "tmux session, bypassing the concierge -- only do that to test the"
echo "transport in isolation."
