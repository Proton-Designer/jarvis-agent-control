#!/usr/bin/env python3
"""Canary for U1 (docs/PLAN-silence-and-ux.md): the meter rendered the
SAME "?????" alarm glyph for "no data yet, completely normal right after
pressing start" and "the meter has genuinely stopped updating" -- making
the real alarm meaningless because it fired on every ordinary startup.

BOTH DIRECTIONS, and a third: the starting-up window must be honestly
distinct from BOTH the calm "not listening" state AND the genuine alarm
-- not just "not alarming", its own label ("starting up...") so it
isn't mistaken for the meter being off either.

Drives the REAL Meter widget (mounted via Pilot -- this is a stateful
widget now, tracking wake_running transitions, so a pure-function test
would miss the actual bug class: state that persists incorrectly
across calls). Monkeypatches meter.STARTUP_GRACE_S to a small value
rather than sleeping the real 5s, so this stays fast.

    l5_console/app/.venv/bin/python3 l5_console/app/meter_canary.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from textual.app import App, ComposeResult  # noqa: E402

import meter as meter_mod  # noqa: E402
from meter import Meter  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((name, passed, detail))
    print(f"  {'ok  ' if passed else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not passed else ""))


@dataclass
class FakeWakeState:
    level: float
    state: str
    stale: bool


class _T(App):
    def compose(self) -> ComposeResult:
        yield Meter(id="m", width=10)


def rendered_text(widget) -> str:
    r = widget.render()
    return r.plain if hasattr(r, "plain") else str(r)


async def main() -> int:
    meter_mod.STARTUP_GRACE_S = 0.3  # real sleeps below stay fast

    app = _T()
    async with app.run_test() as pilot:
        m = app.query_one("#m", Meter)

        print("not running -- calm dots, never the alarm glyph")
        m.update_meter(False, None)
        await pilot.pause()
        text = rendered_text(m)
        check("shows dots, not '?'", "·" in text and "?" not in text, text)
        check("says not listening", "not listening" in text, text)

        print()
        print("JUST started, no data yet -- U1's actual bug: must NOT be the alarm glyph")
        m.update_meter(True, None)
        await pilot.pause()
        text = rendered_text(m)
        check("no '?' glyph during the startup grace window", "?" not in text, text)
        check("uses the calm dots, same character as 'not listening'", "·" in text, text)
        check("has its OWN label -- not mistaken for 'not listening' either", "starting up" in text, text)

        print()
        print("STILL no data after the grace window -- NOW it's the real alarm")
        time.sleep(0.5)  # past the monkeypatched 0.3s grace
        m.update_meter(True, None)
        await pilot.pause()
        text = rendered_text(m)
        check("the alarm glyph fires once genuinely overdue", "?" in text, text)
        check("says no data, not 'starting up' (the window is over)", "starting up" not in text, text)

        print()
        print("genuine stale data (real reading, but stopped updating) -- same alarm as above, unchanged")
        m.update_meter(True, FakeWakeState(level=0.0, state="IDLE", stale=True))
        await pilot.pause()
        text = rendered_text(m)
        check("the alarm glyph fires for genuine staleness", "?" in text)
        check("says STALE specifically, distinct wording from bare 'no data'", "STALE" in text, text)

        print()
        print("real, fresh data -- the actual bar renders, no glyph at all")
        m.update_meter(True, FakeWakeState(level=0.5, state="CAPTURING", stale=False))
        await pilot.pause()
        text = rendered_text(m)
        check("no '?' when data is genuinely live", "?" not in text, text)
        check("shows the dictating label", "dictating" in text, text)

        print()
        print("stop then start again -- the grace window resets, doesn't leak from the previous run")
        m.update_meter(False, None)
        await pilot.pause()
        m.update_meter(True, None)
        await pilot.pause()
        text = rendered_text(m)
        check("a FRESH start gets its own grace window, not instantly flagged stale from last time",
              "starting up" in text and "?" not in text, text)

    print()
    failures = [r for r in RESULTS if not r[1]]
    if failures:
        print(f"{len(failures)}/{len(RESULTS)} FAILED:")
        for name, _, detail in failures:
            print(f"  - {name}" + (f" ({detail})" if detail else ""))
        return 1
    print(f"all {len(RESULTS)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
