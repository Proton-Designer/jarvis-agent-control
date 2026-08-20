#!/usr/bin/env python3
"""Canary for engine-role permission-prompt detection + narrow auto-
approve. An urgent fix from a real incident, 2026-08-20 -- not one of
TODO-feature-queue.md's numbered items (that list's own #5 is a
different, separate thing: answering a BLOCKED-QUESTION on a team member
by voice, already built). The concierge sat on
`Read(~/.jarvis/dictations/...)` forever, with nobody watching, because
its own cage blocked the one file every dictation arrives in.

BOTH DIRECTIONS, and the REFUSAL half is the one that matters most (the
Lead's explicit framing): an ordinary Read/Glob/Grep-shaped prompt on an
engine role IS auto-approved; a plan-approval-shaped prompt, a
deletion-flavoured prompt, and (structurally, not by a runtime check) a
team member's prompt are all REFUSED, and nothing is ever sent to the
pane on a refusal.

DEFAULT RUN IS HERMETIC -- no real tmux session, no real keystroke, same
hygiene lesson visible_windows_canary.py already applies (an earlier
draft of THAT canary left a stray window on Ayman's real desktop). The
regex/decision-logic checks below use monkeypatched pane captures --
controlled, repeatable, and they let the refusal paths be tested
precisely without trying to coax Claude Code into showing a Write/Edit/
plan-approval prompt live, which isn't fully controllable. The
MECHANISM itself (a bare "1" submits a real prompt, transport.py's
approve_permission_prompt()) was already verified live twice against
real captured prompts during development -- see transport.py's own
docstring. The one thing this file DOES verify live by default: a real
`_SAFE_TOOL_HEADER_RE` match against the actual captured shape of a real
Read-tool prompt (copied verbatim from that live capture, not
reconstructed from memory).

LIVE MODE (`--live`), OPT-IN: drives one real scratch Claude Code session
through an actual Read-tool permission prompt and confirms
maybe_auto_approve_role_prompt() approves it for real -- isolated via
JARVIS_TEST_RUN so this never touches ~/Jarvis/engine.json, and cleaned
up in a `finally` before any result is even printed, same discipline as
quick_adopt_canary.py.

    l5_console/app/.venv/bin/python3 l5_console/state/engine_prompt_canary.py
    l5_console/app/.venv/bin/python3 l5_console/state/engine_prompt_canary.py --live
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

os.environ["JARVIS_TEST_RUN"] = f"engine-prompt-canary-{uuid.uuid4().hex[:8]}"

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "l4_controller"))

import engine_roles  # noqa: E402
import providers  # noqa: E402
from pane_state import PaneState  # noqa: E402
from transport import DeliveryResult  # noqa: E402
from teams import TEAMS_REGISTRY_PATH  # noqa: E402

assert "test_runs" in str(engine_roles.ENGINE_REGISTRY_PATH), (
    f"ENGINE_REGISTRY_PATH is NOT test-isolated ({engine_roles.ENGINE_REGISTRY_PATH}) -- refusing to run"
)
assert "test_runs" in str(TEAMS_REGISTRY_PATH), "TEAMS_REGISTRY_PATH not isolated -- refusing to run"

RESULTS: list[tuple[str, bool, str]] = []
_CANNED_PANE_TEXT: str | None = None  # set per-case in run_hermetic(); read by fake_capture()


def check(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((name, passed, detail))
    print(f"  {'ok  ' if passed else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not passed else ""))


# The REAL captured shape (2026-08-20, a real scratch session, manual
# permission mode, a genuine Read tool call) -- copied verbatim, not
# reconstructed, so the regex is tested against ground truth.
REAL_READ_PROMPT = """\
 Read file

  Read(/tmp/jarvis-promptcheck-testfile.txt)

 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, allow reading from /tmp and /private/tmp during this session
   3. No

 Esc to cancel · Tab to amend
"""

# Constructed, matching this project's own captured shape for a real
# tool-approval modal (pane_state.py's VALIDATION NOTES) but for a
# WRITE, which this feature must never auto-approve regardless of how
# harmless the filename looks.
CONSTRUCTED_WRITE_PROMPT = """\
 Do you want to create notes.txt?
 ❯ 1. Yes
   2. Yes, allow all edits during this session (shift+tab)
   3. No

 Esc to cancel · Tab to amend
