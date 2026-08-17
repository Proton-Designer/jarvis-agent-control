#!/bin/bash
# Single startup command for what needs to be up before you run the
# daemon: the orchestrator, and Ollama (used by the L2.5 concierge).
# Deliberately does NOT start the daemon itself (l1_wakeword/daemon.py)
# -- that's a separate, deliberate step you run when you're actually
# ready to dictate, per the standing "no unattended microphone capture"
# rule. This script never touches the microphone.
#
# Idempotent: safe to run again if things are already up -- reports
# current state either way instead of erroring on "already running."
#
# Lives in the source repo (versioned, has history), not ~/Jarvis --
# ~/Jarvis holds personal, unversioned state (CLAUDE.md, knowledge/,
# skills/), and this is code, so it belongs with the rest of the code.
# ~/Jarvis/README.md points here.
set -uo pipefail

echo "Checking Ollama..."
if pgrep -f "ollama serve" > /dev/null 2>&1 || pgrep -x "ollama" > /dev/null 2>&1; then
    echo "  Ollama is running."
else
    echo "  Ollama is NOT running -- starting it (ollama serve, backgrounded)."
    nohup ollama serve > /tmp/ollama-serve.log 2>&1 &
    disown
    sleep 1
    if pgrep -x "ollama" > /dev/null 2>&1; then
        echo "  Started. (Note: models still load cold on first use -- see"
        echo "  ~/Jarvis/README.md's cold-start note. This doesn't warm them, it"
        echo "  just makes sure the service is reachable when the concierge needs it.)"
    else
        echo "  FAILED to start. Check /tmp/ollama-serve.log, or start manually:"
        echo "  ollama serve"
    fi
fi

echo ""
echo "Checking the orchestrator..."
if tmux has-session -t claude-orchestrator 2>/dev/null; then
    echo "  claude-orchestrator is already up."
else
    echo "  Not running -- starting it."
    tmux new-session -d -s claude-orchestrator -c "$HOME/Jarvis"
    tmux send-keys -t claude-orchestrator 'claude' Enter
    echo "  Started. On first launch in this directory, Claude Code will ask to"
    echo "  trust the jarvis-l4 MCP server -- see ~/Jarvis/README.md's note on"
    echo "  this before answering it (choose option 1, not option 2)."
fi

echo ""
echo "Ready. To actually dictate, run the daemon yourself (separate, deliberate step):"
echo "  cd ~/Desktop/Jarvis && l1_wakeword/.venv/bin/python l1_wakeword/daemon.py --live-deliver --target claude-orchestrator"
