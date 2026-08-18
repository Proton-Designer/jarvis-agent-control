"""
Engine role Create/Attach flows (SPEC-engine-roles.md SS2, SS3, SS5).

No typed input anywhere except ONE pre-filled Input, used only for
naming/renaming (the Lead's ruling, 2026-08-18: a name is authored
content, not a bounded choice, so it is the one legitimate exception to
"every action is buttons or selection from a list" -- confirming without
editing accepts the pre-filled default). Every other step is a ListView
selection, matching reconnect_flow.py's pattern -- explicitly NOT
setup_flow.py's Input-heavy pattern, which the Lead said does not carry
over to this feature.

FOCUS DISCIPLINE, per the Lead's explicit landmine warning: Textual only
auto-focuses a Screen's PRIMARY widget on its initial mount. Every step
here is re-composed via remove_children()+mount() (setup_flow.py's own
pattern), so every _show_*_step() explicitly calls .focus() on its
primary widget at the end -- without it, a keystroke typed into what
LOOKS like a focused Input (e.g. a name containing "r" or "a") falls
through to the App-level BINDINGS instead of landing as text, the exact
bug setup_flow.py's docstring documents finding live. main.py's
check_action() (len(screen_stack) > 1 disables add_team/reconnect
globally) is defense in depth on top of this, not a substitute for it --
both must hold, and this module was verified live typing a name
containing both "r" and "a" (engine_flow_focus_canary.py).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Button, Input, Label, ListItem, ListView

from widgets import PlainStatic

sys.path.insert(0, str(Path(__file__).parent.parent / "state"))
import engine_roles  # noqa: E402
from setup import adopt_candidate_info  # noqa: E402


class CreateRoleScreen(Screen):
    """SS2: model -> effort -> name -> spawn, each a selection except the
    final pre-filled name field."""

    BINDINGS = [("escape", "close", "Close")]

    DEFAULT_CSS = """
    CreateRoleScreen { align: center middle; background: $surface; }
    #create_role_body { width: 62; height: auto; max-height: 90%; border: solid $primary; padding: 2; border-title-color: $text-muted; }
    #create_role_body Button { margin-top: 1; }
    #create_role_body ListView { height: auto; max-height: 10; margin-top: 1; }
    #create_role_body Input { margin-top: 1; }
    """

    def __init__(self, role: str) -> None:
        super().__init__()
        self.role = role
        self.model: str | None = None
        self.effort: str | None = None

    def compose(self) -> ComposeResult:
        body = Vertical(id="create_role_body")
        body.border_title = f"CREATE — {self.role.upper()}"
        yield body

    async def on_mount(self) -> None:
        await self._show_model_step()

    def action_close(self) -> None:
        self.app.pop_screen()

    def _body(self) -> Vertical:
        return self.query_one("#create_role_body", Vertical)

    async def _clear(self) -> None:
        await self._body().remove_children()

    async def _show_model_step(self) -> None:
        await self._clear()
        body = self._body()
        body.border_title = f"STEP 1 — {self.role.upper()} MODEL"
        await body.mount(PlainStatic("Which model?"))
        listview = ListView(id="model_list")
        await body.mount(listview)
        for m in engine_roles.MODELS:
            await listview.append(ListItem(Label(m.capitalize()), name=m))
        await body.mount(Button("Cancel", id="cancel", variant="error"))
        listview.focus()

    async def _show_effort_step(self) -> None:
        await self._clear()
        body = self._body()
        body.border_title = f"STEP 2 — {self.role.upper()} EFFORT"
        default = engine_roles.DEFAULT_EFFORT[self.role]
        await body.mount(PlainStatic("Effort?"))
        listview = ListView(id="effort_list")
        await body.mount(listview)
        for e in engine_roles.EFFORTS:
            label = e.capitalize() + ("  (recommended)" if e == default else "")
            await listview.append(ListItem(Label(label), name=e))
        await body.mount(Button("Back", id="back_to_model", variant="error"))
        listview.focus()

    async def _show_name_step(self) -> None:
        await self._clear()
        body = self._body()
        body.border_title = f"STEP 3 — {self.role.upper()} NAME"
        default_name = engine_roles.preview_default_name(self.role)
        await body.mount(PlainStatic("Name -- confirm the suggested default, or edit it:"))
        name_input = Input(value=default_name, id="name_input")
        await body.mount(name_input)
        await body.mount(Button("Create", id="finish_submit", variant="success"))
        await body.mount(Button("Back", id="back_to_effort", variant="error"))
        name_input.focus()

    async def _spawn(self, name: str) -> None:
        await self._clear()
        body = self._body()
        body.border_title = f"CREATING — {self.role.upper()}"
        await body.mount(PlainStatic(f"Creating {self.role} ({self.model}, {self.effort})... this can take 10-15s."))
        # create_fresh_member()/wait_for_ready() are plain blocking calls
        # (subprocess + time.sleep polling), not asyncio-native -- run in
        # a thread so the Screen stays responsive rather than freezing.
        result = await asyncio.to_thread(engine_roles.create_role_session, self.role, self.model, self.effort, name)
        await self._clear()
        if result["ok"]:
            await body.mount(PlainStatic(f"✓ {result['detail']}"))
        else:
            await body.mount(PlainStatic(f"⚠ {result['detail']}"))
        done_button = Button("Done", id="cancel", variant="success" if result["ok"] else "error")
        await body.mount(done_button)
        done_button.focus()

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id == "model_list":
            self.model = event.item.name
            await self._show_effort_step()
        elif event.list_view.id == "effort_list":
            self.effort = event.item.name
            await self._show_name_step()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "name_input":
            await self._spawn(event.value.strip() or engine_roles.preview_default_name(self.role))

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.app.pop_screen()
        elif event.button.id == "back_to_model":
            self.model = None
            await self._show_model_step()
        elif event.button.id == "back_to_effort":
            self.effort = None
            await self._show_effort_step()
        elif event.button.id == "finish_submit":
            name = self.query_one("#name_input", Input).value.strip()
            await self._spawn(name or engine_roles.preview_default_name(self.role))


class AttachRoleScreen(Screen):
    """SS3 (attach) and SS5 (swap -- "the same selection flow as
    attach"): pick a session -> conflict prompt (if already the other
    role) or optional rename (if unused) -> confirm."""

    BINDINGS = [("escape", "close", "Close")]

    DEFAULT_CSS = """
    AttachRoleScreen { align: center middle; background: $surface; }
    #attach_role_body { width: 66; height: auto; max-height: 90%; border: solid $primary; padding: 2; border-title-color: $text-muted; }
    #attach_role_body Button { margin-top: 1; }
    #attach_role_body ListView { height: auto; max-height: 12; margin-top: 1; }
    #attach_role_body Input { margin-top: 1; }
    """

    def __init__(self, role: str) -> None:
        super().__init__()
        self.role = role

    def compose(self) -> ComposeResult:
        body = Vertical(id="attach_role_body")
        body.border_title = f"ATTACH — {self.role.upper()}"
        yield body

    async def on_mount(self) -> None:
        await self._show_list_step()

    def action_close(self) -> None:
        self.app.pop_screen()

    def _body(self) -> Vertical:
        return self.query_one("#attach_role_body", Vertical)

    async def _clear(self) -> None:
        await self._body().remove_children()

    async def _show_list_step(self) -> None:
        await self._clear()
        body = self._body()
        body.border_title = f"STEP 1 — {self.role.upper()} WHICH SESSION"
        sessions = engine_roles.list_attachable_sessions()
        if not sessions:
            await body.mount(PlainStatic("No sessions found under ~/Jarvis/ to attach."))
            close_button = Button("Close", id="cancel", variant="error")
            await body.mount(close_button)
            close_button.focus()
            return

        await body.mount(PlainStatic("Attach which session? (most recent first)"))
        listview = ListView(id="session_list")
        await body.mount(listview)
        for s in sessions:
            flag_note = "" if s["flag"] == "unused" else f"  [{s['flag']}]"
            await listview.append(ListItem(Label(f"{s['tmux']}{flag_note}"), name=s["tmux"]))
        await body.mount(Button("Cancel", id="cancel", variant="error"))
        listview.focus()

    async def _show_conflict_step(self, tmux: str, other_role: str, detail: str) -> None:
        await self._clear()
        body = self._body()
        body.border_title = f"{self.role.upper()} -- ALREADY ATTACHED"
        # Rendered VERBATIM, never paraphrased -- same discipline as the
        # Start-button precondition (SS6): the value is naming the exact
        # other role, not a generic "can't do that."
        await body.mount(PlainStatic(detail))
        back_button = Button("Back", id="back_to_list", variant="error")
        await body.mount(back_button)
        back_button.focus()

    async def _show_rename_step(self, tmux: str, working_dir: str) -> None:
        await self._clear()
        body = self._body()
        body.border_title = f"STEP 2 — {self.role.upper()} NAME"
        # One /status round-trip, adoption-time only (same discipline
        # setup.adopt_candidate_info() already documents for team
        # adoption) -- gives claude_session (required by attach_role())
        # and model (best-effort record field) in the same capture, plus
        # a real ai-title summary to prefer over the bare tmux name as
        # the pre-filled default, when one exists.
        await body.mount(PlainStatic("Checking session..."))
        info = await asyncio.to_thread(adopt_candidate_info, tmux)
        await self._clear()
        body.border_title = f"STEP 2 — {self.role.upper()} NAME"

        if info["claude_session"] is None:
            await body.mount(PlainStatic(f"⚠ {tmux} didn't report a session id -- is it a running Claude Code session?"))
            back_button = Button("Back", id="back_to_list", variant="error")
            await body.mount(back_button)
            back_button.focus()
            return

        default_name = info.get("summary") or tmux
        await body.mount(PlainStatic("Name -- confirm the suggested default, or edit it:"))
        name_input = Input(value=default_name, id="name_input")
        await body.mount(name_input)
        confirm_button = Button("Attach", id="finish_submit", variant="success")
        await body.mount(confirm_button)
        await body.mount(Button("Back", id="back_to_list", variant="error"))
        # Stashed on the screen, not re-derived from the ListView
        # selection (already gone by this step) -- consumed by
        # _do_attach() below.
        self._pending = {"tmux": tmux, "working_dir": working_dir, "claude_session": info["claude_session"], "model": info.get("model")}
        name_input.focus()

    async def _do_attach(self, name: str) -> None:
        await self._clear()
        body = self._body()
        body.border_title = f"ATTACHING — {self.role.upper()}"
        await body.mount(PlainStatic("Attaching..."))
        pending = self._pending
        result = await asyncio.to_thread(
            engine_roles.attach_role,
            self.role, pending["tmux"], pending["working_dir"], pending["claude_session"],
            name, pending.get("model"), None,
        )
        await self._clear()
        if result["ok"]:
            await body.mount(PlainStatic(f"✓ {result['detail']}"))
        else:
            await body.mount(PlainStatic(f"⚠ {result['detail']}"))
        done_button = Button("Done", id="cancel", variant="success" if result["ok"] else "error")
        await body.mount(done_button)
        done_button.focus()

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id != "session_list":
            return
        tmux = event.item.name
        sessions = {s["tmux"]: s for s in engine_roles.list_attachable_sessions()}
        selected = sessions.get(tmux)
        if selected is None:
            await self._show_list_step()  # it vanished between listing and selection -- re-list rather than act on stale data
            return
        if selected["flag"] not in ("unused", self.role):
            detail = f"That session is already attached as the {selected['flag'].capitalize()}."
            await self._show_conflict_step(tmux, selected["flag"], detail)
            return
        await self._show_rename_step(tmux, selected["working_dir"])

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "name_input":
            await self._do_attach(event.value.strip() or self._pending["tmux"])

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.app.pop_screen()
        elif event.button.id == "back_to_list":
            await self._show_list_step()
        elif event.button.id == "finish_submit":
            name = self.query_one("#name_input", Input).value.strip()
            await self._do_attach(name or self._pending["tmux"])
