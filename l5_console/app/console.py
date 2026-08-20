"""
Console: the full-window density (SPEC-TUI.md §3). Wake / orchestrator /
runtime down the left; stream and teams on the right. Build order step 3
-- same JarvisState as Rail, fuller detail because there's room for it.

Visual pass against docs/console-design-studies.html (the Lead's
"the mockup is the spec" instruction, 2026-08-18): panel captions live
in the border via Textual's native border_title, not hand-drawn -- the
mockup's absolutely-positioned `.cap` span is a web-CSS way to fake what
a terminal's own box-drawing border can do directly. Status text uses
Rich Text objects with the mockup's own palette (format_helpers.COLOR_*,
copied from its CSS variables) for the dim-label/bright-value/semantic-
status hierarchy -- Text objects render through Rich's protocol
directly, never through Static's markup parser, so this doesn't touch
or weaken the markup=False safety property PlainStatic exists for
(widgets.py) -- that property is about literal strings like "[STALE]",
not about pre-built Rich renderables.
"""
from __future__ import annotations

import asyncio
import time

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widget import Widget
from textual.widgets import Button
from widgets import PlainStatic, Footer

from staleness import is_stale
from stream import StreamReader
from format_helpers import (
    liveness_icon, liveness_color, team_liveness, compact_model_name, build_hint_line, role_status_phrase, role_status_icon,
    is_blocked, blocked_for,
    COLOR_OK, COLOR_WARN, COLOR_ERR, COLOR_ACCENT, COLOR_DIM, COLOR_INK,
)
from meter import Meter
import wake_control
from engine_flow import CreateRoleScreen, AttachRoleScreen

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "state"))
from models import JarvisState, LIVENESS_RUNNING, LIVENESS_STOPPED, LIVENESS_LOST  # noqa: E402
import engine_roles  # noqa: E402

CONSOLE_ACTIVITY_MAX_LINES = 200


def _staleness_note(polled_at: float, expected_interval: float) -> str:
    return " [STALE -- data has stopped updating]" if is_stale(polled_at, expected_interval) else ""


def _truncate(text: str, max_len: int) -> str:
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def _context_age(captured_at: float | None) -> str:
    if captured_at is None:
        return ""
    return ", " + time.strftime("%b %-d", time.localtime(captured_at))


