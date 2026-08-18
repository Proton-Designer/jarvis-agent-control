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

from textual.widgets import Static


class PlainStatic(Static):
    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("markup", False)
        super().__init__(*args, **kwargs)
