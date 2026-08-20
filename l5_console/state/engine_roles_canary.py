#!/usr/bin/env python3
"""Canary for engine_roles.py (SPEC-engine-roles.md). Proves the four
points the spec's Verification section calls out specifically, plus the
per-role Start-precondition messages.

ISOLATED VIA JARVIS_TEST_RUN, NOT BACKUP/RESTORE for ENGINE_REGISTRY_PATH
(engine.json) -- see team_actions_canary.py's docstring for the real
incident (2026-08-18) that made backup/restore unacceptable for anything
touching ~/Jarvis: a promise that holds only if nothing else writes the
file in the same window and this process never crashes before its own
cleanup, versus a redirected path that holds regardless. ROLE_HOME
(~/Jarvis/concierge, ~/Jarvis/orchestrator) is DELIBERATELY NOT
isolated -- those are the real, already-configured launch directories
(their .mcp.json/.claude/settings.local.json are static, shared,
reused on purpose, never reconstructed), and engine_roles.py itself
keeps them pointed at the real paths regardless of JARVIS_TEST_RUN. Only
the small JSON registry that tracks WHICH session is attached needed
isolating -- that's what actually gets read/written repeatedly and is
what a crashed or racing canary could clobber.

Check 0 is the pure-builder guarantee the Lead asked for directly
(2026-08-20): _build_launch_cmd() and _build_resume_cmd() both delegate
to the SAME _role_cage_args(role), so "the resume command carries the
identical cage as the launch command" is assertable as a fast, hermetic
string comparison -- no live launch required to catch a cage that
drifted between the two call sites. Checks 1-2 are also fast/hermetic
(fake tmux names, no real process). Checks 3-4 are LIVE -- a real `claude --session-id <minted uuid> ...` launch at
the REAL, shared ~/Jarvis cwd (restructured 2026-08-20: both roles now
launch there directly, not into a per-role subdirectory -- reusing
~/Jarvis's own already-in-place CLAUDE.md/settings.local.json, never
writing new ones; only --mcp-config still points into the role's own
subdirectory, for its static .mcp.json), a real kill, and a real
--resume relaunch. Uses a uniquely-named throwaway tmux session and
cleans it up unconditionally -- never touches the real
jarvis-concierge/jarvis-orchestrator sessions already running on this
machine. Tests the CONCIERGE path specifically, per the spec's own
verification wording -- the ORCHESTRATOR path runs through the exact
same create_role_session()/activate_role() functions, parametrized by
role, so this is not a second, independent code path to re-prove live.

NOTE: because JARVIS_HOME is deliberately not test-isolated (same
reasoning as ROLE_HOME before it -- see JARVIS_HOME's own comment in
engine_roles.py), this canary's two live launches write real transcript
files into ~/Jarvis's own real project directory
(~/.claude/projects/-Users-aymanmohammed-Jarvis/), interleaved with
Ayman's real conversation history there. That's an accepted, unavoidable
consequence of both roles now sharing that one real cwd -- not a
regression this canary introduces.

Run after touching engine_roles.py or setup.py's create_fresh_member()
extension (SLOW -- two real Claude Code launches):

    python3 l5_console/state/engine_roles_canary.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

os.environ["JARVIS_TEST_RUN"] = f"engine-roles-canary-{uuid.uuid4().hex[:8]}"

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import engine_roles as er  # noqa: E402
import format_helpers as fh  # noqa: E402
from jarvis_paths import jarvis_home  # noqa: E402
from models import RoleSlot, LIVENESS_RUNNING, LIVENESS_STOPPED, LIVENESS_LOST  # noqa: E402
from reconnect import wait_for_ready  # noqa: E402
from teams import CLAUDE_PROJECTS_DIR, encode_project_path  # noqa: E402

assert "test_runs" in str(jarvis_home()), (
    f"jarvis_home() is NOT test-isolated ({jarvis_home()}) -- refusing to "
    "run against what may be the real latency_log.jsonl"
)

assert "test_runs" in str(er.ENGINE_REGISTRY_PATH), (
    f"ENGINE_REGISTRY_PATH is NOT test-isolated ({er.ENGINE_REGISTRY_PATH}) -- "
    "refusing to run against what may be the real registry"
)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((name, passed, detail))
    print(f"  {'ok  ' if passed else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not passed else ""))


def _live_command_line(tmux_name: str) -> str:
    """The REAL, currently-running `claude` process's own command line
    inside this tmux pane -- via ps, not by re-deriving what we THINK we
    launched. This is the whole point of this check: "we passed the
    right argument" and "the process is running with it" are different
    claims."""
    result = subprocess.run(
        ["tmux", "list-panes", "-t", tmux_name, "-F", "#{pane_pid}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return ""
    shell_pid = result.stdout.strip().splitlines()[0]
    # The `claude` process is a CHILD of the shell tmux launched -- find it.
    children = subprocess.run(["pgrep", "-P", shell_pid], capture_output=True, text=True)
    for pid in children.stdout.split():
        cmd = subprocess.run(["ps", "-o", "command=", "-p", pid], capture_output=True, text=True)
        if cmd.returncode == 0 and "claude" in cmd.stdout:
            return cmd.stdout
    # Fallback: some shells exec() into claude directly, no separate child.
    cmd = subprocess.run(["ps", "-o", "command=", "-p", shell_pid], capture_output=True, text=True)
    return cmd.stdout if cmd.returncode == 0 else ""


def run() -> int:
    print("engine_roles canary\n")
    live_tmux_created: list[str] = []

    try:
        # --- 0. The pure builders share the cage by construction --------
        print("0. _build_launch_cmd()/_build_resume_cmd() share the IDENTICAL cage, by construction")
        concierge_cage = " ".join(er._role_cage_args("concierge"))
        orchestrator_cage = " ".join(er._role_cage_args("orchestrator"))
        check(
            "concierge cage: don't-ask, with deletion still denied (Ayman's 2026-08-20 decision)",
            "--dangerously-skip-permissions" in concierge_cage
            and "Bash(rm:*)" in concierge_cage,
            detail=concierge_cage,
        )
        check(
            "...and the deny patterns are SHELL-QUOTED -- unquoted, Bash(rm:*) is a syntax "
            "error and claude never starts",
            "'Bash(rm:*)'" in concierge_cage,
            detail=concierge_cage,
        )
        check(
            "both roles now carry the IDENTICAL cage -- one _role_cage_args(), no per-role drift",
            orchestrator_cage == concierge_cage,
            detail=f"{orchestrator_cage!r} vs {concierge_cage!r}",
        )
        check(
            "neither cage ever mentions the removed --dangerously-load-development-channels flag",
            "dangerously-load-development-channels" not in concierge_cage and "dangerously-load-development-channels" not in orchestrator_cage,
        )

        sample_launch = er._build_launch_cmd("concierge", "sample-uuid-launch", "Sample Name", "haiku", "low")
        sample_resume = er._build_resume_cmd("concierge", {"claude_session": "sample-uuid-launch", "model": "haiku", "effort": "low", "name": "Sample Name"})
        check(
            "PURE, no live launch: concierge's launch command ends in the exact same cage string as its resume command",
            sample_launch.endswith(concierge_cage) and sample_resume.endswith(concierge_cage),
            detail=f"launch={sample_launch!r}\nresume={sample_resume!r}",
        )

        sample_launch_o = er._build_launch_cmd("orchestrator", "sample-uuid-o", "Sample O", "sonnet", "high")
        sample_resume_o = er._build_resume_cmd("orchestrator", {"claude_session": "sample-uuid-o", "model": "sonnet", "effort": "high", "name": "Sample O"})
        check(
            "PURE, no live launch: orchestrator's launch command ends in the exact same cage string as its resume command",
            sample_launch_o.endswith(orchestrator_cage) and sample_resume_o.endswith(orchestrator_cage),
            detail=f"launch={sample_launch_o!r}\nresume={sample_resume_o!r}",
        )
        check(
            "PURE: _build_resume_cmd() never emits -n/--session-id -- only _build_launch_cmd() establishes a fresh identity",
            "-n " not in sample_resume and "--session-id" not in sample_resume,
            detail=sample_resume,
        )

        # Start from a known-empty state for this run.
        er._save({"concierge": None, "orchestrator": None, "name_history": {"concierge": [], "orchestrator": []}})

        # --- 1. Flag survives "console restart" AND session death -------
        print("1. flag survives console restart AND session death")
        attach = er.attach_role(
            "concierge", tmux="fake-tmux-not-real", working_dir="/tmp/fake-concierge-canary",
            claude_session="fake-uuid-1111", model="haiku", effort="low",
        )
        check("attach succeeded", attach["ok"], detail=attach.get("detail"))

        # "console restart": prove this is genuinely disk-backed, not an
        # in-memory cache, by reading it from a FRESH subprocess.
        proc = subprocess.run(
            [sys.executable, "-c", (
                "import sys; sys.path.insert(0, '.'); import engine_roles as er; "
                "r = er.get_role_record('concierge'); "
                "print(r['tmux'] if r else 'NONE')"
            )],
            cwd=str(Path(__file__).parent), capture_output=True, text=True,
        )
        check(
            "a fresh process (simulated console restart) reads the same attached record from disk",
            proc.stdout.strip() == "fake-tmux-not-real",
            detail=f"got {proc.stdout.strip()!r}, stderr={proc.stderr.strip()!r}",
        )

        # "session death": the tmux session in the record was never real,
        # so this doubles as proving liveness for a genuinely-dead session
        # -- attached stays True, liveness reads STOPPED/LOST, never
        # silently reverts to unattached.
        liveness = er.role_liveness("concierge")
        check(
            "attached-but-dead still reads attached=True",
            liveness["attached"] is True,
            detail=f"got {liveness}",
        )
        check(
            "a dead session's liveness is STOPPED or LOST, never RUNNING",
            liveness["liveness"] in ("stopped", "lost"),
            detail=f"got liveness={liveness['liveness']!r}",
        )

        # --- 2. Attaching an already-attached session is refused ----------
        print("\n2. attaching an already-attached session is refused, correct role named")
        conflict = er.attach_role(
            "orchestrator", tmux="fake-tmux-not-real", working_dir="/tmp/fake-concierge-canary",
            claude_session="fake-uuid-1111",
        )
        check("refused (ok=False)", conflict["ok"] is False)
        check(
            "conflicting_role names the CORRECT role (concierge, not orchestrator)",
            conflict.get("conflicting_role") == "concierge",
            detail=f"got {conflict}",
        )
        check(
            "detail names the role in plain language",
            "Concierge" in conflict.get("detail", ""),
            detail=conflict.get("detail", ""),
        )

        # --- start_precondition() messages, per missing-role case --------
        print("\n2b. start_precondition() messages -- one per missing-role case")
        er._save({"concierge": None, "orchestrator": None, "name_history": {"concierge": [], "orchestrator": []}})
        pre = er.start_precondition()
        check(
            "no concierge attached -> names Concierge and says 'Attach one'",
            not pre["ok"] and "Concierge" in pre["detail"] and "Attach" in pre["detail"],
            detail=pre["detail"],
        )
        er.attach_role(
            "concierge", tmux="fake-tmux-not-real", working_dir="/tmp/fake-concierge-canary",
            claude_session="fake-uuid-1111",
        )
        pre2 = er.start_precondition()
        check(
            "concierge attached-but-dead -> names Concierge and says 'Activate'",
            not pre2["ok"] and "Concierge" in pre2["detail"] and "Activate" in pre2["detail"],
            detail=pre2["detail"],
        )
        er._save({"concierge": None, "orchestrator": None, "name_history": {"concierge": [], "orchestrator": []}})
        er.attach_role(
            "orchestrator", tmux="fake-tmux-not-real-2", working_dir="/tmp/fake-orch-canary",
            claude_session="fake-uuid-2222",
        )
        pre3 = er.start_precondition()
        check(
            "concierge missing entirely takes priority, still names Concierge specifically",
            not pre3["ok"] and "Concierge" in pre3["detail"],
            detail=pre3["detail"],
        )

        # --- 2c. cwd_mismatch: a live session under this name is never
        #         silently indistinguishable from "nothing is running" --
        print("\n2c. role_liveness() surfaces cwd_mismatch/found_cwd instead of a silent not-running (2026-08-20 gap)")
        er._save({"concierge": None, "orchestrator": None, "name_history": {"concierge": [], "orchestrator": []}})
        er.attach_role(
            "concierge", tmux="fake-tmux-cwd-check", working_dir="/old/subdir/expected",
            claude_session="fake-uuid-cwd-check", model="haiku", effort="low",
        )
        _real_list_live_sessions = er._list_live_sessions
        try:
            # Direction 1: tmux name matches, cwd does NOT -- the
            # attach/activate CONTRACT must be unchanged (never RUNNING),
            # but the mismatch itself must be a visible fact, not a
            # silent fall-through to plain not-running.
            er._list_live_sessions = lambda: [{"session_id": "fake-tmux-cwd-check", "working_dir": "/actually/here"}]
            mismatched = er.role_liveness("concierge")
            check(
                "cwd mismatch: liveness contract is unchanged -- never RUNNING",
                mismatched["running"] is False and mismatched["liveness"] in ("stopped", "lost"),
                detail=str(mismatched),
            )
            check(
                "cwd mismatch: cwd_mismatch=True AND found_cwd names the session's REAL live cwd",
                mismatched["cwd_mismatch"] is True and mismatched["found_cwd"] == "/actually/here",
                detail=str(mismatched),
            )

            # Direction 2: tmux name matches AND cwd matches -- RUNNING,
            # and cwd_mismatch must read back False here too (not a
            # stale True bleeding over from the branch above).
            er._list_live_sessions = lambda: [{"session_id": "fake-tmux-cwd-check", "working_dir": "/old/subdir/expected"}]
            matched = er.role_liveness("concierge")
            check(
                "matching cwd: reports RUNNING",
                matched["running"] is True and matched["liveness"] == "running",
                detail=str(matched),
            )
            check(
                "matching cwd: cwd_mismatch=False and found_cwd=None -- no false-positive mismatch flag",
                matched["cwd_mismatch"] is False and matched["found_cwd"] is None,
                detail=str(matched),
            )
        finally:
            er._list_live_sessions = _real_list_live_sessions

        # --- 2d. The mismatch reaches the SURFACE, not just the dict ----
        # The Lead's finding, 2026-08-20: role_liveness() computing
        # cwd_mismatch correctly is not the fix by itself -- the console
        # is a full-screen Textual app whose alt-screen swallows stderr,
        # and Stream reads latency_log.jsonl, not stderr, so a
        # stderr-only signal never reaches Ayman at all. Two surfaces
        # checked here: (a) the render -- a mismatched RoleSlot must
        # produce a VISIBLY DIFFERENT phrase than a genuinely-stopped
        # one, asserted directly against the pure
        # format_helpers.role_status_phrase() (no Textual app mounted);
        # (b) the log -- _log_cwd_mismatch() actually wrote a real
        # engine_role_cwd_mismatch event to the (test-isolated)
        # latency_log.jsonl during 2c above, which is what makes Stream
        # show it live.
        print("\n2d. the mismatch reaches the surface: a distinct render (not just a dict), and a real latency_log event")
        running_slot = RoleSlot(
            attached=True, name="X", model="haiku", effort="low", tmux="t", working_dir="/x",
            claude_session="u", liveness=LIVENESS_RUNNING, tools_reachable=True,
        )
        mismatched_slot = RoleSlot(
            attached=True, name="X", model="haiku", effort="low", tmux="t", working_dir="/old",
            claude_session="u", liveness=LIVENESS_STOPPED, tools_reachable=False,
            cwd_mismatch=True, found_cwd="/new",
        )
        stopped_slot = RoleSlot(
            attached=True, name="X", model="haiku", effort="low", tmux="t", working_dir="/old",
            claude_session="u", liveness=LIVENESS_STOPPED, tools_reachable=False,
        )
        check(
            "render: RUNNING phrase differs from both stopped and mismatched",
            fh.role_status_phrase(running_slot) not in (fh.role_status_phrase(stopped_slot), fh.role_status_phrase(mismatched_slot)),
            detail=f"running={fh.role_status_phrase(running_slot)!r}",
        )
        check(
            "render: a cwd-mismatched slot's phrase is VISIBLY DIFFERENT from a genuinely-stopped slot's -- never the same 'inactive'",
            fh.role_status_phrase(mismatched_slot) != fh.role_status_phrase(stopped_slot),
            detail=f"mismatched={fh.role_status_phrase(mismatched_slot)!r} stopped={fh.role_status_phrase(stopped_slot)!r}",
        )
        check(
            "render: the mismatched phrase never leaks the raw found_cwd path (plain language only, per the Lead's ruling)",
            "/new" not in fh.role_status_phrase(mismatched_slot),
            detail=fh.role_status_phrase(mismatched_slot),
        )

        # The ICON, not just the phrase. Added 2026-08-20 after the Lead
        # looked at the actual rendered row and found it contradicting
        # itself: the mismatched row read "✕ found running elsewhere",
        # and ✕ is the panel legend's "lost -- no saved history". One row
        # claiming a session is running somewhere AND has no history.
        #
        # Every phrase check above passed while that was true, because
        # the phrases were correct -- it was the glyph beside them that
        # lied. That is the whole lesson: asserting on the half you
        # thought about leaves the other half free to say anything.
        lost_slot = RoleSlot(
            attached=True, name="X", model="haiku", effort="low", tmux="t", working_dir="/old",
            claude_session="u", liveness=LIVENESS_LOST, tools_reachable=False,
        )
        check(
            "render: the mismatched slot's ICON is not the genuinely-lost icon -- the row must not claim 'no saved history' about a session it just found alive",
            fh.role_status_icon(mismatched_slot) != fh.role_status_icon(lost_slot),
            detail=f"mismatched={fh.role_status_icon(mismatched_slot)!r} lost={fh.role_status_icon(lost_slot)!r}",
        )
        check(
            "render: a genuinely-lost slot still gets the ordinary liveness icon -- the fix must not change what LOST looks like",
            fh.role_status_icon(lost_slot) == fh.liveness_icon(LIVENESS_LOST),
            detail=f"{fh.role_status_icon(lost_slot)!r} vs {fh.liveness_icon(LIVENESS_LOST)!r}",
        )
        check(
            "render: icon and phrase agree -- a slot whose phrase says 'elsewhere' never carries an icon meaning 'no history'",
            ("elsewhere" in fh.role_status_phrase(mismatched_slot)) and fh.role_status_icon(mismatched_slot) != "\u2715",
            detail=f"{fh.role_status_icon(mismatched_slot)!r} {fh.role_status_phrase(mismatched_slot)!r}",
        )

        latency_log_path = jarvis_home() / "latency_log.jsonl"
        logged_mismatch = False
        if latency_log_path.exists():
            for line in latency_log_path.read_text().splitlines():
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    entry.get("event") == "engine_role_cwd_mismatch"
                    and entry.get("role") == "concierge"
                    and entry.get("tmux") == "fake-tmux-cwd-check"
                    and entry.get("found_cwd") == "/actually/here"
                ):
                    logged_mismatch = True
                    break
        check(
            "log: _log_cwd_mismatch() wrote a real engine_role_cwd_mismatch event to latency_log.jsonl (what Stream actually reads) -- not stderr-only",
            logged_mismatch,
            detail=f"checked {latency_log_path}",
        )

        # --- 2e. activate_role() gives the RIGHT next step on a mismatch,
        #         never a doomed relaunch or the actively-wrong "must be
        #         recreated" -----------------------------------------
        # Found while verifying the icon fix: role_status_icon()'s own
        # docstring argues liveness correctly reads LOST/STOPPED here
        # (the stale record genuinely can't be resumed as-is) -- but
        # activate_role()'s LOST branch was still saying "must be
        # recreated, not activated," which is FALSE for a cwd-mismatched
        # role and actively dangerous: pressing Create spins up a
        # brand-new tmux name/uuid and overwrites the attached record,
        # orphaning the still-live session under its old name with
        # nothing tracking it any more. The correct next step is
        # re-attach (resolves the real uuid+cwd via a live /status call),
        # not recreate -- so activate_role() now says that instead, and
        # says it BEFORE attempting a tmux relaunch that -- for a
        # cwd-mismatched STOPPED case -- would be doomed anyway (`tmux
        # new-session -s <name>` fails outright when a session by that
        # name is already alive, which it is here by definition).
        print("\n2e. activate_role() on a cwd-mismatched role says 're-attach', never 'must be recreated', and never touches real tmux")
        er._save({"concierge": None, "orchestrator": None, "name_history": {"concierge": [], "orchestrator": []}})
        er.attach_role(
            "concierge", tmux="fake-tmux-activate-check", working_dir="/old/subdir/expected",
            claude_session="fake-uuid-activate-check", model="haiku", effort="low",
        )
        _real_list_live_sessions_2 = er._list_live_sessions
        try:
            er._list_live_sessions = lambda: [{"session_id": "fake-tmux-activate-check", "working_dir": "/actually/here"}]
            activate_result = er.activate_role("concierge")
            check(
                "activate_role() refuses (never silently succeeds) on a cwd-mismatched role",
                activate_result["ok"] is False,
                detail=str(activate_result),
            )
            check(
                "activate_role()'s message tells Ayman to re-attach, and never says 'must be recreated'",
                "re-attach" in activate_result["detail"].lower() and "must be recreated" not in activate_result["detail"],
                detail=activate_result["detail"],
            )
            no_real_session = subprocess.run(
                ["tmux", "has-session", "-t", "fake-tmux-activate-check"], capture_output=True,
            ).returncode != 0
            check(
                "activate_role() never even attempted a real tmux relaunch for the mismatched role -- the early return fired, not a doomed subprocess call",
                no_real_session,
            )
        finally:
            er._list_live_sessions = _real_list_live_sessions_2
            subprocess.run(["tmux", "kill-session", "-t", "fake-tmux-activate-check"], capture_output=True)

        # --- 3 & 4. LIVE: create, verify the ACTUAL spawned command line,
        #            kill, activate (--resume), verify again -------------
        print("\n3&4. LIVE: create_role_session() spawns a real concierge; verify the actual process argv")
        er._save({"concierge": None, "orchestrator": None, "name_history": {"concierge": [], "orchestrator": []}})

        # Registry was just reset above, so create_role_session()'s own
        # auto-increment naming will produce exactly this tmux name --
        # registered for cleanup BEFORE the call, not after: a failed
        # create can still leave a real tmux session up (stuck at a
        # prompt, etc.) even when it reports ok=False.
        live_tmux_created.append("claude-concierge-1")
        result = er.create_role_session("concierge", model="haiku", effort="low", name="Canary Concierge")
        check("create_role_session() succeeded", result["ok"], detail=result.get("detail"))

        if result["ok"]:
            record = result["record"]
            time.sleep(1.0)  # let the process fully settle into its argv
            cmdline = _live_command_line(record["tmux"])
            check("could read the live process's real command line", bool(cmdline), detail=f"tmux={record['tmux']}")
            check("actual argv includes --strict-mcp-config", "--strict-mcp-config" in cmdline, detail=cmdline)
            check("actual argv includes --model haiku", "--model haiku" in cmdline, detail=cmdline)
            check("actual argv includes --effort low", "--effort low" in cmdline, detail=cmdline)
            check(
                "actual argv includes --session-id with a real uuid4 (we minted it, never discovered it after launch)",
                f"--session-id {record['claude_session']}" in cmdline,
                detail=cmdline,
            )
            try:
                uuid.UUID(record["claude_session"])
                minted_is_valid_uuid = True
            except ValueError:
                minted_is_valid_uuid = False
            check("the minted claude_session is a well-formed uuid4", minted_is_valid_uuid, detail=record["claude_session"])
            check("actual argv carries don't-ask (Ayman's 2026-08-20 decision)", "--dangerously-skip-permissions" in cmdline, detail=cmdline)
            check("actual argv still carries a --disallowedTools list at all", "--disallowedTools" in cmdline, detail=cmdline)
            check(
            "actual argv DENIES DELETION, shell-quoted so the shell passes it through intact",
            "Bash(rm:*)" in cmdline,
            detail=cmdline,
        )
            check("actual argv NEVER includes the removed --dangerously-load-development-channels flag", "dangerously-load-development-channels" not in cmdline, detail=cmdline)

            # "server_readonly.py" itself never appears in the `claude`
            # process's OWN argv -- that's the DOWNSTREAM MCP server's
            # launch command, spawned per --mcp-config's file CONTENT,
            # not part of this process's command line. Two INDEPENDENT
            # proofs instead: (a) the config file the argv points at
            # really does say server_readonly.py, and (b) the actual
            # downstream process is really running and reachable
            # (role_liveness()'s tools_reachable, checked below) -- (a)
            # alone would only prove intent; (b) alone wouldn't prove
            # THIS session is the one connected to it. Together they're
            # the real claim.
            m = re.search(r"--mcp-config (\S+)", cmdline)
            check("argv's --mcp-config path was found", m is not None, detail=cmdline)
            if m:
                config_content = Path(m.group(1)).read_text()
                check(
                    "the --mcp-config FILE's content really does point at server_readonly.py",
                    "server_readonly.py" in config_content,
                    detail=config_content,
                )

            live_cwd = subprocess.run(
                ["tmux", "display-message", "-p", "-t", record["tmux"], "#{pane_current_path}"],
                capture_output=True, text=True,
            ).stdout.strip()
            check(
                "the session's real cwd is the shared ~/Jarvis itself, NOT the concierge role subdirectory (restructured 2026-08-20)",
                live_cwd == er._JARVIS_HOME_RESOLVED,
                detail=f"got {live_cwd!r}, expected {er._JARVIS_HOME_RESOLVED!r}",
            )

            liveness = er.role_liveness("concierge")
            check("role_liveness() reads RUNNING for the real session", liveness["liveness"] == "running", detail=str(liveness))
            check(
                "tools_reachable is True -- the ACTUAL downstream server_readonly.py process is confirmed running",
                liveness["tools_reachable"] is True,
            )

            # A brand-new session with zero exchanges has no jsonl
            # transcript yet (Claude Code only flushes one once real
            # conversational content happens -- same finding
            # create_fresh_member()'s own docstring already documents).
            # --resume needs that transcript to exist, so force one real
            # exchange before killing -- otherwise this reads as
            # LIVENESS_LOST (nothing to resume from), which is correct
            # behavior for that case but not what this check needs to
            # exercise.
            subprocess.run(["tmux", "send-keys", "-t", record["tmux"], "-l", "--", "say hi"], capture_output=True)
            subprocess.run(["tmux", "send-keys", "-t", record["tmux"], "Enter"], capture_output=True)
            wait_for_ready(record["tmux"])

            # The core claim this whole redesign rests on, checked directly
            # against disk rather than trusted from the Lead's write-up:
            # `claude --session-id <uuid> ...` really does write the
            # transcript to exactly `<uuid>.jsonl`, at the SHARED JARVIS_HOME
            # project directory (not a per-role one).
            transcript_path = CLAUDE_PROJECTS_DIR / encode_project_path(er._JARVIS_HOME_RESOLVED) / f"{record['claude_session']}.jsonl"
            check(
                "the minted uuid's transcript file exists at exactly <uuid>.jsonl under the shared JARVIS_HOME project dir",
                transcript_path.exists(),
                detail=str(transcript_path),
            )

            # --- kill it, then Activate should bring back the SAME session --
            print("\n4b. kill the real session, then Activate revives it with the same model+effort")
            subprocess.run(["tmux", "kill-session", "-t", record["tmux"]], capture_output=True)
            time.sleep(1.0)
            dead_liveness = er.role_liveness("concierge")
            check("after killing, liveness is no longer RUNNING", dead_liveness["liveness"] != "running", detail=str(dead_liveness))
            check(
                "after a real exchange, liveness is STOPPED (resumable), not LOST",
                dead_liveness["liveness"] == "stopped",
                detail=str(dead_liveness),
            )

            activate_result = er.activate_role("concierge")
            check("activate_role() reported success", activate_result["ok"], detail=activate_result.get("detail"))

            if activate_result["ok"]:
                time.sleep(1.0)
                revived_cmdline = _live_command_line(record["tmux"])
                check("revived process actual argv includes --resume with the SAME claude_session", f"--resume {record['claude_session']}" in revived_cmdline, detail=revived_cmdline)
                check("revived process actual argv NEVER includes --session-id (it's a revival, not a fresh identity)", "--session-id" not in revived_cmdline, detail=revived_cmdline)
                check("revived process actual argv still has --model haiku", "--model haiku" in revived_cmdline, detail=revived_cmdline)
                check("revived process actual argv still has --effort low (SAME effort)", "--effort low" in revived_cmdline, detail=revived_cmdline)
                check("revived process actual argv still has --strict-mcp-config", "--strict-mcp-config" in revived_cmdline, detail=revived_cmdline)
                # THE failure this design has to rule out (the Lead's own
                # framing): "a revived concierge coming back uncaged."
                # Checked directly against the real revived process's argv,
                # not assumed from the code reading the same as create's.
                check("revived process argv carries don't-ask, same as a fresh launch", "--dangerously-skip-permissions" in revived_cmdline, detail=revived_cmdline)
                check(
                "revived process argv still DENIES DELETION -- the 'revived uncaged' gap, "
                "now measured on the one thing still denied under don't-ask",
                "Bash(rm:*)" in revived_cmdline,
                detail=revived_cmdline,
            )
                check(
                "revived process argv carries the SAME cage as a fresh launch -- byte-identical, "
                "so create and revive cannot drift",
                er._role_cage_args("concierge")[0] in revived_cmdline
                and all(p.strip("'") in revived_cmdline for p in er._DELETION_DENIED),
                detail=revived_cmdline,
            )
                check("revived process actual argv NEVER includes the removed --dangerously-load-development-channels flag", "dangerously-load-development-channels" not in revived_cmdline, detail=revived_cmdline)
                revived_liveness = er.role_liveness("concierge")
                check(
                    "revived session's tools_reachable is True -- the real server_readonly.py process came back too",
                    revived_liveness["tools_reachable"] is True,
                    detail=str(revived_liveness),
                )

    finally:
        for tmux_name in live_tmux_created:
            subprocess.run(["tmux", "kill-session", "-t", tmux_name], capture_output=True)
        # The isolated test-run tree, not the real file -- nothing here
        # ever touched ~/Jarvis/engine.json.
        import shutil
        test_run_root = er.ENGINE_REGISTRY_PATH.parent
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