class WakePanel(Widget):
    """The one interactive control with a real safety property (§7).

    Two separate lines, deliberately never merged into one:
    - `#wake_status` is the ONLY thing that ever renders "listening" /
      "stopped" -- driven exclusively by JarvisState.wake.running from
      the next real poll, never by which button was clicked, never by
      wake_control's return value, never by this widget's own memory of
      what it last did. Stated explicitly so a later refactor can't
      quietly merge this with the action-result line below and
      reintroduce exactly the bug §7 rules out: rendering "stopped"
      before a poll has actually confirmed the process is gone.
    - `#wake_action_result` is an action LOG line -- "did the click
      succeed at triggering start/stop," which is a different, weaker
      claim than "is it running now." wake_control.stop() itself waits
      for real process death before returning (see its docstring), so by
      the time this line updates the process really is gone or the
      failure is real -- but this line is still just a report of one
      past action, not a substitute for #wake_status."""

    BORDER_TITLE = "WAKE"

    DEFAULT_CSS = """
    WakePanel { height: auto; border: solid $panel; padding: 1; margin-bottom: 1; border-title-color: $text-muted; }
    WakePanel #wake_status { margin-bottom: 1; }
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._wake_running = False  # cached from the last slow-clock update_state(); see update_meter()

    def compose(self) -> ComposeResult:
        yield PlainStatic("", id="wake_status")
        yield Meter(id="wake_meter", width=24)
        yield PlainStatic("", id="wake_action_result")
        yield Button("start", id="wake_button", variant="success")

    def update_meter(self, wake_state) -> None:
        self.query_one("#wake_meter", Meter).update_meter(self._wake_running, wake_state)

    def update_state(self, wake) -> None:
        # Stated explicitly per the Lead's ruling: this method is the
        # ONLY place "listening"/"stopped" ever gets written to
        # #wake_status, and it only ever does so from `wake.running` on
        # THIS poll -- never from wake_control.stop()'s return value,
        # never carried over from a previous call. A later refactor that
        # tries to "optimistically" set status.update("stopped") right
        # after a successful stop() call would reintroduce the exact bug
        # this rule exists to prevent: rendering "stopped" before a poll
        # has actually confirmed the process is gone.
        #
        # self._wake_running (read by update_meter() on the separate fast
        # clock) defaults to False here and is only set True in the fully
        # -trusted branch below -- an error or stale wake reading must
        # never let the meter claim it's showing live data.
        self._wake_running = False
        status = self.query_one("#wake_status", PlainStatic)
        button = self.query_one("#wake_button", Button)
        if wake.error:
            status.update(Text(f"⚠ error -- {wake.error}", style=COLOR_ERR))
            button.disabled = True
            return
        if is_stale(wake.polled_at, wake.expected_interval):
            status.update(Text(f"? unknown (data stale, last checked {time.time() - wake.polled_at:.0f}s ago)", style=COLOR_WARN))
            button.disabled = True
            return
        button.disabled = False
        if wake.running:
            status.update(Text("● LISTENING", style=f"bold {COLOR_OK}"))
            button.label = "stop"
            button.variant = "error"
            self._wake_running = True
        else:
            status.update(Text("○ stopped", style=COLOR_DIM))
            button.label = "start"
            button.variant = "success"

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "wake_button":
            return
        result_line = self.query_one("#wake_action_result", PlainStatic)
        button = self.query_one("#wake_button", Button)
        # Read the button's CURRENT label to decide the action -- not a
        # separately-tracked "is it running" flag on this widget, since
        # that would be exactly the kind of intent-not-reality state §7
        # rules out. The label itself was set from the last real poll.
        #
        # Both branches disable the button and show a transitional
        # "...ing" message immediately, symmetrically -- the click was
        # real and something IS happening, so saying nothing for up to
        # a full REFRESH_INTERVAL_S (the Lead's finding: it read as
        # broken, not slow) is the bug, not the poll-only-render
        # principle itself. #wake_status is still untouched here and
        # still only ever changes from a real poll in update_state() --
        # a transitional state is not an optimistic render of the
        # outcome, it's an honest description of "a click just
        # happened." The app-level poll burst kicked off below shrinks
        # how long that transitional state is visible; update_state()
        # re-enables the button and corrects the label the moment a
        # real poll lands either way.
        if button.label == "start":
            # SPEC-engine-roles.md SS6: starting the daemon opens the
            # microphone, and voice input with no live concierge/router
            # has nowhere to go -- refuse BEFORE arming the mic, and show
            # engine_roles.start_precondition()'s own detail verbatim
            # (never paraphrased into something generic; the whole point
            # is Ayman is told which role is missing and what to press).
            # Pure query, no side effects, so checking it costs nothing
            # even on the common case where it passes. This does not
            # touch the SPACE-key ruling -- that key is stop-only and
            # unaffected; this gate is on the BUTTON only.
            precondition = engine_roles.start_precondition()
            if not precondition["ok"]:
                result_line.update(Text(f"⚠ {precondition['detail']}", style=COLOR_ERR))
                return
            if hasattr(self.app, "start_wake_poll_burst"):
                self.app.start_wake_poll_burst()
            button.disabled = True
            button.label = "starting..."
            result_line.update(Text("starting...", style=COLOR_DIM))
            ok, msg = wake_control.start()
            result_line.update(Text(("✓ " if ok else "⚠ ") + msg, style=COLOR_OK if ok else COLOR_ERR))
            if not ok:
                button.disabled = False
                button.label = "start"
            return

        # Stop is the case that matters most (§7: the mic must actually
        # close, not just receive a signal) -- wake_control.stop() blocks
        # (as a coroutine, not the UI thread) until it has verified real
        # process death or genuinely exhausted SIGTERM+SIGKILL, which can
        # take a few seconds. Disable the button for that window so a
        # second click can't race the same stop() call -- update_state()
        # will correctly re-enable it once the next real poll confirms
        # wake.running is false, same path as any other state change.
        button.disabled = True
        button.label = "stopping..."
        result_line.update(Text("stopping...", style=COLOR_DIM))
        ok, msg = await wake_control.stop()
        result_line.update(Text(("✓ " if ok else "⚠ ") + msg, style=COLOR_OK if ok else COLOR_ERR))


class EnginePanel(Widget):
    """ENGINE (SPEC-engine-roles.md): CONCIERGE and ORCHESTRATOR, one
    role slot each. Both live inside this single bordered panel rather
    than two separate top-level panels -- the spec's own tree

        ENGINE
        +-- CONCIERGE
        +-- ORCHESTRATOR

    is nesting, and a second nested border per role would be visual noise
    at this width (#console_left is 34 columns); a caption line inside
    one outer panel reads as the same hierarchy without it.

    Buttons: a FIXED set of 5 per role, always mounted, visibility
    toggled via `.display` in update_state() -- not remove()/mount()
    cycles. This is a "which of a small known set is visible" state
    machine (SPEC-engine-roles.md SS7's exact matrix), the same shape
    WakePanel already handles by toggling one button's label/variant;
    here there are more buttons instead of one relabeled button, but the
    principle -- render only ever reflects the last real poll, never an
    optimistic guess -- is identical.
    """

    BORDER_TITLE = "ENGINE"

    DEFAULT_CSS = """
    /* padding: 0 1 -- not the original 1 (all sides) -- to give back the
       2 rows margin-top:1 below costs per button-gap: reintroducing
       ANY vertical gap between buttons pushed RUNTIME (the panel below
       this one) off the bottom of a real 170x44 window again, the same
       clipping class as the original oversized-button bug, just from a
       different cause. Horizontal padding (still 1) is what actually
       matters for readability; the vertical rows were spare, not load-
       bearing. */
    EnginePanel { height: auto; border: solid $panel; padding: 0 1; margin-bottom: 0; border-title-color: $text-muted; }
    EnginePanel .role_caption { margin-top: 1; }
    EnginePanel .role_caption.first { margin-top: 0; }
    /* Compact by design (the Lead's live finding, 2026-08-18): Textual's
       default Button border ("tall") cost 3 rows per button on its own
       -- 5 possible buttons per role clipped ORCHESTRATOR's Remove off
       the bottom at a normal 170x44 window. border:none makes each
       button exactly 1 row.

       Width 70% (~30% off, a LATER live finding, same round): full-
       width Swap/Remove read as bars competing with the info above
       them, not compact controls beside it -- left-aligned to match the
       name/model/effort text's own left edge rather than centered.

       margin-top:1 (a THIRD live finding): zero gap between adjacent
       visible buttons made two of them read as one two-tone block split
       in half rather than two controls. The first visible button in a
       role already has a blank line above it from the always-mounted
       (possibly empty) #{role}_result line, so this only adds spacing
       BETWEEN buttons, not before the first one -- paid for by the
       padding trim above, not by re-clipping RUNTIME. */
    EnginePanel Button { width: 70%; border: none !important; margin-top: 1; }
    """

    def compose(self) -> ComposeResult:
        for i, role in enumerate(engine_roles.ROLES):
            classes = "role_caption first" if i == 0 else "role_caption"
            yield PlainStatic(role.upper(), id=f"{role}_caption", classes=classes)
            yield PlainStatic("", id=f"{role}_info")
            yield PlainStatic("", id=f"{role}_result")
            # success/default -- unattached, offering to acquire a session
            yield Button("Create", id=f"{role}_create", variant="success")
            yield Button("Attach", id=f"{role}_attach", variant="success")
            # attached-but-dead: bring it back before anything else
            yield Button("Activate", id=f"{role}_activate", variant="warning")
            # attached (either liveness): change or let go of it
            # variant="primary" (not the default/unvaried style): the
            # Lead's live finding, 2026-08-18 -- the unvaried default
            # variant renders with a background so close to the panel's
            # own that it read as "no fill" next to Remove's solid red,
            # making them look like two different KINDS of control
            # rather than two actions of the same rank. Both now render
            # as solid, same-shaped buttons; only the color signals
            # which one is destructive.
            yield Button("Swap", id=f"{role}_swap", variant="primary")
            yield Button("Remove", id=f"{role}_remove", variant="error")

    def _role_widgets(self, role: str) -> dict[str, Widget]:
        return {
            "info": self.query_one(f"#{role}_info", PlainStatic),
            "result": self.query_one(f"#{role}_result", PlainStatic),
            "create": self.query_one(f"#{role}_create", Button),
            "attach": self.query_one(f"#{role}_attach", Button),
            "activate": self.query_one(f"#{role}_activate", Button),
            "swap": self.query_one(f"#{role}_swap", Button),
            "remove": self.query_one(f"#{role}_remove", Button),
        }

    def update_state(self, engine) -> None:
        note = _staleness_note(engine.polled_at, engine.expected_interval)
        for role in engine_roles.ROLES:
            w = self._role_widgets(role)
            if engine.error:
                w["info"].update(Text(f"⚠ error -- {engine.error}", style=COLOR_ERR))
                for key in ("create", "attach", "activate", "swap", "remove"):
                    w[key].display = False
                continue

            slot = getattr(engine, role)
            if not slot.attached:
                w["info"].update(Text("— no session attached —" + note, style=COLOR_DIM if not note else COLOR_WARN))
                w["create"].display = True
                w["attach"].display = True
                w["activate"].display = False
                w["swap"].display = False
                w["remove"].display = False
                continue

            live = slot.liveness == LIVENESS_RUNNING
            text = Text()
            text.append(f"{slot.name}\n", style=f"bold {COLOR_INK}")
            text.append(f"{compact_model_name(slot.model)} · {slot.effort}\n", style=COLOR_DIM)
            # cwd_mismatch renders in COLOR_WARN with its own phrase --
            # never identical to a genuinely-stopped "inactive" row (see
            # format_helpers.role_status_phrase()'s docstring). No raw
            # path shown here by design; Ayman needs to know Activate
            # won't do what he expects, not the found_cwd value itself.
            status_word = role_status_phrase(slot)
            status_color = COLOR_OK if live else (COLOR_WARN if slot.cwd_mismatch else COLOR_DIM)
            text.append(f"{role_status_icon(slot)} {status_word}", style=status_color)
            if live and not slot.tools_reachable:
                text.append("  ⚠ no tools", style=COLOR_WARN)
            if note:
                text.append(note, style=COLOR_WARN)
            w["info"].update(text)

            w["create"].display = False
            w["attach"].display = False
            w["activate"].display = not live
            w["swap"].display = True
            w["remove"].display = True

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        for role in engine_roles.ROLES:
            if button_id == f"{role}_create":
                self.app.push_screen(CreateRoleScreen(role))
                return
            if button_id in (f"{role}_attach", f"{role}_swap"):
                self.app.push_screen(AttachRoleScreen(role))
                return
            if button_id == f"{role}_remove":
                await self._do_remove(role)
                return
            if button_id == f"{role}_activate":
                await self._do_activate(role)
                return

    async def _do_remove(self, role: str) -> None:
        w = self._role_widgets(role)
        w["remove"].disabled = True
        w["result"].update(Text("removing...", style=COLOR_DIM))
        result = await asyncio.to_thread(engine_roles.remove_role, role)
        w["result"].update(Text(("✓ " if result["ok"] else "⚠ ") + result["detail"], style=COLOR_OK if result["ok"] else COLOR_ERR))
        w["remove"].disabled = False

    async def _do_activate(self, role: str) -> None:
        # Relaunch + wait_for_ready can take 10-15s (a real tmux launch
        # and a real Claude Code cold start, not an instant call) -- both
        # engine_roles.activate_role() and remove_role() are plain
        # blocking functions (subprocess.run/time.sleep polling
        # internally, not asyncio-native), so both run in a thread rather
        # than freezing the whole UI event loop for their duration.
        w = self._role_widgets(role)
        w["activate"].disabled = True
        w["result"].update(Text("activating...", style=COLOR_DIM))
        result = await asyncio.to_thread(engine_roles.activate_role, role)
        w["result"].update(Text(("✓ " if result["ok"] else "⚠ ") + result["detail"], style=COLOR_OK if result["ok"] else COLOR_ERR))
        w["activate"].disabled = False


class RuntimePanel(PlainStatic):
    """Ambient, not primary (§3) -- least visually prominent panel."""

    BORDER_TITLE = "RUNTIME"
    DEFAULT_CSS = "RuntimePanel { height: auto; border: solid $panel; padding: 1; margin-bottom: 1; border-title-color: $text-muted; }"

    def update_state(self, runtime) -> None:
        if runtime.error:
            self.update(Text(f"error -- {runtime.error}", style=COLOR_ERR))
            return
        models = ", ".join(runtime.models_resident) if runtime.models_resident else "none warm"
        mem = f"{runtime.memory_free_pct:.0f}% free" if runtime.memory_free_pct is not None else "mem ?"
        spend = runtime.spend["summary"] if runtime.spend and runtime.spend.get("ok") else "spend unavailable"
        text = Text()
        text.append("  models: ", style=COLOR_DIM)
        text.append(f"{models}\n", style=COLOR_INK)
        text.append("  memory: ", style=COLOR_DIM)
        text.append(mem, style=COLOR_INK)
        text.append(_staleness_note(runtime.polled_at, runtime.expected_interval) + "\n", style=COLOR_WARN)
        text.append("  spend:  ", style=COLOR_DIM)
        text.append(spend, style=COLOR_INK)
        text.append(_staleness_note(runtime.spend_polled_at, runtime.spend_expected_interval), style=COLOR_WARN)
        self.update(text)


class TeamsPanel(PlainStatic):
    """Fuller than Rail's condensed count -- every member, its activity,
    and which one is the inbox (SPEC-TUI.md §4.5: the inbox is the
    default and always wins on an ambiguous reference, so it's always
    shown, not just implied). Real aligned columns via a borderless Rich
    Table -- label left, status right, path dim -- not one joined
    prose string, and never wraps a path: display_path is already
    collapsed/truncated at the source (l5_console/state), this panel
    only renders it."""

    BORDER_TITLE = "AGENTS / TEAMS"
    DEFAULT_CSS = "TeamsPanel { height: 1fr; border: solid $panel; padding: 1; border-title-color: $text-muted; }"

    def update_state(self, teams: list, teams_error, unassigned: list, polled_at, expected_interval) -> None:
        if teams_error:
            self.update(Text(f"⚠ error -- {teams_error}", style=COLOR_ERR))
            return

        table = Table.grid(padding=(0, 1, 0, 0), expand=True)
        table.add_column(no_wrap=True)  # icon + name
        table.add_column(ratio=1, no_wrap=True, overflow="ellipsis")  # activity / member count
        table.add_column(no_wrap=True, justify="right")  # status / path

        note = _staleness_note(polled_at, expected_interval)
        if note:
            table.add_row(Text(note.strip(), style=COLOR_WARN), "", "")

        if not teams:
            # The empty state IS the primary affordance for this panel's
            # main action -- the Lead's live finding, 2026-08-18: "none
            # configured" told him nothing was there and nothing about
            # what to do about it, leaving the global footer's bare
            # "[a] add team" as the only path, which requires noticing
            # it, connecting it to this specific panel, and remembering
            # it -- three steps of inference for the one thing an empty
            # panel most needs to say. Named action, named key, in the
            # panel itself.
            empty = Text("No teams yet -- press ", style=COLOR_DIM)
            empty.append("[a]", style=f"bold {COLOR_ACCENT}")
            empty.append(" to add one.", style=COLOR_DIM)
            table.add_row(empty, "", "")

        for t in teams:
            tl = team_liveness(t)
            table.add_row(
                Text(f"{liveness_icon(tl)} {t.id}", style=f"bold {COLOR_ACCENT}"),
                Text(f"{len(t.members)} agent(s)", style=COLOR_DIM),
                Text(t.display_path or t.root, style=COLOR_DIM),
            )

            # SPEC-teams.md SS2: "no lead assigned" (live members, no
            # pointer set) must render as its OWN distinct state, never
            # indistinguishable from a fully dead team -- and separately
            # from "lead unreachable" (pointer set, but that member isn't
            # currently reachable), which is a different problem needing
            # a different fix (Swap, not Attach-a-lead).
            if not t.has_lead:
                table.add_row(Text("  ⚠ no lead assigned", style=COLOR_WARN), "", "")
            elif not t.lead_reachable:
                table.add_row(Text("  ⚠ lead unreachable", style=COLOR_WARN), "", "")

            # Per-team context line (SPEC-teams.md SS3) -- dim, truncated,
            # the "(self-described, unverified)" qualifier ONLY for
            # agent-sourced context; CLAUDE.md-sourced needs none, it
            # isn't a fabrication risk. Routing hint only, never rendered
            # as if Ayman asked a factual question and got a factual
            # answer -- the qualifier is what keeps that distinction
            # visible instead of implicit.
            if t.context_summary:
                ctx = Text(f"  {_truncate(t.context_summary, 70)}", style=COLOR_DIM)
                if t.context_source == "agent":
                    ctx.append(f"  (self-described, unverified{_context_age(t.context_captured_at)})", style=COLOR_DIM)
                table.add_row(ctx, "", "")

            # Lead rendered FIRST, always, with a real badge -- not a
            # suffix on whatever order teams.json's member list happens
            # to have (the Lead's live finding, 2026-08-18: today's order
            # is just list order). Everyone else follows in their
            # existing order.
            lead_member = next((m for m in t.members if m.is_lead), None) if t.has_lead else None
            ordered_members = ([lead_member] if lead_member else []) + [m for m in t.members if m is not lead_member]

            for m in ordered_members:
                name = m.tmux or "(not bound)"
                is_lead_row = m is lead_member
                # Model folded into the existing activity column as
                # compact text ("busy · sonnet"), not a fourth column
                # (SPEC-teams.md SS5) -- this panel already has less
                # spare width than Engine's fixed-34-col rail, and a
                # dedicated column risks crowding at real terminal
                # widths sooner than a dim inline suffix does.
                activity = m.activity or ("idle" if m.liveness == LIVENESS_RUNNING else "")
                if m.model:
                    # compact_model_name(), not the raw m.model -- an
                    # adopted member's model comes back from /status as
                    # "Opus (claude-opus-5)" verbatim (found live
                    # building Engine's own equivalent column), which
                    # doesn't fit a compact inline suffix.
                    model_short = compact_model_name(m.model)
                    activity = f"{activity} · {model_short}" if activity else model_short
                # A blocked member is NOT a busy one. SPEC-blockers.md
                # SS6: work has stopped and something is required, and it
                # should be the most prominent thing on screen -- more
                # prominent than busy, because busy resolves itself and
                # blocked does not.
                #
                # Built 2026-08-20. Everything behind this was already
                # working: pane_state classifies BLOCKED_QUESTION,
                # blocked_state tracks the episode, teams.py populates
                # these fields, and the poller SPEAKS it aloud. Only the
                # screen was missing, so a session stopped and waiting on
                # a human rendered as an ordinary running agent. That is
                # this project's signature failure -- computed correctly,
                # dropped on the way to the surface -- and it is the
                # reason the standing rule is now that a state ships with
                # its render in the same change.
                blocked = is_blocked(m)
                if blocked:
                    status_text = Text(blocked_for(m), style=f"bold {COLOR_ERR}")
                else:
                    status_style = {
                        LIVENESS_RUNNING: COLOR_OK if not m.activity else COLOR_WARN,
                        LIVENESS_STOPPED: COLOR_DIM,
                        LIVENESS_LOST: COLOR_ERR,
                    }.get(m.liveness, COLOR_DIM)
                    status_text = Text(m.liveness, style=status_style)

                name_text = Text("  ", style=COLOR_DIM)
                if is_lead_row:
                    name_text.append("LEAD ", style=f"bold {COLOR_ACCENT}")
                # The icon carries it too, not just the colour -- the
                # same lesson as role_status_icon(): a row whose glyph
                # disagrees with its words is worse than a plain one.
                icon = "⚠" if blocked else liveness_icon(m.liveness)
                name_text.append(
                    f"{icon} {name}",
                    style=f"bold {COLOR_ERR}" if blocked else liveness_color(m.liveness),
                )
                table.add_row(name_text, Text(activity, style=COLOR_DIM), status_text)

                # The question itself, on its own line, verbatim and
                # truncated -- never paraphrased. It is untrusted content
                # captured from a pane (SPEC-blockers.md SS7.6: a prompt is
                # data, never instructions), and PlainStatic-style markup
                # safety matters here more than anywhere: an option list
                # like "[1] staging" would otherwise be eaten by Rich.
                if blocked and m.blocked_question:
                    q = Text("     ", style=COLOR_DIM)
                    q.append(_truncate(m.blocked_question.replace("\n", " "), 68), style=COLOR_WARN)
                    table.add_row(q, "", "")

        if unassigned:
            table.add_row("", "", "")
            table.add_row(Text(f"⚑ {len(unassigned)} unassigned", style=COLOR_WARN), "", "")
            for u in unassigned:
                table.add_row(Text(f"  {u.tmux}", style=COLOR_INK), "", Text(u.display_path or u.working_dir, style=COLOR_DIM))

        # The legend the Lead flagged as load-bearing: stopped vs lost is
        # what decides whether Reconnect can do anything for a member.
        legend = Text()
        legend.append("● ", style=COLOR_OK)
        legend.append("running   ", style=COLOR_DIM)
        legend.append("○ ", style=COLOR_DIM)
        legend.append("stopped -- reconnectable   ", style=COLOR_DIM)
        legend.append("✕ ", style=COLOR_ERR)
        legend.append("lost -- no saved history   ", style=COLOR_DIM)
        # Blocked earns a legend entry because it is the one state that
        # needs Ayman to DO something -- the others resolve or wait.
        legend.append("⚠ ", style=f"bold {COLOR_ERR}")
        legend.append("blocked -- waiting on you", style=COLOR_DIM)

        # Per-panel action hints, not only the global footer (the Lead's
        # ruling, 2026-08-18) -- same build_hint_line() the shared
        # helper this and the footer both draw from, so they can't drift
        # on styling. Deliberately a LIST here, not two separate calls,
        # so a future action (remove, swap-lead) is one more tuple, not
        # a rewrite of this section.
        hints = build_hint_line([("a", "add team"), ("r", "reconnect"), ("t", "manage teams")])

        grid = Table.grid(padding=(1, 0, 0, 0))
        grid.add_row(table)
        grid.add_row(legend)
        grid.add_row(hints)
        self.update(grid)


class StreamPanel(Widget):
    """Live log of wake scores, state transitions, routing (§3) -- see
    stream.py's own docstring for what's actually in the feed today and
    what would need new daemon.py instrumentation (per-frame wake
    scores, not yet logged anywhere)."""

    BORDER_TITLE = "STREAM"
    DEFAULT_CSS = "StreamPanel { height: 1fr; border: solid $panel; padding: 1; margin-bottom: 1; border-title-color: $text-muted; }"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._stream = StreamReader()
        self._lines: list[str] = []

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="stream_scroll"):
            yield PlainStatic("", id="stream_body")

    def poll_stream(self) -> None:
        new_lines = self._stream.poll()
        if not new_lines:
            return
        self._lines.extend(new_lines)
        self._lines = self._lines[-CONSOLE_ACTIVITY_MAX_LINES:]
        self.query_one("#stream_body", PlainStatic).update("\n".join(self._lines))
        self.query_one("#stream_scroll", VerticalScroll).scroll_end(animate=False)


class Console(Widget):
    """Root Console widget. Two columns: left = wake/orchestrator/
    runtime (control + ambient), right = stream/teams (what's actually
    happening) -- §3's explicit layout."""

    DEFAULT_CSS = """
    Console { width: 100%; height: 100%; padding: 1; }
    Console > Horizontal { height: 1fr; }
    #console_left { width: 34; height: 100%; }
    #console_right { width: 1fr; height: 100%; }
    """

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical(id="console_left"):
                yield WakePanel(id="console_wake")
                yield EnginePanel(id="console_engine")
                yield RuntimePanel(id="console_runtime")
            with Vertical(id="console_right"):
                yield StreamPanel(id="console_stream")
                yield TeamsPanel(id="console_teams")
        yield Footer(id="console_footer")

    def update_state(self, state: JarvisState) -> None:
        self.query_one("#console_wake", WakePanel).update_state(state.wake)
        self.query_one("#console_engine", EnginePanel).update_state(state.engine)
        self.query_one("#console_runtime", RuntimePanel).update_state(state.runtime)
        # Same trusted-boolean discipline as WakePanel's own
        # self._wake_running: an errored or stale wake reading must not
        # let the footer confidently claim space is inert either.
        self.query_one("#console_footer", Footer).update_wake_state(
            bool(state.wake.running and not state.wake.error and not is_stale(state.wake.polled_at, state.wake.expected_interval))
        )
        self.query_one("#console_teams", TeamsPanel).update_state(
            state.teams, state.teams_error, state.unassigned, state.teams_polled_at, state.teams_expected_interval
        )
        self.query_one("#console_stream", StreamPanel).poll_stream()

    def update_meter(self, wake_state) -> None:
        self.query_one("#console_wake", WakePanel).update_meter(wake_state)
