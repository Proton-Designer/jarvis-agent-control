#!/usr/bin/env python3
"""Canary: the console's start button must launch a daemon that actually
DELIVERS.

Found live 2026-08-20 in Ayman's own logs. run_daemon.sh -- the script
wake_control.start() spawns when he presses "start" -- ran

    exec "$PYTHON" daemon.py

with no --live-deliver. Without that flag default_deliver() returns at
its dry-run gate (label FORWARDED, forwarded=false, end_to_end_s=0.001)
and, because the instant ack is gated on the same flag, makes NO SOUND
AT ALL. He said "hey Jarvis, hello, how are you doing", Whisper
transcribed it perfectly in 508ms, and nothing happened.

The console's start button is the PRIMARY way this app is used, so the
primary path could never deliver anything. The only working route was
knowing to run daemon.py by hand with a flag nobody would know to pass.

Why a canary and not just the fix: this is a one-word regression in a
shell script, invisible in every Python test, and its symptom is
SILENCE. Nothing errors. Every layer reports success. It is the exact
shape this project keeps finding, and the only way to hold it is to
assert on the launch command itself.

Asserts on the FILE, deliberately -- not by running the daemon, which
would open the microphone.

    l1_wakeword/.venv/bin/python3 l1_wakeword/live_deliver_canary.py
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).parent
RUN_DAEMON = HERE / "run_daemon.sh"
WAKE_CONTROL = HERE.parent / "l5_console" / "app" / "wake_control.py"

FAILURES: list[str] = []


def check(desc: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ok    {desc}")
    else:
        FAILURES.append(desc)
        print(f"  FAIL  {desc}{('  -- ' + detail) if detail else ''}")


def run() -> int:
    check("run_daemon.sh exists", RUN_DAEMON.exists(), str(RUN_DAEMON))
    src = RUN_DAEMON.read_text()

    exec_lines = [ln.strip() for ln in src.splitlines()
                  if ln.strip().startswith("exec ") and "daemon.py" in ln]
    check("it execs daemon.py exactly once", len(exec_lines) == 1, str(exec_lines))
    line = exec_lines[0] if exec_lines else ""

    check("that exec line passes --live-deliver -- WITHOUT IT THE START BUTTON IS SILENT",
          "--live-deliver" in line, line)
    check("...and does not pass --simulate (that would read a wav, not the mic)",
          "--simulate" not in line, line)

    # The other direction: prove the flag is load-bearing rather than
    # decorative, by checking the code path it gates still exists. If
    # someone removes the dry-run gate, this canary should stop claiming
    # to protect something it no longer protects.
    daemon_src = (HERE / "daemon.py").read_text()
    check("daemon.py still HAS a dry-run gate for the flag to control",
          "if not live_deliver:" in daemon_src)
    check("...and the instant ack is still gated on the same flag "
          "(which is why the failure was silent, not just undelivered)",
          "if live_deliver:" in daemon_src)

    check("wake_control still spawns THIS script (the assertion above is about the right file)",
          WAKE_CONTROL.exists() and "run_daemon.sh" in WAKE_CONTROL.read_text())

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        print("\nA daemon launched without --live-deliver transcribes perfectly")
        print("and then does NOTHING, with no error and no sound.")
        return 1
    print("all 7 checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(run())
