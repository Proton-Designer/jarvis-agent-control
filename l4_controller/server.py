"""
L4 controller, exposed to the L3 orchestrator as an MCP stdio server.

Tools:
  list_sessions()               -- live discovery, for L3 to build a routing
                                    plan before confirming/delivering anything.
  confirm_plan(summary, window) -- speaks the plan summary, opens the
                                    cancel window in parallel, returns
                                    whether Ayman cancelled it.
  deliver_batch(instructions)   -- delivers each {target, payload}, retries
                                    a BUSY refusal once, speaks per-delivery
                                    acks, and speaks + returns an explicit
                                    summary of anything that still didn't go.
                                    Never silently drops a partial failure.

Retry policy (only BUSY is retried): a busy target is very likely mid-tool-
call and will clear on its own shortly. A PERMISSION_PROMPT or UNKNOWN/real-
typed-content refusal is not transient — waiting and retrying just delays
the same wrong outcome, so those are reported immediately instead.
"""

from __future__ import annotations

import time

from mcp.server.mcpserver import MCPServer

from cancel_listener import cancel_socket_available
from registry import SessionRegistry, UnknownSessionError
from say_feedback import speak, speak_with_cancel_window
from transport import TmuxTransport

app = MCPServer(name="jarvis-l4-controller")

registry = SessionRegistry()
transport = TmuxTransport()

RETRY_DELAY_S = 3.0
BATCH_RETRY_BUDGET_S = 10.0  # shared across the whole batch, not per-instruction


@app.tool()
def list_sessions() -> list[dict]:
    """Enumerate real, currently-running tmux sessions with their working
    directory, for resolving loose voice references to a live target.
    Never returns a session that isn't actually running."""
    return [
        {"session_id": s.session_id, "working_dir": s.working_dir, "alias": s.alias}
        for s in registry.list_sessions()
    ]


@app.tool()
def confirm_plan(summary: str, cancel_window_s: float = 2.5) -> dict:
    """Speak the routing plan summary and open a cancel window in parallel
    (not sequentially). cancel_window_s == 0 disables the window (config,
    not hardcoded). Returns {"confirmed": bool, "cancel_window_available":
    bool} -- confirmed is False if Ayman said the cancel word during the
    window. cancel_window_available False means there was no real cancel
    window at all (L1's socket down) -- this is spoken explicitly ("Cancel
    unavailable.") and deliver_batch independently refuses delivery to any
    non-test target in that state, regardless of what this call returns."""
    result = speak_with_cancel_window(summary, cancel_window_s)
    return {"confirmed": not result["cancelled"], "cancel_window_available": result["available"]}


@app.tool()
def deliver_batch(instructions: list[dict], retry_busy_once: bool = True) -> dict:
    """
    instructions: list of {"target": <friendly name or session id>,
    "payload": <text to deliver>}.

    Resolves each target via the live registry (never guesses at a session
    that isn't running), delivers via the tmux transport (pane-state gated,
    slash-guarded), speaks a short ack per successful delivery, retries a
    BUSY refusal once after a short delay (bounded by a shared batch-wide
    retry budget, so N busy targets can't compound into a long silent
    wait), and — critically — never lets a partial batch failure pass
    silently: an explicit summary is always spoken at the end, success or
    not, so silence never has to be interpreted as either outcome.

    Enforces the cancel-window safety property AT THE POINT OF DELIVERY,
    independent of whether/how confirm_plan was called: if L1's cancel
    socket is down, there is no real human-in-the-loop control anywhere in
    the system (auto-mode targets have no permission prompts either), so
    delivery to any target not explicitly marked as a throwaway test
    target (registry.is_test_target) is refused, loudly, rather than
    proceeding on the assumption that confirm_plan already covered it.
    """
    results = []
    failures = []
    retry_budget_s = BATCH_RETRY_BUDGET_S
    socket_up = cancel_socket_available()

    for instr in instructions:
        target_name = instr["target"]
        payload = instr["payload"]

        try:
            session_id = registry.resolve(target_name)
        except UnknownSessionError as e:
            results.append({"target": target_name, "ok": False, "detail": str(e), "reason": "no_session"})
            failures.append(f"{target_name} (no such session)")
            speak(f"Could not find a session for {target_name}, not sent.")
            continue

        if not socket_up and not registry.is_test_target(session_id):
            detail = f"cancel window unavailable and {target_name} is not a test target"
            results.append({"target": session_id, "ok": False, "detail": detail, "reason": "cancel_unavailable"})
            failures.append(f"{target_name} (cancel_unavailable)")
            speak(f"Cancel unavailable, {target_name} not sent.")
            continue

        result = transport.deliver(session_id, payload)

        if (
            not result.ok
            and result.reason == "busy"
            and retry_busy_once
            and retry_budget_s >= RETRY_DELAY_S
        ):
            time.sleep(RETRY_DELAY_S)
            retry_budget_s -= RETRY_DELAY_S
            result = transport.deliver(session_id, payload)

        results.append(
            {"target": session_id, "ok": result.ok, "detail": result.detail, "reason": result.reason}
        )

        if result.ok:
            speak(f"Sent to {target_name}.")
        else:
            failures.append(f"{target_name} ({result.reason})")
            speak(f"{target_name} not sent: {result.detail}.")

    if failures:
        speak(
            f"{len(failures)} of {len(instructions)} instructions did not go through: "
            + ", ".join(failures)
            + "."
        )
    else:
        speak(f"All {len(instructions)} instructions sent." if instructions else "Nothing to send.")

    return {"results": results, "failures": failures}


if __name__ == "__main__":
    app.run(transport="stdio")
