#!/bin/bash
# Stable entrypoint for the LaunchAgent -- this script's own path is what
# goes in the plist, so it can stay a normal project file (edited freely)
# without touching the identity launchd/TCC actually cares about, which is
# the interpreter binary this execs into below.
set -euo pipefail
cd "$(dirname "$0")"

# TCC path pending the live-mic test, per the Lead's ruling: this is
# still a binary we don't own (Homebrew can move/replace it on upgrade),
# but it's the interim choice until the live-mic session determines
# whether TCC actually keys the grant on this path or something else --
# that result decides between an .app bundle or a vendored interpreter.
# See README.md's "Process lifecycle" section for the full reasoning.
PYTHON="/opt/homebrew/opt/python@3.13/bin/python3.13"

# Invoking $PYTHON directly (not via .venv/bin/python) means Python's
# usual venv auto-detection (finding .venv/pyvenv.cfg next to argv[0])
# does NOT fire -- that detection walks from the invoked path, and this
# path has no pyvenv.cfg next to it. So the venv's packages (openwakeword,
# onnxruntime, etc.) have to be made available explicitly, not implicitly:
export PYTHONPATH="$(pwd)/.venv/lib/python3.13/site-packages"

# Absolute path to daemon.py, not a bare relative "daemon.py" -- real bug,
# found live (2026-08-18, Ayman's own session): l5_console/state/wake.py
# detects the daemon via `pgrep -f "l1_wakeword/daemon.py"` against the
# running process's full command line. With a bare relative path, `cd`
# above means the actual command line is "...python3.13 daemon.py" --
# the string "l1_wakeword/daemon.py" never appears in it, so pgrep finds
# nothing and wake.running is permanently false even though the daemon
# (and the wake word) is genuinely working the whole time. Only the
# detection was broken -- exactly the "control reflects reality, not
# intent" failure mode, arriving from a seam neither this file nor
# wake.py could see on its own. $(pwd) is safe here specifically because
# `cd "$(dirname "$0")"` above already made pwd absolute and correct
# regardless of how this script itself was invoked (relative, absolute,
# or via a symlink from the LaunchAgent).
exec "$PYTHON" "$(pwd)/daemon.py"
