#!/usr/bin/env python3
"""
Pane-state classifier canary.

Run this after any Claude Code update: `python3 pane_state_canary.py`.
It spins up one throwaway tmux Claude Code session, drives it live through
every state classify_pane_ansi() knows about, and asserts the classifier
returns what it's supposed to. Prints the Claude Code version it validated
against and fails loudly (non-zero exit, explicit diagnostic) on any
mismatch.

WHY THIS EXISTS (2026-08-17): Claude Code v2.1.234 silently broke
PERSISTENT_VIEW detection (a taller /cost view pushed its footer marker off
an ordinary 80x24 pane) and, separately, it turned out PERMISSION_PROMPT --
the detector guarding the single most dangerous interaction in this system
-- had NEVER been validated against a real tool-approval prompt at all,
only against a different, differently-shaped onboarding dialog. Both were
found by accident, days apart, while testing something else. This script
converts "silently broke, discovered by luck" into "one command, thirty
seconds, an explicit answer."

DESIGN RULE: every check below is LIVE-DRIVEN -- it puts a real Claude Code
pane into the real state and classifies the real capture. It does NOT use
saved/fixture pane text. A fixture is a fine unit test of parsing logic,
but it cannot detect a UI change (the fixture still contains the old text
and still "passes" -- exactly the failure mode this script exists to
catch). If you're tempted to add a fixture-based check here, add it to a
separate, clearly-labeled test instead, and see the note in pane_state.py's
permission_prompt/persistent_view comments for why this distinction is
load-bearing, not stylistic.

This also asserts DISTINGUISHABILITY, not just "each state is non-READY":
PERMISSION_PROMPT and PERSISTENT_VIEW share the tmux pane-state gate's
refusal behavior today, which previously let a real misclassification
(a live approval prompt read as PERSISTENT_VIEW) hide as "no impact" --
harmless by coincidence, not by design. See git history on pane_state.py
(2026-08-17) for the full incident.
"""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from pane_state import PaneState, PaneStatePatterns, classify_pane_ansi

TMUX_BIN = "tmux"
SESSION = "claude-pane-state-canary"
WORKDIR = Path("/tmp/jarvis-pane-state-canary")
POLL_INTERVAL_S = 1.0
POLL_TIMEOUT_S = 25.0


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""
    note: str = ""


def sh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run([TMUX_BIN, *args], capture_output=True, check=check)


def send_literal(text: str) -> None:
    sh("send-keys", "-t", SESSION, "-l", "--", text)


def send_key(key: str) -> None:
    sh("send-keys", "-t", SESSION, key)


def capture() -> str:
    return sh("capture-pane", "-e", "-p", "-t", SESSION).stdout.decode(errors="replace")


def capture_plain() -> str:
    return sh("capture-pane", "-p", "-t", SESSION).stdout.decode(errors="replace")


def classify(patterns: PaneStatePatterns) -> PaneState:
    return classify_pane_ansi(capture(), patterns)


def poll_until(patterns: PaneStatePatterns, target: PaneState, timeout_s: float = POLL_TIMEOUT_S):
    """Returns (reached: bool, last_state)."""
    start = time.monotonic()
    last = None
    while time.monotonic() - start < timeout_s:
        last = classify(patterns)
        if last == target:
            return True, last
        time.sleep(POLL_INTERVAL_S)
    return False, last


def wait_for_stable_ready(patterns: PaneStatePatterns, timeout_s: float = POLL_TIMEOUT_S) -> bool:
    """poll_until(READY) can catch a momentary READY blip between two busy
    segments of a multi-step turn (confirmed live 2026-08-17: a compound
    instruction produced a brief READY frame mid-turn, and code that
    trusted it as "done" typed into a pane that went busy again a moment
    later). Require two consecutive READY reads, 1s apart, before
    considering it actually settled."""
    start = time.monotonic()
    while time.monotonic() - start < timeout_s:
        reached, _ = poll_until(patterns, PaneState.READY, timeout_s=timeout_s - (time.monotonic() - start))
        if not reached:
            return False
        time.sleep(1)
        if classify(patterns) == PaneState.READY:
            return True
    return False


def state_check(name: str, expected: PaneState, reached: bool, actual: PaneState | None) -> CheckResult:
    got = actual.value if actual is not None else "None"
    return CheckResult(
        name=name,
        passed=reached,
        detail=f"expected {expected.value}, got {got}",
    )


def detect_claude_version() -> str:
    text = capture_plain()
    for line in text.splitlines():
        if "Claude Code v" in line:
            return line.strip().strip("─╮╭ ")
    return "(version banner not found on screen)"


