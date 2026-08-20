#!/usr/bin/env python3
"""Canary for revive.py (SPEC-gaps-and-build-plan.md SS1.7). Proves the
two directions the Lead asked for directly: a clean revive reports every
item ok, and a revive where one item genuinely fails reports ok=False AND
names which item -- never rounds a partial failure up to success.

Reuses activate_role()/reconnect_team()'s own proven correctness
(engine_roles_canary.py, team_registry_tools_canary.py already cover
those exhaustively) -- this canary is scoped to revive.py's OWN logic:
target enumeration, skip-vs-attempted classification, and the
never-round-up-to-success aggregation in summarize(). It does not
re-prove that activate_role()/reconnect_team() themselves launch a
correctly-caged process; that's each function's own canary's job.

ISOLATED VIA JARVIS_TEST_RUN for BOTH engine.json and teams.json, same
discipline as engine_roles_canary.py/team_actions_canary.py -- see
either's docstring for the 2026-08-18 incident this rules out
structurally rather than by convention. Never touches ~/Jarvis's real
registries.

Two LIVE sections (real tmux + real `claude` launches, real kills, same
accepted tradeoff engine_roles_canary.py already documents: JARVIS_HOME
itself is deliberately NOT test-isolated, so these write real transcripts
into ~/Jarvis's real project directory):
  - a genuinely STOPPED role, revived for real (success path)
  - a genuinely STOPPED team member, revived for real (success path)
The FAILURE-naming path is proven WITHOUT a live launch: a role record
pointing at a claude_session with no transcript reads LIVENESS_LOST, and
activate_role() already refuses that immediately with no subprocess call
at all (see engine_roles.activate_role()'s own LOST branch) -- fast,
deterministic, and still the REAL activate_role(), not a mock.

Run after touching revive.py (SLOW -- two real Claude Code launches):

    python3 l5_console/state/revive_canary.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

os.environ["JARVIS_TEST_RUN"] = f"revive-canary-{uuid.uuid4().hex[:8]}"

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "l4_controller"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import engine_roles as er  # noqa: E402
import revive  # noqa: E402
import team_registry_tools as trt  # noqa: E402
from jarvis_paths import jarvis_home  # noqa: E402
from teams import TEAMS_REGISTRY_PATH, save_registry  # noqa: E402
from reconnect import wait_for_ready  # noqa: E402

assert "test_runs" in str(jarvis_home()), (
    f"jarvis_home() is NOT test-isolated ({jarvis_home()}) -- refusing to run"
)
assert "test_runs" in str(er.ENGINE_REGISTRY_PATH), (
    f"ENGINE_REGISTRY_PATH is NOT test-isolated ({er.ENGINE_REGISTRY_PATH}) -- "
    "refusing to run against what may be the real registry"
)
assert "test_runs" in str(TEAMS_REGISTRY_PATH), (
    f"TEAMS_REGISTRY_PATH is NOT test-isolated ({TEAMS_REGISTRY_PATH}) -- "
    "refusing to run against what may be the real registry"
)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((name, passed, detail))
    print(f"  {'ok  ' if passed else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not passed else ""))


TEAM_ID = "revivecanaryteam"
WORKDIR = Path("/tmp/jarvis-revive-canary")


def _force_one_exchange(tmux_name: str) -> None:
    """A brand-new session with zero exchanges has no jsonl transcript
    yet (Claude Code only flushes one once real conversational content
    happens -- same finding engine_roles_canary.py and
    setup.create_fresh_member()'s own docstrings already document).
    Without this, killing it reads as LIVENESS_LOST (nothing to resume),
    not STOPPED -- correct behavior, but not what the success-path checks
    below need to exercise."""
    subprocess.run(["tmux", "send-keys", "-t", tmux_name, "-l", "--", "say hi"], capture_output=True)
    subprocess.run(["tmux", "send-keys", "-t", tmux_name, "Enter"], capture_output=True)
    wait_for_ready(tmux_name)


def main() -> int:
    save_registry([])
    shutil.rmtree(WORKDIR, ignore_errors=True)
    WORKDIR.mkdir(parents=True, exist_ok=True)

    try:
        # --- 1. Clean slate: nothing attached, no teams -- everything skipped, still ok ---
        print("1. clean slate -- both roles unattached, no teams registered")
        targets = revive.list_revive_targets()
        check(
            "list_revive_targets() returns exactly the 2 roles when no teams are registered",
            len(targets) == 2 and {t.id for t in targets} == {"concierge", "orchestrator"},
            detail=str(targets),
        )
        results = [revive.revive_target(t) for t in targets]
        check("both roles report skipped=True (nothing attached)", all(r["skipped"] for r in results), detail=str(results))
        check("both roles still report ok=True (skipped is not a failure)", all(r["ok"] for r in results), detail=str(results))
        summary = revive.summarize(results)
        # Asserts the PROPERTY, not the sentence. This check used to
        # demand the string "Nothing needed reviving", which is how it
        # passed while the summary was lying: on a clean slate with
        # nothing attached it announced "all 2 already up." Nothing was
        # up; nothing existed. The canary was asserting the bug.
        #
        # What actually matters is that a summary never claims something
        # is running when it isn't -- that is the whole point of the
        # feature, since a half-empty system described as healthy is the
        # failure it exists to prevent.
        check("summarize() reports nothing to revive", "othing to revive" in summary, detail=summary)
        check("...and NEVER claims the un-attached roles are 'already up'",
              "already up" not in summary, detail=summary)

        # --- 2. A genuinely STOPPED role, revived for real ---
        print("\n2. a real STOPPED concierge -- revive_target() actually calls activate_role() and succeeds")
        create_result = er.create_role_session("concierge", model="haiku", effort="low")
        check("real concierge session created", create_result["ok"], detail=create_result.get("detail", ""))
        if create_result["ok"]:
            record = create_result["record"]
            _force_one_exchange(record["tmux"])
            subprocess.run(["tmux", "kill-session", "-t", record["tmux"]], capture_output=True)
            time.sleep(1.0)
            check("killed concierge reads STOPPED (setup precondition)", er.role_liveness("concierge")["liveness"] == "stopped")

            target = next(t for t in revive.list_revive_targets() if t.kind == "role" and t.id == "concierge")
            result = revive.revive_target(target)
            check("STOPPED concierge is reported as attempted (skipped=False)", result["skipped"] is False, detail=str(result))
            check("STOPPED concierge revival succeeded (ok=True)", result["ok"] is True, detail=str(result))
            check("concierge is really RUNNING again after revive_target()", er.role_liveness("concierge")["liveness"] == "running")

        # --- 3. A genuinely irrecoverable (LOST) role -- ok=False, named, never rounded up ---
        print("\n3. an irrecoverable (LOST) orchestrator -- real activate_role() call, no mock")
        er.attach_role(
            "orchestrator", tmux="revive-canary-nonexistent-tmux", working_dir=str(WORKDIR),
            claude_session=str(uuid.uuid4()),  # no transcript exists for this uuid -- reads LOST
        )
        lost_liveness = er.role_liveness("orchestrator")
        check("fabricated orchestrator record reads LIVENESS_LOST (setup precondition)", lost_liveness["liveness"] == "lost", detail=str(lost_liveness))

        target = next(t for t in revive.list_revive_targets() if t.kind == "role" and t.id == "orchestrator")
        fail_result = revive.revive_target(target)
        check("LOST orchestrator is reported as attempted (skipped=False)", fail_result["skipped"] is False, detail=str(fail_result))
        check("LOST orchestrator revival is reported as FAILED (ok=False) -- never rounded up to success", fail_result["ok"] is False, detail=str(fail_result))
        check(
            "the failure names the item (label + non-empty detail)",
            fail_result["label"] == "Orchestrator" and bool(fail_result["detail"]),
            detail=str(fail_result),
        )

        # --- 4. Aggregation across a mixed batch -- overall never rounds up, summary names the failure ---
        print("\n4. aggregation across a mixed batch")
        mixed = [
            {"kind": "role", "id": "concierge", "label": "Concierge", "ok": True, "skipped": False, "detail": "activated", "sub_results": None},
            fail_result,
        ]
        check("one failure in the batch makes overall ok False", all(r["ok"] for r in mixed) is False)
        mixed_summary = revive.summarize(mixed)
        check("summary contains 'FAILED'", "FAILED" in mixed_summary, detail=mixed_summary)
        check("summary names the specific failing item (Orchestrator)", "Orchestrator" in mixed_summary, detail=mixed_summary)
        check("summary still credits the one that DID succeed (1 of 2)", "1 of 2" in mixed_summary, detail=mixed_summary)

        # --- 5. A real team with one genuinely STOPPED member, revived for real ---
        print("\n5. a real team with one STOPPED member -- revive_target() actually calls reconnect_team() and succeeds")
        fresh_result = trt.register_team_fresh(TEAM_ID, str(WORKDIR / "team"), aliases=["revivecanary"], model="sonnet")
        check("real team created and registered", fresh_result.get("ok", False), detail=str(fresh_result))
        if fresh_result.get("ok"):
            entry = next(t for t in trt.list_teams() if t["id"] == TEAM_ID)
            member_tmux = entry["members"][0]["tmux"]
            _force_one_exchange(member_tmux)
            subprocess.run(["tmux", "kill-session", "-t", member_tmux], capture_output=True)
            time.sleep(1.0)

            target = next(t for t in revive.list_revive_targets() if t.kind == "team" and t.id == TEAM_ID)
            team_result = revive.revive_target(target)
            check("STOPPED team member is reported as attempted (skipped=False)", team_result["skipped"] is False, detail=str(team_result))
            check("STOPPED team member revival succeeded (ok=True)", team_result["ok"] is True, detail=str(team_result))
            check(
                "sub_results carries reconnect_team()'s own per-member list, not discarded",
                team_result["sub_results"] is not None and len(team_result["sub_results"]) == 1,
                detail=str(team_result),
            )

            second = revive.revive_target(target)
            check(
                "an already-running team is skipped on a second revive, not re-attempted",
                second["skipped"] is True and second["ok"] is True,
                detail=str(second),
            )

    finally:
        for role in er.ROLES:
            record = er.get_role_record(role)
            if record:
                subprocess.run(["tmux", "kill-session", "-t", record["tmux"]], capture_output=True)
        subprocess.run(["tmux", "kill-session", "-t", "revive-canary-nonexistent-tmux"], capture_output=True)
        for entry in trt.list_teams():
            for m in entry.get("members", []):
                subprocess.run(["tmux", "kill-session", "-t", m["tmux"]], capture_output=True)
        save_registry([])
        shutil.rmtree(WORKDIR, ignore_errors=True)
        shutil.rmtree(Path(str(WORKDIR).replace("/tmp/", "/private/tmp/")), ignore_errors=True)
        # The isolated test-run trees only -- ~/Jarvis/engine.json and
        # ~/Jarvis/teams.json are never touched by this canary.
        for isolated_path in (er.ENGINE_REGISTRY_PATH.parent, TEAMS_REGISTRY_PATH.parent):
            if "test_runs" in str(isolated_path):
                shutil.rmtree(isolated_path, ignore_errors=True)

    all_ok = all(r[1] for r in RESULTS)
    passed_n = sum(1 for r in RESULTS if r[1])
    print(f"\n{'All checks passed.' if all_ok else 'CANARY FAILED.'} ({passed_n}/{len(RESULTS)})")
    if not all_ok:
        print("Do not trust revive-everything until this is fixed.")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
