"""
Rail: the narrow-pane density (SPEC-TUI.md §3). Status lines + recent
activity, always-visible, steals no room. First layout built, per the
build order (§9) -- Console and Signal come after.

Renders JarvisState read through one call (see main.py's `get_state`
import) -- never holds its own copy of truth between polls. Every
section checks staleness.is_stale() against its own polled_at/
expected_interval before deciding how to render, per the contract's
governing rule: a stopped poller must never look identical to a live one.

Visual pass against docs/console-design-studies.html: flat dim-label/
bright-value rows (not full bordered panels -- Rail's mockup keeps the
status block plain and reserves a border for the one thing worth
framing, recent activity), same palette and Footer as console.py so
the two densities read as one application, not two.
"""
from __future__ import annotations

import time

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widget import Widget
from widgets import PlainStatic, Footer

from staleness import is_stale
from stream import StreamReader
from format_helpers import (
    liveness_icon, liveness_color, team_liveness, compact_model_name, role_status_phrase, role_status_icon,
    is_blocked, is_identity_stale, pending_speech_header, is_prompt_pending, is_server_stale,
    COLOR_OK, COLOR_WARN, COLOR_ERR, COLOR_ACCENT, COLOR_DIM, COLOR_INK,
)
from meter import Meter

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "state"))
from models import JarvisState, LIVENESS_RUNNING  # noqa: E402
import engine_roles  # noqa: E402

RAIL_ACTIVITY_MAX_LINES = 30


def _staleness_suffix() -> str:
    return " [STALE]"


def _wake_line(wake) -> Text:
    """The one line in this whole app that carries a real safety
    property (SPEC-TUI.md §7): it reflects state.wake.running, which
    itself only ever reflects a real pgrep of the daemon process --
    never whether a button was clicked, never this widget's own memory
    of what it last did. If the underlying poll is stale, that's shown
    explicitly rather than trusting the last-known running value."""
    if wake.error:
        return Text(f"⚠ wake: error -- {wake.error}", style=COLOR_ERR)
    if is_stale(wake.polled_at, wake.expected_interval):
        return Text(f"? wake: unknown (data stale, last checked {time.time() - wake.polled_at:.0f}s ago)", style=COLOR_WARN)
    if wake.running:
        return Text("● LISTENING", style=f"bold {COLOR_OK}")
    return Text("○ wake: stopped", style=COLOR_DIM)


class RailWake(PlainStatic):
    def update_state(self, wake) -> None:
        self.update(_wake_line(wake))


class RailEngine(PlainStatic):
    """ENGINE, Rail density: two compact status lines, no buttons -- Rail
    is read-only/ambient everywhere else (even WAKE, the one thing with a
    real control in Console, is a plain status line here), so
    Create/Attach/Swap/Remove/Activate are Console-only. Name and
    model/effort shown, never a session UUID (SPEC-engine-roles.md SS7),
    same rule as Console's EnginePanel."""

    def update_state(self, engine) -> None:
        if engine.error:
            self.update(Text(f"⚠ engine: error -- {engine.error}", style=COLOR_ERR))
            return
        stale = is_stale(engine.polled_at, engine.expected_interval)
        text = Text()
        for i, role in enumerate(engine_roles.ROLES):
            if i:
                text.append("\n")
            slot = getattr(engine, role)
            text.append(f"{role:<12}", style=COLOR_DIM)
            if not slot.attached:
                text.append("unattached", style=COLOR_DIM)
            else:
                live = slot.liveness == LIVENESS_RUNNING
                text.append(f"{slot.name} ", style=COLOR_INK)
                text.append(f"({compact_model_name(slot.model)}·{slot.effort}) ", style=COLOR_DIM)
                # cwd_mismatch renders in COLOR_WARN, never COLOR_DIM --
                # a live-but-mismatched slot must never look like a
                # plain, unremarkable "inactive" row (see
                # format_helpers.role_status_phrase()'s docstring).
                text.append(
                    f"{role_status_icon(slot)} {role_status_phrase(slot)}",
                    style=COLOR_OK if live else (COLOR_WARN if slot.cwd_mismatch else COLOR_DIM),
                )
                # Most prominent thing this role's line can show
                # (the 2026-08-20 stuck-concierge incident) -- same predicate
                # Console uses, so the two densities can't disagree
                # about which roles are stuck. Rail has no room for the
                # preview text, just the fact -- same "count/fact only,
                # detail lives in Console" split as pending-speech.
                if is_prompt_pending(slot):
                    text.append("  ⚠ STUCK ON PROMPT", style=f"bold {COLOR_ERR}")
                # ADVISORY tier (PLAN-silence-and-ux.md R5) -- deliberately
                # BELOW prompt_pending, never shown alongside it (elif):
                # a role stuck on a prompt needs attention now; a role
                # merely running older code does not compete for it.
                elif live and is_server_stale(slot):
                    text.append("  code updated since activate", style=COLOR_DIM)
        if stale:
            text.append(_staleness_suffix(), style=COLOR_WARN)
        self.update(text)


