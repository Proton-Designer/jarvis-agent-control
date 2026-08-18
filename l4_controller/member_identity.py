"""
Detects a team member whose Claude Code process restarted since it was
registered (SPEC-orchestration.md "Known gaps": a crashed-and-relaunched
pane is indistinguishable from an on-task one via tmux-name+cwd liveness
alone -- teams.member_liveness() deliberately never re-verifies
claude_session identity on every poll, because claude_session_id()/
`/status` is an intrusive ~1s round-trip that must never run on a polling
loop (see providers.claude_session_id's own docstring: "ADOPTION-TIME
ONLY. NEVER call this on a polling/liveness loop"). So this checks
identity ONLY at the one moment it actually matters and can afford the
cost: right before the router dispatches real work to a registered team
member, not on every poll tick, and not for every delivery target either
-- only ones that are actually registered team members (a raw/unregistered
or throwaway-test delivery target has no stored identity to compare
against and is skipped entirely, cheaply, with no /status call at all).

Self-heals FORWARD on a genuine restart: once detected, the registry's
stored claude_session is updated to the live one, so the SAME restart is
never re-flagged on a second dispatch to the same member -- it settles
into its new identity exactly like a fresh adoption would.

Never refuses delivery on a mismatch. A restarted session receiving a
NEW instruction is often perfectly fine -- the instruction may not
assume any prior context at all -- and blocking it would just be a new
failure mode of its own (a legitimate instruction silently withheld).
This only returns a flag for the caller to speak/log, so Ayman knows the
target's memory reset without the delivery itself being silently blocked
OR silently proceeding as though nothing happened -- the same "escalate,
don't guess" discipline as everywhere else in this project, applied to
identity instead of routing.
"""

from __future__ import annotations

import sys
from pathlib import Path

from providers import claude_session_id

sys.path.insert(0, str(Path(__file__).parent.parent / "l5_console" / "state"))
from teams import load_registry, save_registry  # noqa: E402


def verify_and_refresh_identity(session_id: str) -> dict:
    """session_id: the tmux/session name about to receive a delivery
    (e.g. deliver_batch's already-resolved target). Returns {"checked":
    bool, "restarted": bool, "detail": str}.

    checked=False means this target isn't a registered team member at
    all -- not every delivery target is (the router's own session, an
    ad-hoc/unregistered session, a throwaway test target) -- and this
    check only applies to ones with a stored identity to compare
    against. No /status call happens in that case."""
    registry = load_registry()
    for team in registry:
        for member in team.get("members", []):
            if member.get("tmux") != session_id:
                continue
            stored = member.get("claude_session")
            live = claude_session_id(session_id)
            if live is None:
                # Could not verify (pane not READY, /status didn't parse)
                # -- fails to "unchecked, no claim made," never asserts a
                # restart it couldn't actually confirm. The normal
                # pane-state gate in transport.deliver() still applies
                # independently regardless of this check's outcome.
                return {"checked": True, "restarted": False, "detail": "could not verify (pane unreadable)"}
            if live == stored:
                return {"checked": True, "restarted": False, "detail": ""}
            member["claude_session"] = live
            save_registry(registry)
            return {
                "checked": True,
                "restarted": True,
                "detail": f"{session_id}'s Claude session changed since it was registered "
                f"(was {stored}, now {live}) -- it has no memory of any prior work",
            }
    return {"checked": False, "restarted": False, "detail": "not a registered team member"}
