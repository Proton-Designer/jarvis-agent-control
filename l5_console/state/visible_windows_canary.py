#!/usr/bin/env python3
"""Canary for the visible/background team feature
(SPEC-gaps-and-build-plan.md SS1.6) -- the reuse/background-team logic,
plus (opt-in only) the one live property that cannot be proven without a
real window: closing it does not kill the agent.

DEFAULT RUN OPENS NO WINDOW. Everything below the "hermetic" marker uses
a nonexistent tmux session name and a real scratch registry entry, never
an actual `open`/`osascript` call -- has_attached_client() on a session
that was never created is trivially False, and ensure_window_for_team()/
set_team_visible(False) never reach terminal_window's open path at all
when visible=False, which is most of what this feature needs proven day
to day. terminal_window_canary.py (the Lead's) covers the injection
guards, also without opening anything.

LIVE MODE (`--live`), OPT-IN ONLY: opens exactly ONE real terminal
window, verifies the close-does-not-kill property against it, and closes
it back down -- all in one tight sequence with a `finally` that cleans up
even if an assertion fails midway, never leaving a window attached to a
session that gets killed out from under it. Added after a real incident,
2026-08-20: an earlier draft of this file opened a window, then failed an
assertion, then proceeded to kill the tmux session anyway in a later
step -- leaving a real Ghostty window attached to nothing on Ayman's
actual desktop, which he had to notice and close by hand. Never again:
open, verify, kill-and-verify, done, in that order, with nothing deferred
past the `finally`.

Run (hermetic, safe for a normal sweep):
    python3 l5_console/state/visible_windows_canary.py

Run (opens and closes exactly one real window):
    python3 l5_console/state/visible_windows_canary.py --live
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

os.environ["JARVIS_TEST_RUN"] = f"visible-windows-canary-{uuid.uuid4().hex[:8]}"

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from jarvis_paths import jarvis_project_home  # noqa: E402

import terminal_window as tw  # noqa: E402
import team_actions as ta  # noqa: E402
from teams import TEAMS_REGISTRY_PATH, save_registry  # noqa: E402
from models import Team, TeamMember, LIVENESS_RUNNING  # noqa: E402

assert "test_runs" in str(TEAMS_REGISTRY_PATH), (
    f"TEAMS_REGISTRY_PATH is NOT test-isolated ({TEAMS_REGISTRY_PATH}) -- refusing to run"
)

RESULTS: list[tuple[str, bool, str]] = []
NONEXISTENT_SESSION = f"jarvis-vwcanary-nonexistent-{uuid.uuid4().hex[:8]}"


def check(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((name, passed, detail))
    print(f"  {'ok  ' if passed else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not passed else ""))


def _has_session(name: str) -> bool:
    return subprocess.run(["tmux", "has-session", "-t", name], capture_output=True).returncode == 0


def run_hermetic() -> None:
    print("hermetic checks (no window opened)")
    check("has_attached_client() is False for a session that was never created", not tw.has_attached_client(NONEXISTENT_SESSION))

    registry_entry_bg = {
        "id": "vwcanary-bg", "aliases": ["vwcanary-bg"], "root": "/tmp/vwcanary-bg",
        "lead": "bg-session", "members": [{"tmux": NONEXISTENT_SESSION, "claude_session": "bg-session"}],
        "visible": False,
    }
    save_registry([registry_entry_bg])
    bg_team = Team(
        id="vwcanary-bg", aliases=["vwcanary-bg"], root="/tmp/vwcanary-bg",
        has_lead=True, lead_reachable=True, visible=False,
        members=[TeamMember(tmux=NONEXISTENT_SESSION, claude_session="bg-session", liveness=LIVENESS_RUNNING, activity=None, is_lead=True)],
    )
    ensure_result = ta.ensure_window_for_team(bg_team)
    check("ensure_window_for_team() no-ops for a background team (never reaches terminal_window)", ensure_result["opened"] is False, detail=str(ensure_result))

    toggle_result = ta.set_team_visible("vwcanary-bg", False)
    check("set_team_visible(False) never reaches terminal_window either", toggle_result["ok"] and toggle_result.get("opened") is not True)

    visible_team = Team(
        id="vwcanary-bg", aliases=["vwcanary-bg"], root="/tmp/vwcanary-bg",
        has_lead=False, lead_reachable=False, visible=True,
        members=[TeamMember(tmux=None, claude_session="bg-session", liveness="stopped", activity=None, is_lead=False)],
    )
    ensure_no_runner = ta.ensure_window_for_team(visible_team)
    check("ensure_window_for_team() no-ops when visible but nothing is RUNNING yet", ensure_no_runner["opened"] is False, detail=str(ensure_no_runner))


def run_live() -> None:
    print()
    print("live check (opens and closes exactly one real window)")
    session = f"jarvis-vwcanary-live-{uuid.uuid4().hex[:8]}"

    def pane_pid() -> str | None:
        result = subprocess.run(["tmux", "list-panes", "-t", session, "-F", "#{pane_pid}"], capture_output=True, text=True)
        return result.stdout.strip() or None

    def kill_window_process() -> None:
        result = subprocess.run(["tmux", "list-clients", "-t", session], capture_output=True, text=True)
        line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else None
        if line is None:
            return
        tty = line.split(":")[0].strip().replace("/dev/", "")
        ps = subprocess.run(["ps", "-t", tty, "-o", "pid="], capture_output=True, text=True)
        for pid_str in ps.stdout.split():
            subprocess.run(["kill", "-HUP", pid_str], capture_output=True)

    try:
        subprocess.run(["tmux", "new-session", "-d", "-s", session, "-x", "80", "-y", "24", "sleep 120"], check=True)
        pid_before = pane_pid()
        check("scratch session came up with a real process inside", bool(pid_before), detail=str(pid_before))

        open_result = tw.open_window_for_session(session)
        check("open_window_for_session() succeeded", open_result["ok"], detail=str(open_result))

        attached = False
        for _ in range(20):
            if tw.has_attached_client(session):
                attached = True
                break
            time.sleep(0.5)
        check("a real client attached after opening", attached)

        kill_window_process()
        detached = False
        for _ in range(10):
            if not tw.has_attached_client(session):
                detached = True
                break
            time.sleep(0.5)
        check("client detached after the window process was killed (closed)", detached)
        check(
            "tmux session is STILL ALIVE after the window closed -- a window is a view, not the process",
            _has_session(session),
        )
        check("the process INSIDE the session is unaffected (same pid, still running)", pane_pid() == pid_before)

    finally:
        subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)
        check("session killed and confirmed gone (cleanup, not a new claim about the design)", not _has_session(session))


def run() -> int:
    live = "--live" in sys.argv
    print(f"visible_windows_canary: JARVIS_TEST_RUN={os.environ['JARVIS_TEST_RUN']!r}, live={live}")

    try:
        run_hermetic()
        if live:
            run_live()
        else:
            print()
            print("(skipping the live window-open check -- pass --live to run it; opens and closes exactly one window)")
    finally:
        test_run_root = TEAMS_REGISTRY_PATH.parent
        if "test_runs" in str(test_run_root):
            import shutil
            shutil.rmtree(test_run_root, ignore_errors=True)

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
