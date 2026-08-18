#!/usr/bin/env python3
"""
/btw busy-tolerant-view canary (SPEC-orchestration.md SS1.5).

Run after any Claude Code update: `python3 btw_view_canary.py`. Live-drives
one throwaway tmux Claude Code session (auto mode -- the mode real targets
run in) through the exact scenario /btw exists for and asserts the real
transport.TmuxTransport.deliver() call succeeds through it.

WHY THIS EXISTS (2026-08-18): /btw is the first command that is both
busy-tolerant (BUSY_TOLERANT_COMMANDS) AND opens a view. classify_pane's
single, mutually-exclusive verdict checks BUSY before PERSISTENT_VIEW, so
routing /btw's view-open/dismiss detection through classify_pane_ansi()
would silently hide a genuinely-open, genuinely-answered /btw view for as
long as the target's own unrelated main turn stays busy -- exactly the
scenario /btw is for. transport.py's BUSY_TOLERANT_VIEW_MARKERS +
_busy_tolerant_view_open() exist to detect this by the view's own positive
marker instead. This script proves that against a real overlap, not a
constructed one -- see pane_state_canary.py's own docstring for why every
check here is live-driven rather than fixture-based.

Also asserts the negative: a ordinary BUSY pane with no /btw view open at
all must NOT match the marker (rules out a marker so loose it always
fires, which would make every check above meaningless).
"""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from pane_state import PaneState, PaneStatePatterns, classify_pane_ansi
from transport import TmuxTransport, _busy_tolerant_view_open

TMUX_BIN = "tmux"
SESSION = "claude-btw-view-canary"
WORKDIR = Path("/tmp/jarvis-btw-view-canary")
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
    start = time.monotonic()
    last = None
    while time.monotonic() - start < timeout_s:
        last = classify(patterns)
        if last == target:
            return True, last
        time.sleep(POLL_INTERVAL_S)
    return False, last


def detect_claude_version() -> str:
    text = capture_plain()
    for line in text.splitlines():
        if "Claude Code v" in line:
            return line.strip().strip("─╮╭ ")
    return "(version banner not found on screen)"


def main() -> int:
    patterns = PaneStatePatterns()
    results: list[CheckResult] = []
    version = "(unknown -- failed before READY was ever reached)"

    sh("kill-session", "-t", SESSION, check=False)
    WORKDIR.mkdir(parents=True, exist_ok=True)
    sh("new-session", "-d", "-s", SESSION, "-c", str(WORKDIR))

    try:
        # Auto mode deliberately -- the mode real dispatch targets run in,
        # and the mode /btw's whole "don't interrupt current work" premise
        # assumes (a long tool call genuinely in flight, not a permission
        # prompt sitting open).
        sh("send-keys", "-t", SESSION, "claude")
        send_key("Enter")

        reached, state = poll_until(patterns, PaneState.READY, timeout_s=20)
        results.append(
            CheckResult(name="READY (fresh launch)", passed=reached, detail=f"got {state.value if state else None}")
        )
        if reached:
            version = detect_claude_version()

        # --- Negative control: plain BUSY, no /btw view open at all ---
        send_literal("run this bash command: sleep 8")
        send_key("Enter")
        reached, state = poll_until(patterns, PaneState.BUSY, timeout_s=15)
        results.append(
            CheckResult(name="BUSY (mid tool call, control)", passed=reached, detail=f"got {state.value if state else None}")
        )
        no_view_capture = capture_plain()
        false_positive = _busy_tolerant_view_open(no_view_capture, "/btw")
        results.append(
            CheckResult(
                name="marker does NOT fire on plain BUSY with no /btw view open",
                passed=not false_positive,
                detail=f"_busy_tolerant_view_open returned {false_positive}",
                note="MARKER IS TOO LOOSE -- fires without a real /btw view, every other check here is meaningless"
                if false_positive
                else "",
            )
        )

        # --- The real scenario: /btw sent while genuinely busy, via the ---
        # --- REAL transport.deliver() call, not a hand-rolled poll.     ---
        send_literal("run this bash command: sleep 20")
        send_key("Enter")
        reached, state = poll_until(patterns, PaneState.BUSY, timeout_s=15)
        results.append(
            CheckResult(
                name="BUSY (mid long tool call, before sending /btw)",
                passed=reached,
                detail=f"got {state.value if state else None}",
            )
        )
        # Confirm the overlap is genuine right before the real call: the
        # target must still be BUSY by the classifier's own verdict at
        # this instant, or this check proves nothing about the coexistence
        # case it exists for.
        busy_at_send = classify(patterns) == PaneState.BUSY

        transport = TmuxTransport(patterns=patterns)
        result = transport.deliver(SESSION, "/btw are you currently busy with something else")

        results.append(
            CheckResult(
                name="genuine BUSY/PERSISTENT_VIEW overlap at send time",
                passed=busy_at_send,
                detail=f"classify_pane_ansi() was BUSY at send time: {busy_at_send}",
                note="target wasn't actually busy when /btw was sent -- this run didn't exercise the overlap at all"
                if not busy_at_send
                else "",
            )
        )
        results.append(
            CheckResult(
                name="transport.deliver('/btw ...') succeeds while target is BUSY",
                passed=result.ok and result.view_content is not None,
                detail=f"ok={result.ok}, reason={result.reason}, detail={result.detail!r}",
            )
        )
        if result.ok and result.view_content:
            results.append(
                CheckResult(
                    name="returned view_content contains the /btw answer, not just chrome",
                    passed="esc to close" in result.view_content.lower(),
                    detail=result.view_content[-200:],
                )
            )

        # --- Dismissal actually landed: marker is gone, not just "some ---
        # --- other state was observed" (the false-success hole).       ---
        after_capture = capture_plain()
        still_open = _busy_tolerant_view_open(after_capture, "/btw")
        results.append(
            CheckResult(
                name="/btw view marker is GONE after deliver() returned ok",
                passed=not still_open,
                detail=f"_busy_tolerant_view_open after dismissal: {still_open}",
                note="MARKER STILL PRESENT BUT deliver() REPORTED SUCCESS -- false-success bug, investigate immediately"
                if still_open
                else "",
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
        "this run, including a real concurrent 20s busy tool call -- a pass "
        "means /btw's view was genuinely detected and dismissed while the "
        "target was genuinely busy, not that it matches saved reference text."
    )

    if not all_ok:
        print(
            "\nCANARY FAILED. /btw delivered to a busy target may report "
            "false failures (view_not_opened / dismiss_failed on a delivery "
            "that actually worked) or, worse, false success on a view that "
            "never actually closed. See transport.py's BUSY_TOLERANT_VIEW_MARKERS."
        )
        return 1

    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
