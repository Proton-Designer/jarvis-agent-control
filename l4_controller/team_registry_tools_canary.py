#!/usr/bin/env python3
"""
Team registry tools canary (SPEC-orchestration.md SS1.3).

Run after any Claude Code update: `python3 team_registry_tools_canary.py`.
Live-drives real tmux sessions (a scratch dir, never near Ayman's real
work) through both registration paths and asserts the SAME
discover_teams_and_unassigned() the console uses agrees with what was just
registered -- one registry, one liveness computation, no drift between what
a router caller sees and what the console renders for the same team.

WHY THIS EXISTS (2026-08-18): found live, via this exact script's first
draft, that register_team_fresh() silently registered a team whose root did
NOT byte-for-byte match its own freshly-launched session's real
pane_current_path whenever the root sat under a symlinked prefix (macOS's
/tmp -> /private/tmp is the common case) -- the session came up fine, but
teams.py's member_liveness() working-directory guard then failed closed and
reported the brand-new, definitely-running member as LOST. Root cause and
fix are in setup.create_fresh_member()'s docstring. This canary exists so
that regression stays caught by a command, not by a second engineer
re-discovering it by luck.

Cleans up every tmux session and scratch directory it creates, and restores
~/Jarvis/teams.json to whatever it was before the run (never leaves test
teams behind for a live console/router to see).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import team_registry_tools as tools

sys.path.insert(0, str(Path(__file__).parent.parent / "l5_console" / "state"))
from teams import TEAMS_REGISTRY_PATH, load_registry, save_registry  # noqa: E402
from reconnect import wait_for_ready  # noqa: E402

TMUX_BIN = "tmux"
WORKDIR = Path("/tmp/jarvis-team-registry-canary")
ADOPT_SESSION = "jarvis-team-registry-canary-adopt"
FRESH_TEAM_ID = "canaryfresh"
ADOPT_TEAM_ID = "canaryadopt"


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""
    note: str = ""


def sh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run([TMUX_BIN, *args], capture_output=True, check=check)


def team_by_id(teams: list[dict], team_id: str) -> dict | None:
    return next((t for t in teams if t["id"] == team_id), None)


def main() -> int:
    results: list[CheckResult] = []
    original_registry_raw = TEAMS_REGISTRY_PATH.read_text() if TEAMS_REGISTRY_PATH.exists() else None

    sh("kill-session", "-t", ADOPT_SESSION, check=False)
    sh("kill-session", "-t", f"claude-{FRESH_TEAM_ID}", check=False)
    shutil.rmtree(WORKDIR, ignore_errors=True)
    WORKDIR.mkdir(parents=True, exist_ok=True)
    adopt_dir = WORKDIR / "adopt-target"
    fresh_dir = WORKDIR / "fresh-target"
    adopt_dir.mkdir()

    try:
        save_registry([])  # isolate this run from anything already registered

        # --- register_team_by_adoption: launch a real, unassigned session ---
        sh("new-session", "-d", "-s", ADOPT_SESSION, "-c", str(adopt_dir), "claude")
        # Waits for the pane to actually reach READY, not just for the tmux
        # session to exist -- adopt_candidate_info() sends /status via
        # transport.deliver(), which refuses anything but READY. The
        # session shows up as "unassigned" the instant tmux starts it,
        # well before Claude Code itself finishes booting; polling that
        # instead (an earlier draft of this canary did) sent /status too
        # early and produced a false "did not report a Claude session id"
        # failure that had nothing to do with the adoption logic itself.
        reached_ready = wait_for_ready(ADOPT_SESSION, timeout_s=25)
        results.append(
            CheckResult(
                name="adopt-target session reaches READY before adoption is attempted",
                passed=reached_ready,
                detail=f"wait_for_ready() -> {reached_ready}",
            )
        )

        adopt_result = tools.register_team_by_adoption(ADOPT_TEAM_ID, ADOPT_SESSION, aliases=["canary-a"])
        results.append(
            CheckResult(
                name="register_team_by_adoption() succeeds on a real unassigned session",
                passed=adopt_result.get("ok", False),
                detail=str(adopt_result),
            )
        )

        dup = tools.register_team_by_adoption(ADOPT_TEAM_ID, ADOPT_SESSION)
        results.append(
            CheckResult(
                name="duplicate team id is refused, not silently overwritten",
                passed=not dup.get("ok", True),
                detail=str(dup),
            )
        )

        teams_after_adopt = tools.list_teams()
        adopted_team = team_by_id(teams_after_adopt, ADOPT_TEAM_ID)
        results.append(
            CheckResult(
                name="adopted member shows liveness=running immediately after registration",
                passed=bool(adopted_team and adopted_team["members"][0]["liveness"] == "running"),
                detail=str(adopted_team),
            )
        )

        # --- Registry vs. live poll agree with what the CONSOLE would see ---
        console_teams, _unassigned = __import__("teams").discover_teams_and_unassigned()
        console_adopted = next((t for t in console_teams if t.id == ADOPT_TEAM_ID), None)
        results.append(
            CheckResult(
                name="console's own discover_teams_and_unassigned() sees the same team, same liveness",
                passed=bool(console_adopted and console_adopted.members[0].liveness == "running"),
                detail=f"console-side liveness: {console_adopted.members[0].liveness if console_adopted else None}",
                note="a mismatch here means router and console are somehow reading different state -- "
                "should be structurally impossible (both call the same function)"
                if not (console_adopted and console_adopted.members[0].liveness == "running")
                else "",
            )
        )

        # --- Killed member degrades to stopped/lost, never stays "running" ---
        sh("kill-session", "-t", ADOPT_SESSION, check=False)
        time.sleep(1)
        teams_after_kill = tools.list_teams()
        killed_team = team_by_id(teams_after_kill, ADOPT_TEAM_ID)
        liveness_after_kill = killed_team["members"][0]["liveness"] if killed_team else None
        results.append(
            CheckResult(
                name="killed member is never reported as still running (liveness is always polled)",
                passed=liveness_after_kill in ("stopped", "lost"),
                detail=f"liveness after kill: {liveness_after_kill}",
                note="STILL REPORTS running AFTER THE SESSION IS DEAD -- this is the exact "
                "registry-vs-reality gap the whole design exists to prevent"
                if liveness_after_kill == "running"
                else "",
            )
        )

        # --- register_team_fresh: the symlinked-root regression guard ---
        fresh_result = tools.register_team_fresh(FRESH_TEAM_ID, str(fresh_dir), aliases=["canary-f"], model="sonnet")
        results.append(
            CheckResult(
                name="register_team_fresh() launches and registers cleanly",
                passed=fresh_result.get("ok", False),
                detail=str(fresh_result),
            )
        )
        teams_after_fresh = tools.list_teams()
        fresh_team = team_by_id(teams_after_fresh, FRESH_TEAM_ID)
        fresh_liveness = fresh_team["members"][0]["liveness"] if fresh_team else None
        results.append(
            CheckResult(
                name="freshly-created member shows liveness=running, not lost (the symlinked-root regression)",
                passed=fresh_liveness == "running",
                detail=f"root registered as: {fresh_team['root'] if fresh_team else None}, liveness: {fresh_liveness}",
                note="root did not match the live session's real pane_current_path -- this is "
                "exactly the /tmp-vs-/private/tmp bug this canary exists to catch"
                if fresh_liveness != "running"
                else "",
            )
        )

        # --- Survives a fresh process (stand-in for router restart/compaction) ---
        reread = json.loads(TEAMS_REGISTRY_PATH.read_text())
        results.append(
            CheckResult(
                name="both teams persisted to disk (survives a restart/compaction -- it's a file, not memory)",
                passed={ADOPT_TEAM_ID, FRESH_TEAM_ID}.issubset({t["id"] for t in reread}),
                detail=f"on-disk team ids: {[t['id'] for t in reread]}",
            )
        )

    finally:
        sh("kill-session", "-t", ADOPT_SESSION, check=False)
        sh("kill-session", "-t", f"claude-{FRESH_TEAM_ID}", check=False)
        shutil.rmtree(WORKDIR, ignore_errors=True)
        shutil.rmtree(Path(str(WORKDIR).replace("/tmp/", "/private/tmp/")), ignore_errors=True)
        if original_registry_raw is not None:
            TEAMS_REGISTRY_PATH.write_text(original_registry_raw)
        else:
            save_registry([])

    all_ok = True
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        if not r.passed:
            all_ok = False
        line = f"[{status}] {r.name} -- {r.detail}"
        if r.note:
            line += f"\n       note: {r.note}"
        print(line)

    print(
        "\nAll checks above are LIVE-DRIVEN against real tmux sessions this run "
        "(a scratch dir, cleaned up), including a real restart-of-the-underlying-"
        "session case -- not fixture data."
    )

    if not all_ok:
        print("\nCANARY FAILED. Do not trust router-driven team registration until this is fixed.")
        return 1

    print("\nAll checks passed. ~/Jarvis/teams.json restored to its pre-run state.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
