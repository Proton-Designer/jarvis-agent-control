#!/usr/bin/env python3
"""Canary for the identity_verified_at render (SPEC-teams.md SS2,
TODO-feature-queue.md item 2).

Exists because of the same failure shape as blocked_render_canary.py:
identity_verified_at is computed at teams.py:264, TeamMember carries it,
and `grep identity_verified_at l5_console/app/*.py` returned nothing --
a member could look "active" while its identity hadn't been re-confirmed
since before a restart, and the console had no way to say so.

BOTH DIRECTIONS ARE ASSERTED, same reasoning as blocked_render_canary.py:
a check that only proves "a stale member renders a note" passes just as
happily if EVERY member renders one, which would train Ayman to ignore
it. Every check has a fresh/never-verified counterpart.

THE JUDGMENT CALL THIS FILE PINS DOWN, so a future change to the
threshold or the None-handling is a deliberate edit here, not a silent
drift: identity_verified_at=None (never dispatched to) renders NOTHING --
there is no prior claim for a restart to have falsified, so flagging it
would be noise on every brand-new team. Only a STALE verification (one
that existed and has gone unrefreshed past IDENTITY_STALE_THRESHOLD_S)
renders a note. See format_helpers.identity_staleness_note()'s own
docstring for the full reasoning.

Pure functions only (format_helpers depends on rich + models), so this
needs no Textual app mounted.

    l5_console/app/.venv/bin/python3 l5_console/app/identity_staleness_canary.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "state"))

import format_helpers as fh  # noqa: E402
from models import TeamMember, LIVENESS_RUNNING  # noqa: E402

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
        activity=None, is_lead=False, identity_verified_at=None,
    )
    base.update(kw)
    return TeamMember(**base)


def run() -> int:
    print("never dispatched (identity_verified_at is None) -- silence, not noise")
    never = member(identity_verified_at=None)
    check("a never-dispatched member is NOT flagged stale", fh.is_identity_stale(never) is False)
    check("...and its note is empty, not 'unverified since None'", fh.identity_staleness_note(never) == "",
          fh.identity_staleness_note(never))

    print()
    print("freshly verified -- also silence, not noise")
    fresh = member(identity_verified_at=time.time() - 30)
    check("verified 30s ago is NOT stale", fh.is_identity_stale(fresh) is False)
    just_under = member(identity_verified_at=time.time() - (fh.IDENTITY_STALE_THRESHOLD_S - 5))
    check("verified just under the threshold is still NOT stale", fh.is_identity_stale(just_under) is False,
          fh.identity_staleness_note(just_under))

    print()
    print("stale -- past the threshold, the false-confident-active case")
    just_over = member(identity_verified_at=time.time() - (fh.IDENTITY_STALE_THRESHOLD_S + 60))
    check("verified just over the threshold IS stale", fh.is_identity_stale(just_over) is True)
    check("...and reports minutes", fh.identity_staleness_note(just_over).startswith("unverified since "),
          fh.identity_staleness_note(just_over))

    old = member(identity_verified_at=time.time() - 7500)  # ~2h05m
    check("a 2-hour-stale member reads in hours -- the case that most needs a glance",
          fh.identity_staleness_note(old).startswith("unverified since 2h"), fh.identity_staleness_note(old))

    print()
    print("liveness doesn't matter to this predicate -- staleness is about IDENTITY, not aliveness")
    stopped_and_stale = member(identity_verified_at=time.time() - 7200, liveness="stopped")
    check("a stopped-but-stale member still flags (it will need re-verification WHEN it comes back)",
          fh.is_identity_stale(stopped_and_stale) is True)

    print()
    print("shared predicate: Rail's count and Console's per-member note can never disagree")
    members = [never, fresh, just_over, old]
    stale_via_predicate = sum(1 for m in members if fh.is_identity_stale(m))
    stale_via_note = sum(1 for m in members if fh.identity_staleness_note(m) != "")
    check("is_identity_stale() and identity_staleness_note() agree on every member",
          stale_via_predicate == stale_via_note == 2, f"{stale_via_predicate} vs {stale_via_note}")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(run())
