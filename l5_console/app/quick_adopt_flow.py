"""
Quick-adopt: one keystroke from "there's an unassigned session" to "it's a
team" (SPEC-TUI.md SS6: "a running session Jarvis cannot reach is how you
discover something to adopt, and assigning it should be one keystroke" --
TODO-feature-queue.md item 3).

Distinct from SetupScreen (setup_flow.py), not a replacement for it.
SetupScreen is the deliberate, full-control path -- multiple members,
picking who leads, choosing a real name -- and stays exactly as it is.
This is the fast path for the overwhelmingly common single-session case:
pick the session, done. `[a]` still opens the full wizard; `[u]` opens
this.

STILL ROUTES THROUGH THE SAME CONFLICT CHECKS the Lead wired into
setup_flow.py in c2e8468 -- a fast path that skipped validation would
reintroduce exactly the bug that commit fixed (real sessions launched, or
here, real sessions adopted, before a conflict is discovered). The only
thing this path removes is INPUT, never validation: same
check_root_available/check_team_id_available/check_alias_available,
called before create_team() same as the full wizard, refusing at pick
time with the same "name the conflicting team" detail rather than a
generic failure.

Defaults, since there's no naming step: team id is the directory's own
name, slugified (setup_flow._slugify -- imported, not duplicated, same
established precedent as team_actions.py importing teams._lead_claude_session
across module boundaries); alias is the same string. A collision on
EITHER auto-suffixes (-2, -3, ...) up to a bounded number of attempts
before giving up and naming the collision explicitly -- an automatic
default silently colliding and refusing outright would turn "one
keystroke" into "one keystroke, unless someone else already used this
folder name," which defeats the point for the case that matters (Ayman
has multiple projects with the same leaf directory name, e.g. two
different "api" checkouts).

No effort collected here, same as any adoption (TODO-feature-queue.md
item 2, SPEC-teams.md SS6): /status doesn't expose it, so it stays None,
never invented.
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

sys.path.insert(0, str(Path(__file__).parent.parent / "state"))
import setup as setup_state  # noqa: E402
import team_actions  # noqa: E402
import api as state  # noqa: E402

from setup_flow import _slugify  # noqa: E402

_MAX_SLUG_ATTEMPTS = 20


def _first_available_slug(base: str) -> dict:
    """Tries `base`, then `base-2`, `base-3`, ... until BOTH the team id
    and the alias are free (team_actions' own uniqueness checks -- the
    same ones create_team() enforces, called here so a collision is
    caught before adoption rather than after). Returns
    {"ok", "slug", "detail"} -- detail names the LAST conflict seen, for
    the bounded-failure case."""
    for i in range(1, _MAX_SLUG_ATTEMPTS + 1):
        candidate = base if i == 1 else f"{base}-{i}"
        id_check = team_actions.check_team_id_available(candidate)
        if not id_check["ok"]:
            last_detail = id_check["detail"]
            continue
        alias_check = team_actions.check_alias_available(candidate)
        if not alias_check["ok"]:
            last_detail = alias_check["detail"]
            continue
        return {"ok": True, "slug": candidate, "detail": ""}
    return {"ok": False, "slug": None, "detail": last_detail}


class QuickAdoptScreen(Screen):
    """One step: pick a session. Everything else is automatic."""

    BINDINGS = [("escape", "close", "Close")]

    DEFAULT_CSS = """
    QuickAdoptScreen { align: center middle; background: $surface; }
    #quick_adopt_body { width: 66; height: auto; max-height: 90%; border: solid $primary; padding: 2; border-title-color: $text-muted; }
    #quick_adopt_body Button { margin-top: 1; }
    #quick_adopt_body ListView { height: auto; max-height: 12; margin-top: 1; }
    """

    def compose(self) -> ComposeResult:
        body = Vertical(id="quick_adopt_body")
        body.border_title = "ADOPT A SESSION"
        yield body

    async def on_mount(self) -> None:
        await self._show_list_step()

    def action_close(self) -> None:
        self.app.pop_screen()

    def _body(self) -> Vertical:
        return self.query_one("#quick_adopt_body", Vertical)

    async def _clear(self) -> None:
        await self._body().remove_children()

    async def _show_list_step(self) -> None:
        await self._clear()
        body = self._body()
        current = state.get_state()
        if not current.unassigned:
            # Mirrors SetupScreen/TeamManageScreen's own empty-state
            # discipline: names what's missing and what to do about it,
            # not just "nothing here."
            await body.mount(PlainStatic(
                "No unassigned running sessions right now -- start one first, "
                "or press [a] to set up a fresh team instead."
            ))
            close_button = Button("Close", id="close", variant="error")
            await body.mount(close_button)
            close_button.focus()
            return

        await body.mount(PlainStatic("Adopt which session? (becomes a solo team, named after its directory)"))
        listview = ListView(id="quick_adopt_list")
        await body.mount(listview)
        for u in current.unassigned:
            await listview.append(
                ListItem(PlainLabel(f"{u.tmux}  {u.display_path or u.working_dir}"), name=u.tmux)
            )
        await body.mount(Button("Cancel", id="close", variant="error"))
        arm_list(listview)

    async def _on_session_chosen(self, tmux: str) -> None:
        current = state.get_state()
        selected = next((u for u in current.unassigned if u.tmux == tmux), None)
        if selected is None:
            # Vanished between listing and selection (session died, or
            # got adopted from another screen in the meantime) -- re-list
            # rather than act on stale data, same discipline as
            # engine_flow.AttachRoleScreen's identical race.
            await self._show_list_step()
            return

        # THE conflict check (same function, same message shape as
        # setup_flow.py's _show_conflict_step) -- BEFORE anything else,
        # same "refuse at pick time" rule c2e8462 established. Nothing
        # has been adopted yet at this point, so refusing here costs
        # nothing to undo.
        root_check = team_actions.check_root_available(selected.working_dir)
        if not root_check["ok"]:
            await self._show_conflict_step(root_check["detail"])
            return

        await self._clear()
        body = self._body()
        await body.mount(PlainStatic(f"Adopting {tmux}..."))
        # adopt_candidate_info() is the same intrusive ~1s /status round
        # trip every adoption already pays (setup_flow.py's own adopt
        # path) -- not new cost, and it's what gives us the model to
        # store, same as the full wizard.
        info = await asyncio.to_thread(setup_state.adopt_candidate_info, tmux)
        if info["claude_session"] is None:
            await self._clear()
            await body.mount(PlainStatic(f"⚠ {tmux} didn't report a session id -- is it a running Claude Code session?"))
            back_button = Button("Back", id="back_to_list", variant="error")
            await body.mount(back_button)
            back_button.focus()
            return

        base_slug = _slugify(Path(selected.working_dir).name)
        slug_result = _first_available_slug(base_slug)
        if not slug_result["ok"]:
            await self._clear()
            await body.mount(PlainStatic(
                f"⚠ Couldn't find a free name starting from {base_slug!r} after {_MAX_SLUG_ATTEMPTS} tries "
                f"({slug_result['detail']}). Use [a] to set up this team with a custom name instead."
            ))
            close_button = Button("Close", id="close", variant="error")
            await body.mount(close_button)
            close_button.focus()
            return

        team_id = slug_result["slug"]
        members = [{"tmux": tmux, "claude_session": info["claude_session"], "model": info.get("model")}]
        try:
            # visible=True (the default): a quick-adopted team is a
            # project agent, same as any other -- SPEC-gaps-and-build-
            # plan.md SS1.6's "visible by default" applies here exactly
            # as it does to the full wizard's fresh path.
            setup_state.create_team(team_id, [team_id], selected.working_dir, tmux, members)
        except ValueError as e:
            # A genuine race (something registered the same root/id
            # between the checks above and this call) -- rare, but the
            # checks are advisory and create_team() is the real gate, so
            # this path must still exist rather than trusting the checks
            # alone.
            await self._clear()
            await body.mount(PlainStatic(f"⚠ {e}"))
            close_button = Button("Close", id="close", variant="error")
            await body.mount(close_button)
            close_button.focus()
            return

        await self._clear()
        await body.mount(PlainStatic(f"✓ team {team_id!r} created from {tmux} -- 1 member, itself the lead."))
        done_button = Button("Done", id="close", variant="success")
        await body.mount(done_button)
        done_button.focus()

    async def _show_conflict_step(self, detail: str) -> None:
        await self._clear()
        body = self._body()
        body.border_title = "ALREADY REGISTERED"
        await body.mount(PlainStatic(detail))
        await body.mount(PlainStatic(""))
        await body.mount(PlainStatic("Nothing was adopted. Manage the existing team with [t], or pick a different session."))
        back = Button("Back", id="back_to_list", variant="error")
        await body.mount(back)
        back.focus()

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id == "quick_adopt_list":
            await self._on_session_chosen(event.item.name)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "close":
            self.app.pop_screen()
        elif bid == "back_to_list":
            await self._show_list_step()
