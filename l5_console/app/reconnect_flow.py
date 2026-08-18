"""
Reconnect flow (SPEC-TUI.md §5.3): "For each stopped member: tmux
new-session -c <root> running claude --resume <uuid>. Verify each came
up. Report anything that didn't -- never report success for a partial
restore."

Wiring, not reimplementing: reconnect_team() (l5_console/state/
reconnect.py, gu2s6tnt's, verified live) does all of the above already,
including the never-partial-success rule. This screen is the picker
(which team) plus rendering their per-member result, nothing more.
"""
from __future__ import annotations

import sys
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Button, ListItem, ListView, Label

from widgets import PlainStatic

sys.path.insert(0, str(Path(__file__).parent.parent / "state"))
import reconnect as reconnect_state  # noqa: E402
import api as state  # noqa: E402
from models import LIVENESS_STOPPED  # noqa: E402


class ReconnectScreen(Screen):
    BINDINGS = [("escape", "close", "Close")]

    DEFAULT_CSS = """
    ReconnectScreen { align: center middle; background: $surface; }
    #reconnect_body { width: 70; height: auto; max-height: 90%; border: solid $primary; padding: 2; }
    #reconnect_body Button { margin-top: 1; }
    #reconnect_body ListView { height: auto; max-height: 12; margin-top: 1; }
    """

    def compose(self) -> ComposeResult:
        yield Vertical(id="reconnect_body")

    async def on_mount(self) -> None:
        await self._show_team_list()

    def action_close(self) -> None:
        self.app.pop_screen()

    def _body(self) -> Vertical:
        return self.query_one("#reconnect_body", Vertical)

    async def _clear(self) -> None:
        await self._body().remove_children()

    async def _show_team_list(self) -> None:
        body = self._body()
        current = state.get_state()
        # Only teams with at least one genuinely stopped (not lost)
        # member are reconnect candidates -- matches reconnect_team()'s
        # own filter, shown here too so the picker doesn't offer a
        # no-op choice.
        reconnectable = [t for t in current.teams if any(m.liveness == LIVENESS_STOPPED for m in t.members)]
        if not reconnectable:
            await body.mount(PlainStatic("No teams have stopped (reconnectable) members right now."))
            await body.mount(Button("Close", id="close"))
            return

        await body.mount(PlainStatic("Reconnect which team?"))
        listview = ListView(id="reconnect_team_list")
        await body.mount(listview)
        for t in reconnectable:
            stopped_count = sum(1 for m in t.members if m.liveness == LIVENESS_STOPPED)
            await listview.append(ListItem(Label(f"{t.id}  ({stopped_count} stopped)"), name=t.id))
        await body.mount(Button("Close", id="close"))

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id != "reconnect_team_list":
            return
        team_id = event.item.name
        await self._clear()
        await self._body().mount(PlainStatic(f"Reconnecting {team_id}..."))
        result = reconnect_state.reconnect_team(team_id)
        await self._show_result(team_id, result)

    async def _show_result(self, team_id: str, result: dict) -> None:
        await self._clear()
        body = self._body()
        if "error" in result:
            await body.mount(PlainStatic(f"⚠ {result['error']}"))
            await body.mount(Button("Close", id="close"))
            return
        if not result["results"]:
            await body.mount(PlainStatic(f"{team_id}: nothing needed reconnecting (already all running)."))
        elif result["ok"]:
            await body.mount(PlainStatic(f"✓ {team_id}: all {len(result['results'])} member(s) reconnected."))
        else:
            # Never report success for a partial restore (§5.3) -- shown
            # per-member, explicitly, not folded into one summary line.
            await body.mount(PlainStatic(f"⚠ {team_id}: NOT all members came back:"))
            for r in result["results"]:
                mark = "✓" if r["ok"] else "✗"
                await body.mount(PlainStatic(f"  {mark} {r['tmux']}: {r['detail']}"))
        await body.mount(Button("Close", id="close"))

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close":
            self.app.pop_screen()
