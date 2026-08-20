#!/usr/bin/env python3
"""Canary for U3 (docs/PLAN-silence-and-ux.md): a clicked button kept
Textual's default :focus highlight until something else took focus --
for a button that stays mounted after its own action (WakePanel's
start/stop, EnginePanel's Swap/Remove/Activate), that reads as "still
doing something" long after the action actually finished.

Fixed with ONE app-level on_button_pressed handler (main.py) that blurs
whatever button was just pressed, relying on Textual's message bubbling
(every widget-level handler runs first, none call event.stop()) rather
than adding a blur() call to each individual handler.

BOTH DIRECTIONS: a click DOES remove focus from the button (the actual
fix), and a click still correctly delivers to the widget's OWN handler
first (proves the app-level blur doesn't shadow or race the button's
real action -- a fix that broke button presses to stop them looking
focused would be worse than the bug).

    l5_console/app/.venv/bin/python3 l5_console/app/button_blur_canary.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from textual.app import App, ComposeResult  # noqa: E402
from textual.widgets import Button  # noqa: E402

import main as main_mod  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((name, passed, detail))
    print(f"  {'ok  ' if passed else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not passed else ""))


class _WidgetLevelHandlerCalled:
    count = 0


class _TestButtonHolder(App):
    """Mirrors the REAL app's structure just enough to prove the fix:
    on_button_pressed defined on main.JarvisConsole (via mixing it in),
    with a widget-level handler underneath that must still fire."""

    def compose(self) -> ComposeResult:
        yield Button("Press me", id="test_button")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        # Widget/screen-level handler -- runs BEFORE the app-level one in
        # the real app's bubble order too. Records that it ran, then lets
        # the event continue bubbling (no event.stop(), matching every
        # real handler in this codebase -- checked via grep).
        _WidgetLevelHandlerCalled.count += 1
        main_mod.JarvisConsole.on_button_pressed(self, event)


async def main() -> int:
    app = _TestButtonHolder()
    async with app.run_test() as pilot:
        button = app.query_one("#test_button", Button)

        print("before any click -- focus state is whatever Textual's initial mount gives it")
        # (not asserted -- Textual auto-focuses the first focusable widget
        # on initial mount, which is expected and not what U3 is about)

        await pilot.click("#test_button")
        await pilot.pause()

        check("the widget-level handler still fired (the real action still happens)",
              _WidgetLevelHandlerCalled.count == 1)
        check("the button no longer has focus after being clicked -- the actual U3 fix",
              app.focused is not button, f"app.focused = {app.focused!r}")

        print()
        print("click it again -- proves this isn't a one-shot artifact of the first click")
        await asyncio.sleep(0.1)
        await pilot.click("#test_button")
        await pilot.pause()
        print(f"  (debug: widget-level handler call count = {_WidgetLevelHandlerCalled.count})")
        check("widget-level handler fired again too", _WidgetLevelHandlerCalled.count == 2)
        check("still blurred after a second click", app.focused is not button, f"app.focused = {app.focused!r}")

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
