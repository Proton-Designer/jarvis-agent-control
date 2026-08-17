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
from dispatch_state import mark_dispatch_complete, report_dispatch_stage as _report_dispatch_stage
from latency_log import log_event
from providers import registry, transport
from providers import list_sessions as _list_sessions
from providers import session_activity as _session_activity
from providers import spend as _spend
from registry import UnknownSessionError
from say_feedback import speak, speak_with_cancel_window
from view_parsers import summarize_view

app = MCPServer(name="jarvis-l4-controller")

RETRY_DELAY_S = 3.0
BATCH_RETRY_BUDGET_S = 10.0  # shared across the whole batch, not per-instruction


@app.tool()
def list_sessions() -> list[dict]:
    """Enumerate real, currently-running tmux sessions with their working
    directory, for resolving loose voice references to a live target.
    Never returns a session that isn't actually running. custom_commands
    lists that target's project-specific slash commands
    (.claude/commands/*.md at its cwd) -- consult this before routing a
    control-plane instruction to a command that isn't universal
    (/compact, /cost, /usage, /status) as those are the only built-ins
    verified safe; anything else needs to actually be in this list for
    that target or it will be refused at delivery."""
    log_event("list_sessions_called")
    return _list_sessions()


@app.tool()
def session_activity(session_id: str) -> dict:
    """What a session is doing right now, read directly from its pane --
    no inference, no model. Returns {"session_id", "state", "activity"}:
    state is the same busy/ready/permission_prompt/persistent_view/unknown
    classification the delivery gate itself uses (or "no_session" if it
    isn't running); activity is a short label of the current tool call or
    "thinking" when state is busy, or None if nothing recognizable
    matched -- never a guess."""
    return _session_activity(session_id)


@app.tool()
def spend(session_id: str) -> dict:
    """How much a session has spent this session, via the same /cost
    capture+parse path used for delivery read-back. Returns {"ok",
    "summary", "raw"} -- summary is None (never a fabricated figure) if
    the view didn't parse cleanly. Requires the target pane to be READY,
    same as any other delivery."""
    return _spend(session_id)


@app.tool()
def report_dispatch_stage(stage: str, detail: str = "") -> dict:
    """Optional: push a note about where you are in handling the current
    dictation (e.g. "plan_spoken" plus the narrated summary text) so the
    concierge can surface it if asked "what's it doing?". This is color,
    not a requirement -- the concierge's forwarded/complete tracking does
    not depend on this ever being called, so skipping it is never a
    correctness problem, just a slightly less specific answer."""
    _report_dispatch_stage(stage, detail)
    return {"ok": True}


@app.tool()
def confirm_plan(summary: str, cancel_window_s: float = 2.5) -> dict:
    """Speak the routing plan summary (with a "say Hey Jarvis to cancel"
    hint appended) and open a cancel window in parallel (not sequentially).
    cancel_window_s == 0 disables the window (config, not hardcoded).
    Returns {"confirmed": bool, "cancel_window_available": bool} --
    confirmed is False if Ayman re-said the "Hey Jarvis" wake word during
    the window (that re-detection IS the cancel trigger, not a separate
    "cancel" keyword -- see cancel_listener.py for why). cancel_window_available
    False means there was no real cancel
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
            {
                "target": session_id,
                "ok": result.ok,
                "detail": result.detail,
                "reason": result.reason,
                "view_content": result.view_content,
            }
        )

        if result.ok and result.view_content is not None:
            # A read-only view command (/cost, /usage, ...) -- speak the
            # parsed answer, not "Sent to X.": the value here is the
            # figure Ayman asked for, not confirmation of delivery.
            command = payload.strip().split(" ", 1)[0]
            summary = summarize_view(command, result.view_content)
            if summary is not None:
                speak(f"{target_name}: {summary}")
            else:
                speak(
                    f"{target_name}'s {command} is on screen but I couldn't parse a clean "
                    "answer from it."
                )
        elif result.ok:
            speak(f"Sent to {target_name}.")
        elif result.reason == "dismiss_failed" and result.view_content is not None:
            # The view was read successfully (so surface the answer we
            # have) but Escape didn't verifiably close it -- the pane is
            # left in an uncertain state, which still counts as a
            # delivery failure per policy, so it's still in `failures`.
            command = payload.strip().split(" ", 1)[0]
            summary = summarize_view(command, result.view_content)
            failures.append(f"{target_name} ({result.reason})")
            if summary is not None:
                speak(
                    f"{target_name}: {summary} But its {command} view didn't close cleanly "
                    "and needs manual attention."
                )
            else:
                speak(f"{target_name}'s {command} view didn't close cleanly and needs manual attention.")
        else:
            failures.append(f"{target_name} ({result.reason})")
            speak(f"{target_name} not sent: {result.detail}.")

    log_event("last_send_issued", count=len(instructions), failures=len(failures))
    # Code-driven "complete" marker for the concierge's dispatch-in-flight
    # state -- this is the point the batch actually has a real result, not
    # an L3 self-report. See dispatch_state.py.
    mark_dispatch_complete({"count": len(instructions), "failures": len(failures)})

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
