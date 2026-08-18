"""
Shared, layout-independent formatting logic -- used by both rail.py
(condensed) and console.py (fuller detail) so the two densities can't
silently disagree about what a liveness icon means or how a team's
overall state is derived. Factored out once needed in two places, same
reasoning as l2_5_concierge/session_match.py's own extraction.
"""
from __future__ import annotations

import sys
from pathlib import Path

from rich.text import Text

sys.path.insert(0, str(Path(__file__).parent.parent / "state"))
from models import LIVENESS_RUNNING, LIVENESS_STOPPED, LIVENESS_LOST  # noqa: E402

# Same palette as docs/console-design-studies.html's mockups (the Lead's
# "match the design, don't approximate it" instruction) -- exact hex
# values, not Textual theme tokens, because these are used inside Rich
# Text objects passed to Static.update(), which render independent of
# the app's CSS theme resolution.
COLOR_OK = "#46C07A"
COLOR_WARN = "#E0A34A"
COLOR_ERR = "#EE6055"
COLOR_ACCENT = "#4FD6E0"
COLOR_DIM = "#6B7688"
# A step below COLOR_DIM -- for a control that is currently inert, not
# merely secondary. Footer's "[space] stop listening" needs this
# distinction specifically: the binding is correct (space IS stop-only,
# by ruling), but showing it at the same weight as every other always-
# live key when wake isn't running reads as "this will do something" --
# the Lead's live finding, 2026-08-18.
COLOR_MUTED = "#4A5262"
COLOR_INK = "#C9D2DE"


def build_hint_line(hints: list[tuple[str, str]]) -> Text:
    """A row of "[key] effect" action hints -- bold accent key, dim
    effect label, matching Footer's own keybind-bar styling exactly
    (widgets.py). Shared so the global footer and any per-panel hint
    area (the Lead's ruling, 2026-08-18: action hints belong with the
    panel they act on, not only in the global footer) can never drift
    on what a hint looks like -- one styling decision, not two places
    to keep in sync by hand. Every hint names the EFFECT, not just the
    key ("[a] add team", never a bare "[a]") -- the Lead's explicit
    requirement, same reasoning as never-a-UUID: a label that requires
    the reader to already know what it does isn't a label."""
    text = Text()
    for i, (key, label) in enumerate(hints):
        if i:
            text.append("   ")
        text.append(f"[{key}]", style=f"bold {COLOR_ACCENT}")
        text.append(f" {label}", style=COLOR_DIM)
    return text


def liveness_icon(liveness: str) -> str:
    return {LIVENESS_RUNNING: "●", LIVENESS_STOPPED: "○", LIVENESS_LOST: "✕"}.get(liveness, "?")


def compact_model_name(model: str | None) -> str:
    """A RoleSlot attached via Create carries a clean "haiku"/"sonnet"/
    "opus" (engine_roles.MODELS). One attached via Attach instead carries
    whatever /status reported verbatim (e.g. "haiku
    (claude-haiku-4-5-20251001)", found live attaching a real concierge
    session) -- both are real, correct values, but the long form doesn't
    fit "at a glance" (SPEC-engine-roles.md SS7) in either density.
    Display-only: the stored value is never truncated, only what's
    rendered here."""
    if not model:
        return "?"
    return model.split()[0].split("(")[0].strip() or model


def liveness_color(liveness: str) -> str:
    return {LIVENESS_RUNNING: COLOR_OK, LIVENESS_STOPPED: COLOR_DIM, LIVENESS_LOST: COLOR_ERR}.get(liveness, COLOR_DIM)


def team_liveness(team) -> str:
    """Derived from members, never stored -- per the Lead's ruling: a
    team-level liveness field that could disagree with its own members
    is a second source of truth that can drift from the first. running
    if ANY member is running (the team is reachable through at least one
    path); otherwise stopped if any member has resumable history, else
    lost."""
    if not team.members:
        return LIVENESS_LOST
    if any(m.liveness == LIVENESS_RUNNING for m in team.members):
        return LIVENESS_RUNNING
    if any(m.liveness == LIVENESS_STOPPED for m in team.members):
        return LIVENESS_STOPPED
    return LIVENESS_LOST
