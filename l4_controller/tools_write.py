"""
Write-capable L4 tool implementations (SPEC-orchestration.md SS0.2). Plain,
undecorated functions -- server.py (the FULL surface, Sonnet router's
connection) registers these as MCP tools. server_readonly.py (Haiku's
connection) does not import this module at all, on purpose: see its
module docstring for why that has to be structural, not a prompt
instruction.

Nothing here weakens or bypasses transport.deliver()'s existing gates
(pane-state check, slash_guard's hard-blocks and readonly/interactive
view classification) or cancel_listener's cancel-window enforcement --
this module is a thin orchestration layer over that existing, unchanged
safety boundary, never a second path around it.
"""

from __future__ import annotations

import time

from blocked_answer import answer_blocked_session as _answer_blocked_session
from cancel_listener import cancel_socket_available
from dispatch_state import mark_dispatch_complete, report_dispatch_stage as _report_dispatch_stage
from latency_log import log_event
from member_identity import verify_and_refresh_identity
from providers import registry, transport
from registry import UnknownSessionError
from say_feedback import speak, speak_with_cancel_window
from view_parsers import summarize_view

RETRY_DELAY_S = 3.0
BATCH_RETRY_BUDGET_S = 10.0  # shared across the whole batch, not per-instruction
PHRASE_PAUSE_MARKER = " [[slnc 400]] "  # macOS `say` embedded-silence command, ~0.4s


def report_dispatch_stage(dictation_ref: str, stage: str, detail: str = "") -> dict:
    """Optional: push a note about where you are in handling a specific
    dictation (dictation_ref is the path you were told to read it from --
    e.g. "plan_spoken" plus the narrated summary text) so the concierge
    can surface it if asked "what's it doing?". This is color, not a
    requirement -- the concierge's forwarded/complete tracking does not
    depend on this ever being called, so skipping it is never a
    correctness problem, just a slightly less specific answer. dictation_ref
    is required (not defaulted to "the current one") because more than one
    dictation can be in flight at once -- see dispatch_state.py."""
    _report_dispatch_stage(dictation_ref, stage, detail)
    return {"ok": True}


def confirm_plan(phrases: list[str], cancel_window_s: float = 2.5) -> dict:
    """Speak the routing plan as a paced sequence of short phrases -- one
    per resolved or held instruction -- with a "say Hey Jarvis to cancel"
    hint appended, and open a cancel window in parallel (not sequentially).
    cancel_window_s == 0 disables the window (config, not hardcoded).
    Returns {"confirmed": bool, "cancel_window_available": bool} --
    confirmed is False if Ayman re-said the "Hey Jarvis" wake word during
    the window (that re-detection IS the cancel trigger, not a separate
    "cancel" keyword -- see cancel_listener.py for why). cancel_window_available
    False means there was no real cancel
    window at all (L1's socket down) -- this is spoken explicitly ("Cancel
    unavailable.") and deliver_batch independently refuses delivery to any
    non-test target in that state, regardless of what this call returns.

    `phrases` is a LIST, not one flat sentence -- e.g. ["API gateway: run
    its test suite and check its health endpoint.", "Mobile app: run its
    tests -- the redeploy was dropped.", "Holding the backend restart --
    ambiguous between three sessions."], one entry per resolved OR held
    instruction. This replaced an earlier design (speak_now, called
    mid-turn to narrate each resolution as it landed) that relied on you
    remembering to call a second tool under load and turned out
    unreliable specifically on complex turns -- exactly the turns where
    a dense, hard-to-follow single sentence needs pacing most. Passing a
    list here gets the same "hear it build up, not one run-on sentence"
    benefit from code that paces deterministically (a short embedded
    pause between each phrase) and cannot fail to run, instead of a
    second tool call that can be skipped under instruction-following
    load. Keep each phrase short and self-contained."""
    joined = PHRASE_PAUSE_MARKER.join(p.strip() for p in phrases if p.strip())
    result = speak_with_cancel_window(joined, cancel_window_s)
    return {"confirmed": not result["cancelled"], "cancel_window_available": result["available"]}


def deliver_batch(instructions: list[dict], dictation_ref: str, retry_busy_once: bool = True) -> dict:
    """
    instructions: list of {"target": <friendly name or session id>,
    "payload": <text to deliver>}.

    dictation_ref: the path you were told to read this dictation from (the
    same string the pointer message gave you). Required so the completion
    this call records closes out THIS dictation specifically -- more than
    one can be in flight at once now, see dispatch_state.py.

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

        # Known-gap fix (SPEC-orchestration.md): a registered team member
        # whose Claude process crashed and relaunched reads as a normal
        # RUNNING session via tmux-name+cwd liveness alone -- routing work
        # to it on the assumption it remembers anything would be silent
        # amnesia. Checked here, not on a poll loop (see
        # member_identity.py's docstring for why), and never blocks
        # delivery -- only announces, since the new instruction may not
        # need any prior context at all.
        identity = verify_and_refresh_identity(session_id)
        if identity["restarted"]:
            speak(identity["detail"])

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
    # a router self-report. Scoped to THIS dictation only. See
    # dispatch_state.py.
    mark_dispatch_complete(dictation_ref, {"count": len(instructions), "failures": len(failures)})

    if failures:
        speak(
            f"{len(failures)} of {len(instructions)} instructions did not go through: "
            + ", ".join(failures)
            + "."
        )
    else:
        speak(f"All {len(instructions)} instructions sent." if instructions else "Nothing to send.")

    return {"results": results, "failures": failures}


def answer_blocked_session(answer: str, target: str = "") -> dict:
    """docs/TODO-feature-queue.md #5 / SPEC-blockers.md SS5: routes an
    answer Ayman ACTUALLY SPOKE back to the specific session that asked
    a question and is still waiting. This is stage 2 (escalation
    routing) only -- it never invents an answer itself; `answer` must be
    what Ayman said, verbatim, same discipline as handoff_to_router()'s
    transcript.

    Call pending_questions() first if you're not sure whether an
    utterance is answering something -- with nothing pending, this call
    always refuses (there's nothing TO answer), so ordinary DISPATCH
    handling is always the safe default when uncertain (the spec's own
    ruling: an ANSWER misread as DISPATCH is loud and recoverable; a
    DISPATCH misread as ANSWER would be silent).

    target: optional team id/alias/tmux hint, when Ayman named who he's
    answering ("tell gateway to use staging"). Leave empty to auto-
    resolve -- but ONLY when exactly one session is pending; with zero or
    two-or-more pending this refuses rather than guessing (SS5.4: hold
    and ask, never deliver an answer to the wrong question).

    Returns {"ok", "detail", "team_id"}. ALWAYS refuses (never guesses)
    when `answer` doesn't unambiguously match one of that question's own
    captured option labels -- delivery uses transport.answer_blocked_question(),
    a single validated keystroke into a freshly-reconfirmed
    BLOCKED_QUESTION pane, never free text (verified live: this UI
    doesn't support safe free-text injection via keystrokes at all).

    Speaks nothing itself -- announce the outcome (ok or not) via
    jarvis_say() yourself; say_feedback.py/return_queue.py are the
    Lead's territory for the batching work in flight right now, and this
    tool's whole job is routing the answer, not narrating it."""
    return _answer_blocked_session(answer, target or None)
