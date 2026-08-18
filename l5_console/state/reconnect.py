"""
Reconnect flow (SPEC-TUI.md SS5.3): for each stopped member,
`tmux new-session -c <root>` running `claude --resume <uuid>`. Verify
each came up. **Never report success for a partial restore.**

Doesn't need claude_session_id()/`/status` at all -- unlike adoption,
reconnect already knows the UUID (it's what teams.json stored), so
there's no identity to resolve, only a launch to verify.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "l4_controller"))
from pane_state import PaneState, PaneStatePatterns, classify_pane_ansi  # noqa: E402
from transport import TmuxTransport  # noqa: E402

from teams import member_liveness, load_registry  # noqa: E402
from models import LIVENESS_STOPPED  # noqa: E402
from providers import list_sessions  # noqa: E402

READY_POLL_TIMEOUT_S = 20.0
READY_POLL_INTERVAL_S = 1.0

_patterns = PaneStatePatterns()
_transport = TmuxTransport()


def _wait_for_ready(tmux_name: str, timeout_s: float = READY_POLL_TIMEOUT_S) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _transport.session_exists(tmux_name):
            return False
        ansi = _transport.capture_pane(tmux_name)
        if classify_pane_ansi(ansi, _patterns) == PaneState.READY:
            return True
        time.sleep(READY_POLL_INTERVAL_S)
    return False


def reconnect_team(team_id: str) -> dict:
    """Reconnects every currently-STOPPED member of the given team.
    Returns {"ok": bool, "results": [{"tmux", "claude_session", "ok",
    "detail"}, ...]}. `ok` at the top level is True only if EVERY member
    that needed reconnecting came up cleanly -- a single failure makes
    the whole call ok=False, per SS5.3's "never report success for a
    partial restore." Members already running are left untouched and not
    included in results."""
    registry = load_registry()
    entry = next((t for t in registry if t["id"] == team_id), None)
    if entry is None:
        return {"ok": False, "results": [], "error": f"no team registered with id {team_id!r}"}

    live_by_name = {s["session_id"]: s for s in list_sessions()}
    results = []

    for member in entry.get("members", []):
        liveness, _tmux_binding, _activity = member_liveness(entry["root"], member, live_by_name)
        if liveness != LIVENESS_STOPPED:
            continue  # only stopped members are reconnect candidates -- lost has nothing to resume, running needs nothing

        tmux_name = member["tmux"]
        uuid = member["claude_session"]

        try:
            subprocess.run(
                ["tmux", "new-session", "-d", "-s", tmux_name, "-c", entry["root"]],
                check=True, capture_output=True,
            )
            subprocess.run(
                ["tmux", "send-keys", "-t", tmux_name, "-l", "--", f"claude --resume {uuid}"],
                check=True, capture_output=True,
            )
            subprocess.run(["tmux", "send-keys", "-t", tmux_name, "Enter"], check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            results.append({
                "tmux": tmux_name, "claude_session": uuid, "ok": False,
                "detail": f"tmux launch failed: {e.stderr.decode(errors='replace')}",
            })
            continue

        came_up = _wait_for_ready(tmux_name)
        results.append({
            "tmux": tmux_name, "claude_session": uuid, "ok": came_up,
            "detail": "reconnected" if came_up else f"did not reach READY within {READY_POLL_TIMEOUT_S:.0f}s",
        })

    return {"ok": all(r["ok"] for r in results), "results": results}