class RailTeams(PlainStatic):
    def update_state(self, teams: list, teams_error, polled_at, expected_interval) -> None:
        if teams_error:
            self.update(Text(f"⚠ teams: error -- {teams_error}", style=COLOR_ERR))
            return
        text = Text()
        text.append("sessions      ", style=COLOR_DIM)
        if not teams:
            text.append("none configured", style=COLOR_DIM)
            self.update(text)
            return
        live = sum(1 for t in teams if any(m.liveness == LIVENESS_RUNNING for m in t.members))
        text.append(f"{len(teams)} team(s)", style=COLOR_INK)
        text.append(f"  {live} live", style=COLOR_OK if live else COLOR_DIM)
        if is_stale(polled_at, expected_interval):
            text.append(_staleness_suffix(), style=COLOR_WARN)
        # Blocked count FIRST, before the per-team lines, and only when
        # non-zero. Rail is the narrow density -- it cannot show the
        # question text -- but it must never be the density where a
        # blocked agent looks like a running one. SPEC-blockers.md SS6:
        # work has stopped and something is required. A count plus a
        # pointer at the fuller view is the most this width can honestly
        # carry, and it is strictly better than silence.
        blocked_members = [m for t in teams for m in t.members if is_blocked(m)]
        if blocked_members:
            text.append(f"  {len(blocked_members)} blocked", style=f"bold {COLOR_ERR}")

        # Same aggregate-count shape as blocked, for the same reason:
        # Rail has no per-member rows to attach a note to, so a count
        # plus a pointer at Console (where the per-member "unverified
        # since Xm" note lives) is the most this width can honestly
        # carry. Deliberately not bold/COLOR_ERR like blocked -- this is
        # "worth a glance," not "work has stopped."
        stale_members = [m for t in teams for m in t.members if is_identity_stale(m)]
        if stale_members:
            text.append(f"  {len(stale_members)} unverified", style=COLOR_WARN)

        for t in teams:
            text.append(f"\n  {liveness_icon(team_liveness(t))} {t.id}", style=liveness_color(team_liveness(t)))
            text.append(f" ({len(t.members)})", style=COLOR_DIM)
            n = sum(1 for m in t.members if is_blocked(m))
            if n:
                text.append(f"  ⚠ {n} blocked", style=f"bold {COLOR_ERR}")
            u = sum(1 for m in t.members if is_identity_stale(m))
            if u:
                text.append(f"  {u} unverified", style=COLOR_WARN)
        self.update(text)


class RailRuntime(PlainStatic):
    """Ambient, not primary (§3) -- deliberately the least prominent
    section in Rail."""

    def update_state(self, runtime) -> None:
        if runtime.error:
            self.update(Text(f"runtime: error -- {runtime.error}", style=COLOR_ERR))
            return
        models = ", ".join(runtime.models_resident) if runtime.models_resident else "none warm"
        mem = f"{runtime.memory_free_pct:.0f}% free" if runtime.memory_free_pct is not None else "mem ?"
        spend = runtime.spend["summary"] if runtime.spend and runtime.spend.get("ok") else "spend ?"
        text = Text()
        text.append("model         ", style=COLOR_DIM)
        text.append(models, style=COLOR_DIM)
        text.append(f"  {mem}", style=COLOR_DIM)
        if is_stale(runtime.polled_at, runtime.expected_interval):
            text.append(_staleness_suffix(), style=COLOR_WARN)
        text.append(f"\n  {spend}", style=COLOR_DIM)
        if is_stale(runtime.spend_polled_at, runtime.spend_expected_interval):
            text.append(_staleness_suffix(), style=COLOR_WARN)
        self.update(text)


