#!/usr/bin/env python3
"""Canary for terminal_window.py + the visible/background team feature
(SPEC-gaps-and-build-plan.md SS1.6). Live-drives a real scratch tmux
session and a real terminal window -- this WILL visibly flash a window
open and closed when run interactively.

ISOLATED VIA JARVIS_TEST_RUN, same discipline as every other canary in
this directory -- see team_actions_canary.py's docstring for the full
incident this pattern exists to prevent. This canary's own tmux session
is a scratch one this file creates and kills directly (never a real
Claude Code launch), so it needs no isolation beyond not touching
~/Jarvis/teams.json, which JARVIS_TEST_RUN + jarvis_project_home() already
guarantees structurally.

Proves the two safety properties the whole feature rests on, plus the
background-team no-op:
  - A window attached to a session, then killed (closed), leaves the
    tmux session -- and the process inside it -- alive and unaffected.
    "A window is a view, not the process."
  - Killing the SESSION itself (not just the window) does end it -- the
    other direction, so the first assertion isn't vacuously true of
    something that can never be killed at all.
  - A background team (visible=False) opens no window -- ensure_window_for_team()
    and set_team_visible(..., False) never call terminal_window at all.
  - Reuse: calling open_window_for_session() a second time while a client
    is still attached does not stack a second window (has_attached_client()
    short-circuits it) -- checked by client count, not by counting OS
    windows (no reliable app-agnostic way to do that, and it isn't the
    property that matters -- see terminal_window.py's own docstring).

Run (opens and closes one real terminal window):

    python3 l5_console/state/visible_windows_canary.py
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

SESSION = f"jarvis-vwcanary-{uuid.uuid4().hex[:8]}"
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((name, passed, detail))
    print(f"  {'ok  ' if passed else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not passed else ""))


def _has_session(name: str) -> bool:
    return subprocess.run(["tmux", "has-session", "-t", name], capture_output=True).returncode == 0


def _pane_pid(name: str) -> str | None:
    result = subprocess.run(
        ["tmux", "list-panes", "-t", name, "-F", "#{pane_pid}"],
        capture_output=True, text=True,
    )
    line = result.stdout.strip()
    return line or None


def _kill_window_process(tmux_session: str) -> None:
    """Simulates the user closing the window. Found live debugging this
    canary's first draft: the GUI app's own process (what `ps aux | grep
    Ghostty` shows) is NOT what's attached to the pty -- `ps -t <tty>`
    on a real attached client shows `login` and `tmux attach -t <session>`
    instead, spawned BY the app but distinct from it. Closing a real
    window tears down the pty, which SIGHUPs whatever's in its foreground
    process group -- killing every process on that tty (not filtering by
    app name, which matched nothing) is the faithful simulation of that,
    not a workaround for it."""
    result = subprocess.run(["tmux", "list-clients", "-t", tmux_session], capture_output=True, text=True)
    line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else None
    if line is None:
        return
    tty = line.split(":")[0].strip().replace("/dev/", "")  # e.g. "/dev/ttys057: ..." -> "ttys057"
    ps = subprocess.run(["ps", "-t", tty, "-o", "pid="], capture_output=True, text=True)
    for pid_str in ps.stdout.split():
        subprocess.run(["kill", "-HUP", pid_str], capture_output=True)


def run() -> int:
    print(f"visible_windows_canary: tmux session {SESSION!r}, JARVIS_TEST_RUN={os.environ['JARVIS_TEST_RUN']!r}")

    try:
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", SESSION, "-x", "80", "-y", "24", "sleep 300"],
            check=True,
        )
        check("scratch session came up", _has_session(SESSION))
        inner_pid_before = _pane_pid(SESSION)
        check("scratch session has a real process inside it", bool(inner_pid_before), detail=str(inner_pid_before))

        # --- Reuse / detection, hermetic, no window opened yet ---------
        check("has_attached_client() is False before anything attaches", not tw.has_attached_client(SESSION))
        app = tw.detect_terminal_app()
        check("detect_terminal_app() found something on this machine", app is not None, detail=str(app))

        # --- Background team: no window, ever ---------------------------
        registry_entry_bg = {
            "id": "vwcanary-bg", "aliases": ["vwcanary-bg"], "root": "/tmp/vwcanary-bg",
            "lead": "bg-session", "members": [{"tmux": SESSION, "claude_session": "bg-session"}],
            "visible": False,
        }
        save_registry([registry_entry_bg])
        bg_team = Team(
            id="vwcanary-bg", aliases=["vwcanary-bg"], root="/tmp/vwcanary-bg",
            has_lead=True, lead_reachable=True, visible=False,
            members=[TeamMember(tmux=SESSION, claude_session="bg-session", liveness=LIVENESS_RUNNING, activity=None, is_lead=True)],
        )
        ensure_result = ta.ensure_window_for_team(bg_team)
        check("ensure_window_for_team() no-ops for a background team", ensure_result["opened"] is False, detail=str(ensure_result))
        check("no window actually opened for the background team", not tw.has_attached_client(SESSION))

        toggle_result = ta.set_team_visible("vwcanary-bg", False)
        check("set_team_visible(False) doesn't open a window either", toggle_result["ok"] and not tw.has_attached_client(SESSION))

        # --- Open a real window, verify attach ---------------------------
        open_result = tw.open_window_for_session(SESSION)
        check("open_window_for_session() reports ok", open_result["ok"], detail=str(open_result))
        check("open_window_for_session() reports opened=True on a fresh session", open_result.get("opened") is True, detail=str(open_result))

        attached = False
        for _ in range(20):  # up to ~10s for the app to actually launch and attach
            if tw.has_attached_client(SESSION):
                attached = True
                break
            time.sleep(0.5)
        check("a real client attached to the session after opening", attached)

        # --- Reuse: opening again while attached does not stack -------
        reopen_result = tw.open_window_for_session(SESSION)
        check(
            "opening again while a client is already attached reuses, doesn't stack",
            reopen_result["ok"] and reopen_result.get("opened") is False,
            detail=str(reopen_result),
        )

        # --- THE safety property: closing the window != killing the agent
        _kill_window_process(SESSION)
        time.sleep(1)
        check(
            "tmux session is STILL ALIVE after the window process was killed -- a window is a view, not the process",
            _has_session(SESSION),
        )
        inner_pid_after = _pane_pid(SESSION)
        check(
            "the process INSIDE the session is unaffected (same pid, still running)",
            inner_pid_after == inner_pid_before,
            detail=f"before={inner_pid_before!r} after={inner_pid_after!r}",
        )
        check("client detached after the window process died", not tw.has_attached_client(SESSION))

        # --- The other direction: killing the SESSION does end it -------
        subprocess.run(["tmux", "kill-session", "-t", SESSION], capture_output=True)
        time.sleep(0.5)
        check("killing the session itself DOES end it (not vacuously unkillable)", not _has_session(SESSION))

    finally:
        subprocess.run(["tmux", "kill-session", "-t", SESSION], capture_output=True)
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
