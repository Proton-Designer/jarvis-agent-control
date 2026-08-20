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
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import say_feedback
from pane_state import PaneState, PaneStatePatterns, classify_pane_ansi
from slash_guard import check_slash_payload, load_known_commands
from view_parsers import VIEW_PARSERS

# Per-target-session delivery lock (SPEC-orchestration.md SS0.3). `tmux
# send-keys` is not atomic against a second concurrent writer to the same
# pane -- two genuinely concurrent deliver() calls to the same target
# (a queued instruction racing a /btw side-question, or two team-lead
# callers) can interleave keystrokes into one pane, which is payload
# corruption, not an ordering nuance. Module-level, not an attribute on
# TmuxTransport: this codebase creates more than one TmuxTransport
# instance (providers.py's shared singleton, l2_l3_handoff.py's own
# per-call instance, ...), and an instance-level lock dict would only
# serialize calls that happen to go through the SAME instance -- the
# actual invariant needed is "no two sends to this tmux session name
# overlap, no matter which Transport object issued them." Keyed by
# target (a tmux session name/session_id string), not global: deliveries
# to different targets must not block each other.
_target_locks: dict[str, threading.Lock] = {}
_target_locks_guard = threading.Lock()


def _lock_for_target(target: str) -> threading.Lock:
    with _target_locks_guard:
        lock = _target_locks.get(target)
        if lock is None:
            lock = threading.Lock()
            _target_locks[target] = lock
        return lock

VIEW_OPEN_POLL_ATTEMPTS = 6
VIEW_OPEN_POLL_INTERVAL_S = 0.5
DISMISS_VERIFY_POLL_ATTEMPTS = 6
DISMISS_VERIFY_POLL_INTERVAL_S = 0.5

# docs/TODO-feature-queue.md #5 / SPEC-blockers.md SS5: how long to wait
# for a BLOCKED_QUESTION pane to actually leave that state after the
# answer keystroke is sent, before reporting the delivery as unconfirmed
# rather than assuming it landed. Same shape as DISMISS_VERIFY_POLL_*
# above, separate constants because this is a semantically different
# wait (an answer being processed, not a view closing) even though the
# numbers happen to match today.
ANSWER_VERIFY_POLL_ATTEMPTS = 6
ANSWER_VERIFY_POLL_INTERVAL_S = 0.5

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
BUSY_TOLERANT_COMMANDS = {"/compact", "/btw"}

# Per-command positive view markers for a command that is BOTH busy-
# tolerant AND opens a view (today: only /btw -- SPEC-orchestration.md
# SS1.5). Deliberately NOT added to PaneStatePatterns.persistent_view:
# classify_pane's single, mutually-exclusive verdict is correct for every
# other caller and stays that way (the Lead's ruling, 2026-08-18) -- BUSY
# is checked before PERSISTENT_VIEW there on purpose, and /btw's view can
# legitimately coexist on screen with the target's own unrelated BUSY
# state for its entire lifetime, which is the whole reason the command
# exists ("ask a quick side question without interrupting Claude's
# current work" -- confirmed live it really does run concurrently, not
# queued behind the busy turn: asked live "are you busy right now" while
# a real 20s tool call was in flight, and /btw answered "I'm a separate
# lightweight instance... the main session is carrying on with its own
# work independently"). Routing this through classify_pane_ansi() would
# mean BUSY wins by priority for as long as the target stays busy,
# hiding a view that is genuinely open and genuinely answered --
# confirmed live: /btw's answer was fully rendered on screen while the
# pane still classified as BUSY.
#
# Deliberately a dict keyed by exact command, not a generic "does this
# response have a footer" heuristic or a boolean flag on the transport --
# a future busy-tolerant command must be added here BY NAME, with its own
# verified marker, rather than inheriting this bypass by accident.
#
# "Esc to close" (distinct wording from "esc to cancel", which
# permission_prompt_pairs and blocked_question_pairs already use for
# PERMISSION_PROMPT/BLOCKED_QUESTION -- no collision) verified stable
# live against Claude Code v2.1.234 (2026-08-18) across 3 samples: a
# single short answer, a multi-answer history view, and a still-
# computing "Answering..." state -- present in the footer from the
# moment the view opens in all three, unlike "c to copy"/"f to fork"
# (only appear once an answer is ready) or "x to clear history" (only
# once 2+ entries exist). See btw_view_canary.py.
BUSY_TOLERANT_VIEW_MARKERS = {"/btw": re.compile(r"esc to close", re.IGNORECASE)}

