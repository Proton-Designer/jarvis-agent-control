"""handoff_to_router() -- the concierge's ONE way to pass work onward.

The concierge is structurally incapable of dispatching (§0.2): tools_write
is not in its process. That is correct and must stay true. But it still
has to hand a real instruction to the router, or the fast front layer is a
dead end that can only ever answer questions.

## Why this is not a hole in the boundary

The dangerous capability is choosing WHO receives arbitrary text. That is
what deliver_batch does, and why the concierge must not have it: arbitrary
text into an arbitrary session means the concierge could type into a team
lead that is mid-work, or into any pane at all.

This tool takes NO TARGET. The destination is read from engine.json's
router slot, by this module, at call time. A caller cannot name a
recipient, cannot enumerate recipients, and cannot reach anything except
the one session Ayman himself attached to the ORCHESTRATOR role in the
console.

So the blast radius is: the concierge can put arbitrary text in front of
the router. Which is precisely the flow being built -- the router reads
it, applies judgment, and is the thing holding the write tools. That is a
categorically smaller capability than "text into any pane," and it is the
reason this can live on the read-only surface while deliver_batch cannot.

Registered on the CONCIERGE surface only, not the router's. The router has
no reason to hand off to itself, and a tool present on a surface that
never needs it is one more thing a confused model can call.

## The synchronous-ack rule (SPEC-orchestration.md §1.4)

The concierge's whole job is not making Ayman wait, which invites building
this as pure fire-and-forget. That would make a FAILED handoff silent by
construction on day one -- the single worst outcome on this project.

So: the ~20ms budget buys a confirmed ENQUEUE, not a completed dispatch.
This function returns only after the transcript is written and delivery
has been attempted and checked. What stays async is the router's
reasoning, which is where the seconds actually are. A caller that gets
ok=False must say so out loud; it has not been handled by anyone.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "l5_console" / "state"))

from latency_log import log_event  # noqa: E402
from say_feedback import speak, PRIORITY_HIGH  # noqa: E402


def handoff_to_router(transcript: str) -> dict:
    """Hand a dictation to the router for parsing and dispatch.

    transcript: what Ayman said, VERBATIM. Do not summarise, split, or
        rewrite it -- the router does that, and a concierge that
        paraphrases first destroys information the router needs. Pass it
        through unchanged even if parts of it are unclear to you.

    Returns {"ok": bool, "reason": str, "target": str}. On ok=False the
    work has NOT been handed to anyone, and the caller must tell Ayman so
    rather than replying as though it were handled -- this function
    already speaks the failure aloud, so the caller should not repeat it,
    only avoid claiming success.
    """
    text = (transcript or "").strip()
    if not text:
        return {"ok": False, "reason": "empty transcript -- nothing to hand off", "target": ""}

    # Target resolved HERE, never accepted from the caller. This single
    # fact is what keeps the tool safe enough for the read-only surface.
    try:
        import engine_roles
        record = engine_roles.get_role_record("orchestrator")
        liveness = engine_roles.role_liveness("orchestrator")
    except Exception as e:
        log_event("handoff_to_router_failed", reason="engine_lookup_error", error=str(e))
        speak("I couldn't reach the router. Nothing was sent.", priority=PRIORITY_HIGH)
        return {"ok": False, "reason": f"could not read the engine roles: {e}", "target": ""}

    if not record:
        # Fails CLOSED and AUDIBLY. Ayman spoke an instruction; if there is
        # nowhere for it to go he has to hear that now, not discover it
        # when nothing ever happens.
        log_event("handoff_to_router_failed", reason="no_router_attached")
        speak("No orchestrator is attached, so I couldn't pass that on.", priority=PRIORITY_HIGH)
        return {"ok": False, "reason": "no session is attached to the ORCHESTRATOR role", "target": ""}

    # `running` (bool) -- see role_liveness()'s real shape; there is no
    # "state" key. Same guess, same place, caught by the same dry run.
    if not liveness.get("running"):
        log_event("handoff_to_router_failed", reason="router_not_running", liveness=liveness.get("liveness"))
        speak("The orchestrator isn't running, so I couldn't pass that on.", priority=PRIORITY_HIGH)
        return {
            "ok": False,
            "reason": f"the ORCHESTRATOR session is {liveness.get('liveness')}, not running",
            "target": record.get("tmux", ""),
        }

    target = record["tmux"]

    # deliver_transcript() owns the parts that must not be reimplemented
    # here: the preflight, the dictation-file write, the keyed dispatch
    # record, the pane-state gate, and the spoken failure if delivery
    # itself does not land. Reaching past it to a raw send-keys "because
    # it's simpler" is the specific thing SPEC-orchestration.md §0.2 warns
    # against, and it would bypass every one of those guarantees at once.
    try:
        from l2_l3_handoff import deliver_transcript
        from transport import TmuxTransport
        result = deliver_transcript(text, TmuxTransport(), orchestrator_target=target)
    except Exception as e:
        log_event("handoff_to_router_failed", reason="delivery_exception", error=str(e), target=target)
        speak("Something went wrong passing that to the orchestrator.", priority=PRIORITY_HIGH)
        return {"ok": False, "reason": f"delivery raised: {e}", "target": target}

    delivered = result is not None and getattr(result, "ok", False)
    log_event("handoff_to_router", ok=delivered, target=target, chars=len(text))
    if not delivered:
        # deliver_transcript already spoke its own failure -- do not speak
        # a second one over it. Report it so the caller cannot mistake
        # this for success.
        return {"ok": False, "reason": "delivery did not land; the failure was announced", "target": target}
    return {"ok": True, "reason": "", "target": target}
