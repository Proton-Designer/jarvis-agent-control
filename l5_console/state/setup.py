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
import terminal_window  # noqa: E402

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


def preseed_mcp_trust(root: str, server_names: set[str] | None = None) -> None:
    """Answers the MCP trust prompt in ~/.claude.json BEFORE launch, so
    the session never sits in the silent no-tools state confirmed live
    during cold-start validation (SPEC-TUI.md SS5.2, SS7). Read-modify-
    write on a shared file real Claude Code processes also write to
    (usage stats, etc.) -- kept as tight a window as practical (fresh
    read immediately before write) but this is not a fully race-free
    operation; acceptable because fresh-team-creation is a rare,
    deliberate action, not something running continuously.

    server_names defaults to {MCP_SERVER_NAME} (today's exact behavior).
    A role launch (SPEC-engine-roles.md) needs an explicit, different set
    -- --strict-mcp-config does NOT skip this prompt the way it was
    assumed to (found live, 2026-08-18, building this feature's own
    canary): launching inside ~/Jarvis/concierge still raised "2 new MCP
    servers found" for BOTH the parent ~/Jarvis/.mcp.json's "jarvis-l4"
    (Claude Code merges a nested directory's config with its parent's)
    AND the concierge's own ~/Jarvis/concierge/.mcp.json's
    "jarvis-l4-readonly" -- --strict-mcp-config only restricts which
    servers the SESSION can actually use, it does not bypass the
    trust-approval gate for servers merely discovered on disk."""
    names = server_names if server_names is not None else {MCP_SERVER_NAME}
    data = json.loads(CLAUDE_JSON_PATH.read_text())
    projects = data.setdefault("projects", {})
    entry = projects.setdefault(root, {})
    existing = set(entry.get("enabledMcpjsonServers", []))
    entry["enabledMcpjsonServers"] = sorted(existing | names)
    entry["hasTrustDialogAccepted"] = True
    CLAUDE_JSON_PATH.write_text(json.dumps(data))


