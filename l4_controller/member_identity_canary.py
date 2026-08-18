#!/usr/bin/env python3
"""Canary for member_identity.py (SPEC-orchestration.md Known Gaps:
"a restarted team lead reads as READY"). Proves the real
verify_and_refresh_identity() function against a controlled fake registry
and a monkeypatched claude_session_id() (never touches a real tmux
session or pane -- no /status is actually sent anywhere in this run).

Follows team_registry_tools_canary.py's established pattern for touching
the REAL ~/Jarvis/teams.json safely: back up its current content, run
against test data, restore the original content in a finally block no
matter what happens. There is no JARVIS_TEST_RUN-style isolation for this
file (it lives outside ~/.jarvis), so this is the only safe way to test
against the real load_registry()/save_registry() functions rather than
reimplementing them.

Also proves the end-to-end wiring into tools_write.deliver_batch(): a
mismatched member gets an audible flag AND still receives its delivery --
never silently blocked, never silently proceeding as though nothing
happened.

Run after touching member_identity.py or tools_write.py's use of it:

    python3 l4_controller/member_identity_canary.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "l5_console" / "state"))

import teams  # noqa: E402
import member_identity as mi  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((name, passed, detail))
    print(f"  {'ok  ' if passed else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not passed else ""))


TEST_REGISTRY = [
    {
        "id": "gateway",
        "aliases": ["gateway", "api"],
        "root": "/tmp/fake-gateway",
        "inbox": "claude-gateway",
        "members": [{"tmux": "claude-gateway", "claude_session": "uuid-original-1111"}],
    }
]


def run() -> int:
    print("member_identity canary\n")
    original_raw = teams.TEAMS_REGISTRY_PATH.read_text() if teams.TEAMS_REGISTRY_PATH.exists() else None
    original_existed = teams.TEAMS_REGISTRY_PATH.exists()

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
        result = mi.verify_and_refresh_identity("claude-gateway")
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
        result = mi.verify_and_refresh_identity("claude-gateway")
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
        result2 = mi.verify_and_refresh_identity("claude-gateway")
        check(
            "restarted=False now -- the same restart is never re-flagged once healed",
            result2["restarted"] is False,
        )

        # --- 4. Live check can't verify (pane unreadable) -----------------
        print("\n4. live check fails (pane unreadable) -- no false claim either way")
        mi.claude_session_id = lambda session_id: None
        result = mi.verify_and_refresh_identity("claude-gateway")
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
                return "claude-gateway"

            def is_test_target(self, session_id):
                return True

        tw.transport = FakeTransport()
        tw.registry = FakeRegistry()
        out = tw.deliver_batch([{"target": "gateway", "payload": "run the tests"}], dictation_ref="/tmp/fake-dictation.txt")

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
        if original_existed:
            teams.TEAMS_REGISTRY_PATH.write_text(original_raw)
        elif teams.TEAMS_REGISTRY_PATH.exists():
            teams.TEAMS_REGISTRY_PATH.unlink()

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
