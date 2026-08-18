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
from textual.widgets import Static

from format_helpers import COLOR_ACCENT, COLOR_DIM, COLOR_MUTED


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

    KEYBINDS = [("space", "stop listening"), ("a", "add team"), ("r", "reconnect"), ("t", "manage teams"), ("q", "quit")]

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
