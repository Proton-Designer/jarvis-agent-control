#!/usr/bin/env python3
"""Canary for team_actions.py + team_context.py (SPEC-teams.md). Live-
drives real tmux sessions in a scratch directory, never near Ayman's
real work.

ISOLATED VIA JARVIS_TEST_RUN, NOT BACKUP/RESTORE. A real incident
(2026-08-18): this file used to back up ~/Jarvis/teams.json, replace it
with test data, and restore it in a `finally` block. The Lead registered
a real team mid a live end-to-end voice test while this canary happened
to be running; the registry read as EMPTY during the window between this
canary's backup and its restore, and the restore only happened to land
correctly because nothing else raced it in that window. Backup-and-
restore is a promise that holds only if nothing else touches the file
meanwhile and this process never crashes before its own cleanup runs --
a redirected path (jarvis_paths.jarvis_project_home(), which teams.py/
engine_roles.py/team_context.py now resolve TEAMS_REGISTRY_PATH/
ENGINE_REGISTRY_PATH/CONTEXT_DIR through) is a guarantee that holds even
if this process is killed outright. JARVIS_TEST_RUN is set below BEFORE
any of those modules are imported, since their paths are module-level
constants resolved at import time.

Proves the spec's Verification section specifically:
  - a directory/id/alias already used by another team is refused AT PICK
    TIME, naming the conflicting team
  - a team with zero members is refused
  - the lead pointer is refused when aimed at a non-member, and set
    correctly when aimed at a real one
  - REMOVING A TEAM LEAVES ITS SESSIONS ALIVE -- the one the Lead cares
    about most: "it detached correctly" is not the same claim as
    "nothing got killed", so this checks the real tmux session is still
    running after remove_team(), not just that the registry entry is gone
  - relocate updates root for a verifiably-same claude_session at a new
    path, and the team reads RUNNING again afterward
  - context capture writes team_id inside the file; a CLAUDE.md-sourced
    capture needs no qualifier trigger, an agent-sourced one does
    (source == "agent")

Run (SLOW -- two real Claude Code launches, one real agent conversation
turn for context capture):

    python3 l5_console/state/team_actions_canary.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

os.environ["JARVIS_TEST_RUN"] = f"team-actions-canary-{uuid.uuid4().hex[:8]}"

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "l4_controller"))

from pane_state import PaneState  # noqa: E402

import team_actions as ta  # noqa: E402
import team_context as tc  # noqa: E402
from teams import TEAMS_REGISTRY_PATH, discover_teams_and_unassigned, load_registry, save_registry  # noqa: E402
from setup import create_fresh_member  # noqa: E402
from reconnect import wait_for_ready  # noqa: E402

WORKDIR = Path("/tmp/jarvis-team-actions-canary")
TEAM_A_ID, TEAM_B_ID = "canaryactionsA", "canaryactionsB"
TMUX_A, TMUX_B = "claude-canaryactionsA", "claude-canaryactionsB"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((name, passed, detail))
    print(f"  {'ok  ' if passed else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not passed else ""))


def team_by_id(teams, team_id):
    return next((t for t in teams if t.id == team_id), None)


def _check_parse_response_regression() -> None:
    """Fast, hermetic unit check -- no tmux needed. Reproduces the exact
    bug ue6rruxg found live (2026-08-18): CONTEXT_PROMPT's own text
    contains the literal field labels as a format example ("teaching the
    format by showing it"), Claude Code echoes the typed prompt back into
    the pane BEFORE the real reply, and the original re.search (first
    match) grabbed the echoed prompt's own placeholder text instead of
    the actual answer sitting further down, correctly formatted. This
    pins the fix (_last_match, not _first_match) against regressing."""
    fake_pane = tc.CONTEXT_PROMPT + "\n\n" + (
        "SUMMARY: A single Python file, app.py, that prints the string "
        "'hello' to standard output.\n"
        "SUBSYSTEMS: single script entry point (app.py); console output via print\n"
        "TECH_STACK: Python"
    )
    parsed = tc._parse_context_response(fake_pane)
    check(
        "parses the REAL reply, not the echoed prompt's own placeholder text",
        parsed is not None and parsed["summary"].startswith("A single Python file"),
        detail=str(parsed),
    )
    check(
        "does not accidentally capture the prompt's own instructional text",
        parsed is not None and "<one sentence" not in parsed["summary"],
        detail=str(parsed),
    )
    check(
        "subsystems parsed correctly from the real reply, not the placeholder",
        parsed is not None and parsed["subsystems"] == ["single script entry point (app.py)", "console output via print"],
        detail=str(parsed),
    )


def _check_wait_for_reply_settled_race() -> None:
    """Hermetic (no tmux, no real timing luck) reproduction of the SECOND
    bug ue6rruxg found live (2026-08-18): right after send-keys, the pane
    can still read READY for one tick BEFORE the model actually starts
    processing. reconnect.wait_for_ready()'s single poll caught that
    pre-processing blip and returned true ~6s before the real reply had
    even been written -- the saved context was still literally the
    prompt's placeholder text.

    Scripts a state sequence (READY blip -> BUSY -> BUSY -> READY,
    settled) via monkeypatched transport calls, so this is deterministic
    regardless of how fast a real model turn happens to run -- the live
    canary case elsewhere in this file exercises the real timing, but
    real timing is exactly what made the original bug intermittent
    (ue6rruxg's repro hit it, an earlier run of this same canary
    apparently didn't). This pins the FIX (must observe an actual BUSY
    before trusting READY) against regressing regardless of luck."""
    scripted_states = [PaneState.READY, PaneState.BUSY, PaneState.BUSY, PaneState.READY]
    call_log: list = []

    class FakeTransport:
        def session_exists(self, tmux_name):
            return True

        def capture_pane(self, tmux_name):
            return "fake-ansi"

    def fake_classify(ansi_text, patterns):
        state = scripted_states[min(len(call_log), len(scripted_states) - 1)]
        call_log.append(state)
        return state

    original_transport, original_classify = tc._transport, tc.classify_pane_ansi
    original_start_interval, original_settle_interval = tc.CONTEXT_TURN_START_POLL_INTERVAL_S, tc.CONTEXT_SETTLE_POLL_INTERVAL_S
    tc._transport = FakeTransport()
    tc.classify_pane_ansi = fake_classify
    tc.CONTEXT_TURN_START_POLL_INTERVAL_S = 0.01
    tc.CONTEXT_SETTLE_POLL_INTERVAL_S = 0.01
    try:
        result = tc._wait_for_reply_settled("fake-tmux")
        check("waits through the READY-blip -> BUSY -> READY sequence, does not stop on the first blip", result is True)
        check("actually observed a real BUSY state before declaring settled", PaneState.BUSY in call_log, detail=str(call_log))
        check("did not short-circuit after just one read", len(call_log) >= 4, detail=str(call_log))
    finally:
        tc._transport, tc.classify_pane_ansi = original_transport, original_classify
        tc.CONTEXT_TURN_START_POLL_INTERVAL_S, tc.CONTEXT_SETTLE_POLL_INTERVAL_S = original_start_interval, original_settle_interval

    print("  -- and the inverse: BUSY genuinely never observed fails closed, never assumes done")
    call_log.clear()
    scripted_states[:] = [PaneState.READY, PaneState.READY, PaneState.READY]
    tc._transport = FakeTransport()
    tc.classify_pane_ansi = fake_classify
    original_turn_timeout = tc.CONTEXT_TURN_START_TIMEOUT_S
    tc.CONTEXT_TURN_START_POLL_INTERVAL_S = 0.01
    tc.CONTEXT_TURN_START_TIMEOUT_S = 0.05
    try:
        result = tc._wait_for_reply_settled("fake-tmux")
        check("never observing BUSY at all returns False (fails closed)", result is False)
    finally:
        tc._transport, tc.classify_pane_ansi = original_transport, original_classify
        tc.CONTEXT_TURN_START_POLL_INTERVAL_S = original_start_interval
        tc.CONTEXT_TURN_START_TIMEOUT_S = original_turn_timeout

    print("  -- and the THIRD manifestation: a multi-segment turn's mid-turn READY gap is not mistaken for settled (ue6rruxg's find #2b, live repro: 'Waddling... (8s, thinking)' caught moments after a premature True)")
    call_log.clear()
    # BUSY (real work segment 1) -> READY (a gap BETWEEN steps, e.g. after
    # a tool call finishes and before the model starts "thinking") ->
    # BUSY (segment 2) -> READY, READY (genuinely settled, confirmed
    # stable). Phase 1 (observed BUSY) is satisfied by the very first
    # read; the bug this reproduces is phase 2 trusting the FIRST READY
    # after that instead of requiring it to HOLD.
    scripted_states[:] = [PaneState.BUSY, PaneState.READY, PaneState.BUSY, PaneState.READY, PaneState.READY]
    tc._transport = FakeTransport()
    tc.classify_pane_ansi = fake_classify
    tc.CONTEXT_TURN_START_POLL_INTERVAL_S = 0.01
    tc.CONTEXT_SETTLE_POLL_INTERVAL_S = 0.01
    try:
        result = tc._wait_for_reply_settled("fake-tmux")
        check("waits through a mid-turn READY gap, does not settle on the first post-BUSY READY", result is True, detail=str(call_log))
        check("read the pane at least 5 times (observed BUSY, the blip, the second BUSY, and two confirming READYs)", len(call_log) >= 5, detail=str(call_log))
    finally:
        tc._transport, tc.classify_pane_ansi = original_transport, original_classify
        tc.CONTEXT_TURN_START_POLL_INTERVAL_S, tc.CONTEXT_SETTLE_POLL_INTERVAL_S = original_start_interval, original_settle_interval


def run() -> int:
    print("team_actions / team_context canary\n")

    print("-1. regression: parsing the real reply, not the echoed prompt (ue6rruxg's find #1)")
    _check_parse_response_regression()

    print("\n-1b. regression: waiting for a real BUSY->READY transition, not a pre-processing READY blip (ue6rruxg's find #2)")
    _check_wait_for_reply_settled_race()

    # No backup/restore needed -- JARVIS_TEST_RUN (set above, before any
    # of these modules were imported) already means TEAMS_REGISTRY_PATH
    # points at an isolated path under ~/Jarvis/test_runs/, never the
    # real ~/Jarvis/teams.json. Confirmed below, not just assumed.
    assert "test_runs" in str(TEAMS_REGISTRY_PATH), (
        f"TEAMS_REGISTRY_PATH is NOT test-isolated ({TEAMS_REGISTRY_PATH}) -- "
        "refusing to run against what may be the real registry"
    )
    tmux_created: list[str] = []

    if WORKDIR.exists():
        shutil.rmtree(WORKDIR)
    WORKDIR.mkdir(parents=True)
    (WORKDIR / "team-a").mkdir()
    (WORKDIR / "team-b").mkdir()
    (WORKDIR / "team-a" / "CLAUDE.md").write_text("# Canary Test Project A\n\nA throwaway scratch project used only by team_actions_canary.py.\n")

    try:

        print("0. setup: launch two real fresh teams")
        result_a = create_fresh_member(TMUX_A, str(WORKDIR / "team-a"), "haiku")
        check("team A session launched", result_a["ok"], detail=result_a.get("detail"))
        result_b = create_fresh_member(TMUX_B, str(WORKDIR / "team-b"), "haiku")
        check("team B session launched", result_b["ok"], detail=result_b.get("detail"))
        if not (result_a["ok"] and result_b["ok"]):
            return 1
        tmux_created.extend([TMUX_A, TMUX_B])

        from setup import create_team
        create_team(TEAM_A_ID, ["canary-a-alias"], result_a["root"], TMUX_A, [{"tmux": TMUX_A, "claude_session": result_a["claude_session"]}])
        create_team(TEAM_B_ID, ["canary-b-alias"], result_b["root"], TMUX_B, [{"tmux": TMUX_B, "claude_session": result_b["claude_session"]}])

        # --- SS1.2: uniqueness, at pick time -------------------------------
        print("\n1. uniqueness checks (pick-time)")
        root_check = ta.check_root_available(result_a["root"])
        check("duplicate root refused", not root_check["ok"], detail=str(root_check))
        check("conflicting_team names the right team", root_check.get("conflicting_team") == TEAM_A_ID, detail=str(root_check))

        id_check = ta.check_team_id_available(TEAM_A_ID)
        check("duplicate team_id refused", not id_check["ok"], detail=str(id_check))

        alias_check = ta.check_alias_available("canary-a-alias")
        check("duplicate alias refused (case-insensitive)", not alias_check["ok"], detail=str(alias_check))
        alias_check_case = ta.check_alias_available("CANARY-A-ALIAS")
        check("alias check is case-insensitive", not alias_check_case["ok"], detail=str(alias_check_case))

        fresh_check = ta.check_root_available("/tmp/genuinely-unused-path-xyz")
        check("a genuinely unused root is available", fresh_check["ok"], detail=str(fresh_check))

        # --- SS2: zero-member refusal --------------------------------------
        print("\n2. create_team() refuses zero members")
        try:
            create_team("shouldnotexist", [], "/tmp/nowhere", "", [])
            check("zero-member team was refused", False, detail="did not raise")
        except ValueError as e:
            check("zero-member team was refused", True, detail=str(e))

        # --- SS2: lead pointer ----------------------------------------------
        print("\n3. lead pointer: refused for a non-member, set correctly for a real one")
        cross_team = ta.set_lead(TEAM_A_ID, result_b["claude_session"])
        check("set_lead refuses a non-member (cross-team)", not cross_team["ok"], detail=str(cross_team))

        real_lead = ta.set_lead(TEAM_A_ID, result_a["claude_session"])
        check("set_lead succeeds for a real member", real_lead["ok"], detail=str(real_lead))
        teams_now, _ = discover_teams_and_unassigned()
        team_a = team_by_id(teams_now, TEAM_A_ID)
        check("team A now has_lead=True", team_a is not None and team_a.has_lead)
        check("team A's member shows is_lead=True", team_a is not None and team_a.members[0].is_lead)

        # --- SS3: context capture --------------------------------------------
        print("\n4. context capture: CLAUDE.md shortcut (team A has one)")
        ctx_a = tc.capture_context(TEAM_A_ID)
        check("CLAUDE.md capture succeeded", ctx_a["ok"], detail=str(ctx_a))
        teams_now, _ = discover_teams_and_unassigned()
        team_a = team_by_id(teams_now, TEAM_A_ID)
        check("team A context_source is claude_md", team_a is not None and team_a.context_source == "claude_md", detail=str(team_a))
        check(
            "team A context_summary contains the real CLAUDE.md text",
            team_a is not None and "Canary Test Project A" in (team_a.context_summary or ""),
            detail=str(team_a.context_summary if team_a else None),
        )
        check("CLAUDE.md-sourced context needs no special file (re-read live)", not tc._context_path(TEAM_A_ID).exists())

        print("\n5. context capture: agent turn (team B has no CLAUDE.md)")
        ctx_b = tc.capture_context(TEAM_B_ID)
        check("agent capture succeeded", ctx_b["ok"], detail=str(ctx_b))
        if ctx_b["ok"]:
            teams_now, _ = discover_teams_and_unassigned()
            team_b = team_by_id(teams_now, TEAM_B_ID)
            check("team B context_source is agent", team_b is not None and team_b.context_source == "agent", detail=str(team_b))
            check("team B has a non-empty summary", team_b is not None and bool(team_b.context_summary), detail=str(team_b))
            check("team B has subsystems", team_b is not None and len(team_b.context_subsystems) > 0, detail=str(team_b))
            body = json.loads(tc._context_path(TEAM_B_ID).read_text())
            check("the context FILE has team_id written inside it", body.get("team_id") == TEAM_B_ID, detail=str(body))

        # --- SS1.3: relocate --------------------------------------------------
        print("\n6. relocate: kill team B's session, resume it at a NEW path, relocate")
        subprocess.run(["tmux", "kill-session", "-t", TMUX_B], capture_output=True)
        time.sleep(1.0)
        teams_now, _ = discover_teams_and_unassigned()
        team_b = team_by_id(teams_now, TEAM_B_ID)
        check("team B reads not-running after kill", team_b is not None and team_b.members[0].liveness != "running", detail=str(team_b))

        new_root = WORKDIR / "team-b-relocated"
        new_root.mkdir()
        subprocess.run(["tmux", "new-session", "-d", "-s", TMUX_B, "-c", str(new_root)], check=True, capture_output=True)
        subprocess.run(["tmux", "send-keys", "-t", TMUX_B, "-l", "--", f"claude --resume {result_b['claude_session']}"], check=True, capture_output=True)
        subprocess.run(["tmux", "send-keys", "-t", TMUX_B, "Enter"], check=True, capture_output=True)
        came_up = wait_for_ready(TMUX_B)
        check("resumed session at the new path reached READY", came_up)

        if came_up:
            relocate_result = ta.relocate_team_member(TEAM_B_ID, TMUX_B)
            check("relocate_team_member() succeeded", relocate_result["ok"], detail=str(relocate_result))
            teams_now, _ = discover_teams_and_unassigned()
            team_b = team_by_id(teams_now, TEAM_B_ID)
            check(
                "team B reads RUNNING again after relocate",
                team_b is not None and team_b.members[0].liveness == "running",
                detail=str(team_b),
            )
            check(
                "team B's root updated to the resolved new path",
                team_b is not None and team_b.root == str(new_root.resolve()),
                detail=f"got {team_b.root if team_b else None!r}",
            )

        wrong_relocate = ta.relocate_team_member(TEAM_A_ID, "definitely-not-a-real-session")
        check("relocate refuses a nonexistent tmux name", not wrong_relocate["ok"], detail=str(wrong_relocate))

        # --- SS6: remove -- THE ONE THE LEAD CARES ABOUT MOST ----------------
        print("\n7. remove_team(): the registry entry disappears, the SESSION STAYS ALIVE")
        remove_member_lead = ta.remove_member(TEAM_A_ID, result_a["claude_session"])
        check("remove_member refuses the lead (must swap first)", not remove_member_lead["ok"], detail=str(remove_member_lead))

        remove_result = ta.remove_team(TEAM_A_ID)
        check("remove_team() reported success", remove_result["ok"], detail=str(remove_result))

        teams_now, unassigned_now = discover_teams_and_unassigned()
        check("team A no longer appears in the registry", team_by_id(teams_now, TEAM_A_ID) is None)

        # THE critical check: is the tmux session (the real process) STILL RUNNING.
        live_check = subprocess.run(["tmux", "has-session", "-t", TMUX_A], capture_output=True)
        check(
            "team A's tmux session is STILL ALIVE after remove_team() -- detaching never kills",
            live_check.returncode == 0,
            detail=f"tmux has-session exit code {live_check.returncode} -- if nonzero, remove_team() KILLED A LIVE SESSION",
        )
        check(
            "the now-orphaned session shows up as unassigned, not vanished",
            any(u.tmux == TMUX_A for u in unassigned_now),
        )
        check("removing the team also removed its context file", not tc._context_path(TEAM_A_ID).exists())

        wrong_remove = ta.remove_team("definitely-does-not-exist")
        check("remove_team refuses a nonexistent team", not wrong_remove["ok"], detail=str(wrong_remove))

    finally:
        for t in tmux_created:
            subprocess.run(["tmux", "kill-session", "-t", t], capture_output=True)
        shutil.rmtree(WORKDIR, ignore_errors=True)
        # The whole isolated test-run tree, not just teams.json -- this
        # also removes the isolated engine.json/context/ this run may
        # have touched, and needs no "did the real file exist before"
        # bookkeeping at all, because the real file was never touched.
        test_run_root = TEAMS_REGISTRY_PATH.parent
        if "test_runs" in str(test_run_root):
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