# /btw's view opens (and matches its marker above) BEFORE its own answer
# has actually finished generating -- confirmed live: the marker appears
# immediately, showing a spinner + "Answering..." placeholder, distinct
# from the read-back content itself. Breaking on first-marker-match alone
# (as _handle_readonly_view does for /cost/usage/status, where content is
# static the instant the view opens) would capture that placeholder
# instead of the real answer essentially every time -- the first poll
# tick lands well before a real model call resolves. So the busy-tolerant
# path waits out this SECOND, narrower pending state before treating the
# content as final. Bounded, not indefinite: /btw is still expected to
# answer in a few seconds (SS1's "no latency budget" is Sonnet's routing
# tier, not license for an unbounded wait inside a synchronous delivery
# call) -- times out to whatever was last captured rather than hanging,
# same "never block forever" discipline as every other poll in this file.
BTW_PENDING_MARKER = re.compile(r"answering", re.IGNORECASE)
BTW_ANSWER_SETTLE_POLL_ATTEMPTS = 10
BTW_ANSWER_SETTLE_POLL_INTERVAL_S = 1.0


def _busy_tolerant_view_open(plain_pane_text: str, leading_token: str) -> bool:
    """True only if `leading_token`'s own positive marker is found in the
    capture. False is the only safe default -- never inferred from the
    ABSENCE of some other state. (An earlier draft of the /btw fix
    treated "classify_pane_ansi() says BUSY" as evidence the view had
    closed; that is exactly this project's recurring failure-reads-as-
    success bug, arriving through the fix for it -- BUSY is a legitimate
    SURROUNDING state for this command, never itself the success signal.)
    Scanned against the full capture, not just the tail, matching
    persistent_view's own existing rationale in pane_state.py -- a long
    /btw answer can push its footer well past the last 10 lines."""
    marker = BUSY_TOLERANT_VIEW_MARKERS.get(leading_token)
    return marker is not None and bool(marker.search(plain_pane_text))


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

    def capture_pane_plain(self, target: str, history_lines: int = 0) -> str:
        """Visible pane by default. history_lines > 0 also pulls that many
        lines of scrollback (tmux -S).

        The default stays 0 deliberately: every state CLASSIFIER here wants
        the visible screen and nothing else, because "what is on screen
        right now" is the actual question and scrollback would drag in
        long-dead prompts.

        The parameter exists for the opposite job -- reading a REPLY.
        Found 2026-08-20, twice, as an intermittent team_actions_canary
        failure ("couldn't parse a structured response"): capture-pane
        with no -S returns only what currently fits the pane, and the
        context-capture reply is three lines whose FIRST is SUMMARY. A
        reply long enough to scroll pushes SUMMARY off the top, the parser
        requires all three fields, and it returns None -- reported as "the
        agent didn't follow the format" when the agent had followed it
        perfectly and we simply read too late and too narrow.

        Intermittent because it depends on pane height versus reply length,
        so it fails more the more the agent has to say -- i.e. more often
        on real projects than on the small ones we test with."""
        cmd = [self.tmux_bin, "capture-pane", "-p", "-t", target]
        if history_lines > 0:
            cmd += ["-S", f"-{history_lines}"]
        result = subprocess.run(cmd, check=True, capture_output=True)
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
        """Thin locking wrapper: the entire delivery (pane-state read,
        every send-keys call, the stuck-view/readonly-view dances) runs
        under one per-target lock (SPEC-orchestration.md SS0.3) so two
        concurrent deliver() calls to the SAME target -- from this or any
        other Transport instance, see _lock_for_target's docstring -- can
        never interleave keystrokes into one pane. Calls to different
        targets are unaffected by each other."""
        with _lock_for_target(target):
            return self._deliver_locked(target, payload)

    def _deliver_locked(self, target: str, payload: str) -> DeliveryResult:
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
        leading_token = command.strip().split(" ", 1)[0]
        if leading_token in BUSY_TOLERANT_VIEW_MARKERS:
            # A busy-tolerant readonly command's view can legitimately
            # coexist on screen with the target's own unrelated BUSY
            # state -- classify_pane_ansi()'s single mutually-exclusive
            # verdict would hide it. See BUSY_TOLERANT_VIEW_MARKERS.
            return self._handle_busy_tolerant_view(target, command, leading_token)

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

    def answer_blocked_question(self, target: str, option_index: int) -> DeliveryResult:
        """docs/TODO-feature-queue.md #5 / SPEC-blockers.md SS5: answers a
        real, currently-open AskUserQuestion picker by selecting
        `option_index` (1-based, matching the picker's own numbering) --
        a SEPARATE, narrow path from deliver(), never a relaxation of its
        READY-only gate.

        Verified live (2026-08-20, real pickers, throwaway tmux sessions):
        a single literal digit keystroke, with NO Enter, immediately and
        correctly submits that option. This is also why deliver()'s own
        gate is right to refuse BLOCKED_QUESTION for an ordinary payload:
        a payload that happens to start with a digit matching a real
        option number would silently answer the question with whatever
        digit came first, then type the REST of the payload into
        whatever the pane shows next -- the exact keystrokes-as-UI-input
        hazard already documented for PERSISTENT_VIEW/PERMISSION_PROMPT
        (the /config incident), found again here rather than assumed
        away. Also verified live: selecting the picker's own "N. Type
        something." entry and pressing Enter DECLINES the question
        instead of opening a text field -- so this method never attempts
        free text, only a pre-validated numbered option (see
        blocked_answer.py's _match_option(), which is what decides
        option_index before this is ever called).

        Runs under the SAME per-target lock as deliver() (module-level
        _lock_for_target, not per-instance) -- a concurrent normal
        delivery attempt and an answer attempt to the same target must
        never interleave keystrokes any more than two normal deliveries
        would.

        Refuses -- never sends a keystroke on a guess -- unless the pane
        is POSITIVELY, FRESHLY reconfirmed as BLOCKED_QUESTION at the
        moment of the call, not trusted from whatever state a caller
        last observed: the question may have resolved itself (Ayman
        answered from the console, the session moved on) in the time
        between detection and this call."""
        with _lock_for_target(target):
            return self._answer_blocked_question_locked(target, option_index)

    def _answer_blocked_question_locked(self, target: str, option_index: int) -> DeliveryResult:
        if not self.session_exists(target):
            return DeliveryResult(ok=False, detail=f"no such tmux session: {target}", reason="no_session")
        if option_index < 1:
            return DeliveryResult(ok=False, detail=f"invalid option index: {option_index}", reason="invalid_option")

        ansi_text = self.capture_pane(target)
        state = classify_pane_ansi(ansi_text, self.patterns)
        if state != PaneState.BLOCKED_QUESTION:
            return DeliveryResult(
                ok=False,
                detail=f"refused: {target} is not currently showing a question (pane state is {state.value})",
                reason=state.value,
            )

        try:
            subprocess.run(
                [self.tmux_bin, "send-keys", "-t", target, "-l", "--", str(option_index)],
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as e:
            return DeliveryResult(
                ok=False,
                detail=f"tmux send-keys failed: {e.stderr.decode(errors='replace')}",
                reason="tmux_error",
            )

        for _ in range(ANSWER_VERIFY_POLL_ATTEMPTS):
            time.sleep(ANSWER_VERIFY_POLL_INTERVAL_S)
            ansi_text = self.capture_pane(target)
            if classify_pane_ansi(ansi_text, self.patterns) != PaneState.BLOCKED_QUESTION:
                return DeliveryResult(ok=True, detail=f"answered {target}'s question with option {option_index}")

        return DeliveryResult(
            ok=False,
            detail=f"sent option {option_index} to {target} but it's still showing a question after "
            f"{ANSWER_VERIFY_POLL_ATTEMPTS * ANSWER_VERIFY_POLL_INTERVAL_S:.1f}s -- may not have landed, check manually",
            reason="answer_not_confirmed",
        )

    def _handle_busy_tolerant_view(self, target: str, command: str, leading_token: str) -> DeliveryResult:
        """
        Same capture-then-dismiss-then-return-content contract as
        _handle_readonly_view, for a command whose view can legitimately
        coexist on screen with the target's own unrelated BUSY state
        (today: only /btw). Detection uses the command's own POSITIVE
        marker (BUSY_TOLERANT_VIEW_MARKERS) directly against the raw
        capture, never classify_pane_ansi()'s single mutually-exclusive
        verdict -- that verdict resolves BUSY over PERSISTENT_VIEW by
        priority (correct for every other caller; the Lead's ruling,
        2026-08-18), which would hide this view for as long as the
        target's unrelated main turn stays busy -- exactly the case this
        command exists for.

        Success on both ends is defined by the marker's own presence or
        absence, NEVER by inferring from some other state:
          - open   = marker found in the capture
          - closed = marker ABSENT after Escape
        BUSY is an acceptable surrounding state throughout (the whole
        point of the command) but is never itself read as evidence about
        this view -- an earlier draft of this fix treated "landed back on
        BUSY after Escape" as a successful dismissal, which is exactly
        this project's recurring failure-reads-as-success bug arriving
        through the fix meant to prevent it: if Escape didn't land and
        the view is genuinely still open while the main turn is busy,
        that draft would have reported a clean dismissal anyway.

        Never sends Escape without first POSITIVELY finding the marker --
        same discipline _recover_stuck_view already applies, for the same
        reason: the /config incident happened because
        PaneState.PERSISTENT_VIEW alone was never sufficient to justify a
        keystroke (it shares a marker with /cost/usage/status). If the
        marker is never found, do nothing beyond reporting the failure --
        no Escape sent on a guess.
        """
        view_content = None
        for _ in range(VIEW_OPEN_POLL_ATTEMPTS):
            time.sleep(VIEW_OPEN_POLL_INTERVAL_S)
            plain_text = self.capture_pane_plain(target)
            if _busy_tolerant_view_open(plain_text, leading_token):
                view_content = plain_text.strip()
                break

        if view_content is None:
            return DeliveryResult(
                ok=False,
                detail=f"{command} on {target} did not open the expected view within "
                f"{VIEW_OPEN_POLL_ATTEMPTS * VIEW_OPEN_POLL_INTERVAL_S:.1f}s",
                reason="view_not_opened",
            )

        # The view opened, but its OWN content may still be a pending
        # placeholder (e.g. /btw's "Answering..."), not the real answer
        # yet -- wait that out too, bounded, before treating view_content
        # as final. Re-checks the open marker on every tick as well: if
        # the view closed on its own somehow mid-wait, stop waiting on a
        # marker that's no longer there rather than looping to timeout.
        for _ in range(BTW_ANSWER_SETTLE_POLL_ATTEMPTS):
            if not BTW_PENDING_MARKER.search(view_content):
                break
            time.sleep(BTW_ANSWER_SETTLE_POLL_INTERVAL_S)
            plain_text = self.capture_pane_plain(target)
            if not _busy_tolerant_view_open(plain_text, leading_token):
                break
            view_content = plain_text.strip()

        subprocess.run([self.tmux_bin, "send-keys", "-t", target, "Escape"], check=True)

        for _ in range(DISMISS_VERIFY_POLL_ATTEMPTS):
            time.sleep(DISMISS_VERIFY_POLL_INTERVAL_S)
            plain_text = self.capture_pane_plain(target)
            if not _busy_tolerant_view_open(plain_text, leading_token):
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
