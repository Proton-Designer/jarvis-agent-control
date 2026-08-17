"""
Live session discovery, replacing the old static friendly-name -> tmux-id map.

Ayman's active projects change constantly, so nothing is hardcoded. Sessions
are enumerated for real via `tmux list-sessions` at call time and enriched
with each session's working directory (`pane_current_path`) — session name
alone often isn't enough for L3 to resolve a loose reference like "the API
one" or "the one I was just working in"; name + cwd usually is.

sessions.json is now an OPTIONAL alias-override file, empty by default. It
exists only for the case where Ayman wants to use a spoken nickname that
doesn't match the real tmux session name (e.g. "the api one" -> some tmux
session that isn't literally named that). Never required, never seeded with
real project names — this file must not go back to being a hardcoded map.

Note on peers: `list_peers` (a Claude Code peers-network tool) gives a third
enrichment signal — a session's own self-reported summary — but that's a
tool L3 already has natively as a Claude Code agent, not something this
Python module proxies. This module covers what peers can't: tmux sessions
in general, including ones that aren't Claude Code / aren't peers-registered.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ALIAS_PATH = Path(__file__).parent / "sessions.json"


@dataclass
class SessionInfo:
    session_id: str  # real tmux session name, e.g. "claude-shipcheck"
    working_dir: str  # pane_current_path of that session's active pane
    alias: str | None = None  # matching alias from sessions.json, if any


class UnknownSessionError(Exception):
    pass


class SessionRegistry:
    def __init__(self, tmux_bin: str = "tmux", alias_path: Path = DEFAULT_ALIAS_PATH):
        self.tmux_bin = tmux_bin
        self.alias_path = alias_path

    def _load_aliases(self) -> dict[str, str]:
        if not self.alias_path.exists():
            return {}
        data = json.loads(self.alias_path.read_text())
        return {k.lower(): v for k, v in data.items()}

    def list_sessions(self) -> list[SessionInfo]:
        """Enumerate real, currently-running tmux sessions. Never guesses at
        a session that isn't actually running."""
        result = subprocess.run(
            [self.tmux_bin, "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
        )
        if result.returncode != 0:
            return []  # no tmux server running at all -> no sessions, not an error

        aliases = self._load_aliases()
        alias_by_target = {}
        for alias, target in aliases.items():
            alias_by_target.setdefault(target, alias)

        sessions = []
        for name in result.stdout.decode(errors="replace").splitlines():
            name = name.strip()
            if not name:
                continue
            cwd_result = subprocess.run(
                [self.tmux_bin, "display-message", "-p", "-t", name, "#{pane_current_path}"],
                capture_output=True,
            )
            cwd = cwd_result.stdout.decode(errors="replace").strip() if cwd_result.returncode == 0 else ""
            sessions.append(
                SessionInfo(session_id=name, working_dir=cwd, alias=alias_by_target.get(name))
            )
        return sessions

    def resolve(self, name: str) -> str:
        """Resolve a name to a real, currently-running tmux session id.
        Tries: exact session-name match, then alias-override match. Never
        falls back to fuzzy/substring matching — loose reference resolution
        ("the one I was just working in") is L3's job, done against the
        list_sessions() enrichment, not this method's."""
        key = name.strip()
        live = {s.session_id for s in self.list_sessions()}
        if key in live:
            return key

        aliases = self._load_aliases()
        target = aliases.get(key.lower())
        if target and target in live:
            return target

        raise UnknownSessionError(
            f"'{name}' does not match any currently running tmux session "
            f"(running: {sorted(live)})"
        )
