#!/usr/bin/env python3
"""Canary for the split MCP tool surface (SPEC-orchestration.md SS0.2 --
the CRITICAL item). Same purpose and discipline as pane_state_canary.py:
proves the property live rather than trusting the module docstrings that
describe it.

Two things must both be true, checked independently:

  1. THE REGISTERED SURFACE. server_readonly.app's tool list is exactly
     {list_sessions, session_activity, spend} -- deliver_batch,
     confirm_plan, report_dispatch_stage are not registered on it.
  2. THE IMPORT GRAPH. A process that only ever imports server_readonly
     never loads tools_write.py or dispatch_state.py at all. This is the
     property that makes it structural rather than a registration bug
     someone could reintroduce by registering deliver_batch on the wrong
     app instance -- even if that happened, the actual sending code
     (transport.deliver with an arbitrary caller-supplied payload) still
     has to come from tools_write.py, and this proves that module is
     never in memory for a Haiku-only process. Not the same claim as
     "transport.py is absent" -- the read tools legitimately use
     transport.py too (a /cost capture is still a pane interaction); see
     the check below for exactly what is and isn't expected to leak.

Both checks matter: (1) alone would pass even if tools_write were
imported for some unrelated reason and just not registered as a tool --
still safe today, but one `app.tool()` call away from a leak. (2) alone
would pass even if someone registered the wrong function under the right
name. Together they're the actual guarantee.

Run after touching server.py, server_readonly.py, tools_read.py, or
tools_write.py:

    python3 l4_controller/mcp_surface_canary.py
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((name, passed, detail))
    print(f"  {'ok  ' if passed else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not passed else ""))


async def _tool_names(app) -> set[str]:
    return {t.name for t in await app.list_tools()}


def run() -> int:
    print("MCP tool surface canary\n")

    print("1. registered tool lists")
    import server
    import server_readonly

    full_names = asyncio.run(_tool_names(server.app))
    ro_names = asyncio.run(_tool_names(server_readonly.app))
    # The tool OBJECTS, not just names -- the handoff schema check below
    # needs the published inputSchema, which is what a model actually
    # sees and can fill in.
    ro_tools = asyncio.run(server_readonly.app.list_tools())

    expected_full = {
        "jarvis_say",
        "list_sessions", "session_activity", "spend",
        "report_dispatch_stage", "confirm_plan", "deliver_batch",
        "list_teams", "register_team_by_adoption", "register_team_fresh",
    }
    # jarvis_say is on BOTH surfaces deliberately (tools_voice.py): the
    # boundary this canary guards is "can the concierge ACT on Ayman's
    # systems," not "can it do anything at all." Speaking touches no
    # session and imports no transport, so it does not belong on the
    # forbidden list.
    #
    # Kept as an EXACT set rather than relaxed to a subset check when this
    # was added. An exact set is the entire value here -- it fails when a
    # tool is added, which is how it caught jarvis_say landing on the
    # read-only surface in the first place. A subset check would have said
    # nothing, and the next addition would arrive unnoticed.
    expected_ro = {"list_sessions", "session_activity", "spend", "jarvis_say", "handoff_to_router"}

    check("full surface has all nine tools", full_names == expected_full, detail=f"got {sorted(full_names)}")
    check(
        "read-only surface has exactly its three read tools plus jarvis_say and handoff_to_router",
        ro_names == expected_ro,
        detail=f"got {sorted(ro_names)}",
    )
    check("deliver_batch is NOT on the read-only surface", "deliver_batch" not in ro_names)
    check("confirm_plan is NOT on the read-only surface", "confirm_plan" not in ro_names)
    check("report_dispatch_stage is NOT on the read-only surface", "report_dispatch_stage" not in ro_names)
    # Team registry tools (SS1.3) are ALL Sonnet-only, including the
    # read-shaped list_teams() -- see server.py's docstring for why (a
    # live tmux round trip, not the console's cached poll layer Haiku's
    # tier is supposed to use instead).
    check("list_teams is NOT on the read-only surface", "list_teams" not in ro_names)
    check("register_team_by_adoption is NOT on the read-only surface", "register_team_by_adoption" not in ro_names)
    check("register_team_fresh is NOT on the read-only surface", "register_team_fresh" not in ro_names)
    check(
        "the read-only server's tool manager has no entry for 'deliver_batch' under any lookup",
        asyncio.run(_get_tool_or_none(server_readonly.app, "deliver_batch")) is None,
    )

        # THE actual safety property for handoff_to_router, and the reason it
    # is allowed on this surface at all: the caller cannot choose a
    # recipient. The router is resolved from engine.json inside the tool.
    # If a target/session parameter ever appears in this schema, the
    # concierge has silently gained arbitrary dispatch and the entire
    # read-only split is void -- so assert on the PUBLISHED SCHEMA, which
    # is what a model actually sees and can fill in, rather than on the
    # Python signature.
    handoff = next((t for t in ro_tools if t.name == "handoff_to_router"), None)
    check("handoff_to_router is published on the concierge surface", handoff is not None)
    if handoff is not None:
        props = set((handoff.input_schema or {}).get("properties", {}))
        forbidden = {"target", "session", "session_id", "tmux", "orchestrator_target", "to", "destination"}
        leaked = props & forbidden
        check(
            "handoff_to_router exposes NO target parameter -- the destination is not caller-chosen",
            not leaked,
            detail=f"schema properties = {sorted(props)}; forbidden present = {sorted(leaked)}",
        )
        check(
            "handoff_to_router takes only the transcript",
            props == {"transcript"},
            detail=f"got {sorted(props)}",
        )

    print("\n2. import graph -- a Haiku-only process never loads tools_write.py")
    # Deliberately a FRESH subprocess, not this process's sys.modules: this
    # process already imported both server and server_readonly above for
    # check 1, so checking sys.modules here would prove nothing about
    # what a real, isolated Haiku process actually loads.
    # Checks for tools_write.py itself and dispatch_state.py -- the
    # actual mutation capability (mark_dispatch_complete et al lives only
    # in dispatch_state.py, imported only by tools_write.py). Deliberately
    # does NOT check for transport/slash_guard/say_feedback/cancel_listener:
    # those are legitimately, safely pulled in by the READ path too
    # (providers.spend()/session_activity() go through transport.py for a
    # /cost capture; transport.py's stuck-view self-heal speaks an
    # announcement via say_feedback, which in turn imports cancel_listener
    # for its unrelated speak_with_cancel_window function -- none of that
    # grants the ability to send an arbitrary payload to an arbitrary
    # target, which is the one capability this file guards).
    proc = subprocess.run(
        [sys.executable, "-c", (
            "import sys; sys.path.insert(0, '.'); "
            "import server_readonly; "
            "leaked = [m for m in sys.modules if m in ('tools_write', 'dispatch_state')]; "
            "print(','.join(leaked))"
        )],
        cwd=str(Path(__file__).parent),
        capture_output=True,
        text=True,
    )
    leaked = [m for m in proc.stdout.strip().split(",") if m]
    check(
        "tools_write.py and dispatch_state.py never enter a Haiku-only process's import graph",
        proc.returncode == 0 and not leaked,
        detail=f"stderr={proc.stderr.strip()!r} leaked={leaked}",
    )

    print()
    failures = [r for r in RESULTS if not r[1]]
    if failures:
        print(f"{len(failures)}/{len(RESULTS)} FAILED:")
        for name, _, detail in failures:
            print(f"  - {name}" + (f" ({detail})" if detail else ""))
        return 1
    print(f"all {len(RESULTS)} checks passed")
    return 0


async def _get_tool_or_none(app, name: str):
    for t in await app.list_tools():
        if t.name == name:
            return t
    return None


if __name__ == "__main__":
    sys.exit(run())
