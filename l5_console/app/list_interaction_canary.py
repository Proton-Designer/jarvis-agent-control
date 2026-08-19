#!/usr/bin/env python3
"""Canary for the two render/interaction bugs found on 2026-08-19 by
driving the real console, neither of which any existing canary could
have caught: every canary in this project tests a state module, and both
of these bugs lived entirely between correct state and the screen.

1. arm_list -- a ListView built by appending ListItems AFTER mount has
   index None, and Enter selects the HIGHLIGHTED child, so the first
   Enter was swallowed in silence in all 14 of this app's list pickers.
   Observed live: open Manage Teams, press Enter on the only team,
   nothing happens; press Down (which cannot move in a one-item list)
   and Enter then works.

2. PlainLabel -- Label defaults to markup=True, so Rich parsed
   "jarvis-orchestrator  [orchestrator]" as a style tag and rendered the
   flag as nothing. The state layer was correct; the screen silently
   dropped the one field that says a session is already spoken for.

BOTH DIRECTIONS ARE ASSERTED for each, and that is the point rather than
a formality. A test that only checks "armed list selects on Enter" would
still pass if arm_list were deleted and Textual's default changed under
us; the unarmed row is what proves the arming is doing the work. Same
for markup: the bare-Label row is what proves markup=False is load-
bearing and not decoration.

Drives a real Textual app through its real pilot, real keypresses, real
message dispatch -- not a call to the handler function. The bug was that
a keypress produced nothing; only a keypress can prove it now produces
something.

Speaks nothing, touches no tmux session, reads and writes no registry.

    l5_console/app/.venv/bin/python3 l5_console/app/list_interaction_canary.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from textual.app import App, ComposeResult
from textual.widgets import Label, ListItem, ListView

from widgets import PlainLabel, arm_list

FAILURES: list[str] = []


def check(ok: bool, desc: str, detail: str = "") -> None:
    if ok:
        print(f"  ok    {desc}")
    else:
        FAILURES.append(desc)
        print(f"  FAIL  {desc}{('  -- ' + detail) if detail else ''}")


class _ListApp(App):
    """Two lists, identical except for how they were readied: one via
    arm_list, one via a bare .focus() the way every call site used to."""

    def __init__(self) -> None:
        super().__init__()
        self.selected: list[str] = []

    def compose(self) -> ComposeResult:
        yield ListView(id="armed")
        yield ListView(id="bare")

    async def on_mount(self) -> None:
        for lid in ("armed", "bare"):
            lv = self.query_one(f"#{lid}", ListView)
            # Appended AFTER mount -- the exact construction order every
            # flow in this app uses, and the one that leaves index None.
            await lv.append(ListItem(PlainLabel("only-row"), name=f"{lid}-row"))

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.selected.append(event.item.name or "?")


async def _run() -> None:
    print("arm_list -- the dead first Enter")
    app = _ListApp()
    async with app.run_test() as pilot:
        bare = app.query_one("#bare", ListView)
        armed = app.query_one("#armed", ListView)

        bare.focus()
        await pilot.pause()
        check(bare.index is None,
              "a list readied with a bare .focus() has NO highlight (index is None)",
              f"index={bare.index!r}")
        await pilot.press("enter")
        await pilot.pause()
        check(app.selected == [],
              "...and its first Enter selects nothing -- the original bug, reproduced",
              f"selected={app.selected!r}")

        arm_list(armed)
        await pilot.pause()
        check(armed.index == 0,
              "arm_list highlights the first row (index 0)",
              f"index={armed.index!r}")
        await pilot.press("enter")
        await pilot.pause()
        check(app.selected == ["armed-row"],
              "...and its FIRST Enter selects, with no Down needed",
              f"selected={app.selected!r}")

    print()
    print("arm_list -- the empty case")
    app2 = _ListApp()
    async with app2.run_test() as pilot:
        empty = ListView()
        await app2.mount(empty)
        await pilot.pause()
        arm_list(empty)
        await pilot.pause()
        check(empty.index is None,
              "arm_list on an EMPTY list sets no index and does not raise",
              f"index={empty.index!r}")

    print()
    print("PlainLabel -- the swallowed flag")
    app3 = _ListApp()
    async with app3.run_test() as pilot:
        flagged = "jarvis-orchestrator  [orchestrator]"
        plain = PlainLabel(flagged)
        marked = Label(flagged)
        await app3.mount(plain)
        await app3.mount(marked)
        await pilot.pause()

        plain_text = plain.render().plain if hasattr(plain.render(), "plain") else str(plain.render())
        marked_text = marked.render().plain if hasattr(marked.render(), "plain") else str(marked.render())

        check("[orchestrator]" in plain_text,
              "PlainLabel keeps the role flag verbatim",
              f"rendered={plain_text!r}")
        check("[orchestrator]" not in marked_text,
              "a bare Label STILL eats it -- so markup=False is load-bearing, not decoration",
              f"rendered={marked_text!r}")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    asyncio.run(_run())