def create_fresh_member(
    tmux_name: str,
    root: str,
    model: str,
    effort: str | None = None,
    mcp_config_path: str | None = None,
    strict_mcp: bool = False,
    mcp_trust_server_names: set[str] | None = None,
) -> dict:
    """Launches one brand-new Claude Code session with MCP trust
    pre-seeded so it never hits the confirmed-live silent no-tools gap.

    New, OPTIONAL params (SPEC-engine-roles.md SS2) -- all default to
    today's exact behavior when omitted, so team_registry_tools.py's
    existing positional calls (tmux_name, root, model) are completely
    unaffected:

    effort: passed as `--effort <effort>` on the launch command when
    given. Never validated here (that's engine_roles.py's job, against
    ENGINE_EFFORTS) -- this function just passes through what it's told.

    mcp_config_path / strict_mcp / mcp_trust_server_names: when
    strict_mcp=True, launches with `--mcp-config <mcp_config_path>
    --strict-mcp-config` INSTEAD OF the default project-.mcp.json write
    below (one already exists, static, at the role's directory -- this
    never writes a new one), but STILL preseeds trust
    (mcp_trust_server_names, required in this mode -- --strict-mcp-config
    restricts the session's tool surface, it does not bypass the
    trust-approval prompt for servers Claude Code discovers on disk,
    found live building this feature's own canary). This is the SS0.2/
    SS1.6 security boundary: a team member gets the full default toolset
    PLUS jarvis-l4 (unrestricted, by design -- team leads aren't meant to
    be sandboxed), while an engine-role session (concierge/orchestrator)
    must get ONLY the role-scoped jarvis-l4 surface and nothing else --
    confirmed live (SPEC-orchestration.md SS1.6) that omitting
    --strict-mcp-config lets a concierge inherit every user-level MCP
    server (Gmail, Drive, Calendar, ...), which voids the read-only split
    entirely. mcp_config_path is REQUIRED when strict_mcp=True (raises
    ValueError otherwise -- a caller passing strict_mcp=True without a
    path is a programming error, not a runtime condition to degrade
    gracefully from, since silently falling back to the unrestricted
    path would be exactly the security hole this parameter exists to
    prevent).

    Returns {"ok", "claude_session", "detail", "root"} -- callers that
    subsequently register this member (create_team) MUST use the
    RETURNED root, not whatever they originally passed in: this function
    resolves it (see below), and a caller using its own unresolved copy
    would register a team whose root doesn't byte-for-byte match the
    live session's actual pane_current_path, which teams.py's
    member_liveness() requires -- found live exactly this way
    (2026-08-18): l5_console/app/setup_flow.py's Create-Fresh flow and
    this module's own team_registry_tools.py caller both had this latent
    bug, invisible for any root under $HOME (never symlinked in
    practice) and only surfaced by a /tmp-rooted scratch test.

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
    operation.

    root is resolved (symlinks/`..` collapsed) before anything else uses
    it -- found live (2026-08-18, team_registry_tools.py's canary): a
    root under a symlinked prefix (e.g. /tmp, which macOS symlinks to
    /private/tmp) launches tmux fine (`-c` just cd's there), but Claude
    Code's own project-path resolution for ~/.claude.json lands on the
    RESOLVED path, so preseed_mcp_trust() below -- keyed on the
    caller's literal, unresolved `root` -- silently wrote its trust
    entry under a path the launched session never looks up, and the
    session sat on the real MCP-trust prompt instead of skipping it.
    Resolving once here, at the one place both the tmux launch and the
    trust preseed derive from, fixes it for every caller (console and
    router alike) rather than requiring each one to remember to pass an
    already-resolved path."""
    if strict_mcp and not mcp_config_path:
        raise ValueError("mcp_config_path is required when strict_mcp=True")

    root = str(Path(root).resolve())
    Path(root).mkdir(parents=True, exist_ok=True)

    if strict_mcp:
        # Role-scoped launch (SPEC-engine-roles.md): the session's ENTIRE
        # MCP TOOL SURFACE is replaced by mcp_config_path's content,
        # nothing inherited from user-level config -- but --strict-mcp-
        # config does NOT skip the trust-approval prompt, found live
        # (2026-08-18): launching inside ~/Jarvis/concierge still raised
        # "2 new MCP servers found" for both the parent ~/Jarvis/.mcp.json
        # (Claude Code merges a nested directory's config with its
        # parent's) and the role's own .mcp.json. No project-.mcp.json
        # gets WRITTEN here (one already exists, static, shared across
        # every generation of this role) but trust still needs preseeding
        # -- mcp_trust_server_names is required, not defaulted, so a
        # caller can't silently reintroduce this exact bug by omission.
        if not mcp_trust_server_names:
            raise ValueError("mcp_trust_server_names is required when strict_mcp=True")
        preseed_mcp_trust(root, mcp_trust_server_names)
        cmd = (
            f"claude --model {model} --permission-mode acceptEdits "
            f"--mcp-config {mcp_config_path} --strict-mcp-config"
        )
    else:
        _write_mcp_json(root)
        preseed_mcp_trust(root)
        cmd = f"claude --model {model}"
    if effort:
        cmd += f" --effort {effort}"

    try:
        subprocess.run(["tmux", "new-session", "-d", "-s", tmux_name, "-c", root], check=True, capture_output=True)
        subprocess.run(
            ["tmux", "send-keys", "-t", tmux_name, "-l", "--", cmd],
            check=True, capture_output=True,
        )
        subprocess.run(["tmux", "send-keys", "-t", tmux_name, "Enter"], check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        return {"ok": False, "claude_session": None, "detail": f"tmux launch failed: {e.stderr.decode(errors='replace')}", "root": root}

    if not wait_for_ready(tmux_name):
        return {"ok": False, "claude_session": None, "detail": "did not reach READY -- MCP prompt may not have been pre-seeded correctly, check manually", "root": root}

    session_id = claude_session_id(tmux_name)
    if session_id is None:
        return {"ok": False, "claude_session": None, "detail": "session came up but /status didn't report a session id -- cannot verify identity", "root": root}
    return {"ok": True, "claude_session": session_id, "detail": "created", "root": root}


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


def create_team(
    team_id: str, aliases: list[str], root: str, inbox_tmux: str, members: list[dict],
    visible: bool = True,
) -> None:
    """members: [{"tmux", "claude_session"}, ...]. Appends to the
    registry -- never overwrites an existing team with the same id
    silently.

    Enforces three things this function did NOT check before
    (SPEC-teams.md SS1.2, SS2) -- all raise ValueError, same convention
    the existing team_id check already used, so an existing caller that
    already pre-checks team_id itself (team_registry_tools.py does) gets
    the new checks for free as an additional safety net, not a new
    contract to learn:
      - `root` (resolved) must not already belong to another team.
      - every entry in `aliases` must not already belong to another
        team's aliases (case-insensitive).
      - `members` must be non-empty -- a team with zero members was a
        reachable, silently-valid state before this.

    `inbox_tmux` (a tmux name, unchanged caller-facing contract) is
    resolved HERE to that member's claude_session and stored as the
    team's `lead` -- SS2's "key it on claude_session, not tmux name"
    happens internally so team_registry_tools.py's call shape never has
    to change. Empty string means no lead, same as before.

    `visible` (SPEC-gaps-and-build-plan.md SS1.6): defaults True, matching
    Ayman's "project agents are visible by default." On success, opens a
    terminal window for the team's representative session (the lead if
    one was named, else the first member) -- best-effort, deliberately
    never raises: a window failing to open must not fail team creation,
    which already did the real work (real sessions exist, the registry
    write already succeeded). Not called at all when visible=False, so a
    background team never touches terminal_window."""
    registry = load_registry()
    if any(t["id"] == team_id for t in registry):
        raise ValueError(f"a team with id {team_id!r} is already registered")
    if not members:
        raise ValueError("a team must have at least one member")

    resolved_root = str(Path(root).resolve())
    for t in registry:
        if t["root"] == resolved_root:
            raise ValueError(f"{resolved_root!r} is already registered as team {t['id']!r}")

    normalized_new_aliases = {a.strip().lower() for a in aliases}
    for t in registry:
        existing_aliases = {a.strip().lower() for a in t.get("aliases", [])}
        collision = normalized_new_aliases & existing_aliases
        if collision:
            raise ValueError(f"alias {sorted(collision)[0]!r} is already used by team {t['id']!r}")

    lead_claude_session = None
    if inbox_tmux:
        lead_member = next((m for m in members if m.get("tmux") == inbox_tmux), None)
        if lead_member is None:
            raise ValueError(f"inbox_tmux {inbox_tmux!r} is not one of this team's own members")
        lead_claude_session = lead_member["claude_session"]

    registry.append({
        "id": team_id,
        "aliases": aliases,
        "root": resolved_root,
        "lead": lead_claude_session,
        "members": members,
        "visible": visible,
    })
    save_registry(registry)

    if visible:
        representative = inbox_tmux or members[0].get("tmux")
        if representative:
            terminal_window.open_window_for_session(representative)
