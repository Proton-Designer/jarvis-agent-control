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
from abc import ABC, abstractmethod
from dataclasses import dataclass

from pane_state import PaneState, PaneStatePatterns, classify_pane_ansi
from slash_guard import is_safe_slash_payload, load_known_commands


@dataclass
class DeliveryResult:
    ok: bool
    detail: str = ""
    # Structured reason code for callers that need to act on WHY a delivery
    # failed (e.g. batch-delivery retry logic), without string-matching
    # `detail`. One of: None (ok), "no_session", "unsafe_slash", "busy",
    # "permission_prompt", "unknown" (real/ambiguous pane content, not
    # classifiable as ready), "tmux_error".
    reason: str | None = None


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
    """

    def __init__(
        self,
        tmux_bin: str = "tmux",
        patterns: PaneStatePatterns | None = None,
        known_slash_commands: set[str] | None = None,
    ):
        self.tmux_bin = tmux_bin
        self.patterns = patterns or PaneStatePatterns()
        self.known_slash_commands = (
            known_slash_commands if known_slash_commands is not None else load_known_commands()
        )

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

        # A partial/unrecognized slash command can hijack Enter into
        # submitting whatever Claude Code's own completion overlay has
        # highlighted, instead of the literal payload — confirmed live
        # (see pane_state.py VALIDATION NOTES). Refuse rather than risk
        # executing a different command than the one actually resolved.
        if not is_safe_slash_payload(normalized, self.known_slash_commands):
            return DeliveryResult(
                ok=False,
                detail=f"refused: '{normalized}' looks like a slash command but isn't a "
                "complete, known one — partial slash commands can submit a different "
                "command than the one typed",
                reason="unsafe_slash",
            )

        pane_text = self.capture_pane(target)
        state = classify_pane_ansi(pane_text, self.patterns)
        if state != PaneState.READY:
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

        return DeliveryResult(ok=True, detail=f"delivered to {target}")
