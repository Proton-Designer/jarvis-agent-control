"""
L4 delivery transport.

Swappable interface: the orchestrator (L3) never talks tmux directly. It calls
deliver(target, payload) on whatever Transport is configured. Today that's
TmuxTransport (tmux send-keys). A future PeersTransport (claude-peers native
injection) can drop in behind the same interface with no change above it.
"""

from __future__ import annotations

import re
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import say_feedback
from pane_state import PaneState, PaneStatePatterns, classify_pane_ansi
from slash_guard import check_slash_payload, load_known_commands
from view_parsers import VIEW_PARSERS

VIEW_OPEN_POLL_ATTEMPTS = 6
VIEW_OPEN_POLL_INTERVAL_S = 0.5
DISMISS_VERIFY_POLL_ATTEMPTS = 6
DISMISS_VERIFY_POLL_INTERVAL_S = 0.5

# A pane already stuck in PERSISTENT_VIEW before this delivery attempt
# even started (self-heal, added 2026-08-18 -- see README's Contributing
# section for the incident that shaped its constraints). Waited out first
# in case it's transient or Ayman is actively reading it -- only touched
# if still stuck after this window.
STUCK_VIEW_WAIT_ATTEMPTS = 4
STUCK_VIEW_WAIT_INTERVAL_S = 2.0


def _is_known_readonly_view(view_text: str) -> bool:
    """PaneState.PERSISTENT_VIEW alone is NOT a safe basis for the
    self-heal to act on -- confirmed live (2026-08-18) that /config
    matches the exact same tab-bar marker as /cost, /usage, /status, so
    the pane-state classifier cannot by itself distinguish a safe
    read-only view from the interactive picker that caused a real
    settings-toggle incident, twice. Positive identification instead
    means the view's actual CONTENT matches one of view_parsers.py's
    known readonly shapes (Total cost:/session-percentage for /cost and
    /usage, "Model:" for /status) -- /config's content (a scrollable
    settings list) matches none of them, confirmed live. UNKNOWN and any
    view whose content doesn't match a known parser never self-heal."""
    return any(parser(view_text) is not None for parser in VIEW_PARSERS.values())

# Exact-match allowlist (by name, not by category) of commands that may be
# sent into a BUSY pane rather than refused, trusting Claude Code's own
# mid-turn message queueing instead. See QUEUEING_INVESTIGATION.md: /compact
# was verified reliable at multiple points in a busy window, in order, with
# no swallowing, AND (the finding that actually justifies this) queued
# actionable instructions get genuinely performed when a merged turn
# resolves, not just acknowledged -- verified with independently-checked
# file artifacts, not model self-report.
#
# By-name, not "any conversation-state-touching command": a generic rule
# would silently extend to a future command nobody's tested, the same
# mistake as the unnamed-slash-command hazard this codebase already found
# once. Widen only with the same evidence standard, one command at a time.
#
# Only applies to BUSY. PERMISSION_PROMPT and UNKNOWN/real-typed-content
# are untouched -- a different hazard class this investigation says
# nothing about.
BUSY_TOLERANT_COMMANDS = {"/compact"}


@dataclass
class DeliveryResult:
    ok: bool
    detail: str = ""
    # Structured reason code for callers that need to act on WHY a delivery
    # failed (e.g. batch-delivery retry logic), without string-matching
    # `detail`. One of: None (ok), "no_session", "unsafe_slash", "busy",
    # "permission_prompt", "unknown" (real/ambiguous pane content, not
    # classifiable as ready), "tmux_error", "view_not_opened",
    # "dismiss_failed".
    reason: str | None = None
    # Set only for a "readonly" view command (e.g. /cost): the captured,
    # ANSI-stripped view content, for the caller to read back to Ayman.
    # Present even on a dismiss_failed result, since the content was still
    # successfully read even though closing the view didn't go cleanly.
    view_content: str | None = None


class Transport(ABC):
    @abstractmethod
    def deliver(self, target: str, payload: str) -> DeliveryResult:
        """Deliver payload to target. target is a transport-specific session id
        (already resolved from the friendly-name registry by the caller)."""
        ...

    @abstractmethod
    def session_exists(self, target: str) -> bool:
        ...


