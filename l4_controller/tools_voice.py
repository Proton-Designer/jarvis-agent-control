"""jarvis_say() -- the return channel. The one tool BOTH MCP surfaces
expose, because speaking is not authority.

Everything else in this system flows one way: Ayman speaks, the concierge
routes, the router dispatches, agents work. Nothing has ever been able to
talk back. That is why the router can finish thirty seconds later with
nobody listening, and why a blocked session can sit waiting all afternoon
without Ayman knowing it needs him (SPEC-blockers.md's entire §5).

## Why this is on the read-only surface too

The §0.2 split exists so the concierge is STRUCTURALLY incapable of
dispatching work. Speaking does not dispatch work. A concierge that
cannot speak cannot do the one job it has, so this tool goes on both
surfaces -- and it does not weaken the boundary, because the boundary is
about *acting on Ayman's systems*, not about producing sound.

Concretely: this module never imports transport, never touches tmux,
never writes to any session. The worst a compromised caller can do is say
something untrue out loud, which Ayman can hear and correct. That is a
categorically different blast radius from delivering keystrokes to a
pane, and it is why the split is drawn where it is rather than at "can
this tool do anything at all."

## Typed classes, not free prose

`kind` is required and closed. Free prose from any agent at any time is a
spam channel wired directly into Ayman's attention, and untyped messages
cannot be batched or prioritised -- ue6rruxg's design point, and it is
the right one. An agent that wants to narrate its progress does not get
to; only the four events below are worth interrupting a person for.

## The line that matters most: answer vs completion

`answer` and `completion` are not two words for "I have something to
say." They are the difference between a reply and a report, and putting
a reply in the wrong one is the bug this file was rewritten to kill.

An `answer` is a response to something Ayman said seconds ago. He is
standing there waiting for it, so it goes out IMMEDIATELY. A
`completion` is work that finished while he was doing something else,
so it BATCHES -- three agents finishing together become one
interruption instead of three.

Batching a reply is wrong by design, not by accident: it makes a fast
system feel slow, it lets a live answer merge with an unrelated
completion into one blob, and -- measured -- it pushed reply latency
past the instant ack's fallback threshold, so Ayman heard "Okay, one
sec" before nearly every answer. The delay that makes three completions
polite makes one answer broken.

**When both fit, choose `answer`.** A report spoken a moment early
costs nothing; a reply held 2.5 seconds is the failure above.

## Batching, and what it deliberately does not touch

`completion` and `blocked_question` route through return_queue.py:
collect, settle, flush in priority-tier order, never mid-dictation.
`answer` and `error` go straight to speak().

Nothing that was already immediate is delayed by any of this. The
instant ack, refusals and cancel-window confirmations never come through
jarvis_say at all -- they call speak() directly.

Priority ordering is real and independent of batching -- say_feedback's
queue is priority-then-FIFO, so an escalation submitted behind three
completions still goes first.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from latency_log import log_event  # noqa: E402
from say_feedback import speak, PRIORITY_HIGH, PRIORITY_NORMAL  # noqa: E402

# Closed set, deliberately small. Each entry is an event Ayman would
# actually want to be interrupted for; "agent X started task Y" is not
# one and must not become one.
#
# Priorities mirror SPEC-orchestration.md §2.3's tiers. Refusals are not
# here because they are spoken by the transport itself, synchronously, on
# the path that produced them -- say_feedback's own docstring treats a
# delayed refusal as equivalent to a lost instruction, and routing them
# through a queue any agent can also fill would put that guarantee behind
# other people's traffic.
KINDS = {
    "answer": PRIORITY_HIGH,             # Ayman asked; this is the reply. He is standing there waiting for it.
    "blocked_question": PRIORITY_HIGH,   # work has STOPPED and needs Ayman -- outranks anything merely finished
    "error": PRIORITY_HIGH,              # something failed; silence here is the failure class this project keeps finding
    "completion": PRIORITY_NORMAL,       # work finished; it can wait behind a blocker
}

MAX_CHARS = 300


def jarvis_say(message: str, kind: str, team: str = "") -> dict:
    """Speak one short sentence to Ayman through the single Jarvis voice.

    message: what to say, written FOR THE EAR -- no file paths, no code,
        no session ids. Name the team as the grammatical subject rather
        than prefixing a callsign: "Gateway finished its tests, three
        passing" reads as one voice reporting, where "Gateway here --"
        turns the system into a phone tree (SPEC-orchestration.md §2.2).
    kind: one of answer | blocked_question | error | completion. Required
        and closed -- see the module docstring for why this is not free
        prose. Use `answer` whenever Ayman just asked something and this
        is the reply; `completion` is for work that finished while he was
        doing something else.
    team: optional, for the log only. Never spoken; the message already
        names its own subject.

    Returns {"ok": bool, "reason": str} -- and REFUSES rather than
    guessing on an unknown kind. A tool that silently downgrades a
    misspelled "blocked" to normal priority would make a stopped session
    wait behind three completions, which is exactly the failure this
    channel exists to prevent.
    """
    if kind not in KINDS:
        log_event("jarvis_say_refused", reason="unknown_kind", kind=str(kind)[:40], team=team[:60])
        return {
            "ok": False,
            "reason": f"unknown kind {kind!r}; must be one of: {', '.join(sorted(KINDS))}",
        }

    text = (message or "").strip()
    if not text:
        log_event("jarvis_say_refused", reason="empty_message", kind=kind, team=team[:60])
        return {"ok": False, "reason": "message was empty -- nothing to say"}

    # Truncate rather than refuse: a too-long message still contains
    # something Ayman needs, and refusing it outright would turn a
    # verbose agent into a silent one. Announce the truncation in the
    # log, never in the speech.
    truncated = len(text) > MAX_CHARS
    if truncated:
        text = text[:MAX_CHARS].rstrip() + "."

    # BATCHED or IMMEDIATE, decided by the kind -- see return_queue.py.
    # `error` is not batchable and goes straight out: something failed,
    # and silence there is the failure class this project keeps finding.
    # Completions and blocked questions collect, so three agents
    # finishing together become one interruption instead of three.
    #
    # Note this does NOT delay anything that was already immediate. The
    # instant ack, refusals and cancel-window confirmations never come
    # through jarvis_say at all -- they call speak() directly, and are
    # untouched by this.
    import return_queue  # noqa: E402  (local: avoids a cycle at import time)

    if kind in return_queue.BATCHABLE_KINDS:
        queued = return_queue.enqueue(kind, text, team=team or None)
        if not queued["ok"]:
            # Never silently drop it. If the queue refuses for any
            # reason, speak it now -- a message that reaches Ayman late
            # or out of order is recoverable; one that vanishes is not.
            log_event("jarvis_say_queue_refused", kind=kind, reason=queued["reason"])
            speak(text, priority=KINDS[kind])
    else:
        speak(text, priority=KINDS[kind])

    log_event(
        "jarvis_say", kind=kind, team=team[:60], chars=len(text),
        truncated=truncated, priority=KINDS[kind], at=round(time.time(), 3),
        batched=kind in return_queue.BATCHABLE_KINDS,
    )
    return {"ok": True, "reason": ""}
