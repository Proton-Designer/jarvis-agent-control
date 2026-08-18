#!/usr/bin/env python3
"""Canary for l2_l3_handoff._looks_like_real_server_process() /
orchestrator_has_tools() (SPEC-orchestration.md Known Gaps:
"orchestrator_has_tools() scoping"). Same hazard class as
wake.is_running()'s pgrep collision, found live earlier this session: a
bare `pgrep -f` substring match can be fooled by any process whose full
command line happens to contain the pattern, not just the real one --
including a throwaway `-c "<script>"` test fixture that merely mentions
the path in a comment.

Tests _looks_like_real_server_process() directly against synthetic
command-line strings, not by spawning real processes and inspecting the
live process table -- this machine routinely has a REAL server.py process
running (someone's actual router session), which would make any
process-table-based "nothing should be detected" assertion flaky by
environment, not by code correctness. Testing the pure string -> bool
function in isolation is deterministic and exercises exactly the logic
that changed, same as wake.py's own hardening was reasoned about.

Asserts BOTH directions -- a canary that only proves the happy path
protects nothing.

Run after touching l2_l3_handoff.py's orchestrator_has_tools()/
_looks_like_real_server_process():

    python3 l4_controller/orchestrator_has_tools_canary.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import l2_l3_handoff as h  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((name, passed, detail))
    print(f"  {'ok  ' if passed else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not passed else ""))


REAL_SHAPES = [
    "/usr/bin/python3 /Users/aymanmohammed/Desktop/Jarvis/l4_controller/server.py",
    "python3 l4_controller/server.py",
    "/opt/homebrew/opt/python@3.13/bin/python3.13 /abs/path/l4_controller/server.py",
]

NOT_REAL = [
    # The exact class of bug that hit wake.py: a `-c` script that merely
    # MENTIONS the path in a comment, not a real invocation of it.
    'python3 -c "import time  # l4_controller/server.py placeholder\ntime.sleep(30)"',
    "python3 -c 'print(1)  # l4_controller/server.py'",
    # server_readonly.py must never be mistaken for server.py -- the
    # entire point of the SS0.2 split would be defeated one layer up if a
    # scoping bug here reported "router has tools" when only Haiku's
    # read-only surface is actually up.
    "/usr/bin/python3 /Users/aymanmohammed/Desktop/Jarvis/l4_controller/server_readonly.py",
    "python3 l4_controller/server_readonly.py",
    # Embedded, not the process's OWN trailing argument (e.g. part of a
    # longer path component or an unrelated argument that happens to
    # contain the substring without being bounded by whitespace).
    "python3 /some/other/l4_controller/server.pyc",
    "vim l4_controller/server.py",  # not even python
    "grep -r l4_controller/server.py .",
]


def run() -> int:
    print("orchestrator_has_tools() / _looks_like_real_server_process() canary\n")

    print("1. real invocation shapes ARE recognized")
    for cmdline in REAL_SHAPES:
        check(f"recognized: {cmdline!r}", h._looks_like_real_server_process(cmdline))

    print("\n2. non-real shapes (the actual hazard class) are NOT recognized")
    for cmdline in NOT_REAL:
        check(f"rejected: {cmdline!r}", not h._looks_like_real_server_process(cmdline))

    print()
    failures = [r for r in RESULTS if not r[1]]
    if failures:
        print(f"{len(failures)}/{len(RESULTS)} FAILED:")
        for name, _, detail in failures:
            print(f"  - {name}" + (f" ({detail})" if detail else ""))
        return 1
    print(f"all {len(RESULTS)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(run())
