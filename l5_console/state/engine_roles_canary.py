#!/usr/bin/env python3
"""Canary for engine_roles.py (SPEC-engine-roles.md). Proves the four
points the spec's Verification section calls out specifically, plus the
per-role Start-precondition messages.

Follows team_registry_tools_canary.py's established pattern for safely
touching the REAL ~/Jarvis/engine.json (no JARVIS_TEST_RUN-style
isolation exists for it): back up whatever's there, run against it,
restore in a finally block no matter what happens.

Checks 1-2 are fast/hermetic (fake tmux names, no real process). Checks
3-4 are LIVE -- a real `claude --model haiku` launch into the REAL,
already-configured ~/Jarvis/concierge directory (reusing its existing
.mcp.json/.claude/settings.local.json, never writing new ones), a real
kill, and a real --resume relaunch. Uses a uniquely-named throwaway tmux
session and cleans it up unconditionally -- never touches the real
jarvis-concierge/jarvis-orchestrator sessions already running on this
machine. Tests the CONCIERGE path specifically, per the spec's own
verification wording -- the ORCHESTRATOR path runs through the exact
same create_role_session()/activate_role() functions, parametrized by
role, so this is not a second, independent code path to re-prove live.

Run after touching engine_roles.py or setup.py's create_fresh_member()
extension (SLOW -- two real Claude Code launches):

    python3 l5_console/state/engine_roles_canary.py
"""
from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import engine_roles as er  # noqa: E402
from reconnect import wait_for_ready  # noqa: E402

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
    original_raw = er.ENGINE_REGISTRY_PATH.read_text() if er.ENGINE_REGISTRY_PATH.exists() else None
    original_existed = er.ENGINE_REGISTRY_PATH.exists()
    live_tmux_created: list[str] = []

    try:
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
                "the session's real cwd is the concierge role subdirectory, not ~/Jarvis itself",
                live_cwd == str(er.ROLE_HOME["concierge"]),
                detail=f"got {live_cwd!r}",
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
                check("revived process actual argv still has --model haiku", "--model haiku" in revived_cmdline, detail=revived_cmdline)
                check("revived process actual argv still has --effort low (SAME effort)", "--effort low" in revived_cmdline, detail=revived_cmdline)
                check("revived process actual argv still has --strict-mcp-config", "--strict-mcp-config" in revived_cmdline, detail=revived_cmdline)
                revived_liveness = er.role_liveness("concierge")
                check(
                    "revived session's tools_reachable is True -- the real server_readonly.py process came back too",
                    revived_liveness["tools_reachable"] is True,
                    detail=str(revived_liveness),
                )

    finally:
        for tmux_name in live_tmux_created:
            subprocess.run(["tmux", "kill-session", "-t", tmux_name], capture_output=True)
        if original_existed:
            er.ENGINE_REGISTRY_PATH.write_text(original_raw)
        elif er.ENGINE_REGISTRY_PATH.exists():
            er.ENGINE_REGISTRY_PATH.unlink()

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
