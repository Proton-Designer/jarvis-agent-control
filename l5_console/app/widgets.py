"""
PlainStatic: a Static that never interprets its content as Rich markup.

Real bug, found by testing, not theoretical: Textual's Static defaults
to `markup=True`, so `.update("claude-api-lead [inbox] -- busy")` silently
swallows "[inbox]" -- Rich parses `[inbox]` as a style tag (an unknown
one, so it renders as nothing) rather than literal text. Confirmed
directly: `rich.markup.render("... [inbox] ...")` returns the text with
that span stripped. This would have silently defeated the one thing this
whole app is required to make loud: "[STALE]" (SPEC-TUI.md §7, "better to
look broken while broken than healthy while stale") would have rendered
as nothing, the exact failure the requirement exists to prevent.

Every custom Static subclass in this app inherits from this instead of
Static directly, and every ad-hoc Static(...) instance uses this too --
one fix, structural, rather than remembering markup=False at each call
site (which is exactly the kind of convention-not-guarantee this
project's own governing rules argue against).
"""
from __future__ import annotations

from rich.text import Text
from textual.widgets import Label, Static

from format_helpers import COLOR_ACCENT, COLOR_DIM, COLOR_MUTED


class PlainLabel(Label):
    """Label with markup off. Same fix as PlainStatic above, for the
    widget that goes INSIDE a ListItem -- and it was missed there, which
    is how the bug this docstring exists for got back in.

    Found live 2026-08-19 by driving the real console. Engine > Swap
    lists every attachable tmux session and marks the ones already
    spoken for: `jarvis-orchestrator  [orchestrator]`. On screen both
    rows rendered BARE -- no flag at all -- so the two sessions that are
    already the concierge and the orchestrator looked exactly as free as
    the genuinely unused one. The state layer was right the whole time
    (list_attachable_sessions returns flag='orchestrator'); Rich ate the
    text on the way to the screen, parsing `[orchestrator]` as a style
    tag and rendering the span as nothing.

    Verified, not inferred: rich.markup.render('x  [orchestrator]').plain
    == 'x  '.

    Also hit setup_flow's lead picker, where `[haiku]` / `[sonnet]`
    vanished from the candidate rows -- so you chose which agent receives
    your instructions with the model column invisible.

    Both are the failure this project keeps finding in new clothes:
    information computed correctly, then dropped in silence, with the
    result reading as "there was nothing to show." Deliberately a
    subclass rather than markup=False at 16 call sites -- the convention
    is what failed last time."""

    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("markup", False)
        super().__init__(*args, **kwargs)


def arm_list(listview) -> None:
    """Highlight the first row, THEN focus. Use instead of a bare
    listview.focus() everywhere in this app.

    Found live 2026-08-19 by driving the real console: open Manage
    Teams, press Enter on the one and only team, and nothing happens.
    Press Down first -- which cannot move anywhere in a one-item list --
    and Enter then works. The Down press was not navigating, it was
    establishing a highlight that did not exist yet.

    Cause: a ListView built by appending ListItems AFTER mount has
    index None, and ListView's Enter action selects the HIGHLIGHTED
    child, so with no highlight there is nothing to select and the
    keypress is swallowed in silence. focus() gives the widget the
    keyboard; it does not give it a cursor.

    Every one of this app's 14 list pickers was built that way, so the
    first Enter was dead in every no-typed-input flow we have -- engine
    create, attach and swap, add team, the directory walker, model and
    effort pickers, swap lead, relocate, remove member, reconnect. The
    flows were correct; they just looked broken on contact, which for a
    keyboard-only console is the same thing.

    Guarded on children rather than assumed: setting index on an empty
    ListView is meaningless, and the empty case is already handled by
    each flow's own empty-state branch before it ever gets here."""
    if len(listview.children):
        listview.index = 0
    listview.focus()


class PlainStatic(Static):
    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("markup", False)
        super().__init__(*args, **kwargs)


class Footer(PlainStatic):
    """The keybind bar the Lead flagged as missing entirely (docs/
    console-design-studies.html). Shows this app's REAL bindings
    (main.py's BINDINGS), not a copy of the mockup's illustrative footer
    text, since the mockup covered a broader set of view-switching keys
    this build doesn't implement. Shared between rail.py and console.py
    so the two densities can't drift on what the keys actually do."""

    DEFAULT_CSS = "Footer { height: 1; color: $text-muted; }"

    # "tab" is listed even though it is not a BINDINGS entry -- it is
    # Textual's own focus movement, and it is the ONLY way to reach the
    # ENGINE panel's Swap/Remove buttons, which have no key of their own.
    # Driving the console live on 2026-08-19 that was invisible: the
    # footer advertised five keys, none of which touched the engine, so
    # the two buttons Ayman most needs read as mouse-only in a console
    # that is otherwise entirely keyboard-driven. Naming the effect
    # ("move between buttons"), not the mechanism, same as every other
    # entry here.
    KEYBINDS = [
        ("space", "stop listening"),
        ("tab", "move between buttons"),
        ("a", "add team"),
        ("u", "adopt a session"),
        ("r", "reconnect"),
        ("v", "revive everything"),
        ("t", "manage teams"),
        ("q", "quit"),
    ]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._wake_running = False

    def on_mount(self) -> None:
        self._render_footer()

    def update_wake_state(self, running: bool) -> None:
        # space is a real, always-registered binding (main.py's
        # action_panic_stop_wake) regardless of wake.running -- it's
        # never disabled, it's just a no-op while nothing is listening
        # (action_panic_stop_wake's own early-return). Muting the
        # footer entry when inert is cosmetic, not a change to what the
        # key does; found live 2026-08-18 that showing it at the same
        # weight as every other always-live key while stopped read as
        # "this will do something."
        if running == self._wake_running:
            return
        self._wake_running = running
        self._render_footer()

    def _render_footer(self) -> None:
        # Named _render_footer, not _render -- Widget._render() is a
        # REAL internal Textual method (returns a Visual for the
        # rendering pipeline). A same-named override here silently
        # replaced it, returning None instead of a Visual, and Textual's
        # own compositor crashed on the next render pass trying to call
        # .render_strips() on that None -- AttributeError: 'NoneType'
        # object has no attribute 'render_strips'. Same collision class
        # already found once this session (a method named `_attach` in
        # engine_flow.py shadowed Widget._attach()) -- underscore-
        # prefixed Textual internals are real API, not free names.
        text = Text()
        for i, (key, label) in enumerate(self.KEYBINDS):
            if i:
                text.append("   ")
            muted = key == "space" and not self._wake_running
            key_color = COLOR_MUTED if muted else f"bold {COLOR_ACCENT}"
            label_color = COLOR_MUTED if muted else COLOR_DIM
            text.append(f"[{key}]", style=key_color)
            text.append(f" {label}", style=label_color)
        self.update(text)
