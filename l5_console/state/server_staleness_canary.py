#!/usr/bin/env python3
"""Canary for R5 (docs/PLAN-silence-and-ux.md): "this role is older than
your code."

The 2026-08-20 incident this exists to make impossible to repeat
silently again: both engine roles' MCP server subprocesses were forked
before a real fix to code they import landed, ran the pre-fix version
for their entire life, and the console showed both green throughout --
nothing said "running code from before your last fix." That cost four
fix attempts and three engineers an afternoon.

BOTH DIRECTIONS: a server forked before the newest l4_controller/ change
IS flagged stale; one forked after is NOT (same discipline as every
other canary here -- a check that only proves "sometimes it says stale"
passes just as happily if it says stale for everything, which would be
as useless as the invisibility it replaces).

LIVE HALF, READ-ONLY: cross-checks _role_server_stale()'s actual verdict
for the REAL running concierge/orchestrator against ground truth this
canary computes independently (its own ps + stat calls), rather than
trusting the function's own arithmetic to grade itself. Never launches,
kills, or restarts anything -- this whole feature is a read.

    l5_console/app/.venv/bin/python3 l5_console/state/server_staleness_canary.py
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "l4_controller"))

import engine_roles  # noqa: E402
import format_helpers as fh  # noqa: E402
from models import RoleSlot, LIVENESS_RUNNING, LIVENESS_STOPPED  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((name, passed, detail))
    print(f"  {'ok  ' if passed else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not passed else ""))


def slot(**kw) -> RoleSlot:
    base = dict(
        attached=True, name="r", model="sonnet", effort="medium", tmux="t", working_dir="/tmp",
        claude_session="u", liveness=LIVENESS_RUNNING, tools_reachable=True,
    )
    base.update(kw)
    return RoleSlot(**base)


def run() -> int:
    print("PREDICATE -- is_server_stale(), both directions")
    stale = slot(server_stale=True)
    fresh = slot(server_stale=False)
    check("a slot with server_stale=True IS flagged", fh.is_server_stale(stale) is True)
    check("a slot with server_stale=False is NOT flagged", fh.is_server_stale(fresh) is False)
    never_set = slot()
    check("a slot that never set the field defaults to NOT stale (never invented)", fh.is_server_stale(never_set) is False)

    print()
    print("_newest_relevant_mtime() -- sane against the real l4_controller/ directory")
    newest = engine_roles._newest_relevant_mtime()
    check("returns a real, non-zero timestamp", newest > 0, str(newest))
    check("not in the future", newest <= time.time() + 5, str(newest))
    l4_dir = Path(__file__).parent.parent.parent / "l4_controller"
    real_newest = max((f.stat().st_mtime for f in l4_dir.glob("*.py")), default=0.0)
    check("matches an independently-computed max(mtime) over the same directory",
          abs(newest - real_newest) < 1.0, f"{newest} vs {real_newest}")

    print()
    print("_role_server_stale() -- LIVE, read-only, cross-checked against independently gathered ground truth")
    for role in engine_roles.ROLES:
        pid = engine_roles._role_server_pid(role)
        if pid is None:
            print(f"  --    {role}: no server running right now, skipping this role's live check")
            continue
        # Ground truth, computed HERE, independently of the function under test.
        ps_result = subprocess.run(["ps", "-o", "lstart=", "-p", pid], capture_output=True, text=True)
        started_at = time.mktime(time.strptime(ps_result.stdout.strip(), "%a %b %d %H:%M:%S %Y"))
        expected_stale = real_newest > started_at

        actual = engine_roles._role_server_stale(role)
        check(
            f"{role}: _role_server_stale() agrees with independently-gathered ps+stat ground truth",
            actual == expected_stale,
            f"function said {actual}, ground truth says {expected_stale} "
            f"(server started {time.strftime('%H:%M:%S', time.localtime(started_at))}, "
            f"newest l4_controller/ change {time.strftime('%H:%M:%S', time.localtime(real_newest))})",
        )

    print()
    print("role_liveness() -- the field actually reaches the RoleSlot contract")
    for role in engine_roles.ROLES:
        liveness = engine_roles.role_liveness(role)
        check(f"{role}: role_liveness() returns a server_stale key", "server_stale" in liveness, str(liveness.keys()))
        if liveness.get("liveness") != LIVENESS_RUNNING:
            check(f"{role}: not running -- server_stale correctly False", liveness["server_stale"] is False)

    print()
    if failures := [r for r in RESULTS if not r[1]]:
        print(f"{len(failures)}/{len(RESULTS)} FAILED:")
        for name, _, detail in failures:
            print(f"  - {name}" + (f" ({detail})" if detail else ""))
        return 1
    print(f"all {len(RESULTS)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(run())
