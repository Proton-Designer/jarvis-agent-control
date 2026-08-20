#!/usr/bin/env python3
"""Canary for conflict refusal happening BEFORE side effects
(SPEC-teams.md SS1.2).

Found 2026-08-20 in Engineer 2's L5 audit. team_actions has had
check_root_available / check_team_id_available / check_alias_available
since SS1.2 was written -- all three correct, and setup_flow.py never
imported any of them. They were dead code.

The only enforcement was create_team()'s ValueError, thrown at the LAST
step of the wizard. On the fresh path that is not merely untidy:
_launch_fresh_members() has already spawned real tmux sessions and real
Claude processes by the time the conflict surfaces, so a rejected team
leaves them running, orphaned, registered to nothing. Side effects
before validation.

This file asserts the CHECKS themselves in both directions, and asserts
the wiring -- that setup_flow actually calls them -- because the bug was
never that the checks were wrong. They were right and unreachable, which
no test of the checks alone would ever have caught.

Isolated via JARVIS_TEST_RUN; never touches the real registry.

    l5_console/app/.venv/bin/python3 l5_console/state/setup_conflict_canary.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("JARVIS_TEST_RUN", "setup-conflict-canary")
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

import team_actions  # noqa: E402
from teams import load_registry, save_registry, TEAMS_REGISTRY_PATH  # noqa: E402

FAILURES: list[str] = []


def check(desc: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ok    {desc}")
    else:
        FAILURES.append(desc)
        print(f"  FAIL  {desc}{('  -- ' + detail) if detail else ''}")


def run() -> int:
    assert "test_runs" in str(TEAMS_REGISTRY_PATH), f"NOT ISOLATED: {TEAMS_REGISTRY_PATH}"
    TEAMS_REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    save_registry([])

    taken_root = str(Path("/private/tmp/jarvis-conflict-canary-root").resolve())
    save_registry([{
        "id": "already", "aliases": ["already", "The Already"],
        "root": taken_root, "lead": "u1",
        "members": [{"tmux": "t1", "claude_session": "u1", "model": "haiku"}],
    }])

    print("root -- both directions")
    bad = team_actions.check_root_available(taken_root)
    good = team_actions.check_root_available("/private/tmp/jarvis-conflict-canary-free")
    check("a registered directory is refused", bad["ok"] is False)
    check("...and the refusal NAMES the conflicting team, not just 'no'",
          bad.get("conflicting_team") == "already" and "already" in bad["detail"],
          bad["detail"])
    check("an unregistered directory is allowed", good["ok"] is True, good["detail"])

    print()
    print("team id -- both directions")
    check("a taken id is refused", team_actions.check_team_id_available("already")["ok"] is False)
    check("a free id is allowed", team_actions.check_team_id_available("brandnew")["ok"] is True)

    print()
    print("alias -- both directions, and case-insensitively")
    check("an exact alias is refused", team_actions.check_alias_available("already")["ok"] is False)
    check("a DIFFERENTLY-CASED alias is still refused -- spoken aliases are not case-sensitive",
          team_actions.check_alias_available("THE ALREADY")["ok"] is False)
    check("a free alias is allowed", team_actions.check_alias_available("gateway")["ok"] is True)

    print()
    print("wiring -- the bug was unreachable checks, not wrong ones")
    flow_src = (Path(__file__).parent.parent / "app" / "setup_flow.py").read_text()
    check("setup_flow imports team_actions at all (it did not, and that WAS the bug)",
          "import team_actions" in flow_src)
    check("the root check is called on the FRESH path, before count/model/launch",
          "check_root_available" in flow_src.split("_on_fresh_directory_chosen")[1][:600]
          if "_on_fresh_directory_chosen" in flow_src else False)
    check("the root check is called on the ADOPT path too",
          "check_root_available" in flow_src.split("_on_adopt_group_chosen")[1][:600]
          if "_on_adopt_group_chosen" in flow_src else False)
    check("id and alias are checked before create_team() is reached",
          flow_src.index("check_team_id_available") < flow_src.index("setup_state.create_team")
          and flow_src.index("check_alias_available") < flow_src.index("setup_state.create_team"))

    save_registry([])
    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("all 12 checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(run())