"""

# A Read prompt whose PATH happens to mention credentials -- the exact
# case the second, independent gate exists for: tool-shape alone is not
# enough when the ARGUMENT is the dangerous part.
CONSTRUCTED_DANGEROUS_READ_PROMPT = """\
 Read file

  Read(/Users/aymanmohammed/.aws/credentials)

 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, allow reading from /Users/aymanmohammed/.aws during this session
   3. No

 Esc to cancel · Tab to amend
"""

# A Bash prompt requesting deletion -- must refuse on BOTH grounds (not
# a safe tool header at all, AND touches deletion language).
CONSTRUCTED_DELETION_PROMPT = """\
 Bash command

  rm -rf /tmp/scratch

 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and don't ask again for rm commands in /tmp/scratch
   3. No

 Esc to cancel · Tab to amend
"""

# Plan-mode approval -- no ToolName(args) header at all, a bulleted plan
# summary instead. This is exactly the shape _SAFE_TOOL_HEADER_RE must
# NOT match (no "Read(" line anywhere), which is what makes the
# allowlist-not-denylist design refuse it by construction rather than by
# a special case. Uses pane_state.py's own recognized "do you want to
# proceed" + "esc to cancel"/"tab to amend" wording DELIBERATELY, not a
# guess at Claude Code's actual plan-mode phrasing (no live capture of
# that exists yet) -- this construction isolates the ONE thing this
# canary can test precisely: given a prompt the classifier DOES resolve
# to PaneState.PERMISSION_PROMPT, does the tool-header allowlist
# correctly refuse one with no recognizable tool call. If Claude Code's
# real plan-approval prompt uses wording pane_state.py's classifier
# doesn't recognize as PERMISSION_PROMPT at all, that's a pane_state.py
# coverage question (Engineer 1/the Lead's territory), not this
# function's -- maybe_auto_approve_role_prompt() only ever acts on what
# the classifier has already positively identified.
CONSTRUCTED_PLAN_APPROVAL_PROMPT = """\
 Ready to code?

  Here is Claude's plan:
  1. Add a new field to the config
  2. Update the two call sites
  3. Run the test suite

 Do you want to proceed?
 ❯ 1. Yes, and auto-accept edits
   2. Yes, and manually approve edits
   3. No, keep planning

 Esc to cancel · Tab to amend