def main() -> int:
    patterns = PaneStatePatterns()
    results: list[CheckResult] = []
    permission_prompt_capture = None
    persistent_view_capture = None
    blocked_question_capture = None
    version = "(unknown -- failed before READY was ever reached)"

    sh("kill-session", "-t", SESSION, check=False)
    WORKDIR.mkdir(parents=True, exist_ok=True)
    sh("new-session", "-d", "-s", SESSION, "-c", str(WORKDIR))

    try:
        # Manual mode deliberately: this is the mode that produces a real
        # tool-approval prompt (auto mode auto-approves everything, which is
        # exactly why PERMISSION_PROMPT never got validated against the real
        # thing until this script existed).
        sh("send-keys", "-t", SESSION, "claude --permission-mode default")
        send_key("Enter")

        # --- READY (fresh launch, empty box) ---
        reached, state = poll_until(patterns, PaneState.READY, timeout_s=20)
        results.append(state_check("READY (fresh launch)", PaneState.READY, reached, state))
        if reached:
            version = detect_claude_version()

        # --- BUSY (mid tool call) ---
        send_literal("run this in bash and wait for it: sleep 6")
        send_key("Enter")
        reached, state = poll_until(patterns, PaneState.BUSY, timeout_s=15)
        results.append(state_check("BUSY (mid tool call)", PaneState.BUSY, reached, state))
        wait_for_stable_ready(patterns, timeout_s=20)  # let it finish before the next check

        # --- Ghost/autosuggest text in the input box (still READY) ---
        # Needs a plausible next-action suggestion to populate; a bare
        # conversational exchange doesn't reliably trigger it, a
        # tool-oriented one does (confirmed live 2026-08-17). Uses
        # wait_for_stable_ready, not poll_until: a compound instruction
        # like this one can produce a momentary READY blip mid-turn
        # (between finishing one tool call and starting to compose the
        # next suggestion) that poll_until would wrongly treat as done.
        send_literal("list the files in this directory using bash ls")
        send_key("Enter")
        wait_for_stable_ready(patterns, timeout_s=25)
        time.sleep(2)  # ghost text populates just after settling to READY
        ghost_ansi = capture()
        ghost_state = classify_pane_ansi(ghost_ansi, patterns)
        idx = ghost_ansi.rfind(patterns.prompt_glyph or "")
        box_has_text = bool(ghost_ansi[idx:].strip()) if idx != -1 else False
        results.append(
            CheckResult(
                name="READY (ghost/autosuggest text in box)",
                passed=(ghost_state == PaneState.READY),
                detail=f"expected ready, got {ghost_state.value}",
                note="box was empty -- ghost text didn't populate in time, inconclusive rather than a real failure"
                if not box_has_text
                else "",
            )
        )

        # --- UNKNOWN (real unsubmitted text) ---
        # Re-confirm stable READY first: the previous step may have left a
        # ghost suggestion in the box (expected, harmless -- typing over
        # ghost text is exactly what's safe) or still be settling.
        wait_for_stable_ready(patterns, timeout_s=15)
        send_literal("zzz canary real typed text, do not submit")
        time.sleep(1)
        state = classify(patterns)
        results.append(
            CheckResult(
                name="UNKNOWN (real unsubmitted text)",
                passed=(state == PaneState.UNKNOWN),
                detail=f"expected unknown, got {state.value}",
            )
        )
        send_key("C-u")  # clear the line without submitting it
        poll_until(patterns, PaneState.READY, timeout_s=10)

        # --- PERMISSION_PROMPT (real tool-approval prompt) ---
        probe_name = "pane_state_canary_probe.txt"
        send_literal(f"write a new file called {probe_name} with content hi")
        send_key("Enter")
        reached, state = poll_until(patterns, PaneState.PERMISSION_PROMPT, timeout_s=20)
        results.append(
            state_check("PERMISSION_PROMPT (real tool-approval prompt)", PaneState.PERMISSION_PROMPT, reached, state)
        )
        if reached:
            permission_prompt_capture = capture()
        send_key("Escape")  # decline -- this menu's own footer documents Escape as cancel
        poll_until(patterns, PaneState.READY, timeout_s=10)
        probe_created = (WORKDIR / probe_name).exists()
        results.append(
            CheckResult(
                name="declining the approval prompt had no side effect",
                passed=not probe_created,
                detail=f"{probe_name} exists: {probe_created}",
                note="FILE WAS CREATED DESPITE DECLINING -- investigate immediately, this is not a detection bug"
                if probe_created
                else "",
            )
        )

        # --- PERSISTENT_VIEW (/cost) ---
        send_literal("/cost")
        send_key("Enter")
        reached, state = poll_until(patterns, PaneState.PERSISTENT_VIEW, timeout_s=15)
        results.append(state_check("PERSISTENT_VIEW (/cost)", PaneState.PERSISTENT_VIEW, reached, state))
        if reached:
            persistent_view_capture = capture()
        send_key("Escape")
        poll_until(patterns, PaneState.READY, timeout_s=10)

        # --- BLOCKED_QUESTION (real AskUserQuestion prompt) ---
        # Structurally the closest state to PERMISSION_PROMPT (both a
        # numbered, arrow-navigable picker) -- exactly the pair most
        # likely to collapse into each other the way PERSISTENT_VIEW and
        # PERMISSION_PROMPT once did, so this check and the
        # distinguishability check below matter more than most.
        send_literal(
            "do not use any skills. just directly ask me a single multiple choice question "
            "using your AskUserQuestion tool about what font size to use, then stop"
        )
        send_key("Enter")
        reached, state = poll_until(patterns, PaneState.BLOCKED_QUESTION, timeout_s=25)
        results.append(state_check("BLOCKED_QUESTION (real AskUserQuestion prompt)", PaneState.BLOCKED_QUESTION, reached, state))
        if reached:
            blocked_question_capture = capture()
        send_key("Escape")  # dismiss -- confirmed live this menu's own footer documents Escape as cancel too
        poll_until(patterns, PaneState.READY, timeout_s=10)

        # --- Distinguishability: not just "both/all non-READY" ---
        if permission_prompt_capture is not None and persistent_view_capture is not None:
            perm_state = classify_pane_ansi(permission_prompt_capture, patterns)
            view_state = classify_pane_ansi(persistent_view_capture, patterns)
            distinguishable = (
                perm_state != view_state
                and perm_state == PaneState.PERMISSION_PROMPT
                and view_state == PaneState.PERSISTENT_VIEW
            )
            results.append(
                CheckResult(
                    name="PERMISSION_PROMPT and PERSISTENT_VIEW are distinguishable from each other",
                    passed=distinguishable,
                    detail=f"permission-prompt capture -> {perm_state.value}, persistent-view capture -> {view_state.value}",
                    note="both captures resolved to the same state -- a collapse regression, the exact failure this check exists for"
                    if not distinguishable
                    else "",
                )
            )
        else:
            results.append(
                CheckResult(
                    name="PERMISSION_PROMPT and PERSISTENT_VIEW are distinguishable from each other",
                    passed=False,
                    detail="skipped",
                    note="one or both source states were never reached above -- cannot evaluate",
                )
            )

        if permission_prompt_capture is not None and blocked_question_capture is not None:
            perm_state = classify_pane_ansi(permission_prompt_capture, patterns)
            blocked_state = classify_pane_ansi(blocked_question_capture, patterns)
            distinguishable = (
                perm_state != blocked_state
                and perm_state == PaneState.PERMISSION_PROMPT
                and blocked_state == PaneState.BLOCKED_QUESTION
            )
            results.append(
                CheckResult(
                    name="PERMISSION_PROMPT and BLOCKED_QUESTION are distinguishable from each other",
                    passed=distinguishable,
                    detail=f"permission-prompt capture -> {perm_state.value}, blocked-question capture -> {blocked_state.value}",
                    note="both captures resolved to the same state -- these two are structurally the closest pair "
                    "this classifier distinguishes (both numbered, arrow-navigable pickers); a collapse here means "
                    "the orchestrator could end up treating a question the same as an authorisation prompt"
                    if not distinguishable
                    else "",
                )
            )
        else:
            results.append(
                CheckResult(
                    name="PERMISSION_PROMPT and BLOCKED_QUESTION are distinguishable from each other",
                    passed=False,
                    detail="skipped",
                    note="one or both source states were never reached above -- cannot evaluate",
                )
            )

    finally:
        sh("kill-session", "-t", SESSION, check=False)
        for p in WORKDIR.glob("*"):
            try:
                p.unlink()
            except OSError:
                pass

    print(f"Claude Code version validated against: {version}\n")
    all_ok = True
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        if not r.passed:
            all_ok = False
        line = f"[{status}] {r.name} -- {r.detail}"
        if r.note:
            line += f"\n       note: {r.note}"
        print(line)

    print(
        "\nAll checks above are LIVE-DRIVEN against a real Claude Code pane "
        "this run -- a pass means the classifier matched reality just now, "
        "not that it matches some saved reference text."
    )

    if not all_ok:
        print(
            "\nCANARY FAILED. The safety gate that refuses to inject keystrokes into "
            "a non-ready pane may be misclassifying a real state. Do not trust "
            "delivery behavior until this is fixed -- see pane_state.py."
        )
        return 1

    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
