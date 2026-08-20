#!/usr/bin/env python3
"""Canary for answering a blocked agent by voice (docs/TODO-feature-
queue.md #5, SPEC-blockers.md SS5). Live-driven against REAL
AskUserQuestion pickers -- three real throwaway `claude` sessions, real
tmux, real keystrokes -- because the one thing this feature lives or
dies on (a bare digit keystroke immediately and correctly submits an
option; "Type something." + Enter DECLINES instead of opening a text
field) was found by testing, not documentation, and nothing about it is
safe to trust from a mock.

Proves the asymmetry the Lead asked for explicitly, in both directions:
  - ANSWER misread as DISPATCH is loud and recoverable: an answer that
    doesn't cleanly match any captured option REFUSES (check 2) rather
    than guessing which option was meant -- the router hears a refusal
    and falls back to normal handling, out loud, not silently.
  - A DISPATCH-shaped utterance can't silently land as an ANSWER: the
    structural reason is the same refusal path (check 2) -- ordinary
    instruction text essentially never cleanly matches a real option
    label, so it gets refused the same way a genuine non-match does.
    Also: with nothing pending at all, the whole feature is unreachable
    (check 6) -- "the class does not exist" is checked directly, not
    assumed.

Also proves hold-and-ask (check 3/4) and the held-instruction-lifecycle
rule this module reuses: a question whose session died is dropped
immediately, not aged out (check 5).

ISOLATED VIA JARVIS_TEST_RUN (blocked_state.json, teams.json,
latency_log.jsonl all redirect) -- never touches ~/Jarvis or ~/.jarvis's
real files. Also sets JARVIS_NO_RETURN_QUEUE_WORKER=1 per the Lead's
2026-08-20 guard, even though this canary never calls jarvis_say/speak
itself -- belt and suspenders against anything imported transitively.

SLOW (three real Claude Code launches). Run after touching
blocked_answer.py, providers.py's pending_questions()/
_parse_blocked_question(), or transport.answer_blocked_question():

    l4_controller/.venv/bin/python3 l4_controller/blocked_answer_canary.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

os.environ["JARVIS_TEST_RUN"] = f"blocked-answer-canary-{uuid.uuid4().hex[:8]}"
os.environ["JARVIS_NO_RETURN_QUEUE_WORKER"] = "1"

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
# Canary-only, not production: reconnect.wait_for_ready() is an l5_console
# test/launch helper. Fine to reach for here (this script is never part
# of an MCP server's own process, so it isn't subject to the import-graph
# guarantee mcp_surface_canary.py checks for server_readonly.py).
sys.path.insert(0, str(Path(__file__).parent.parent / "l5_console" / "state"))

import blocked_answer  # noqa: E402
import blocked_state  # noqa: E402
import providers  # noqa: E402
from jarvis_paths import jarvis_home, jarvis_project_home  # noqa: E402
from pane_state import PaneState, classify_pane_ansi  # noqa: E402
from reconnect import wait_for_ready  # noqa: E402

assert "test_runs" in str(jarvis_home()), f"jarvis_home() not isolated ({jarvis_home()}) -- refusing to run"
assert "test_runs" in str(jarvis_project_home()), f"jarvis_project_home() not isolated ({jarvis_project_home()}) -- refusing to run"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((name, passed, detail))
    print(f"  {'ok  ' if passed else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not passed else ""))


WORKDIR = Path("/tmp/jarvis-blocked-answer-canary")
TEAMS_JSON_PATH = jarvis_project_home() / "teams.json"

_sessions: list[str] = []  # tmux names launched, for cleanup


def _launch_and_ask(name: str, question: str, opt_a: str, opt_b: str) -> str:
    """Launches a real throwaway `claude` session and drives it to a real
    AskUserQuestion picker with exactly two options. Returns the tmux
    name once the picker is confirmed on screen."""
    tmux_name = f"jarvis-blocked-answer-canary-{name}"
    _sessions.append(tmux_name)
    subprocess.run(["tmux", "kill-session", "-t", tmux_name], capture_output=True)
    d = WORKDIR / name
    d.mkdir(parents=True, exist_ok=True)
    subprocess.run(["tmux", "new-session", "-d", "-s", tmux_name, "-c", str(d), "claude"], check=True)
    if not wait_for_ready(tmux_name, timeout_s=40):
        raise RuntimeError(f"{tmux_name} never reached READY")
    prompt = f"Use the AskUserQuestion tool right now to ask me: {question} with options {opt_a} and {opt_b}."
    subprocess.run(["tmux", "send-keys", "-t", tmux_name, "-l", "--", prompt], check=True)
    subprocess.run(["tmux", "send-keys", "-t", tmux_name, "Enter"], check=True)
    deadline = time.monotonic() + 40.0
    last_state = None
    while time.monotonic() < deadline:
        ansi = providers.transport.capture_pane(tmux_name)
        last_state = classify_pane_ansi(ansi, providers.transport.patterns)
        if last_state == PaneState.BLOCKED_QUESTION:
            return tmux_name
        time.sleep(1.0)
    raise RuntimeError(f"{tmux_name} never reached a real BLOCKED_QUESTION picker (last state: {last_state}, last capture: {providers.transport.capture_pane_plain(tmux_name)[-500:]!r})")


def _register_team(team_id: str, claude_session: str, tmux: str) -> None:
    registry = json.loads(TEAMS_JSON_PATH.read_text()) if TEAMS_JSON_PATH.exists() else []
    registry.append({
        "id": team_id, "aliases": [team_id], "root": str(WORKDIR / team_id),
        "members": [{"tmux": tmux, "claude_session": claude_session, "model": "sonnet"}],
    })
    TEAMS_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    TEAMS_JSON_PATH.write_text(json.dumps(registry))


def main() -> int:
    WORKDIR.mkdir(parents=True, exist_ok=True)

    try:
        # --- Session A: real picker, staging/production ------------------
        print("Launching session A (real AskUserQuestion picker)...")
        tmux_a = _launch_and_ask("a", "Should I use staging or production?", "staging", "production")
        plain_a = providers.transport.capture_pane_plain(tmux_a)
        parsed_a = providers._parse_blocked_question(plain_a)
        check("session A: a real picker was parsed", parsed_a is not None, detail=repr(plain_a[-400:]))
        if parsed_a is None:
            raise RuntimeError("cannot continue without a parsed question")

        print("\n1. the fixed options parser excludes UI chrome, not real options")
        check(
            "'Type something.' is NOT in the captured options (the bug this build found and fixed)",
            "Type something." not in parsed_a["options"],
            detail=str(parsed_a["options"]),
        )
        check(
            "'Chat about this' is NOT in the captured options",
            not any("chat about this" in o.lower() for o in parsed_a["options"]),
            detail=str(parsed_a["options"]),
        )
        check(
            "exactly the two real options were captured",
            len(parsed_a["options"]) == 2,
            detail=str(parsed_a["options"]),
        )

        session_a = "canary-claude-session-a"
        blocked_state.mark_blocked(session_a, parsed_a["question"], parsed_a["options"])
        _register_team("team-a", session_a, tmux_a)

        print("\n2. ANSWER misread as DISPATCH is loud and recoverable -- a non-matching answer REFUSES")
        result = blocked_answer.answer_blocked_session("please go ahead and refactor the auth module")
        check("a dispatch-shaped, non-matching utterance is REFUSED, not guessed at", result["ok"] is False, detail=str(result))
        ansi = providers.transport.capture_pane(tmux_a)
        check(
            "the picker is UNTOUCHED after the refusal -- no keystroke was sent on the guess",
            classify_pane_ansi(ansi, providers.transport.patterns) == PaneState.BLOCKED_QUESTION,
            detail=str(classify_pane_ansi(ansi, providers.transport.patterns)),
        )

        # --- Session B: second real picker, for hold-and-ask -------------
        print("\nLaunching session B (real AskUserQuestion picker)...")
        tmux_b = _launch_and_ask("b", "Should I use Postgres or MySQL?", "Postgres", "MySQL")
        plain_b = providers.transport.capture_pane_plain(tmux_b)
        parsed_b = providers._parse_blocked_question(plain_b)
        check("session B: a real picker was parsed", parsed_b is not None, detail=repr(plain_b[-400:]))
        session_b = "canary-claude-session-b"
        blocked_state.mark_blocked(session_b, parsed_b["question"], parsed_b["options"])
        _register_team("team-b", session_b, tmux_b)

        print("\n3. hold-and-ask: 2 pending, no target -- REFUSES and names both")
        result = blocked_answer.answer_blocked_session("staging")
        check("refused rather than guessing which of the two", result["ok"] is False, detail=str(result))
        check("names team-a in the refusal", "team-a" in result["detail"], detail=result["detail"])
        check("names team-b in the refusal", "team-b" in result["detail"], detail=result["detail"])

        print("\n4. disambiguated by target -- delivers the REAL answer via a REAL keystroke")
        result = blocked_answer.answer_blocked_session("staging", target="team-a")
        check("answer to team-a, disambiguated by target, succeeds", result["ok"] is True, detail=str(result))
        ansi = providers.transport.capture_pane(tmux_a)
        check(
            "session A's picker actually resolved (no longer BLOCKED_QUESTION) -- the digit really landed",
            classify_pane_ansi(ansi, providers.transport.patterns) != PaneState.BLOCKED_QUESTION,
            detail=str(classify_pane_ansi(ansi, providers.transport.patterns)),
        )

        print("\n   ...now only team-b is pending -- auto-resolves with no target needed")
        result = blocked_answer.answer_blocked_session("MySQL")
        check("answer to the one remaining pending session succeeds with no target", result["ok"] is True, detail=str(result))
        check("delivered the RIGHT team (team-b)", result["team_id"] == "team-b", detail=str(result))

        # --- Session C: dies before being answered ------------------------
        print("\nLaunching session C (real AskUserQuestion picker, then killing it unanswered)...")
        tmux_c = _launch_and_ask("c", "Should I deploy now or wait?", "now", "wait")
        plain_c = providers.transport.capture_pane_plain(tmux_c)
        parsed_c = providers._parse_blocked_question(plain_c)
        session_c = "canary-claude-session-c"
        blocked_state.mark_blocked(session_c, parsed_c["question"], parsed_c["options"])
        _register_team("team-c", session_c, tmux_c)
        subprocess.run(["tmux", "kill-session", "-t", tmux_c], check=True)
        time.sleep(1.0)

        print("\n5. held-instruction lifecycle: a dead session's question is DROPPED, not aged out")
        pending_now = providers.pending_questions()
        check(
            "team-c is no longer in pending_questions() -- dropped immediately on death, not on a timer",
            not any(p["team_id"] == "team-c" for p in pending_now),
            detail=str(pending_now),
        )
        check(
            "blocked_state itself was cleared for the dead session (self-healed, not just filtered)",
            blocked_state.get_blocked(session_c) is None,
        )
        result = blocked_answer.answer_blocked_session("now", target="team-c")
        check("answering the now-dead team-c refuses cleanly (nothing left to answer)", result["ok"] is False, detail=str(result))

        print("\n6. with NOTHING pending, the whole class is unreachable")
        result = blocked_answer.answer_blocked_session("staging")
        check("no pending questions at all -> refuses (the class does not exist)", result["ok"] is False, detail=str(result))
        check("says nothing is waiting", "waiting" in result["detail"].lower(), detail=result["detail"])

    finally:
        for tmux_name in _sessions:
            subprocess.run(["tmux", "kill-session", "-t", tmux_name], capture_output=True)
        import shutil
        shutil.rmtree(WORKDIR, ignore_errors=True)
        shutil.rmtree(Path(str(WORKDIR).replace("/tmp/", "/private/tmp/")), ignore_errors=True)
        for isolated_path in (jarvis_home(), jarvis_project_home()):
            if "test_runs" in str(isolated_path):
                shutil.rmtree(isolated_path, ignore_errors=True)

    all_ok = all(r[1] for r in RESULTS)
    passed_n = sum(1 for r in RESULTS if r[1])
    print(f"\n{'All checks passed.' if all_ok else 'CANARY FAILED.'} ({passed_n}/{len(RESULTS)})")
    if not all_ok:
        print("Do not trust answer-blocked-session until this is fixed.")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