class TmuxTransport(Transport):
    """
    Delivers text into a named tmux session via `tmux send-keys`.

    Two separate send-keys calls, deliberately:
      1. `send-keys -l -- <payload>` — literal mode, so tmux does not try to
         interpret the payload as key names (e.g. a payload containing the
         literal text "Enter" or "C-c" must be typed, not executed).
      2. `send-keys Enter` — a second, non-literal call to actually submit.

    No shell=True anywhere: argv is passed as a list, so shell metacharacters
    in the payload (backticks, semicolons, quotes) never reach a shell parser
    — they're just bytes tmux types into the pane.

    Slash-command payloads are classified before sending (see slash_guard.py):
    "none" (runs inline, deliver normally), "readonly" (opens a persistent
    view — captured, dismissed, and its content returned for read-back),
    or refused (unrecognized / explicitly blocked as interactive/mutating —
    confirmed live that an interactive view treats injected keystrokes as UI
    input, not text: a stray send toggled a real global setting).

    Pane-state gate: refuses BUSY/PERMISSION_PROMPT/UNKNOWN, EXCEPT the
    exact-match BUSY_TOLERANT_COMMANDS allowlist (currently just /compact),
    which is sent into a BUSY pane rather than refused, trusting Claude
    Code's own mid-turn message queueing (verified reliable — see
    QUEUEING_INVESTIGATION.md, including that queued actionable
    instructions genuinely get performed, not just acknowledged, when a
    merged turn resolves). PERMISSION_PROMPT and UNKNOWN are never
    bypassed this way, for any command.
    """

    def __init__(
        self,
        tmux_bin: str = "tmux",
        patterns: PaneStatePatterns | None = None,
        known_commands=None,
        registry=None,
    ):
        self.tmux_bin = tmux_bin
        self.patterns = patterns or PaneStatePatterns()
        self.known_commands = known_commands if known_commands is not None else load_known_commands()
        # Optional: a SessionRegistry, used to look up per-target custom
        # slash commands (.claude/commands/*.md) so those aren't refused as
        # unrecognized. Delivery works without one; custom commands just
        # won't be recognized as safe.
        self.registry = registry

    def session_exists(self, target: str) -> bool:
        result = subprocess.run(
            [self.tmux_bin, "has-session", "-t", target],
            capture_output=True,
        )
        return result.returncode == 0

    def capture_pane(self, target: str) -> str:
        # -e preserves ANSI/SGR codes: classify_pane_ansi needs them to tell
        # ghost/autosuggest text (dim) apart from real unsubmitted input
        # (plain) — see pane_state.py VALIDATION NOTES.
        result = subprocess.run(
            [self.tmux_bin, "capture-pane", "-e", "-p", "-t", target],
            check=True,
            capture_output=True,
        )
        return result.stdout.decode(errors="replace")

    def capture_pane_plain(self, target: str) -> str:
        result = subprocess.run(
            [self.tmux_bin, "capture-pane", "-p", "-t", target],
            check=True,
            capture_output=True,
        )
        return result.stdout.decode(errors="replace")

    def _custom_commands_for(self, target: str) -> set[str]:
        if self.registry is None:
            return set()
        for s in self.registry.list_sessions():
            if s.session_id == target:
                return set(s.custom_commands)
        return set()

    def _friendly_name(self, target: str) -> str:
        if self.registry is not None:
            for s in self.registry.list_sessions():
                if s.session_id == target and s.alias:
                    return s.alias
        return target

    def _recover_stuck_view(self, target: str) -> None:
        """A pane already stuck in PERSISTENT_VIEW before this delivery
        attempt even started is permanently unreachable otherwise -- every
        future delivery would refuse at the same gate, forever, with no
        recovery path and no way for Ayman to know why. Constraints below
        are load-bearing, not style (see README's Contributing section for
        the incident that produced them):

        1. Waits STUCK_VIEW_WAIT_ATTEMPTS first -- may be transient, or
           Ayman may be actively reading it. Returns immediately, doing
           nothing, if it clears on its own.
        2. Requires positive content identification (_is_known_readonly_view)
           before touching anything -- PaneState.PERSISTENT_VIEW alone is
           not enough, confirmed live that /config shares the exact same
           tab-bar marker as /cost/usage/status. UNKNOWN and unidentified
           content are left alone, still refused same as before this
           existed.
        3. Sends Escape EXACTLY ONCE, ever, then re-classifies. Never a
           second keystroke of any kind, regardless of outcome -- a
           misidentified view degrades to "declined something", not
           "approved something", but only if the ONLY key ever sent is
           the one documented to decline/close a readonly view.
        4. Always announces -- spoken (and logged, via speak() itself) --
           whether it succeeded or not. Ayman may have opened the view
           deliberately; a silent dismissal under him is worse than the
           delay, same rule as auto-answers in SPEC-blockers.md.
        """
        for _ in range(STUCK_VIEW_WAIT_ATTEMPTS):
            time.sleep(STUCK_VIEW_WAIT_INTERVAL_S)
            pane_text = self.capture_pane(target)
            if classify_pane_ansi(pane_text, self.patterns) != PaneState.PERSISTENT_VIEW:
                return  # cleared on its own -- nothing to do, not our doing to announce

        plain_text = self.capture_pane_plain(target)
        if not _is_known_readonly_view(plain_text):
            return  # not positively identified -- leave it alone, stays refused

        name = self._friendly_name(target)
        subprocess.run([self.tmux_bin, "send-keys", "-t", target, "Escape"], check=True)

        dismissed = False
        for _ in range(DISMISS_VERIFY_POLL_ATTEMPTS):
            time.sleep(DISMISS_VERIFY_POLL_INTERVAL_S)
            pane_text = self.capture_pane(target)
            if classify_pane_ansi(pane_text, self.patterns) == PaneState.READY:
                dismissed = True
                break

        if dismissed:
            say_feedback.speak(f"{name} had a view open, I dismissed it.")
        else:
            say_feedback.speak(f"{name} had a view open and I couldn't close it cleanly -- check it manually.")

    def deliver(self, target: str, payload: str) -> DeliveryResult:
        if not self.session_exists(target):
            return DeliveryResult(
                ok=False, detail=f"no such tmux session: {target}", reason="no_session"
            )

        # Embedded newlines are NOT safe to send literally: a raw \n inside a
        # single -l payload acts as an actual Enter keypress mid-instruction,
        # which can prematurely submit a partial payload into the target's
        # input box and then keep typing the remainder as a second, unrelated
        # submission. Voice transcripts are single utterances — there's no
        # legitimate case for an embedded Enter — so collapse to whitespace.
        normalized = re.sub(r"[\r\n]+", " ", payload).strip()

        is_safe, view, refusal_reason = check_slash_payload(
            normalized, self.known_commands, self._custom_commands_for(target)
        )
        if not is_safe:
            return DeliveryResult(
                ok=False, detail=f"refused: {refusal_reason}", reason="unsafe_slash"
            )

        pane_text = self.capture_pane(target)
        state = classify_pane_ansi(pane_text, self.patterns)
        if state == PaneState.PERSISTENT_VIEW:
            self._recover_stuck_view(target)
            pane_text = self.capture_pane(target)
            state = classify_pane_ansi(pane_text, self.patterns)
        leading_token = normalized.split(" ", 1)[0]
        busy_tolerated = state == PaneState.BUSY and leading_token in BUSY_TOLERANT_COMMANDS
        if state != PaneState.READY and not busy_tolerated:
            return DeliveryResult(
                ok=False,
                detail=f"refused: {target} pane state is {state.value}, not ready",
                reason=state.value,
            )

        try:
            subprocess.run(
                [self.tmux_bin, "send-keys", "-t", target, "-l", "--", normalized],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [self.tmux_bin, "send-keys", "-t", target, "Enter"],
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as e:
            return DeliveryResult(
                ok=False,
                detail=f"tmux send-keys failed: {e.stderr.decode(errors='replace')}",
                reason="tmux_error",
            )

        if view == "readonly":
            return self._handle_readonly_view(target, normalized)

        return DeliveryResult(ok=True, detail=f"delivered to {target}")

    def _handle_readonly_view(self, target: str, command: str) -> DeliveryResult:
        """
        Capture-then-dismiss-then-return-content flow for a command known
        to open a read-only persistent view (/cost, /usage, /status, ...).

        Never chains a send immediately after Escape without verifying it
        landed — a stray keystroke sequence sent while a view is still
        open/closing can act as UI INPUT rather than text (confirmed live:
        toggled a real setting). So: poll for the view to actually open,
        Escape, then poll again for a genuinely empty prompt before calling
        the delivery complete. If dismissal doesn't verify within the
        window, stop and report it as a delivery failure rather than
        sending anything further into an uncertain state.
        """
        view_content = None
        for _ in range(VIEW_OPEN_POLL_ATTEMPTS):
            time.sleep(VIEW_OPEN_POLL_INTERVAL_S)
            ansi_text = self.capture_pane(target)
            if classify_pane_ansi(ansi_text, self.patterns) == PaneState.PERSISTENT_VIEW:
                view_content = self.capture_pane_plain(target).strip()
                break

        if view_content is None:
            return DeliveryResult(
                ok=False,
                detail=f"{command} on {target} did not open the expected view within "
                f"{VIEW_OPEN_POLL_ATTEMPTS * VIEW_OPEN_POLL_INTERVAL_S:.1f}s",
                reason="view_not_opened",
            )

        subprocess.run([self.tmux_bin, "send-keys", "-t", target, "Escape"], check=True)

        for _ in range(DISMISS_VERIFY_POLL_ATTEMPTS):
            time.sleep(DISMISS_VERIFY_POLL_INTERVAL_S)
            ansi_text = self.capture_pane(target)
            if classify_pane_ansi(ansi_text, self.patterns) == PaneState.READY:
                return DeliveryResult(
                    ok=True, detail=f"delivered to {target}", view_content=view_content
                )

        return DeliveryResult(
            ok=False,
            detail=f"{command}'s view on {target} did not dismiss cleanly after Escape — "
            "pane left in an uncertain state, needs manual attention",
            reason="dismiss_failed",
            view_content=view_content,
        )
