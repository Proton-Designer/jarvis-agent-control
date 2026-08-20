"""
Team management: select a team, see it in detail, act on it
(SPEC-teams.md §5's "selected-team highlight with actions in a compact
strip" -- Ayman's decision, modal stays modal, but the actions are one
click away from the team they act on, not buried in a per-row inline
control that would bury the information TeamsPanel exists to show).

Same discipline as engine_flow.py/setup_flow.py throughout: ListView/
Button only, explicit .focus() on every step (the Lead's focus-landmine
ruling), the empty-state-names-the-action convention TeamsPanel already
established, and every hint labelled with its effect.

Wiring, not reimplementing: every write goes through
l5_console/state/team_actions.py / team_context.py (gu2s6tnt's) --
set_lead, relocate_team_member, remove_team, remove_member,
capture_context. This module is the picker + sequencing between those
calls, same relationship setup_flow.py has with setup.py.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Button, ListItem, ListView

from widgets import PlainStatic, PlainLabel, arm_list
from format_helpers import liveness_icon, liveness_color, team_liveness, COLOR_ACCENT, COLOR_DIM, COLOR_WARN

sys.path.insert(0, str(Path(__file__).parent.parent / "state"))
import team_actions  # noqa: E402
import team_context  # noqa: E402
import api as state  # noqa: E402
from models import LIVENESS_RUNNING  # noqa: E402


class TeamManageScreen(Screen):
    BINDINGS = [("escape", "close_step", "Back/Close")]

    DEFAULT_CSS = """
    TeamManageScreen { align: center middle; background: $surface; }
    #team_manage_body { width: 78; height: auto; max-height: 90%; border: solid $primary; padding: 2; border-title-color: $text-muted; }
    #team_manage_body Button { margin-top: 1; }
    #team_manage_body ListView { height: auto; max-height: 12; margin-top: 1; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.team_id: str | None = None

    def compose(self) -> ComposeResult:
        yield Vertical(id="team_manage_body")

    async def on_mount(self) -> None:
        await self._show_team_list_step()

    def action_close_step(self) -> None:
        self.app.pop_screen()

    def _body(self) -> Vertical:
        return self.query_one("#team_manage_body", Vertical)

    async def _clear(self) -> None:
        await self._body().remove_children()

    def _current_team(self):
        current = state.get_state()
        return next((t for t in current.teams if t.id == self.team_id), None)

    # --- Step 1: which team ------------------------------------------------

    async def _show_team_list_step(self) -> None:
        await self._clear()
        body = self._body()
        body.border_title = "MANAGE TEAMS"
        current = state.get_state()

        if not current.teams:
            # Same empty-state discipline as TeamsPanel itself -- names
            # the action and the key, not "no teams" and nothing else.
            empty = PlainStatic("")
            await body.mount(empty)
            from rich.text import Text
            msg = Text("No teams yet -- press ", style=COLOR_DIM)
            msg.append("[a]", style=f"bold {COLOR_ACCENT}")
            msg.append(" to add one.", style=COLOR_DIM)
            empty.update(msg)
            close_button = Button("Close", id="close", variant="error")
            await body.mount(close_button)
            close_button.focus()
            return

        await body.mount(PlainStatic("Which team?"))
        listview = ListView(id="team_list")
        await body.mount(listview)
        for t in current.teams:
            tl = team_liveness(t)
            lead = next((m for m in t.members if m.is_lead), None)
            lead_note = f"  lead: {lead.tmux or '(not bound)'}" if lead else "  no lead"
            await listview.append(ListItem(PlainLabel(f"{liveness_icon(tl)} {t.id}{lead_note}"), name=t.id))
        await body.mount(Button("Close", id="close", variant="error"))
        arm_list(listview)

    async def _on_team_chosen(self, team_id: str) -> None:
        self.team_id = team_id
        await self._show_detail_step()

    # --- Step 2: detail + action strip --------------------------------------

    async def _show_detail_step(self) -> None:
        await self._clear()
        body = self._body()
        team = self._current_team()
        if team is None:
            await self._show_team_list_step()
            return
        body.border_title = f"TEAM — {team.id.upper()}"

        info = PlainStatic("")
        await body.mount(info)
        self._render_detail_info(info, team)

        result = PlainStatic("", id="team_manage_result")
        await body.mount(result)

        # One compact strip -- the actions this team has, not a per-row
        # control on every member (SPEC-teams.md §5: this is what makes
        # modal acceptable instead of inline).
        swap_button = Button("Swap Lead", id="swap_lead")
        await body.mount(swap_button)
        await body.mount(Button("Relocate a Member", id="relocate"))
        await body.mount(Button("Remove a Member", id="remove_member"))
        await body.mount(Button("Refresh Context", id="refresh_context"))
        # SPEC-gaps-and-build-plan.md §1.6: the label names the EFFECT of
        # pressing it (what you'll get), not the current state -- same
        # convention as every other hint in this app -- so a team that's
        # currently visible offers "Make Background", never a bare toggle
        # whose direction you'd have to infer.
        visibility_label = "Make Background (no window)" if team.visible else "Make Visible (open a window)"
        await body.mount(Button(visibility_label, id="toggle_visible"))
        await body.mount(Button("Remove Team", id="remove_team", variant="error"))
        await body.mount(Button("Back", id="back_to_list", variant="error"))
        swap_button.focus()

    def _render_detail_info(self, widget: PlainStatic, team) -> None:
        from rich.text import Text
        text = Text()
        text.append(f"{team.display_path or team.root}\n", style=COLOR_DIM)
        if not team.has_lead:
            text.append("⚠ no lead assigned\n", style=COLOR_WARN)
        elif not team.lead_reachable:
            text.append("⚠ lead unreachable\n", style=COLOR_WARN)
        for m in team.members:
            name = m.tmux or "(not bound)"
            badge = "LEAD " if m.is_lead else "     "
            text.append(f"{badge}", style=f"bold {COLOR_ACCENT}" if m.is_lead else COLOR_DIM)
            text.append(f"{liveness_icon(m.liveness)} {name}  {m.liveness}\n", style=liveness_color(m.liveness))
        if team.context_summary:
            ctx = team.context_summary.strip().splitlines()[0][:200]
            text.append(f"\n{ctx}", style=COLOR_DIM)
            if team.context_source == "agent":
                text.append("  (self-described, unverified)", style=COLOR_DIM)
        widget.update(text)

    # --- Swap Lead: picker over THIS team's own members --------------------

    async def _show_swap_lead_step(self) -> None:
        await self._clear()
        body = self._body()
        team = self._current_team()
        body.border_title = f"SWAP LEAD — {team.id.upper()}"
        await body.mount(PlainStatic("New lead? (this team's own members only)"))
        listview = ListView(id="swap_lead_list")
        await body.mount(listview)
        for m in team.members:
            label = (m.tmux or "(not bound)") + ("  (current lead)" if m.is_lead else "")
            await listview.append(ListItem(PlainLabel(label), name=m.claude_session))
        await body.mount(Button("Back", id="back_to_detail", variant="error"))
        arm_list(listview)

    async def _on_swap_lead_chosen(self, claude_session: str) -> None:
        result = team_actions.set_lead(self.team_id, claude_session)
        await self._show_detail_step()
        self.query_one("#team_manage_result", PlainStatic).update(
            f"{'✓' if result['ok'] else '⚠'} {result['detail']}"
        )

    # --- Relocate: picker over UNASSIGNED live sessions ---------------------

    async def _show_relocate_step(self) -> None:
        await self._clear()
        body = self._body()
        team = self._current_team()
        body.border_title = f"RELOCATE — {team.id.upper()}"
        current = state.get_state()
        if not current.unassigned:
            await body.mount(PlainStatic("No unassigned running sessions to relocate from."))
            back_button = Button("Back", id="back_to_detail", variant="error")
            await body.mount(back_button)
            back_button.focus()
            return
        await body.mount(PlainStatic("Which running session is this team's member, moved?"))
        listview = ListView(id="relocate_list")
        await body.mount(listview)
        for u in current.unassigned:
            await listview.append(ListItem(PlainLabel(f"{u.tmux}  {u.display_path or u.working_dir}"), name=u.tmux))
        await body.mount(Button("Back", id="back_to_detail", variant="error"))
        arm_list(listview)

    async def _on_relocate_chosen(self, tmux: str) -> None:
        result = await asyncio.to_thread(team_actions.relocate_team_member, self.team_id, tmux)
        await self._show_detail_step()
        self.query_one("#team_manage_result", PlainStatic).update(
            f"{'✓' if result['ok'] else '⚠'} {result['detail']}"
        )

    # --- Remove a member (never the lead -- swap first) --------------------

    async def _show_remove_member_step(self) -> None:
        await self._clear()
        body = self._body()
        team = self._current_team()
        body.border_title = f"REMOVE MEMBER — {team.id.upper()}"
        removable = [m for m in team.members if not m.is_lead]
        if not removable:
            await body.mount(PlainStatic(
                "Every remaining member is the lead -- swap the lead before removing it, "
                "or use Remove Team instead."
            ))
            back_button = Button("Back", id="back_to_detail", variant="error")
            await body.mount(back_button)
            back_button.focus()
            return
        await body.mount(PlainStatic("Remove which member? (detaches only -- never kills the session)"))
        listview = ListView(id="remove_member_list")
        await body.mount(listview)
        for m in removable:
            await listview.append(ListItem(PlainLabel(m.tmux or "(not bound)"), name=m.claude_session))
        await body.mount(Button("Back", id="back_to_detail", variant="error"))
        arm_list(listview)

    async def _on_remove_member_chosen(self, claude_session: str) -> None:
        result = team_actions.remove_member(self.team_id, claude_session)
        await self._show_detail_step()
        self.query_one("#team_manage_result", PlainStatic).update(
            f"{'✓' if result['ok'] else '⚠'} {result['detail']}"
        )

    # --- Remove team / Refresh context: direct actions, no sub-picker ------

    async def _do_remove_team(self) -> None:
        result = team_actions.remove_team(self.team_id)
        if result["ok"]:
            await self._show_team_list_step()
        else:
            await self._show_detail_step()
            self.query_one("#team_manage_result", PlainStatic).update(f"⚠ {result['detail']}")

    async def _do_refresh_context(self) -> None:
        result_widget = self.query_one("#team_manage_result", PlainStatic)
        result_widget.update("Refreshing context... this can take a while (a real turn to the lead).")
        # capture_context() delivers a real instruction and waits for a
        # real reply when it can't shortcut via CLAUDE.md -- plain
        # blocking call (transport.deliver + a poll loop), not
        # asyncio-native, so it runs in a thread like every other
        # multi-second state-layer call in this app.
        result = await asyncio.to_thread(team_context.capture_context, self.team_id, True)
        await self._show_detail_step()
        self.query_one("#team_manage_result", PlainStatic).update(
            f"{'✓' if result['ok'] else '⚠'} {result['detail']}"
        )

    # --- Dispatch ------------------------------------------------------------

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        list_id = event.list_view.id
        name = event.item.name
        if list_id == "team_list":
            await self._on_team_chosen(name)
        elif list_id == "swap_lead_list":
            await self._on_swap_lead_chosen(name)
        elif list_id == "relocate_list":
            await self._on_relocate_chosen(name)
        elif list_id == "remove_member_list":
            await self._on_remove_member_chosen(name)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "close":
            self.app.pop_screen()
        elif bid == "back_to_list":
            await self._show_team_list_step()
        elif bid == "back_to_detail":
            await self._show_detail_step()
        elif bid == "swap_lead":
            await self._show_swap_lead_step()
        elif bid == "relocate":
            await self._show_relocate_step()
        elif bid == "remove_member":
            await self._show_remove_member_step()
        elif bid == "refresh_context":
            await self._do_refresh_context()
        elif bid == "remove_team":
            await self._do_remove_team()
