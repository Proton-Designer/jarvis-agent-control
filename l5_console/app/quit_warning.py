"""
Quit-while-listening warning (the Lead's ruling, 2026-08-18): quitting
the console does not stop daemon.py -- they are separate processes by
design, so the microphone can genuinely keep listening after the window
closes. Silently letting that happen is exactly the trust failure the
visible-indicator work exists to prevent. Warn explicitly rather than
auto-stop -- killing an in-progress dictation because the window closed
would be its own surprise, so the choice is the user's, made informed.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Button

from widgets import PlainStatic


class QuitWarningScreen(Screen):
    BINDINGS = [("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    QuitWarningScreen { align: center middle; background: $surface; }
    #quit_warning_body { width: 60; height: auto; border: solid $warning; padding: 2; border-title-color: $text-muted; }
    #quit_warning_body Button { margin-top: 1; margin-right: 1; }
    #quit_warning_buttons { height: auto; }
    """

    def compose(self) -> ComposeResult:
        body = Vertical(id="quit_warning_body")
        body.border_title = "MIC IS STILL LISTENING"
        with body:
            yield PlainStatic(
                "Quitting this window does not stop the wake-word "
                "daemon -- it will keep listening after the console "
                "closes."
            )
            with Vertical(id="quit_warning_buttons"):
                yield Button("Quit anyway (mic keeps listening)", id="quit_anyway", variant="error")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    def action_cancel(self) -> None:
        self.app.pop_screen()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "quit_anyway":
            self.app.exit()
        elif event.button.id == "cancel":
            self.app.pop_screen()
