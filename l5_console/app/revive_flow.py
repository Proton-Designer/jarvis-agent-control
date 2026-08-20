"""
Revive-everything flow (SPEC-gaps-and-build-plan.md SS1.7): one action,
one screen, a per-item result rendered AS EACH ITEM COMPLETES -- not a
spinner that goes silent for the tens of seconds this genuinely takes.
"Silence during that is the failure mode this project keeps hitting"
(the Lead's own framing for why this exists).

Wiring, not reimplementing: revive.list_revive_targets()/revive_target()/
summarize() (l5_console/state/revive.py) do all the actual work -- this
screen only drives the loop and renders. Each revive_target() call is a
plain blocking function (subprocess.run/wait_for_ready polling
internally, same as engine_roles.activate_role()/reconnect.reconnect_team()
it wraps), so each is awaited via asyncio.to_thread, same discipline
console.py's _do_activate() already uses -- never one blocking call for
the whole run, which would freeze the event loop for the entire duration
instead of just each item's own share of it.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button

from widgets import PlainStatic

sys.path.insert(0, str(Path(__file__).parent.parent / "state"))
import revive as revive_state  # noqa: E402


class ReviveScreen(Screen):
    BINDINGS = [("escape", "close", "Close")]

    DEFAULT_CSS = """
    ReviveScreen { align: center middle; background: $surface; }
    #revive_body { width: 84; height: auto; max-height: 90%; border: solid $primary; padding: 2; border-title-color: $text-muted; }
    #revive_log { height: auto; max-height: 16; margin-top: 1; }
    #revive_body Button { margin-top: 1; }
    """

    def compose(self) -> ComposeResult:
        body = Vertical(id="revive_body")
        body.border_title = "REVIVE EVERYTHING"
        yield body

    async def on_mount(self) -> None:
        await self._run_revive()

    def action_close(self) -> None:
        self.app.pop_screen()

    def _body(self) -> Vertical:
        return self.query_one("#revive_body", Vertical)

    async def _run_revive(self) -> None:
        body = self._body()
        await body.mount(PlainStatic("Reviving every attached role and registered team..."))
        log = VerticalScroll(id="revive_log")
        await body.mount(log)

        # list_revive_targets() is a cheap file read (engine.json's two
        # constant role slots + teams.json), no thread needed -- unlike
        # revive_target() below, which does real tmux/subprocess work per
        # item and can take up to the wait_for_ready timeout each.
        targets = revive_state.list_revive_targets()
        results = []
        for target in targets:
            row = PlainStatic(f"… {target.label}")
            await log.mount(row)
            log.scroll_end(animate=False)

            result = await asyncio.to_thread(revive_state.revive_target, target)
            results.append(result)

            mark = "·" if result["skipped"] else ("✓" if result["ok"] else "✗")
            row.update(f"{mark} {target.label}: {result['detail']}")
            log.scroll_end(animate=False)

        summary = revive_state.summarize(results)
        overall_ok = all(r["ok"] for r in results)
        await body.mount(PlainStatic(("✓ " if overall_ok else "⚠ ") + summary))

        close_button = Button("Close", id="close")
        await body.mount(close_button)
        close_button.focus()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close":
            self.app.pop_screen()
