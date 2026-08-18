"""
Setup flows (SPEC-TUI.md §5.1/§5.2): adopting existing sessions into a
team, or starting a fresh one. One flow, one fork, two questions, per
the spec -- which kind, then who receives (identical screen either
way), then what to call it. Reconnect (§5.3) is in reconnect_flow.py.

Wiring, not reimplementing (the Lead's instruction): every action that
touches tmux, ~/.claude.json, or teams.json goes through
l5_console/state/setup.py's functions (gu2s6tnt's, verified live on
their side) -- this module is UI plus the sequencing between those
calls, never a second implementation of adoption/creation logic.

One Screen with internal steps, not a Screen per step -- keeps this
flow's accumulated data (chosen root, candidate list, chosen inbox,
team id/aliases) in one place instead of threading it through screen
constructors and dismiss callbacks, which would be more Textual-idiomatic
for a longer wizard but adds real complexity this flow's actual size
doesn't need.

"No terminal work at any point" (§5.2) -- every step here is a widget,
never a shell-out the user has to watch or intervene in outside this
screen.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Input, ListItem, ListView, Label

from widgets import PlainStatic

sys.path.insert(0, str(Path(__file__).parent.parent / "state"))
import setup as setup_state  # noqa: E402
import teams as teams_state  # noqa: E402
import api as state  # noqa: E402

FRESH_TEAM_MODELS = ["sonnet", "opus"]


def _slugify(text: str) -> str:
    """team id from a directory name or typed text -- lowercase,
    hyphenated, alnum only. Not fancy; teams.py's create_team() rejects a
    collision explicitly, so a merely-reasonable slug is enough, the
    real correctness check happens there, not here."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "team"


