"""
Engine role registry (SPEC-engine-roles.md): the CONCIERGE and
ORCHESTRATOR slots, each holding at most one attached session at a time.

Persisted BY US (~/Jarvis/engine.json), not by the session -- survives
the session dying, the laptop restarting, the console being killed and
reopened. Same "records intent, never status" rule teams.json already
established: no liveness/status field is ever stored here. Attachment
(this file) and running (computed fresh, every call, by role_liveness())
can never disagree by construction, because there is nowhere for a
stored "running" flag to go stale.

DIRECTORY LAYOUT (SS0, restructured 2026-08-18 after a real mismatch was
found -- see docs/SPEC-engine-roles.md SS0):

    ~/Jarvis/
    ├── CLAUDE.md          shared, role-neutral
    ├── teams.json         team registry (team_registry_tools.py's, not ours)
    ├── engine.json         <- this file
    ├── concierge/         .mcp.json -> server_readonly.py, --strict-mcp-config
    └── orchestrator/      .mcp.json -> server.py, --strict-mcp-config

Each role's working directory is FIXED and shared across every
generation of that role -- "Concierge 1", "Concierge 2", etc. all launch
into the same ~/Jarvis/concierge, reusing its already-in-place
.mcp.json/.claude/settings.local.json (ops/jarvis-home/ in the repo
mirrors this exact layout; those are the Lead's working copies, reused
here, never reconstructed). Only the tmux session name, model, effort,
and claude_session identity vary per generation.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from jarvis_paths import jarvis_project_home  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "l4_controller"))
from providers import list_sessions as _list_live_sessions  # noqa: E402

from models import LIVENESS_LOST, LIVENESS_RUNNING, LIVENESS_STOPPED  # noqa: E402
from reconnect import wait_for_ready  # noqa: E402
from setup import create_fresh_member, preseed_mcp_trust  # noqa: E402
from teams import CLAUDE_PROJECTS_DIR, encode_project_path  # noqa: E402

ROLES = ("concierge", "orchestrator")
MODELS = ("haiku", "sonnet", "opus")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_EFFORT = {"concierge": "medium", "orchestrator": "high"}

# ALWAYS the real ~/Jarvis, deliberately NOT jarvis_project_home() --
# these are the two role subdirectories' already-in-place, static
# .mcp.json/CLAUDE.md/settings.local.json (ops/jarvis-home/ mirrors
# them). A canary reuses these ON PURPOSE (real config, not
# reconstructed), so redirecting ROLE_HOME under JARVIS_TEST_RUN would
# just point every test launch at an empty directory with no MCP config
# at all. Only the REGISTRY (below) needs isolating -- that's the file
# that actually got clobbered, not the shared launch directories.
JARVIS_HOME = Path.home() / "Jarvis"
ROLE_HOME = {"concierge": JARVIS_HOME / "concierge", "orchestrator": JARVIS_HOME / "orchestrator"}
ROLE_MCP_CONFIG = {role: str(home / ".mcp.json") for role, home in ROLE_HOME.items()}

# jarvis_project_home(), not JARVIS_HOME -- see teams.py's identical
# comment on TEAMS_REGISTRY_PATH for the incident this isolates against.
ENGINE_REGISTRY_PATH = jarvis_project_home() / "engine.json"

# Both known role server aliases, always -- not just the one this
# specific role's .mcp.json declares. Found live building this feature's
# own canary: launching inside ~/Jarvis/concierge raised a trust prompt
# for BOTH "jarvis-l4" (the PARENT ~/Jarvis/.mcp.json's server -- Claude
# Code merges a nested directory's MCP config with its parent's) and
# "jarvis-l4-readonly" (the concierge's own). Pre-approving a name that
# happens to be unused for a given role is harmless (--strict-mcp-config
# is what actually restricts the session's usable tool surface; this
# only silences the discovery-time approval prompt), and it means this
# doesn't need to duplicate which alias belongs to which role.
_ROLE_TRUST_SERVER_NAMES = {"jarvis-l4", "jarvis-l4-readonly"}

# Same hazard class already hardened in l2_l3_handoff.orchestrator_has_tools()
# and wake.is_running(): a bare `pgrep -f` substring match can be fooled by
# any process whose command line merely mentions the path. Both role
# processes (server.py, server_readonly.py) get the same invocation-shape
# treatment here rather than importing l2_l3_handoff's orchestrator-specific
# one, since this needs BOTH patterns, not just server.py's.
_ROLE_SERVER_PATTERN = {
    "concierge": "l4_controller/server_readonly.py",
    "orchestrator": "l4_controller/server.py",
}
_ROLE_SERVER_INVOCATION = {
    role: re.compile(rf"(?:^|\s)\S*python\S*\s+\S*{re.escape(pattern)}(?:\s|$)")
    for role, pattern in _ROLE_SERVER_PATTERN.items()
}


def _role_tools_reachable(role: str) -> bool:
    """Whether THIS role's own MCP server process (server_readonly.py for
    concierge, server.py for orchestrator) is actually running -- not
    just "some jarvis-l4-shaped process is up somewhere". Verifies real
    invocation shape (own trailing argument, never inside a `-c` body),
    same discipline as orchestrator_has_tools()."""
    pattern = _ROLE_SERVER_PATTERN[role]
    invocation = _ROLE_SERVER_INVOCATION[role]
    pids = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
    if pids.returncode != 0:
        return False
    for pid in pids.stdout.split():
        cmd = subprocess.run(["ps", "-o", "command=", "-p", pid], capture_output=True, text=True)
        if cmd.returncode != 0:
            continue
        cmdline = cmd.stdout
        if " -c " in cmdline or cmdline.rstrip().endswith(" -c"):
            continue
        if invocation.search(cmdline):
            return True
    return False


def _load() -> dict:
    if not ENGINE_REGISTRY_PATH.exists():
        return {"concierge": None, "orchestrator": None, "name_history": {"concierge": [], "orchestrator": []}}
    try:
        data = json.loads(ENGINE_REGISTRY_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {"concierge": None, "orchestrator": None, "name_history": {"concierge": [], "orchestrator": []}}
    for role in ROLES:
        data.setdefault(role, None)
    data.setdefault("name_history", {})
    for role in ROLES:
        data["name_history"].setdefault(role, [])
    return data


def _save(data: dict) -> None:
    ENGINE_REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    ENGINE_REGISTRY_PATH.write_text(json.dumps(data, indent=2))


def get_role_record(role: str) -> dict | None:
    """Raw persisted intent for `role` -- no liveness, no side effects.
    None means unattached ("unused")."""
    return _load().get(role)


def _has_history(working_dir: str, claude_session: str | None) -> bool:
    if not claude_session:
        return False
    jsonl_path = CLAUDE_PROJECTS_DIR / encode_project_path(working_dir) / f"{claude_session}.jsonl"
    return jsonl_path.exists()


def role_liveness(role: str) -> dict:
    """{"attached": bool, "running": bool, "liveness": str|None, "record":
    dict|None, "tools_reachable": bool}. Liveness is ALWAYS computed
    fresh here (cheap tmux-name+cwd match, mirrors
    teams.member_liveness() exactly) -- never trusted from the stored
    record, which has no status field to trust in the first place."""
    record = get_role_record(role)
    if record is None:
        return {"attached": False, "running": False, "liveness": None, "record": None, "tools_reachable": False}

    live_by_name = {s["session_id"]: s for s in _list_live_sessions()}
    live = live_by_name.get(record["tmux"])
    if live is not None and live["working_dir"] == record["working_dir"]:
        return {
            "attached": True,
            "running": True,
            "liveness": LIVENESS_RUNNING,
            "record": record,
            "tools_reachable": _role_tools_reachable(role),
        }

    liveness = LIVENESS_STOPPED if _has_history(record["working_dir"], record["claude_session"]) else LIVENESS_LOST
    return {"attached": True, "running": False, "liveness": liveness, "record": record, "tools_reachable": False}


def get_engine_state() -> dict:
    """{"concierge": role_liveness("concierge"), "orchestrator":
    role_liveness("orchestrator")} -- the caller (the poller) attaches
    polled_at/expected_interval/error, same split of responsibility as
    orchestrator.compute_orchestrator_state() before it."""
    return {role: role_liveness(role) for role in ROLES}


def _default_name(role: str, data: dict) -> str:
    """Concierge N / Orchestrator N, incrementing against every name
    EVER assigned to this role (name_history), not just the currently
    attached one -- a swapped-out "Concierge 1" still existed once and
    must not be reissued, or two different sessions read as the same
    name across time."""
    history = data["name_history"][role]
    n = len(history) + 1
    return f"{role.capitalize()} {n}"


def preview_default_name(role: str) -> str:
    """Public, side-effect-free wrapper around _default_name() -- for a
    render layer that needs to PRE-FILL a name picker with the generated
    default before the user decides whether to keep or change it,
    without reaching into engine.json's internal shape (name_history)
    directly. Read-only: does not reserve or commit the name -- calling
    this twice in a row with nothing else happening returns the same
    value both times, since nothing is written until attach_role()/
    create_role_session() actually assigns one."""
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}")
    return _default_name(role, _load())


def attach_role(role: str, tmux: str, working_dir: str, claude_session: str | None, name: str | None = None, model: str | None = None, effort: str | None = None) -> dict:
    """Attaches an EXISTING (already-running) session to `role`. Refuses
    if `tmux` is already the OTHER role's attached session -- never
    silently steals it (SS3). Idempotent if `tmux` is already THIS
    role's own attached session. model/effort are best-effort here (an
    adopted session's original launch effort is not generally knowable;
    callers that resolved it via /status-based adoption_info() should
    pass what they found, otherwise this defaults to the role's standard
    effort so Activate has SOMETHING sane to relaunch with)."""
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}")

    data = _load()
    for other_role in ROLES:
        if other_role == role:
            continue
        other = data.get(other_role)
        if other is not None and other["tmux"] == tmux:
            return {
                "ok": False,
                "detail": f"That session is already attached as the {other_role.capitalize()}.",
                "conflicting_role": other_role,
            }

    existing = data.get(role)
    is_same_session = existing is not None and existing["tmux"] == tmux
    if name:
        display_name = name
    elif is_same_session:
        display_name = existing["name"]
    else:
        display_name = _default_name(role, data)

    if display_name not in data["name_history"][role]:
        data["name_history"][role].append(display_name)

    record = {
        "tmux": tmux,
        "working_dir": working_dir,
        "claude_session": claude_session,
        "name": display_name,
        "model": model or (existing["model"] if is_same_session else None),
        "effort": effort or (existing["effort"] if is_same_session else DEFAULT_EFFORT[role]),
        "attached_at": time.time(),
    }
    data[role] = record
    _save(data)
    return {"ok": True, "detail": f"attached as {role.capitalize()}", "record": record}


def remove_role(role: str) -> dict:
    """Detaches -- sets the slot to None (unused). Never touches the
    actual tmux session or process (SS4)."""
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}")
    data = _load()
    data[role] = None
    _save(data)
    return {"ok": True, "detail": f"{role.capitalize()} detached"}


def create_role_session(role: str, model: str, effort: str, name: str | None = None) -> dict:
    """Spawns a brand-new session for `role`, launched role-scoped
    (--mcp-config <role's static .mcp.json> --strict-mcp-config -- SS2,
    SPEC-orchestration.md SS1.6) and auto-attaches it on success. Never
    falls back to the unrestricted launch path on any failure -- an
    engine-role session with the wrong MCP surface is the exact security
    hole this whole feature exists to prevent, so a launch failure here
    is reported, not silently downgraded."""
    if role not in ROLES:
        return {"ok": False, "detail": f"unknown role {role!r}", "record": None}
    if model not in MODELS:
        return {"ok": False, "detail": f"unknown model {model!r} (expected one of {MODELS})", "record": None}
    if effort not in EFFORTS:
        return {"ok": False, "detail": f"unknown effort {effort!r} (expected one of {EFFORTS})", "record": None}

    data = _load()
    display_name = name or _default_name(role, data)
    n = len(data["name_history"][role]) + 1
    tmux_name = f"claude-{role}-{n}"

    result = create_fresh_member(
        tmux_name,
        str(ROLE_HOME[role]),
        model,
        effort=effort,
        mcp_config_path=ROLE_MCP_CONFIG[role],
        strict_mcp=True,
        mcp_trust_server_names=_ROLE_TRUST_SERVER_NAMES,
    )
    if not result["ok"]:
        return {"ok": False, "detail": result["detail"], "record": None}

    attach_result = attach_role(
        role, tmux=tmux_name, working_dir=result["root"], claude_session=result["claude_session"],
        name=display_name, model=model, effort=effort,
    )
    if not attach_result["ok"]:
        # Genuinely shouldn't happen (a freshly-created tmux name cannot
        # already be attached as the other role), but never silently
        # discard a launched-but-unregistered session if it somehow does.
        return {"ok": False, "detail": f"created but failed to attach: {attach_result['detail']}", "record": None}
    return {"ok": True, "detail": f"created and attached as {role.capitalize()}", "record": attach_result["record"]}


def activate_role(role: str) -> dict:
    """Relaunches the persisted record's session via `claude --resume
    <claude_session>` (SS5) -- reuses reconnect.py's exact mechanism
    (tmux new-session + resume + wait_for_ready + never report success on
    a partial restore), extended with the same role-scoped
    --mcp-config/--strict-mcp-config as creation, so a revived session
    gets the identical security boundary a freshly-created one would,
    not a weaker one. Preserves conversation memory -- matters more here
    than for a team lead, since the concierge's whole value includes
    cross-turn memory (SPEC-orchestration.md SS1.2)."""
    if role not in ROLES:
        return {"ok": False, "detail": f"unknown role {role!r}"}
    record = get_role_record(role)
    if record is None:
        return {"ok": False, "detail": f"The {role.capitalize()} has no session attached. Attach one before activating."}

    liveness = role_liveness(role)
    if liveness["liveness"] == LIVENESS_RUNNING:
        return {"ok": True, "detail": f"The {role.capitalize()} is already running."}
    if liveness["liveness"] == LIVENESS_LOST:
        return {
            "ok": False,
            "detail": f"The {role.capitalize()}'s session has no history to resume from -- it must be recreated, not activated.",
        }

    # Idempotent belt-and-suspenders: create_role_session() already
    # preseeded this on first creation, but re-preseed here too rather
    # than assuming it's still in place -- an attached-but-never-created-
    # by-us session (adopted via attach_role()) or a cleared
    # ~/.claude.json would otherwise sit on the same trust prompt found
    # live building create_role_session()'s own path.
    preseed_mcp_trust(record["working_dir"], _ROLE_TRUST_SERVER_NAMES)

    cmd = (
        f"claude --resume {record['claude_session']} --model {record['model']} "
        f"--permission-mode acceptEdits --mcp-config {ROLE_MCP_CONFIG[role]} --strict-mcp-config"
    )
    if record.get("effort"):
        cmd += f" --effort {record['effort']}"

    try:
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", record["tmux"], "-c", record["working_dir"]],
            check=True, capture_output=True,
        )
        subprocess.run(["tmux", "send-keys", "-t", record["tmux"], "-l", "--", cmd], check=True, capture_output=True)
        subprocess.run(["tmux", "send-keys", "-t", record["tmux"], "Enter"], check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        return {"ok": False, "detail": f"tmux relaunch failed: {e.stderr.decode(errors='replace')}"}

    if not wait_for_ready(record["tmux"]):
        return {"ok": False, "detail": f"{role.capitalize()} did not reach READY after relaunch -- check it manually"}
    return {"ok": True, "detail": f"{role.capitalize()} activated"}


def list_attachable_sessions() -> list[dict]:
    """Every live tmux session whose cwd falls under ~/Jarvis/ (SS0 --
    NOT system-wide; team leads in unrelated project directories are a
    different universe, enumerated by team_registry_tools.list_teams()
    instead), sorted most-recent-first by tmux's own last-activity
    timestamp, each: {"tmux", "working_dir", "flag":
    "concierge"|"orchestrator"|"unused"}."""
    data = _load()
    concierge = data.get("concierge")
    orchestrator = data.get("orchestrator")

    result = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}\t#{session_activity}"],
        capture_output=True, text=True,
    )
    activity_by_name: dict[str, float] = {}
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 2:
                try:
                    activity_by_name[parts[0]] = float(parts[1])
                except ValueError:
                    continue

    jarvis_home_resolved = str(JARVIS_HOME.resolve())
    sessions = []
    for s in _list_live_sessions():
        working_dir = s["working_dir"]
        if not (working_dir == jarvis_home_resolved or working_dir.startswith(jarvis_home_resolved + "/")):
            continue
        tmux = s["session_id"]
        if concierge is not None and concierge["tmux"] == tmux:
            flag = "concierge"
        elif orchestrator is not None and orchestrator["tmux"] == tmux:
            flag = "orchestrator"
        else:
            flag = "unused"
        sessions.append({"tmux": tmux, "working_dir": working_dir, "flag": flag, "_activity": activity_by_name.get(tmux, 0.0)})

    sessions.sort(key=lambda s: s["_activity"], reverse=True)
    for s in sessions:
        del s["_activity"]
    return sessions


def start_precondition() -> dict:
    """Pure query, no side effects -- same contract as get_state() (the
    Lead's explicit instruction). The UI calls this before invoking
    wake_control.start(), and renders the reason verbatim if refused
    (SS6): starting the daemon opens the microphone, and voice input with
    no live concierge or no live router has nowhere to go."""
    for role in ROLES:
        liveness = role_liveness(role)
        label = role.capitalize()
        if not liveness["attached"]:
            return {"ok": False, "detail": f"The {label} has no session attached. Attach one before starting."}
        if liveness["liveness"] != LIVENESS_RUNNING:
            return {"ok": False, "detail": f"The {label} session isn't running. Press Activate to bring it back."}
    return {"ok": True, "detail": None}
