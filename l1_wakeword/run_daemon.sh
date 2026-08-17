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

# Not the live-mic loop yet -- daemon.py's live-mic branch is still a
# stub (see daemon.py's __main__). This script exists so the LaunchAgent
# plumbing (install, KeepAlive, logs, stop switch) can be built and
# reviewed now without loading it, per the standing no-unattended-mic
# rule -- it gets pointed at the real live-mic entrypoint and loaded for
# the first time during the live-mic session with Ayman present.
exec "$PYTHON" daemon.py
