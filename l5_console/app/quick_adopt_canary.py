#!/usr/bin/env python3
"""Canary for quick-adopt (SPEC-TUI.md SS6, TODO-feature-queue.md item 3):
"assigning [a known unassigned session] should be one keystroke", vs.
today's `[a]` opening the full multi-step wizard.

Live-drives ONE real scratch tmux session (a genuine `claude` launch, not
a fixture) through the exact sequence quick_adopt_flow.QuickAdoptScreen
runs, without mounting the Textual app -- the sequence itself is plain
function calls (team_actions.check_root_available, setup.adopt_candidate_info,
_first_available_slug, setup.create_team), so this canary calls them
directly, same testability discipline as every other pure/near-pure
function in this codebase.

BOTH DIRECTIONS, same discipline as every other canary here:
  1. The happy path actually creates a solo team, correctly (1 member,
     that member IS the lead, no longer shows as unassigned).
  2. THE CONFLICT PATH STILL REFUSES -- this is the check that matters
     most for this specific item. The Lead's explicit condition on this
     work: "it still has to route through the conflict checks I wired in
     c2e8468 -- a fast path that skips validation would reintroduce
     exactly the bug I just fixed." Re-adopting the SAME now-registered
     directory must be refused, by name, before anything happens twice.
  3. Slug collision auto-suffixes rather than dead-ending "one keystroke"
     on the first name clash.

ISOLATED VIA JARVIS_TEST_RUN, same discipline as every canary in this
project -- never touches ~/Jarvis/teams.json.

    l4_controller/.venv/bin/python3 l5_console/app/quick_adopt_canary.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

os.environ["JARVIS_TEST_RUN"] = f"quick-adopt-canary-{uuid.uuid4().hex[:8]}"

sys.path.insert(0, str(Path(__file__).parent.parent / "state"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "l4_controller"))

import setup as setup_state  # noqa: E402
import team_actions  # noqa: E402
from teams import TEAMS_REGISTRY_PATH, discover_teams_and_unassigned  # noqa: E402

assert "test_runs" in str(TEAMS_REGISTRY_PATH), (
    f"TEAMS_REGISTRY_PATH is NOT test-isolated ({TEAMS_REGISTRY_PATH}) -- refusing to run"
)

TMUX_SESSION = "jarvis-quickadopt-canary"
WORKDIR = Path("/tmp/jarvis-quickadopt-canary")

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((name, passed, detail))
    print(f"  {'ok  ' if passed else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not passed else ""))


def _slugify(text: str) -> str:
    """Same regex as quick_adopt_flow.py imports from setup_flow._slugify
    -- duplicated here rather than imported, deliberately: importing
    quick_adopt_flow (or setup_flow) pulls in textual, and this canary is
    meant to run under l4_controller/.venv (which has the providers/
    transport chain a real /status round-trip needs) rather than
    l5_console/app/.venv -- same reason blocked_render_canary.py stays
    app-venv-only for the opposite direction. The two must agree; a
    canary comparing this copy against the real one would be the more
    thorough fix if this ever needs to be more than a formality."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "team"


def _first_available_slug(base: str) -> dict:
    for i in range(1, 21):
        candidate = base if i == 1 else f"{base}-{i}"
        id_check = team_actions.check_team_id_available(candidate)
        if not id_check["ok"]:
            continue
        alias_check = team_actions.check_alias_available(candidate)
        if not alias_check["ok"]:
            continue
        return {"ok": True, "slug": candidate}
    return {"ok": False, "slug": None}


def run() -> int:
    subprocess.run(["tmux", "kill-session", "-t", TMUX_SESSION], capture_output=True)
    import shutil
    shutil.rmtree(WORKDIR, ignore_errors=True)

    try:
        launch = setup_state.create_fresh_member(TMUX_SESSION, str(WORKDIR), "sonnet")
        check("scratch session launched", launch["ok"], detail=str(launch))
        root = launch["root"]

        teams, unassigned = discover_teams_and_unassigned()
        u = next((x for x in unassigned if x.tmux == TMUX_SESSION), None)
        check("a freshly launched, never-registered session shows as unassigned", u is not None)

        print()
        print("happy path -- the exact sequence QuickAdoptScreen runs")
        root_check = team_actions.check_root_available(u.working_dir)
        check("no prior conflict on a genuinely free directory", root_check["ok"], detail=str(root_check))

        info = setup_state.adopt_candidate_info(u.tmux)
        check("adopt_candidate_info() resolves a real claude_session", info["claude_session"] is not None, detail=str(info))

        base_slug = _slugify(Path(u.working_dir).name)
        slug_result = _first_available_slug(base_slug)
        check("a free slug is found on the first try (nothing registered yet)", slug_result["ok"] and slug_result["slug"] == base_slug, detail=str(slug_result))
        team_id = slug_result["slug"]

        members = [{"tmux": u.tmux, "claude_session": info["claude_session"], "model": info.get("model")}]
        setup_state.create_team(team_id, [team_id], u.working_dir, u.tmux, members)

        teams2, unassigned2 = discover_teams_and_unassigned()
        t = next((tm for tm in teams2 if tm.id == team_id), None)
        check("the solo team exists after quick-adopt", t is not None)
        check("it has exactly 1 member", t is not None and len(t.members) == 1, detail=str(t.members) if t else "")
        check("that member IS the lead (solo -- no separate lead-picking step needed)", t is not None and t.has_lead and t.members[0].is_lead)
        check("no longer shows as unassigned", not any(x.tmux == TMUX_SESSION for x in unassigned2))
        check("no effort was invented for an adopted member", t is not None and t.members[0].effort is None)

        print()
        print("THE CHECK THAT MATTERS MOST: the conflict path still refuses")
        root_check_2 = team_actions.check_root_available(u.working_dir)
        check(
            "re-quick-adopting the SAME now-registered directory is REFUSED",
            not root_check_2["ok"],
            detail=str(root_check_2),
        )
        check(
            "...and names the conflicting team, not a generic failure",
            not root_check_2["ok"] and team_id in root_check_2["detail"],
            detail=root_check_2.get("detail", ""),
        )
        id_check_2 = team_actions.check_team_id_available(team_id)
        check("the slug is also no longer available as a fresh team id", not id_check_2["ok"])

        print()
        print("slug collision -- auto-suffix rather than a dead end")
        # team_id is already taken (the team created above) -- a second
        # quick-adopt landing on the SAME base slug from a different
        # directory (the realistic case: two projects both named "api")
        # must not just fail; it must find the next free name.
        collision_result = _first_available_slug(team_id)
        check(
            "a colliding base slug auto-suffixes to -2, not a bare failure",
            collision_result["ok"] and collision_result["slug"] == f"{team_id}-2",
            detail=str(collision_result),
        )

    finally:
        subprocess.run(["tmux", "kill-session", "-t", TMUX_SESSION], capture_output=True)
        shutil.rmtree(WORKDIR, ignore_errors=True)
        shutil.rmtree(Path(str(WORKDIR).replace("/tmp/", "/private/tmp/")), ignore_errors=True)
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
