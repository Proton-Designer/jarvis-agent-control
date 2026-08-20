#!/usr/bin/env python3
"""Canary for the blocked-member render (SPEC-blockers.md SS6).

Exists because of what was found on 2026-08-20: every piece of the
blocked pipeline was built and working -- pane_state.py classifies
BLOCKED_QUESTION, blocked_state.py tracks the episode, teams.py
populates TeamMember.blocked_*, and poller._maybe_escalate_blocked()
SPEAKS it aloud -- and `grep blocked l5_console/app/*.py` returned
nothing. A session stopped and waiting on a human rendered as an
ordinary running agent, on the panel whose whole job is telling Ayman
where his attention is needed.

That is this project's signature failure in its purest form: computed
correctly, then dropped on the way to the surface. The pipeline was
never broken. Only the last inch was missing, and the last inch is the
only part Ayman can see.

BOTH DIRECTIONS ARE ASSERTED throughout, and that is the whole design of
this file rather than a formality. A test that only checks "a blocked
member renders differently" passes just as happily if EVERY member
renders as blocked -- which would be worse than the bug, because it
would train him to ignore the warning. So every check has a
not-blocked counterpart.

Pure functions only (format_helpers depends on rich + models), so this
needs no Textual app mounted -- same testability discipline as
_build_launch_cmd() and role_status_icon().

    l5_console/app/.venv/bin/python3 l5_console/app/blocked_render_canary.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "state"))

import format_helpers as fh  # noqa: E402
from models import TeamMember, LIVENESS_RUNNING, LIVENESS_STOPPED  # noqa: E402

FAILURES: list[str] = []


def check(desc: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ok    {desc}")
    else:
        FAILURES.append(desc)
        print(f"  FAIL  {desc}{('  -- ' + detail) if detail else ''}")


def member(**kw) -> TeamMember:
    base = dict(
        tmux="t", claude_session="u", liveness=LIVENESS_RUNNING,
        activity=None, is_lead=False,
        blocked_question=None, blocked_since=None, blocked_surfaced=False,
    )
    base.update(kw)
    return TeamMember(**base)


def run() -> int:
    print("is_blocked -- both directions")
    blocked = member(blocked_question="Should I use staging or production?",
                     blocked_since=time.time() - 240)
    busy = member(activity="busy")
    idle = member()
    stopped = member(liveness=LIVENESS_STOPPED)

    check("a member with a captured question is blocked", fh.is_blocked(blocked) is True)
    check("a BUSY member is NOT blocked -- busy resolves itself, blocked does not",
          fh.is_blocked(busy) is False)
    check("an idle member is NOT blocked", fh.is_blocked(idle) is False)
    check("a stopped member is NOT blocked", fh.is_blocked(stopped) is False)

    print()
    print("blocked_for -- duration is signal, not decoration")
    check("blocked member reports a duration", fh.blocked_for(blocked).startswith("blocked "),
          fh.blocked_for(blocked))
    check("4 minutes reads as minutes, not seconds", fh.blocked_for(blocked) == "blocked 4m",
          fh.blocked_for(blocked))
    check("a NOT-blocked member reports nothing at all -- never 'blocked 0s'",
          fh.blocked_for(busy) == "" and fh.blocked_for(idle) == "",
          f"busy={fh.blocked_for(busy)!r} idle={fh.blocked_for(idle)!r}")
    fresh = member(blocked_question="q?", blocked_since=time.time() - 5)
    check("a 5-second block still reports seconds (it may yet resolve itself)",
          fh.blocked_for(fresh) == "blocked 5s", fh.blocked_for(fresh))
    long = member(blocked_question="q?", blocked_since=time.time() - 7500)
    check("a 2-hour block reads in hours -- the case that most needs his attention",
          fh.blocked_for(long).startswith("blocked 2h"), fh.blocked_for(long))

    print()
    print("degradation -- a blocked member with no timestamp still reads as blocked")
    no_ts = member(blocked_question="q?", blocked_since=None)
    check("missing blocked_since degrades to 'blocked', never to silence",
          fh.blocked_for(no_ts) == "blocked", fh.blocked_for(no_ts))
    check("...and is still classified blocked", fh.is_blocked(no_ts) is True)

    print()
    print("the question survives verbatim -- it is DATA, never instructions")
    opts = member(blocked_question="Pick one: [1] staging  [2] production",
                  blocked_since=time.time())
    from console import _truncate  # noqa: E402
    rendered = _truncate(opts.blocked_question.replace("\n", " "), 68)
    check("a bracketed option list is NOT eaten (the PlainStatic/PlainLabel bug, third time)",
          "[1] staging" in rendered and "[2] production" in rendered, rendered)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"all {8 + 4} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(run())
