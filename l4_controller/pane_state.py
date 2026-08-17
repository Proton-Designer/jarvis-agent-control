"""
Pane-state classification, used to fail closed before injecting keystrokes.

Blind send-keys into a pane that isn't at an idle input line can get
swallowed, or worse, answer a live permission/y-n prompt with arbitrary
transcribed text. So: capture the pane, classify it, and only ever inject
into READY. Everything else — BUSY, PERMISSION_PROMPT, UNKNOWN — refuses.

Patterns are configurable because "what does idle/busy/a permission prompt
look like" is per-target-app, not universal. Defaults below are validated
against a real, live Claude Code v2.1.233 pane (throwaway tmux sessions,
captured 2026-08-17) — see VALIDATION NOTES at the bottom.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class PaneState(Enum):
    READY = "ready"
    BUSY = "busy"
    PERMISSION_PROMPT = "permission_prompt"
    UNKNOWN = "unknown"


_ANSI_CODE_RE = re.compile(r"\x1b\[([0-9;]*)m")


def _strip_ansi(text: str) -> str:
    return _ANSI_CODE_RE.sub("", text)


def _has_dim_code(text: str) -> bool:
    """True if an SGR code with param exactly '2' (faint/dim) appears."""
    for params in _ANSI_CODE_RE.findall(text):
        parts = params.split(";") if params else ["0"]
        if "2" in parts:
            return True
    return False


@dataclass
class PaneStatePatterns:
    # Checked first — most dangerous state to misclassify. Captured from a
    # real modal (the "Set up auto mode?" onboarding dialog): boxed numbered
    # options with an explicit confirm/cancel footer. Structurally this is
    # the same shape a real tool-permission approval box uses, but an actual
    # tool-permission prompt was NOT captured (auto mode suppressed it for
    # every tool call tested, including `rm`) — see validation notes.
    permission_prompt: list[str] = field(
        default_factory=lambda: [
            r"enter to confirm",
            r"esc to cancel",
            r"do you want to proceed",
            r"\(y\/n\)",
            r"allow this (tool|command)",
        ]
    )

    # Live/active indicator only. Real shape observed: a spinner glyph +
    # present-participle verb + ellipsis, e.g. "✢ Pouncing… (7s · ↓ 220
    # tokens)". Deliberately requires the ellipsis so it does NOT match the
    # leftover completed-turn summary line Claude Code leaves on screen
    # after finishing, e.g. "✻ Churned for 20s" (past tense, no ellipsis,
    # no token count) — that summary persists into the next READY state and
    # is not a busy signal.
    busy: list[str] = field(
        default_factory=lambda: [
            r"[✻✢✽✶⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]\s*\S+…",
            r"↓\s*\d+\s*tokens",
            r"esc to interrupt",
            r"\(ctrl\+b to run in background\)",
            r"sleeping for \d+ seconds",
        ]
    )

    # Fallback for non-Claude-Code targets (e.g. a bare shell): matched
    # against the true last non-blank line when prompt_glyph isn't found.
    ready: list[str] = field(default_factory=list)

    # Claude Code's input box is marked with this leading glyph. Located by
    # scanning the tail for it (NOT by assuming it's the last non-blank line
    # — Claude Code renders a persistent bottom status bar below the input
    # box, so the literal last line is never the prompt).
    prompt_glyph: str | None = "❯"


def classify_pane(pane_text: str, patterns: PaneStatePatterns) -> PaneState:
    """
    pane_text: plain capture-pane output (no -e), used for the busy/
    permission substring scan — unaffected by ANSI noise either way.
    """
    lines = [ln for ln in pane_text.splitlines() if ln.strip()]
    tail_lines = lines[-10:]
    tail = "\n".join(tail_lines)

    for pat in patterns.permission_prompt:
        if re.search(pat, tail, re.IGNORECASE):
            return PaneState.PERMISSION_PROMPT

    for pat in patterns.busy:
        if re.search(pat, tail, re.IGNORECASE):
            return PaneState.BUSY

    if patterns.prompt_glyph:
        prompt_lines = [
            ln.strip() for ln in tail_lines if ln.strip().startswith(patterns.prompt_glyph)
        ]
        if not prompt_lines:
            return PaneState.UNKNOWN
        return PaneState.READY if prompt_lines[-1] == patterns.prompt_glyph else PaneState.UNKNOWN

    if patterns.ready:
        last_line = lines[-1].lower() if lines else ""
        for pat in patterns.ready:
            if re.search(pat, last_line, re.IGNORECASE):
                return PaneState.READY
        return PaneState.UNKNOWN

    return PaneState.UNKNOWN


def classify_pane_ansi(ansi_pane_text: str, patterns: PaneStatePatterns) -> PaneState:
    """
    ansi_pane_text: capture-pane -e output (ANSI/SGR codes preserved).

    Runs the same busy/permission scan as classify_pane (on the ANSI text
    with codes stripped — those checks are plain-substring based and
    unaffected either way), then classifies the input line by what the text
    actually IS rather than where the cursor sits:

    - nothing after the prompt glyph            -> READY (truly empty)
    - text wrapped in a dim/faint SGR (code 2)   -> READY (ghost/autosuggest
      text; the real buffer is empty, so typing directly overwrites it
      cleanly — validated empirically, see VALIDATION NOTES)
    - text with no dim wrapper                   -> UNKNOWN (Ayman's own
      unsubmitted text; refuse rather than risk corrupting/concatenating)

    This replaced an earlier cursor-position-based discriminator
    (cursor_x == input-line start => empty) that was proven wrong: moving
    the cursor back to the start of real, non-empty typed text (e.g. via
    Left/Home — an ordinary editing action, not a constructed edge case)
    produces the exact same cursor_x as a genuinely empty box. The SGR
    check is immune to this because it reads the text's own render
    attributes, not caret position.
    """
    plain = _strip_ansi(ansi_pane_text)
    state = classify_pane(plain, patterns)
    if state != PaneState.READY and state != PaneState.UNKNOWN:
        # BUSY / PERMISSION_PROMPT already decided from the plain-text scan.
        return state
    if not patterns.prompt_glyph:
        return state

    idx = ansi_pane_text.rfind(patterns.prompt_glyph)
    if idx == -1:
        return PaneState.UNKNOWN

    after = ansi_pane_text[idx + len(patterns.prompt_glyph) :]
    nl = after.find("\n")
    if nl != -1:
        after = after[:nl]

    plain_after = _strip_ansi(after).strip()
    if not plain_after:
        return PaneState.READY  # truly empty box
    if _has_dim_code(after):
        return PaneState.READY  # ghost/autosuggest text, buffer is empty
    return PaneState.UNKNOWN  # real unsubmitted text — refuse


# VALIDATION NOTES (2026-08-17, Claude Code v2.1.233, throwaway tmux sessions):
#
# Busy/permission/ready base classification (classify_pane):
# - READY: fresh-launch idle screen — input box renders as a bare "❯" line
#   between two horizontal rules, status bar below it.
# - BUSY: captured two real forms while a `sleep 15` bash tool call was in
#   flight: a tool-progress line ("Sleeping for 15 seconds · 3s" / "⎿ $
#   sleep 15 (3s)" / "(ctrl+b to run in background)") and the model-turn
#   spinner ("✢ Pouncing… (7s · ↓ 220 tokens)"). Also captured the
#   post-completion leftover line ("✻ Churned for 20s") and confirmed it
#   must NOT classify as busy (no ellipsis) — verified the busy regex
#   distinguishes the two.
# - PERMISSION_PROMPT: captured a real modal (auto-mode onboarding dialog:
#   boxed numbered options, "Enter to confirm · Esc to cancel" footer). Did
#   NOT capture an actual tool-call permission prompt — auto mode
#   auto-approved every tool call tested, including an `rm` on a throwaway
#   file. In auto mode (the mode target sessions need to run in for
#   hands-free operation at all), ordinary tool-permission gates mostly
#   won't fire — permission_prompt patterns are validated against a real
#   modal's *shape*, not a live tool-approval box.
#
# Ghost-vs-real input-line discriminator (classify_pane_ansi):
# - Auto mode persistently pre-fills a suggested next command into the
#   input box between turns (confirmed NOT transient — sampled every 3s for
#   30s with zero change, so a bounded-retry-then-refuse approach does not
#   solve this).
# - First attempted discriminator: cursor_x from `tmux display-message -p
#   '#{cursor_x}'`. Ghost text -> cursor_x sits at the input-line start.
#   FALSIFIED: real, non-empty, unsubmitted typed text ("real human typed
#   text right here"), after walking the cursor back to the start with 40x
#   Left, produces the IDENTICAL cursor_x as both the empty box and ghost
#   text. Left/Home is an ordinary editing action, not a constructed edge
#   case, so this discriminator was rejected.
# - Working discriminator: `tmux capture-pane -e` preserves ANSI/SGR codes.
#   Ghost text is rendered dim/faint: `<ESC>[39m❯ <ESC>[2m<text><ESC>[0m`
#   (SGR 2 = faint). Real typed text carries no such wrapper at all:
#   `<ESC>[39m❯ <text>`, verbatim, in BOTH cursor-at-end and
#   cursor-walked-to-start states. Validated across: empty box, ghost text,
#   real typed (cursor at end), real typed (cursor walked to start) — all
#   four classify correctly and the SGR check does not depend on cursor
#   position at all.
#
# Slash-command hazard (informs the L4 slash-command guard, not this file):
# - A COMPLETE but unrecognized slash word ("/zzz-not-a-real-command-99")
#   submits safely: Claude Code responds "Unknown command: ..." with no
#   side effect.
# - A PARTIAL slash word ("/comp", with the completion overlay open showing
#   "/compact" as the top suggestion) does NOT submit the literal typed
#   text on Enter — it submits "/compact" instead, a different command than
#   what was actually typed. Confirmed via a live throwaway session. This
#   is a real hazard, not theoretical: any payload reaching the transport
#   that is a partial/incomplete slash command can silently execute a
#   different command than intended. The transport must refuse (not
#   deliver) any payload starting with "/" that isn't a complete, exact,
#   known command.
#
# NOT tested: false-refusal rate over a longer real session — needs a
# longer live run to measure, not a single-session snapshot pass.