class RailPendingSpeech(PlainStatic):
    """Rail density of PendingSpeechPanel (SPEC-orchestration.md Phase 3,
    TODO-feature-queue.md item 4) -- Rail has no room for the item list,
    so a count and the same "why" Console shows, not the items
    themselves. Same is-it-empty-then-render-nothing discipline as
    RailUnassigned right below it: an empty update("") still occupies a
    line in Rail's plain-line layout, matching that established
    precedent exactly rather than inventing a second mechanism."""

    def update_state(self, pending) -> None:
        if pending.error:
            self.update(Text(f"⚠ pending: error -- {pending.error}", style=COLOR_ERR))
            return
        if not pending.items:
            self.update("")
            return
        text = Text(f"🔈 {pending_speech_header(len(pending.items))}", style=f"bold {COLOR_WARN}")
        if is_stale(pending.polled_at, pending.expected_interval):
            text.append(_staleness_suffix(), style=COLOR_WARN)
        self.update(text)


class RailUnassigned(PlainStatic):
    def update_state(self, unassigned: list) -> None:
        if not unassigned:
            self.update("")
            return
        # 'u', not 'a' -- TODO-feature-queue.md item 3: [u] is the
        # one-keystroke quick-adopt path (quick_adopt_flow.py), [a] is
        # the full multi-step wizard. This is the exact spot Ayman
        # discovers an unassigned session, so it should point at the
        # fast path, not the deliberate one.
        self.update(Text(f"⚑ {len(unassigned)} unassigned session(s) -- press 'u' to adopt", style=COLOR_WARN))


class Rail(Widget):
    """Root Rail widget: composes every status line, refreshed on every
    poll. No widget here ever computes its own state -- each just
    formats what JarvisState already reported."""

    DEFAULT_CSS = """
    Rail {
        width: 100%;
        height: 100%;
        padding: 1;
    }
    Rail > Vertical {
        height: auto;
    }
    #rail_activity {
        height: 1fr;
        border: solid $panel;
        margin-top: 1;
        padding: 0 1;
        border-title-color: $text-muted;
    }
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._stream = StreamReader()
        self._activity_lines: list[str] = []
        self._wake_running = False  # cached from the last slow-clock update_state(); see update_meter()

    def compose(self) -> ComposeResult:
        with Vertical():
            yield RailWake(id="rail_wake")
            yield Meter(id="rail_meter", width=16)
            yield RailEngine(id="rail_engine")
            yield RailPendingSpeech(id="rail_pending_speech")
            yield RailTeams(id="rail_teams")
            yield RailRuntime(id="rail_runtime")
            yield RailUnassigned(id="rail_unassigned")
        activity = VerticalScroll(id="rail_activity")
        activity.border_title = "RECENT"
        with activity:
            yield PlainStatic("", id="rail_activity_body")
        yield Footer(id="rail_footer")

    def update_state(self, state: JarvisState) -> None:
        self.query_one("#rail_wake", RailWake).update_state(state.wake)
        # Only trust wake.running as a real "is it alive" signal when the
        # reading itself is neither errored nor stale -- an error/stale
        # wake reading must never let the meter claim it's showing live
        # data (matches WakePanel's identical guard in console.py).
        self._wake_running = bool(
            state.wake.running
            and not state.wake.error
            and not is_stale(state.wake.polled_at, state.wake.expected_interval)
        )
        self.query_one("#rail_footer", Footer).update_wake_state(self._wake_running)
        self.query_one("#rail_engine", RailEngine).update_state(state.engine)
        self.query_one("#rail_pending_speech", RailPendingSpeech).update_state(state.pending_speech)
        self.query_one("#rail_teams", RailTeams).update_state(
            state.teams, state.teams_error, state.teams_polled_at, state.teams_expected_interval
        )
        self.query_one("#rail_runtime", RailRuntime).update_state(state.runtime)
        self.query_one("#rail_unassigned", RailUnassigned).update_state(state.unassigned)
        self._update_activity()

    def update_meter(self, wake_state) -> None:
        """Called on main.py's separate, faster clock (SPEC-TUI.md §6.1's
        dual-clock principle -- the meter needs to look live, JarvisState's
        ~1s poll doesn't). wake_running comes from the last slow-clock
        update_state(), not re-derived here -- at most ~1s stale on "is it
        running at all," which is the JarvisState poll's own native
        cadence anyway, not a new staleness this introduces."""
        self.query_one("#rail_meter", Meter).update_meter(self._wake_running, wake_state)

    def _update_activity(self) -> None:
        new_lines = self._stream.poll()
        if not new_lines:
            return
        self._activity_lines.extend(new_lines)
        self._activity_lines = self._activity_lines[-RAIL_ACTIVITY_MAX_LINES:]
        body = self.query_one("#rail_activity_body", PlainStatic)
        body.update("\n".join(self._activity_lines))
        self.query_one("#rail_activity", VerticalScroll).scroll_end(animate=False)
