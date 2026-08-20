#!/usr/bin/env python3
"""Canary for member_identity.py (SPEC-orchestration.md Known Gaps:
"a restarted team lead reads as READY"). Proves the real
verify_and_refresh_identity() function against a controlled fake registry
and a monkeypatched claude_session_id() (never touches a real tmux
session or pane -- no /status is actually sent anywhere in this run).

ISOLATED VIA JARVIS_TEST_RUN, NOT BACKUP/RESTORE. This file used to back
up ~/Jarvis/teams.json and restore it in a finally block -- the same
pattern that caused a real incident (2026-08-18, see
team_actions_canary.py's docstring for the full account): a canary's
backup/restore window is a promise, not a guarantee, and this file's own
TEST_REGISTRY used "gateway" as its team_id -- the EXACT id the Lead
registered for a real end-to-end test the same day. jarvis_paths.
jarvis_project_home() now makes TEAMS_REGISTRY_PATH resolve to an
isolated path under JARVIS_TEST_RUN (set below, before teams.py is
imported, since the path is a module-level constant resolved at import
time) -- this canary structurally cannot touch the real file at all,
regardless of how it exits.

Also proves the end-to-end wiring into tools_write.deliver_batch(): a
mismatched member gets an audible flag AND still receives its delivery --
never silently blocked, never silently proceeding as though nothing
happened.

MUST run via l4_controller/.venv (2026-08-20: say_feedback.py now imports
kokoro_tts at module load, transitively pulled in through tools_write.py).

Run after touching member_identity.py or tools_write.py's use of it:

    l4_controller/.venv/bin/python3 l4_controller/member_identity_canary.py
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

os.environ["JARVIS_TEST_RUN"] = f"member-identity-canary-{uuid.uuid4().hex[:8]}"

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "l5_console" / "state"))

import teams  # noqa: E402
import member_identity as mi  # noqa: E402

assert "test_runs" in str(teams.TEAMS_REGISTRY_PATH), (
    f"TEAMS_REGISTRY_PATH is NOT test-isolated ({teams.TEAMS_REGISTRY_PATH}) -- "
    "refusing to run against what may be the real registry"
)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((name, passed, detail))
    print(f"  {'ok  ' if passed else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not passed else ""))


TEST_REGISTRY = [
    {
        # Deliberately NOT "gateway" -- that was this file's own test id
        # before the JARVIS_TEST_RUN fix, and it happens to be the exact
        # id the Lead used for a real registration the same day this was
        # found. Isolation makes that collision impossible now regardless
        # of the name, but there's no reason to keep tempting it.
        "id": "canarymemberidentity",
        "aliases": ["canarymemberidentity", "canary-member-identity"],
        "root": "/tmp/fake-canary-member-identity",
        "inbox": "claude-canary-member-identity",
        "members": [{"tmux": "claude-canary-member-identity", "claude_session": "uuid-original-1111"}],
    }
]


def run() -> int:
    print("member_identity canary\n")

    try:
        teams.save_registry([dict(t, members=[dict(m) for m in t["members"]]) for t in TEST_REGISTRY])

        # --- 1. Not a registered member: no /status call, no false claim --
        print("1. an unregistered session is skipped entirely, no /status call")

        def _fail_if_called(session_id):
            raise AssertionError("claude_session_id() must never be called for an unregistered target")

        mi.claude_session_id = _fail_if_called
        result = mi.verify_and_refresh_identity("claude-not-a-team-member")
        check("checked=False for an unregistered session", result["checked"] is False)
        check("restarted=False", result["restarted"] is False)

        # --- 2. Registered member, identity matches -----------------------
        print("\n2. registered member, live identity matches stored -- no restart claimed")
        mi.claude_session_id = lambda session_id: "uuid-original-1111"
        result = mi.verify_and_refresh_identity("claude-canary-member-identity")
        check("checked=True", result["checked"] is True)
        check("restarted=False when identity matches", result["restarted"] is False)
        registry_after = teams.load_registry()
        check(
            "registry untouched when nothing changed",
            registry_after[0]["members"][0]["claude_session"] == "uuid-original-1111",
        )

        # --- 3. Registered member, identity MISMATCHES (the real bug) -----
        print("\n3. registered member, live identity differs -- restart detected and self-healed forward")
        mi.claude_session_id = lambda session_id: "uuid-NEW-after-crash-9999"
        result = mi.verify_and_refresh_identity("claude-canary-member-identity")
        check("checked=True", result["checked"] is True)
        check("restarted=True -- this is the gap this file exists to catch", result["restarted"] is True)
        check(
            "detail names both the old and new identity",
            "uuid-original-1111" in result["detail"] and "uuid-NEW-after-crash-9999" in result["detail"],
            detail=result["detail"],
        )
        registry_after = teams.load_registry()
        check(
            "registry SELF-HEALS to the new identity immediately",
            registry_after[0]["members"][0]["claude_session"] == "uuid-NEW-after-crash-9999",
        )

        print("\n3b. a SECOND check against the same (now-settled) session does not re-flag it")
        result2 = mi.verify_and_refresh_identity("claude-canary-member-identity")
        check(
            "restarted=False now -- the same restart is never re-flagged once healed",
            result2["restarted"] is False,
        )

        # --- 4. Live check can't verify (pane unreadable) -----------------
        print("\n4. live check fails (pane unreadable) -- no false claim either way")
        mi.claude_session_id = lambda session_id: None
        result = mi.verify_and_refresh_identity("claude-canary-member-identity")
        check("checked=True", result["checked"] is True)
        check("restarted=False -- never asserts a restart it couldn't confirm", result["restarted"] is False)
        registry_after = teams.load_registry()
        check(
            "registry untouched when unverifiable",
            registry_after[0]["members"][0]["claude_session"] == "uuid-NEW-after-crash-9999",
        )

        # --- 5. End-to-end: deliver_batch() flags AND still delivers ------
        print("\n5. deliver_batch(): a mismatch is announced but delivery still proceeds")
        teams.save_registry([dict(t, members=[dict(m) for m in t["members"]]) for t in TEST_REGISTRY])  # reset to original-1111
        import tools_write as tw
        from transport import DeliveryResult, Transport
        from registry import SessionRegistry

        spoken: list[str] = []
        tw.speak = lambda text, priority=1: spoken.append(text)
        tw.cancel_socket_available = lambda: True
        mi.claude_session_id = lambda session_id: "uuid-NEW-after-crash-9999"  # mismatch vs "uuid-original-1111"

        class FakeTransport(Transport):
            def deliver(self, target, payload):
                return DeliveryResult(ok=True, detail="delivered")

            def session_exists(self, target):
                return True

        class FakeRegistry(SessionRegistry):
            def resolve(self, name):
                return "claude-canary-member-identity"

            def is_test_target(self, session_id):
                return True

        tw.transport = FakeTransport()
        tw.registry = FakeRegistry()
        out = tw.deliver_batch([{"target": "canarymemberidentity", "payload": "run the tests"}], dictation_ref="/tmp/fake-dictation.txt")

        check(
            "the restart was announced (spoken)",
            any("no memory of any prior work" in s for s in spoken),
            detail=f"spoke: {spoken}",
        )
        check(
            "delivery STILL succeeded -- a mismatch never blocks a legitimate new instruction",
            out["results"][0]["ok"] is True,
        )
        registry_after = teams.load_registry()
        check(
            "registry self-healed via the real deliver_batch() call path, not just the unit call above",
            registry_after[0]["members"][0]["claude_session"] == "uuid-NEW-after-crash-9999",
        )

    finally:
        # The isolated test-run tree, not the real file -- nothing here
        # ever touched ~/Jarvis/teams.json.
        import shutil
        test_run_root = teams.TEAMS_REGISTRY_PATH.parent
        if "test_runs" in str(test_run_root):
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
