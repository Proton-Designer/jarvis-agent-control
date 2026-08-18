"""
L4 controller, READ-ONLY surface: the Haiku concierge's MCP connection
(SPEC-orchestration.md SS0.2). Exposes exactly list_sessions(),
session_activity(), spend() -- tools_read.py -- and nothing else.

STRUCTURAL, NOT A PROMPT INSTRUCTION. This module never imports
tools_write.py -- deliver_batch and confirm_plan (arbitrary-payload
delivery, and the cancel-window speech tool) are simply absent from this
process's import graph, not merely unregistered. There is no
`if role == "haiku": skip write tools` branch to get wrong.

Note what this does NOT mean: tools_read.py's session_activity()/spend()
legitimately go through transport.py too (a /cost capture is still a
pane interaction), and that's fine -- transport.deliver()'s pane-state
gate and slash_guard's hard-blocks are the correct, shared choke point
for ALL pane interaction, read or write, and both read tools only ever
send fixed, hardcoded, pre-verified payloads ("/cost") through it, never
arbitrary text. The guarantee this file provides is narrower and exact:
nothing reachable from here can send an ARBITRARY, caller-supplied
payload to an arbitrary target -- that capability lives only in
tools_write.deliver_batch, which this file never imports. If a future
change makes this file import anything from tools_write.py (even
transitively), it has silently broken the one guarantee this file exists
to provide -- check the import graph, not just the registered tool list,
after touching this.

Point Haiku's MCP client config at this file's absolute path, never at
server.py.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

import tools_read

app = MCPServer(name="jarvis-l4-readonly")

list_sessions = app.tool()(tools_read.list_sessions)
session_activity = app.tool()(tools_read.session_activity)
spend = app.tool()(tools_read.spend)


if __name__ == "__main__":
    app.run(transport="stdio")
