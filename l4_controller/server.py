"""
L4 controller, FULL surface: read tools (tools_read.py) plus write tools
(tools_write.py), exposed as one MCP stdio server. This is the Sonnet
router's connection (SPEC-orchestration.md SS0.2) -- the tier that
"holds the write authority" gets this file; the Haiku concierge connects
to server_readonly.py instead, which cannot reach deliver_batch at all
because it never imports tools_write.

Why two files rather than one server with a runtime mode flag: a flag
still imports every tool's implementation into the same process --
whichever tools get registered is then a runtime branch that could be
wrong, skipped, or (worse) bypassed by whatever's calling this file with
a slightly different argument than expected. Two separate entry points
make "Haiku cannot dispatch" a fact about which Python module ever gets
imported into its process, not a fact about which `if` branch ran. This
project's standing rule is that safety lives at the transport layer, not
the prompt layer (see slash_guard.py's /config and /model hard-blocks) --
the same reasoning applies one level up, to which tools even exist for a
given caller to try.

Tools registered here:
  list_sessions() / session_activity() / spend() / pending_questions() -- tools_read.py
  report_dispatch_stage() / confirm_plan() / deliver_batch() /
    answer_blocked_session() -- tools_write.py. The last is Sonnet-only
    (docs/TODO-feature-queue.md #5): it delivers a keystroke to a real
    pane, same write-authority reasoning as deliver_batch -- Haiku gets
    pending_questions() (read-only) to recognize an utterance MIGHT be
    answering something, then hands off via handoff_to_router() like any
    other utterance; it never answers directly itself.
  list_teams() / register_team_by_adoption() / register_team_fresh() --
    team_registry_tools.py (SPEC-orchestration.md SS1.3). All three are
    Sonnet-only, including the read-shaped list_teams(): it calls
    teams.discover_teams_and_unassigned() directly, a live tmux round
    trip, not the console's cached poll layer -- putting it on the
    read-only surface would give Haiku a second, uncached polling path
    for team state, exactly the two-pollers-disagree case SS1.2 says to
    avoid by construction (agreed with ue6rruxg, who owns
    team_registry_tools.py's correctness). A cheaper, Haiku-facing team
    query (reading l5_console/state/api.py's cached JarvisState.teams
    instead) is a separate, not-yet-built tool if that's ever wanted.

Do not add a delivery path here that bypasses tools_write.deliver_batch()
"because it's simpler" -- transport.deliver()'s pane-state gate and
slash_guard's hard-blocks are enforced inside that one path, and a second
path around it is a second, unguarded way to reach a real tmux session.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

import kokoro_tts
import team_registry_tools
import tools_read
import tools_voice
import tools_write
from latency_log import log_event

app = MCPServer(name="jarvis-l4-controller")

list_sessions = app.tool()(tools_read.list_sessions)
session_activity = app.tool()(tools_read.session_activity)
spend = app.tool()(tools_read.spend)
pending_questions = app.tool()(tools_read.pending_questions)

# jarvis_say is on BOTH surfaces deliberately -- speaking is not
# authority. See tools_voice.py's docstring: this module never imports
# transport, never touches tmux, never writes to a session, so exposing
# it here does not weaken the read-only guarantee the split exists for.
jarvis_say = app.tool()(tools_voice.jarvis_say)

report_dispatch_stage = app.tool()(tools_write.report_dispatch_stage)
confirm_plan = app.tool()(tools_write.confirm_plan)
deliver_batch = app.tool()(tools_write.deliver_batch)
answer_blocked_session = app.tool()(tools_write.answer_blocked_session)

list_teams = app.tool()(team_registry_tools.list_teams)
register_team_by_adoption = app.tool()(team_registry_tools.register_team_by_adoption)
register_team_fresh = app.tool()(team_registry_tools.register_team_fresh)


if __name__ == "__main__":
    # Warm Kokoro BEFORE serving, not on the first real speak() call --
    # measured live (kokoro_tts.py's docstring): the first synthesis call
    # after a fresh model load costs an extra ~1.5s (ONNX Runtime's own
    # session warmup) on top of ~390ms model load. Paying that here, at
    # process startup where a couple extra seconds is invisible, is the
    # whole point -- the alternative is the first thing this session ever
    # speaks (which could be the instant ack) eating that cost instead,
    # undermining the exact silence the ack exists to remove. Never
    # printed to stdout: this process's stdout IS the MCP stdio protocol
    # stream, and anything written there outside the protocol corrupts
    # it (the same hazard jarvis_say_canary.py's own probe already
    # documents). Best-effort and non-fatal either way -- a failed warm
    # here just means the first real speak() call falls back to `say`
    # audibly/logged, same as any other Kokoro-unavailable case.
    log_event("kokoro_tts_warm_startup", **kokoro_tts.warm())
    app.run(transport="stdio")
