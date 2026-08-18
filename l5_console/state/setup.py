"""
Setup flows (SPEC-TUI.md SS5.1, SS5.2): adopting existing sessions into a
team, and creating a fresh team. Both write to ~/Jarvis/teams.json via
teams.save_registry(); this module never lies about liveness the way
discovery would refuse to -- these are one-time construction actions,
not something re-run on a poll.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "l4_controller"))
from providers import adoption_info, claude_session_id  # noqa: E402

from reconnect import wait_for_ready  # noqa: E402
from teams import CLAUDE_PROJECTS_DIR, load_registry, save_registry  # noqa: E402

CLAUDE_JSON_PATH = Path.home() / ".claude.json"
# Matches the .mcp.json content used everywhere else in this project
# (l3_orchestrator_test/.mcp.json, ~/Jarvis/.mcp.json) -- one canonical
# template, not a fifth copy invented here.
MCP_SERVER_NAME = "jarvis-l4"
_L4_SERVER_PY = str(Path(__file__).parent.parent.parent / "l4_controller" / "server.py")
_L4_VENV_PYTHON = str(Path(__file__).parent.parent.parent / "l4_controller" / ".venv" / "bin" / "python3")


def _mcp_json_content() -> dict:
    return {
        "mcpServers": {
            MCP_SERVER_NAME: {
                "command": _L4_VENV_PYTHON,
                "args": [_L4_SERVER_PY],
                "env": {"JARVIS_MUTE": "0"},
            }
        }
    }


def _write_mcp_json(root: str) -> None:
    (Path(root) / ".mcp.json").write_text(json.dumps(_mcp_json_content(), indent=2))


def preseed_mcp_trust(root: str) -> None:
    """Answers the MCP trust prompt in ~/.claude.json BEFORE launch, so
    the session never sits in the silent no-tools state confirmed live
    during cold-start validation (SPEC-TUI.md SS5.2, SS7). Read-modify-
    write on a shared file real Claude Code processes also write to
    (usage stats, etc.) -- kept as tight a window as practical (fresh
    read immediately before write) but this is not a fully race-free
    operation; acceptable because fresh-team-creation is a rare,
    deliberate action, not something running continuously."""
    data = json.loads(CLAUDE_JSON_PATH.read_text())
    projects = data.setdefault("projects", {})
    entry = projects.setdefault(root, {})
    existing = set(entry.get("enabledMcpjsonServers", []))
    entry["enabledMcpjsonServers"] = sorted(existing | {MCP_SERVER_NAME})
    entry["hasTrustDialogAccepted"] = True
    CLAUDE_JSON_PATH.write_text(json.dumps(data))


def create_fresh_member(tmux_name: str, root: str, model: str) -> dict:
    """Launches one brand-new Claude Code session with MCP trust
    pre-seeded so it never hits the confirmed-live silent no-tools gap.
    Returns {"ok", "claude_session", "detail"}.

    Resolves the new session's UUID via /status (claude_session_id()),
    NOT by reading its transcript file off disk. Originally built the
    other way (per the Lead's stated assumption that fresh creation
    "can identify its own new transcript file directly after launch,
    which is cheaper and unambiguous") -- verified live that assumption
    doesn't hold: a genuinely brand-new session with zero exchanges has
    no jsonl file on disk AT ALL yet (Claude Code only flushes a
    transcript once real conversational content happens), so the
    "newest jsonl in this directory" read finds nothing. /status,
    despite being the "expensive" mechanism, is answered from the
    session's own in-memory state and works immediately regardless of
    whether anything has been persisted -- confirmed live, correctly
    returned a UUID with zero jsonl files existing anywhere in the
    project directory. Fine to use here specifically because, same as
    adoption, this is a one-time creation-time call, not a polling
    operation."""
    Path(root).mkdir(parents=True, exist_ok=True)
    _write_mcp_json(root)
    preseed_mcp_trust(root)

    try:
        subprocess.run(["tmux", "new-session", "-d", "-s", tmux_name, "-c", root], check=True, capture_output=True)
        subprocess.run(
            ["tmux", "send-keys", "-t", tmux_name, "-l", "--", f"claude --model {model}"],
            check=True, capture_output=True,
        )
        subprocess.run(["tmux", "send-keys", "-t", tmux_name, "Enter"], check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        return {"ok": False, "claude_session": None, "detail": f"tmux launch failed: {e.stderr.decode(errors='replace')}"}

    if not wait_for_ready(tmux_name):
        return {"ok": False, "claude_session": None, "detail": "did not reach READY -- MCP prompt may not have been pre-seeded correctly, check manually"}

    session_id = claude_session_id(tmux_name)
    if session_id is None:
        return {"ok": False, "claude_session": None, "detail": "session came up but /status didn't report a session id -- cannot verify identity"}
    return {"ok": True, "claude_session": session_id, "detail": "created"}


def adopt_candidate_info(tmux_name: str) -> dict:
    """One /status round-trip per candidate (adoption-time only, per the
    Lead's ruling) -- returns {"tmux", "claude_session", "model",
    "summary"} for SS5.1's informed-choice screen. `summary` is Claude
    Code's own auto-generated session title (the "ai-title" jsonl entry
    -- a real field it already writes, not invented here), read directly
    from the transcript file once the UUID is known, no extra round trip."""
    info = adoption_info(tmux_name)
    claude_session = info["claude_session"]
    summary = None
    if claude_session is not None:
        summary = _read_ai_title(claude_session)
    return {"tmux": tmux_name, "claude_session": claude_session, "model": info["model"], "summary": summary}


def _read_ai_title(claude_session: str) -> str | None:
    """Searches every project directory for this session's transcript --
    the caller doesn't necessarily know the root yet at adoption time
    (that's the working_dir already reported by discovery, but this
    keeps the lookup self-contained rather than threading root through
    every caller)."""
    for project_dir in CLAUDE_PROJECTS_DIR.iterdir():
        jsonl_path = project_dir / f"{claude_session}.jsonl"
        if not jsonl_path.exists():
            continue
        try:
            with jsonl_path.open() as f:
                for line in f:
                    entry = json.loads(line)
                    if entry.get("type") == "ai-title":
                        return entry.get("aiTitle")
        except (OSError, json.JSONDecodeError):
            return None
        return None
    return None


def create_team(team_id: str, aliases: list[str], root: str, inbox_tmux: str, members: list[dict]) -> None:
    """members: [{"tmux", "claude_session"}, ...]. Appends to the
    registry -- never overwrites an existing team with the same id
    silently."""
    registry = load_registry()
    if any(t["id"] == team_id for t in registry):
        raise ValueError(f"a team with id {team_id!r} is already registered")
    registry.append({
        "id": team_id,
        "aliases": aliases,
        "root": root,
        "inbox": inbox_tmux,
        "members": members,
    })
    save_registry(registry)
