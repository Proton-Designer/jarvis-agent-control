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
to; only the three events below are worth interrupting a person for.

## What this does NOT do yet

Speak IMMEDIATELY, one utterance per call. The batching and end-of-
dictation flush policy (SPEC-blockers.md §5.3, generalised to all return
traffic in SPEC-orchestration.md §2.3) is deliberately NOT built here
yet: it needs the dictation lifecycle to hang off, and wiring that comes
with the daemon->concierge change. Priority ordering is already real
though -- say_feedback's queue is priority-then-FIFO, so an escalation
submitted behind three completions still goes first.

Callers should therefore treat this as "say this when you can," not "say
this now" -- the queue may hold it behind higher-priority speech, which
is the intended behaviour and not a bug to route around.
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
    kind: one of blocked_question | error | completion. Required and
        closed -- see the module docstring for why this is not free prose.
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

    speak(text, priority=KINDS[kind])
    log_event(
        "jarvis_say", kind=kind, team=team[:60], chars=len(text),
        truncated=truncated, priority=KINDS[kind], at=round(time.time(), 3),
    )
    return {"ok": True, "reason": ""}