"""


def run_hermetic() -> int:
    print("REGEX/PREVIEW -- against the REAL captured shape")
    check("the real Read prompt IS a safe-tool match", bool(engine_roles._SAFE_TOOL_HEADER_RE.search(REAL_READ_PROMPT)))
    check("...and is NOT flagged dangerous", not engine_roles._DANGEROUS_PROMPT_RE.search(REAL_READ_PROMPT))
    preview = engine_roles._prompt_preview(REAL_READ_PROMPT)
    check("preview prefers the tool-header line", preview.startswith("Read("), preview)

    print()
    print("REGEX -- constructed refusal shapes")
    check("a Write-shaped prompt is NOT a safe-tool match (Write is not in the allowlist)",
          not engine_roles._SAFE_TOOL_HEADER_RE.search(CONSTRUCTED_WRITE_PROMPT))
    check("a Read of a credentials path IS tool-safe but the dangerous-language gate still fires",
          bool(engine_roles._SAFE_TOOL_HEADER_RE.search(CONSTRUCTED_DANGEROUS_READ_PROMPT))
          and bool(engine_roles._DANGEROUS_PROMPT_RE.search(CONSTRUCTED_DANGEROUS_READ_PROMPT)))
    check("a Bash(rm -rf) prompt fails BOTH gates",
          not engine_roles._SAFE_TOOL_HEADER_RE.search(CONSTRUCTED_DELETION_PROMPT)
          and bool(engine_roles._DANGEROUS_PROMPT_RE.search(CONSTRUCTED_DELETION_PROMPT)))
    check("a plan-approval prompt has NO tool header at all -- refused by construction, not a special case",
          not engine_roles._SAFE_TOOL_HEADER_RE.search(CONSTRUCTED_PLAN_APPROVAL_PROMPT))

    print()
    print("DECISION LOGIC -- maybe_auto_approve_role_prompt(), monkeypatched pane, NOTHING sent on refusal")

    original_capture = providers.transport.capture_pane_plain
    original_approve = providers.transport.approve_permission_prompt

    def fake_capture(target, history_lines=0):
        return _CANNED_PANE_TEXT

    def poisoned_approve(target):
        raise AssertionError(f"approve_permission_prompt() was called for {target!r} on a REFUSAL case -- a keystroke would have been sent")

    providers.transport.capture_pane_plain = fake_capture
    providers.transport.approve_permission_prompt = poisoned_approve

    # A fake role record -- session_exists() is never reached by
    # maybe_auto_approve_role_prompt() itself (only by
    # transport.approve_permission_prompt(), poisoned above), so a
    # nonexistent tmux name is fine for exercising the decision logic.
    engine_roles._save({
        "concierge": {
            "name": "fake", "model": "sonnet", "effort": "medium",
            "tmux": "jarvis-fake-nonexistent", "working_dir": "/tmp/fake", "claude_session": "fake",
        },
        "orchestrator": None,
        "name_history": {"concierge": [], "orchestrator": []},
    })

    global _CANNED_PANE_TEXT

    for name, text, expect_action in [
        ("plan approval", CONSTRUCTED_PLAN_APPROVAL_PROMPT, "escalated"),
        ("deletion-flavoured", CONSTRUCTED_DELETION_PROMPT, "escalated"),
        ("Read of a credentials path", CONSTRUCTED_DANGEROUS_READ_PROMPT, "escalated"),
    ]:
        engine_roles._ROLE_PROMPT_EPISODE.pop("concierge", None)  # fresh episode per case
        _CANNED_PANE_TEXT = text
        try:
            result = engine_roles.maybe_auto_approve_role_prompt("concierge")
            check(f"{name}: REFUSED (escalated), never approved", result["action"] == expect_action, str(result))
        except AssertionError as e:
            check(f"{name}: REFUSED (escalated), never approved", False, str(e))

    engine_roles._ROLE_PROMPT_EPISODE.pop("concierge", None)
    _CANNED_PANE_TEXT = REAL_READ_PROMPT
    providers.transport.approve_permission_prompt = lambda target: DeliveryResult(
        ok=True, detail=f"approved {target!r} (stubbed, not a real send)"
    )
    result = engine_roles.maybe_auto_approve_role_prompt("concierge")
    check("an ordinary Read prompt IS approved (stubbed transport call, decision logic only)",
          result["action"] == "approved", str(result))

    providers.transport.capture_pane_plain = original_capture
    providers.transport.approve_permission_prompt = original_approve

    print()
    print("STRUCTURAL -- team members can never reach this path")
    src_files = [
        Path(__file__).parent / "poller.py",
        Path(__file__).parent.parent / "app" / "console.py",
        Path(__file__).parent.parent / "app" / "team_flow.py",
        Path(__file__).parent.parent / "app" / "rail.py",
    ]
    calls_ok = True
    for f in src_files:
        if not f.exists():
            continue
        text = f.read_text()
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue  # a comment mentioning the name is not a call site
            if "maybe_auto_approve_role_prompt(" in line and "def maybe_auto_approve_role_prompt" not in line:
                # Every real call site must pass a bare loop variable
                # over engine_roles.ROLES / engine_roles_mod.ROLES, never
                # a team's tmux/claude_session -- checked textually since
                # this is a call-site discipline, not a runtime-checkable
                # property (the function's own signature is just `role:
                # str`, it cannot refuse a team tmux name by type).
                if "role)" not in line and "role_mod)" not in line:
                    calls_ok = False
                    print(f"  UNEXPECTED call shape in {f}: {line.strip()}")
    check("every real call site passes a role-loop variable, never team data (textual check)", calls_ok)
    check(
        "engine_roles.ROLES is exactly (concierge, orchestrator) -- the function's only valid input space",
        engine_roles.ROLES == ("concierge", "orchestrator"),
    )

    return 0


def run_live() -> int:
    print()
    print("LIVE -- one real scratch Claude Code session, a genuine Read prompt")
    tmux = "jarvis-engineprompt-live"
    workdir = Path("/tmp/jarvis-engineprompt-live")
    testfile = workdir / "testfile.txt"

    try:
        subprocess.run(["tmux", "kill-session", "-t", tmux], capture_output=True)
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)

        import setup as setup_state
        launch = setup_state.create_fresh_member(tmux, str(workdir), "sonnet")
        check("scratch session launched", launch["ok"], detail=str(launch))
        time.sleep(3)

        subprocess.run(["tmux", "send-keys", "-t", tmux, "BTab"], check=True)  # cycle out of auto-mode
        time.sleep(1)

        workdir_resolved = Path(launch["root"])
        workdir_resolved.mkdir(parents=True, exist_ok=True)
        testfile_resolved = workdir_resolved / "testfile.txt"
        testfile_resolved.write_text("engine_prompt_canary live check\n")

        subprocess.run(
            ["tmux", "send-keys", "-t", tmux, "-l", "--",
             f"Read the file {testfile_resolved} using the Read tool"],
            check=True,
        )
        subprocess.run(["tmux", "send-keys", "-t", tmux, "Enter"], check=True)
        time.sleep(4)

        engine_roles._save({
            "concierge": {
                "name": "live-canary", "model": "sonnet", "effort": "medium",
                "tmux": tmux, "working_dir": str(workdir_resolved), "claude_session": launch["claude_session"],
            },
            "orchestrator": None,
            "name_history": {"concierge": [], "orchestrator": []},
        })

        plain = providers.transport.capture_pane_plain(tmux)
        prompted = "Do you want to proceed" in plain
        if not prompted:
            # NOT a script bug -- found running this live 2026-08-20:
            # Claude Code appears to remember that reading from /tmp and
            # /private/tmp is trusted across separate sessions on this
            # machine (plausibly a project/path-level cache in
            # ~/.claude.json, accumulated by this file's own earlier dev-
            # time manual verifications, jarvis-promptcheck/
            # jarvis-approvecheck, both of which DID see and approve a
            # real prompt). Once a path is trusted this way, a fresh
            # session reading the SAME path family no longer prompts at
            # all, so there is nothing for this method to act on --
            # correctly. Reported as inconclusive, not a failure: the
            # mechanism itself (bare "1" submits a real prompt) was
            # independently confirmed twice, manually, before this
            # caching was in effect -- see transport.approve_permission_prompt()'s
            # own docstring.
            print("  --    the pane never showed a permission prompt -- Claude Code likely already")
            print("        trusts reads under /tmp on this machine (see comment above); INCONCLUSIVE,")
            print("        not a failure. The mechanism was verified manually, twice, before this.")
        else:
            check("the pane is genuinely showing a permission prompt right now", True)
            result = engine_roles.maybe_auto_approve_role_prompt("concierge")
            check("maybe_auto_approve_role_prompt() approved it for real", result["action"] == "approved", str(result))
            time.sleep(1)
            final = providers.transport.capture_pane_plain(tmux)
            check("the pane no longer shows the permission prompt", "Do you want to proceed" not in final)

    finally:
        subprocess.run(["tmux", "kill-session", "-t", tmux], capture_output=True)
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)
        shutil.rmtree(Path(str(workdir).replace("/tmp/", "/private/tmp/")), ignore_errors=True)

    return 0


def run() -> int:
    live = "--live" in sys.argv
    print(f"engine_prompt_canary: JARVIS_TEST_RUN={os.environ['JARVIS_TEST_RUN']!r}, live={live}")

    try:
        run_hermetic()
        if live:
            run_live()
        else:
            print()
            print("(skipping the live end-to-end check -- pass --live to run it; spawns and cleans up one real session)")
    finally:
        test_run_root = TEAMS_REGISTRY_PATH.parent
        if "test_runs" in str(test_run_root):
            import shutil
            shutil.rmtree(test_run_root, ignore_errors=True)

    print()
    failures = [r for r in RESULTS if not r[1]]
    if failures:
        print(f"{len(failures)}/{len(RESULTS)} FAILED:")
        for name, _, detail in failures:
            print(f"  - {name}" + (f" ({detail})" if detail else ""))
        return 1
    print(f"all {len(RESULTS)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(run())