class SetupScreen(Screen):
    """The whole add-a-team wizard. Pushed onto the app's screen stack
    (main.py binds a key to it) -- overlays whichever of Rail/Console/
    Signal is currently showing, same as any modal Textual screen."""

    BINDINGS = [("escape", "cancel_step", "Back/Cancel")]

    DEFAULT_CSS = """
    SetupScreen { align: center middle; background: $surface; }
    #setup_body { width: 70; height: auto; max-height: 90%; border: solid $primary; padding: 2; border-title-color: $text-muted; }
    #setup_body Button { margin-top: 1; }
    #setup_body Input { margin-top: 1; }
    #setup_body ListView { height: auto; max-height: 12; margin-top: 1; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.kind: str | None = None  # "adopt" | "fresh"
        self.root: str | None = None
        self.candidates: list[dict] = []  # {"tmux", "claude_session", "model", "summary"}
        self.inbox_tmux: str | None = None
        self.fresh_model: str = FRESH_TEAM_MODELS[0]

    def compose(self) -> ComposeResult:
        yield Vertical(id="setup_body")

    async def on_mount(self) -> None:
        await self._show_kind_step()

    def action_cancel_step(self) -> None:
        self.app.pop_screen()

    def _body(self) -> Vertical:
        return self.query_one("#setup_body", Vertical)

    async def _clear(self) -> None:
        await self._body().remove_children()

    # --- Step 1: which kind (§5.1 step 1) ---------------------------------

    async def _show_kind_step(self) -> None:
        await self._clear()
        body = self._body()
        body.border_title = "STEP 1 — WHICH"
        adopt_button = Button("Adopt agents already running", id="kind_adopt")
        await body.mount(adopt_button)
        await body.mount(Button("Start a fresh team", id="kind_fresh"))
        await body.mount(Button("Cancel", id="cancel", variant="error"))
        adopt_button.focus()

    async def _on_kind_chosen(self, kind: str) -> None:
        self.kind = kind
        if kind == "adopt":
            await self._show_adopt_group_step()
        else:
            await self._show_fresh_params_step()

    # --- Adopt path: group live unassigned sessions by directory ----------

    async def _show_adopt_group_step(self) -> None:
        await self._clear()
        body = self._body()
        body.border_title = "STEP 1 — ADOPT · WHICH DIRECTORY"
        current = state.get_state()
        groups: dict[str, list] = {}
        for u in current.unassigned:
            groups.setdefault(u.working_dir, []).append(u)

        if not groups:
            await body.mount(PlainStatic(
                "No unassigned running sessions found. Start a session first, "
                "or use 'Start a fresh team' instead."
            ))
            back_button = Button("Back", id="back_to_kind")
            await body.mount(back_button)
            back_button.focus()
            return

        await body.mount(PlainStatic("Which directory? (live sessions grouped by working directory)"))
        listview = ListView(id="adopt_group_list")
        await body.mount(listview)
        self._adopt_groups = groups  # keyed by working_dir, read back in on_list_view_selected
        for working_dir, sessions in groups.items():
            names = ", ".join(s.tmux for s in sessions)
            await listview.append(ListItem(Label(f"{working_dir}  ({len(sessions)}: {names})"), name=working_dir))
        await body.mount(Button("Back", id="back_to_kind"))
        listview.focus()

    async def _on_adopt_group_chosen(self, working_dir: str) -> None:
        self.root = working_dir
        sessions = self._adopt_groups[working_dir]
        await self._show_loading("Reading each candidate's status and self-written summary...")
        # adopt_candidate_info() is an intrusive ~1s /status round-trip
        # PER candidate (setup.py's own docstring) -- adoption-time only,
        # never on a poll, exactly the case it's built for.
        self.candidates = [setup_state.adopt_candidate_info(s.tmux) for s in sessions]
        await self._show_inbox_step()

    # --- Fresh path: directory, count, model, then launch -----------------

    async def _show_fresh_params_step(self) -> None:
        await self._clear()
        body = self._body()
        body.border_title = "STEP 1 — FRESH · TARGET DIRECTORY"
        await body.mount(PlainStatic("Target directory:"))
        root_input = Input(placeholder="/path/to/project", id="fresh_root")
        await body.mount(root_input)
        await body.mount(PlainStatic("How many agents?"))
        await body.mount(Input(placeholder="1", id="fresh_count"))
        await body.mount(PlainStatic(f"Model for all agents (default: {self.fresh_model}):"))
        await body.mount(Input(placeholder="sonnet or opus", id="fresh_model_input"))
        await body.mount(Button("Create", id="fresh_submit", variant="success"))
        await body.mount(Button("Back", id="back_to_kind"))
        root_input.focus()

    async def _on_fresh_submit(self) -> None:
        body = self._body()
        root = self.query_one("#fresh_root", Input).value.strip()
        count_text = self.query_one("#fresh_count", Input).value.strip() or "1"
        model_text = self.query_one("#fresh_model_input", Input).value.strip() or self.fresh_model

        if not root:
            await body.mount(PlainStatic("⚠ target directory is required"))
            return
        try:
            count = int(count_text)
            if count < 1:
                raise ValueError
        except ValueError:
            await body.mount(PlainStatic("⚠ number of agents must be a positive whole number"))
            return
        if model_text not in FRESH_TEAM_MODELS:
            await body.mount(PlainStatic(f"⚠ model must be one of: {', '.join(FRESH_TEAM_MODELS)}"))
            return

        self.root = root
        self.fresh_model = model_text
        await self._launch_fresh_members(root, count, model_text)

    async def _launch_fresh_members(self, root: str, count: int, model: str) -> None:
        await self._show_loading(f"Launching {count} agent(s) in {root}...")
        base = _slugify(Path(root).name)
        results = []
        for i in range(1, count + 1):
            tmux_name = f"claude-{base}-{i}" if count > 1 else f"claude-{base}"
            result = setup_state.create_fresh_member(tmux_name, root, model)
            results.append((tmux_name, result))

        failed = [(name, r) for name, r in results if not r["ok"]]
        if failed:
            body = self._body()
            await self._clear()
            await body.mount(PlainStatic("⚠ Not every agent came up cleanly:"))
            for name, r in failed:
                await body.mount(PlainStatic(f"  {name}: {r['detail']}"))
            succeeded = [(name, r) for name, r in results if r["ok"]]
            continue_button = None
            if succeeded:
                await body.mount(PlainStatic(f"{len(succeeded)} did come up -- you can still form a team from those, or cancel."))
                self.candidates = [
                    {"tmux": name, "claude_session": r["claude_session"], "model": model, "summary": "(freshly created, no summary yet)"}
                    for name, r in succeeded
                ]
                continue_button = Button("Continue with the ones that came up", id="continue_partial")
                await body.mount(continue_button)
            cancel_button = Button("Cancel", id="cancel", variant="error")
            await body.mount(cancel_button)
            (continue_button or cancel_button).focus()
            return

        self.candidates = [
            {"tmux": name, "claude_session": r["claude_session"], "model": model, "summary": "(freshly created, no summary yet)"}
            for name, r in results
        ]
        await self._show_inbox_step()

    # --- Step 2: who receives (§5.1 step 2, identical either path) --------

    async def _show_inbox_step(self) -> None:
        await self._clear()
        body = self._body()
        body.border_title = "STEP 2 — WHO RECEIVES INSTRUCTIONS"
        listview = ListView(id="inbox_list")
        await body.mount(listview)
        for c in self.candidates:
            summary = c["summary"] or "(no summary available)"
            await listview.append(
                ListItem(Label(f"{c['tmux']}  [{c['model']}]  {summary}"), name=c["tmux"])
            )
        await body.mount(Button("Back", id="cancel", variant="error"))
        listview.focus()

    async def _on_inbox_chosen(self, tmux_name: str) -> None:
        self.inbox_tmux = tmux_name
        await self._show_alias_step()

    # --- Step 3: what do you call it (§5.1 step 3) -------------------------

    async def _show_alias_step(self) -> None:
        await self._clear()
        body = self._body()
        body.border_title = "STEP 3 — WHAT DO YOU CALL IT"
        default_id = _slugify(Path(self.root).name) if self.root else "team"
        await body.mount(PlainStatic("Team id (short, no spaces):"))
        id_input = Input(value=default_id, id="team_id_input")
        await body.mount(id_input)
        await body.mount(PlainStatic("Spoken aliases (comma-separated -- how you'll refer to it by voice):"))
        await body.mount(Input(placeholder=f"the {default_id} project, {default_id}", id="aliases_input"))
        await body.mount(Button("Create team", id="finish_submit", variant="success"))
        await body.mount(Button("Back", id="cancel", variant="error"))
        id_input.focus()

    async def _on_alias_submit(self) -> None:
        body = self._body()
        team_id = _slugify(self.query_one("#team_id_input", Input).value.strip())
        aliases_raw = self.query_one("#aliases_input", Input).value.strip()
        aliases = [a.strip() for a in aliases_raw.split(",") if a.strip()] or [team_id]

        members = [{"tmux": c["tmux"], "claude_session": c["claude_session"]} for c in self.candidates]
        try:
            setup_state.create_team(team_id, aliases, self.root, self.inbox_tmux, members)
        except ValueError as e:
            await body.mount(PlainStatic(f"⚠ {e}"))
            return

        await self._clear()
        await body.mount(PlainStatic(f"✓ team {team_id!r} created, inbox: {self.inbox_tmux}"))
        done_button = Button("Done", id="cancel", variant="success")
        await body.mount(done_button)
        done_button.focus()

    # --- Shared helpers -----------------------------------------------------

    async def _show_loading(self, message: str) -> None:
        await self._clear()
        await self._body().mount(PlainStatic(message))

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "kind_adopt":
            await self._on_kind_chosen("adopt")
        elif bid == "kind_fresh":
            await self._on_kind_chosen("fresh")
        elif bid == "back_to_kind":
            await self._show_kind_step()
        elif bid == "fresh_submit":
            await self._on_fresh_submit()
        elif bid == "continue_partial":
            await self._show_inbox_step()
        elif bid == "finish_submit":
            await self._on_alias_submit()
        elif bid == "cancel":
            self.app.pop_screen()

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        list_id = event.list_view.id
        chosen_name = event.item.name
        if list_id == "adopt_group_list":
            await self._on_adopt_group_chosen(chosen_name)
        elif list_id == "inbox_list":
            await self._on_inbox_chosen(chosen_name)
