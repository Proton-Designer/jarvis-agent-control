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

ONE STABLE CONVERSATION PER ROLE (restructured 2026-08-20, see
SPEC-engine-roles.md's Verification section): a role's Claude Code
session identity (the UUID Claude Code keys its transcript on) is MINTED
BY US via uuid.uuid4() and handed in on launch as `--session-id <uuid>`,
never discovered after the fact. Verified live against real sessions:
`claude --session-id <uuid> ...` writes the transcript to exactly
`<uuid>.jsonl`, and `claude --resume <uuid> ...` reuses that SAME id and
SAME file (no new file, no fork -- `--fork-session` is what would change
that, and it is never passed here). This makes the registry
authoritative from the moment a session is created instead of a guess
reconciled against reality after the fact -- the exact class of drift
that made a prior generation's concierge/orchestrator records wrong (a
stored UUID whose transcript predates the session it's supposedly
pointing at).

DIRECTORY LAYOUT (restructured 2026-08-20 -- both roles now launch with
cwd = ~/Jarvis itself, NOT their own subdirectory):

    ~/Jarvis/
    ├── CLAUDE.md          shared, role-neutral -- both roles' actual context now
    ├── teams.json         team registry (team_registry_tools.py's, not ours)
    ├── engine.json         <- this file
    ├── concierge/         holds ONLY .mcp.json now (--mcp-config points here)
    └── orchestrator/      holds ONLY .mcp.json now (--mcp-config points here)

A role's subdirectory is no longer where the session RUNS -- it exists
solely so `--mcp-config <role>/.mcp.json --strict-mcp-config` has a
static, already-in-place, role-scoped file to point at (ops/jarvis-home/
in the repo mirrors these exact two files). Both roles sharing one cwd
means they also share ONE ~/.claude.json trust-dialog project entry
(keyed on ~/Jarvis itself) instead of one each -- preseed_mcp_trust()'s
existing union-of-server-names behavior already handles that correctly
without any change to it.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from jarvis_paths import jarvis_project_home  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "l4_controller"))
from providers import list_sessions as _list_live_sessions  # noqa: E402

from models import LIVENESS_LOST, LIVENESS_RUNNING, LIVENESS_STOPPED  # noqa: E402
from reconnect import wait_for_ready  # noqa: E402
from setup import preseed_mcp_trust  # noqa: E402
from teams import CLAUDE_PROJECTS_DIR, encode_project_path  # noqa: E402

ROLES = ("concierge", "orchestrator")
MODELS = ("haiku", "sonnet", "opus")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_EFFORT = {"concierge": "medium", "orchestrator": "high"}

# The ONE shared launch directory for BOTH roles now (see this module's
# top docstring). Real ~/Jarvis, deliberately NOT jarvis_project_home()
# -- this is where the real `claude` process's cwd actually points, same
# "reuse the already-in-place, real config, never point launches at an
# empty test directory" reasoning the old per-role split already used.
# Only the REGISTRY (below) needs JARVIS_TEST_RUN isolation -- that's the
# file that actually got clobbered by concurrent test runs, not this.
# Resolved once, here, the same discipline setup.create_fresh_member()
# established (a root under a symlinked prefix launches tmux fine but
# Claude Code's own trust-dialog keying lands on the RESOLVED path) --
# every comparison against a live session's pane_current_path (itself
# always OS-resolved) needs to be comparing the same resolved string, or
# role_liveness() silently never matches.
JARVIS_HOME = Path.home() / "Jarvis"
_JARVIS_HOME_RESOLVED = str(JARVIS_HOME.resolve())

# Each role's ONLY remaining reason to have its own subdirectory: a
# static, already-in-place .mcp.json for `--mcp-config` to point at (SS0).
# Nothing else under here is on the session's path anymore -- the
# session's cwd is JARVIS_HOME itself now, not this.
ROLE_HOME = {"concierge": JARVIS_HOME / "concierge", "orchestrator": JARVIS_HOME / "orchestrator"}
ROLE_MCP_CONFIG = {role: str(home / ".mcp.json") for role, home in ROLE_HOME.items()}

# jarvis_project_home(), not JARVIS_HOME -- see teams.py's identical
# comment on TEAMS_REGISTRY_PATH for the incident this isolates against.
ENGINE_REGISTRY_PATH = jarvis_project_home() / "engine.json"

# All server names EITHER role's own .mcp.json might declare, always
# preseeded together -- both roles now share ONE ~/.claude.json
# trust-dialog project entry (keyed on JARVIS_HOME, since both launch
# with that same cwd), so there is no more per-role split to maintain
# here; pre-approving a name unused by a given role's own --mcp-config
# file is harmless (--strict-mcp-config is what actually restricts the
# session's usable tool surface -- this only silences the
# discovery-time trust prompt). claude-peers joined this set 2026-08-20:
# added directly to BOTH role .mcp.json files (the only way it survives
# --strict-mcp-config -- verified live, yields exactly 9 tools: peers +
# the role's own server, nothing else), so it needs preseeding same as
# jarvis-l4/jarvis-l4-readonly always did.
_ROLE_TRUST_SERVER_NAMES = {"jarvis-l4", "jarvis-l4-readonly", "claude-peers"}

# Sent exactly once, via a literal tmux send-keys, right after a FRESH
# creation reaches READY -- never on activate_role()'s revival, which is
# resuming an ongoing conversation, not introducing a blank one to its
# role for the first time. Verified wording (2026-08-20), sent verbatim,
# never paraphrased.
_ROLE_BOOT_MESSAGE = {
    "concierge": "You are the Concierge. Get context about your scope.",
    "orchestrator": "You are the Orchestrator. Get context about your scope.",
}

# The concierge's real tool list, measured live (2026-08-20): denying
# only Bash/Write/Edit/Agent still left it able to spawn subagents
# (Workflow), schedule unsupervised future execution (CronCreate/
# CronDelete/CronList), and reach RemoteTrigger/Monitor. This is the
# exact list that was verified to close all of them -- do not trim it
# without re-measuring what --strict-mcp-config plus this list actually
# leaves reachable; a shorter list that "sounds equivalent" is exactly
# how the Workflow/Cron/RemoteTrigger gap got missed the first time.
_CONCIERGE_DISALLOWED_TOOLS = (
    "Bash", "Write", "Edit", "NotebookEdit", "Agent", "Task", "WebFetch", "WebSearch",
    "Workflow", "CronCreate", "CronDelete", "CronList", "RemoteTrigger", "Monitor",
    "EnterWorktree", "ExitWorktree", "DesignSync", "PushNotification", "TaskCreate",
    "TaskUpdate", "TaskStop",
)

# REQUIRED, not optional -- verified live (2026-08-20): moving cwd to the
# shared JARVIS_HOME orphaned ~/Jarvis/concierge/.claude/settings.local.json,
# which is where the concierge's own MCP tools used to be pre-approved
# per its old, dedicated working directory. Without an explicit
# --allowedTools, a tool call to a server the concierge's OWN .mcp.json
# declares does not error -- it silently sits on "Do you want to
# proceed? 1. Yes / 2. No" forever. For a voice turn that means the
# concierge hangs instead of answering, not a visible failure -- the
# exact shape reproduced live asking it to call its own list_sessions.
# Keep this in sync with whatever jarvis-l4-readonly
# (concierge/.mcp.json) and claude-peers actually expose -- a tool
# declared in one and absent from this list reproduces exactly that
# hang, silently.
_CONCIERGE_ALLOWED_TOOLS = (
    "mcp__jarvis-l4-readonly__list_sessions",
    "mcp__jarvis-l4-readonly__session_activity",
    "mcp__jarvis-l4-readonly__spend",
    "mcp__jarvis-l4-readonly__jarvis_say",
    "mcp__jarvis-l4-readonly__handoff_to_router",
    "mcp__claude-peers__list_peers",
    "mcp__claude-peers__send_message",
    "mcp__claude-peers__check_messages",
    "mcp__claude-peers__set_summary",
)

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
    effort so Activate has SOMETHING sane to relaunch with).

    Still generic on purpose -- this is also the ordinary Attach flow's
    entry point (engine_flow.AttachRoleScreen), which can point `tmux`/
    `working_dir` at ANY live session under ~/Jarvis/, not only a
    canonically-launched one at JARVIS_HOME. It is also how a role
    record created before the 2026-08-20 cwd restructure gets healed:
    re-attaching that role's still-live session captures its real,
    current working_dir and a freshly /status-resolved claude_session in
    one shot, the same path any other adoption already goes through --
    no separate migration mechanism needed."""
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


def _send_boot_message(role: str, tmux_name: str) -> None:
    """Exactly one literal tmux send-keys, exactly once, exactly after
    the session reaches READY -- see _ROLE_BOOT_MESSAGE. Raises
    subprocess.CalledProcessError on failure like every other
    launch-step helper here; the caller folds that into its own
    ok/detail contract rather than this swallowing it, since a session
    that came up but never learned what it is is a partial launch, not a
    success."""
    message = _ROLE_BOOT_MESSAGE[role]
    subprocess.run(["tmux", "send-keys", "-t", tmux_name, "-l", "--", message], check=True, capture_output=True)
    subprocess.run(["tmux", "send-keys", "-t", tmux_name, "Enter"], check=True, capture_output=True)


# NOTE, 2026-08-20: an earlier version of this file also added
# `--dangerously-load-development-channels server:claude-peers` here,
# believing it was required for claude-peers to survive
# --strict-mcp-config. Verified live that this was backwards: with
# claude-peers declared directly in the role's own .mcp.json (see
# _ROLE_TRUST_SERVER_NAMES) and --strict-mcp-config set, the session
# already has claude-peers -- the flag adds NOTHING on top of that. Its
# only real effect was to raise an interactive "WARNING: Loading
# development channels" confirmation on every single launch, which an
# earlier version of this module auto-answered with a bare Enter. That
# screen's own footer ("Enter to confirm · Esc to cancel") is literally
# pane_state.py's PERMISSION_PROMPT pattern -- auto-confirming it is
# teaching this system to press Enter through something it otherwise
# treats as a security prompt (SPEC-blockers.md SS2). Removed entirely,
# not worked around: the flag is never passed, and there is nothing left
# here that needs a keystroke sent before wait_for_ready().


def _role_cage_args(role: str) -> list[str]:
    """The security-boundary flags -- IDENTICAL regardless of whether
    this is a fresh launch or a revival, because both _build_launch_cmd()
    and _build_resume_cmd() call this ONE function rather than each
    building their own copy. "The cage drifts between create and revive"
    -- a revived session coming back weaker than the one it was created
    with -- is the failure this whole design has to rule out, and a
    single shared source of the cage rules it out by construction
    instead of by two functions happening to agree.

    Concierge: `--permission-mode acceptEdits` plus BOTH an explicit
    --allowedTools (_CONCIERGE_ALLOWED_TOOLS) and --disallowedTools
    (_CONCIERGE_DISALLOWED_TOOLS) -- see both constants' own comments for
    why each is required, not redundant with the other: disallowedTools
    closes off Claude Code's built-in tools; allowedTools is what lets
    the concierge's OWN pre-approved MCP tools run without a permission
    prompt, now that moving cwd to JARVIS_HOME orphaned the
    per-directory settings.local.json that used to pre-approve them.

    Orchestrator: `--dangerously-skip-permissions` instead -- team leads
    aren't meant to be sandboxed (SPEC-orchestration.md SS1.6), so no
    allow/deny list is needed."""
    if role == "concierge":
        return [
            "--permission-mode", "acceptEdits",
            "--allowedTools", *_CONCIERGE_ALLOWED_TOOLS,
            "--disallowedTools", *_CONCIERGE_DISALLOWED_TOOLS,
        ]
    return ["--dangerously-skip-permissions"]


def _build_launch_cmd(role: str, session_id: str, name: str | None, model: str, effort: str | None) -> str:
    """Fresh-launch command -- `--session-id <session_id> -n <name>`
    establishes a brand-new identity minted by the CALLER via
    uuid.uuid4() before this is ever invoked, never discovered here or
    after the fact (verified live: `claude --session-id <uuid> ...`
    writes the transcript to exactly `<uuid>.jsonl`).

    PURE: builds and returns a string, no subprocess/tmux calls, no
    filesystem access -- callable directly from a canary without
    launching anything, specifically so "does the resume command carry
    the identical cage as the launch command" is a checkable fact
    (compare _build_launch_cmd()'s and _build_resume_cmd()'s output
    directly) instead of a convention two hand-synced call sites could
    silently drift out of."""
    parts = ["claude", "--session-id", session_id]
    if name:
        # shlex.quote(), not the bare string: this whole cmd is typed
        # into the pane as ONE literal string (tmux send-keys -l) and
        # then parsed by the real shell running there, same as every
        # other command built here -- an unquoted multi-word name
        # ("Canary Concierge", or any real display name with a space in
        # it, which is the common case: "Concierge 4", "Concierge 3 -
        # New") splits into extra shell words and `claude` never starts.
        # Found live (2026-08-20) building this function's own canary:
        # create_role_session() reported ok=False, "did not reach
        # READY", for exactly this reason.
        parts += ["-n", shlex.quote(name)]
    parts += ["--model", model]
    if effort:
        parts += ["--effort", effort]
    parts += ["--mcp-config", ROLE_MCP_CONFIG[role], "--strict-mcp-config"]
    parts += _role_cage_args(role)
    return " ".join(parts)


def _build_resume_cmd(role: str, record: dict) -> str:
    """Revival command -- `--resume <claude_session>` reuses that SAME id
    and SAME file (verified live: no new file, no fork -- `--fork-session`
    is what would change that, and it is never passed here). `name` is
    deliberately never passed on this branch -- it names a NEW session at
    creation time, and whether `-n` even composes with `--resume` was
    never verified live; guessing wrong there risks the whole revival
    command over one cosmetic flag.

    Shares _role_cage_args(role) with _build_launch_cmd() -- not a
    second, hand-written cage -- so a revived session cannot come back
    weaker than the one it was created with by construction.

    PURE, same reason as _build_launch_cmd(): takes the persisted record
    dict and returns a string, nothing else, so a canary can assert
    facts about it directly."""
    parts = ["claude", "--resume", record["claude_session"], "--model", record["model"]]
    if record.get("effort"):
        parts += ["--effort", record["effort"]]
    parts += ["--mcp-config", ROLE_MCP_CONFIG[role], "--strict-mcp-config"]
    parts += _role_cage_args(role)
    return " ".join(parts)


def create_role_session(role: str, model: str, effort: str, name: str | None = None) -> dict:
    """Spawns a brand-new session for `role`, launched at the shared
    JARVIS_HOME cwd with the role-scoped --mcp-config/--strict-mcp-config
    and its full cage (--disallowedTools or --dangerously-skip-
    permissions, per role -- SS2, SPEC-orchestration.md SS1.6), and
    auto-attaches it on success.

    Mints the session's identity itself -- uuid.uuid4(), passed in via
    --session-id -- instead of discovering it after launch via /status
    (the OLD mechanism: setup.create_fresh_member()'s claude_session_id()
    round-trip). Verified live (2026-08-20) that `claude --session-id
    <uuid> ...` writes the transcript to exactly `<uuid>.jsonl`, so
    there is nothing left to discover -- the registry is authoritative
    the instant this function decides the uuid, not a guess reconciled
    against reality afterward. This also makes moot the exact gap
    create_fresh_member()'s docstring documents (a brand-new,
    zero-exchange session has no jsonl file on disk yet, so "find the
    newest transcript" never worked either): there's no discovery step
    left to have that gap in.

    Launches DIRECTLY rather than through setup.create_fresh_member() --
    that helper's generic tmux+claude launch shape no longer matches an
    engine role's (fixed JARVIS_HOME cwd shared by both roles,
    self-minted --session-id, and a role-specific cage are all
    engine-role-specific, not something a shared team-member launcher
    should grow more parameters to express). Still reuses
    preseed_mcp_trust() and wait_for_ready() -- those really are generic.

    Sends the ONE role-appropriate boot message (_send_boot_message)
    once READY, before anything else can reach the session, so it knows
    what it is from its very first turn.

    Never falls back to the unrestricted launch path on any failure -- an
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
    session_id = str(uuid.uuid4())
    working_dir = _JARVIS_HOME_RESOLVED

    preseed_mcp_trust(working_dir, _ROLE_TRUST_SERVER_NAMES)
    cmd = _build_launch_cmd(role, session_id, display_name, model, effort)

    try:
        subprocess.run(["tmux", "new-session", "-d", "-s", tmux_name, "-c", working_dir], check=True, capture_output=True)
        subprocess.run(["tmux", "send-keys", "-t", tmux_name, "-l", "--", cmd], check=True, capture_output=True)
        subprocess.run(["tmux", "send-keys", "-t", tmux_name, "Enter"], check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        return {"ok": False, "detail": f"tmux launch failed: {e.stderr.decode(errors='replace')}", "record": None}

    if not wait_for_ready(tmux_name):
        return {"ok": False, "detail": "did not reach READY after launch -- MCP prompt may not have been pre-seeded correctly, check manually", "record": None}

    try:
        _send_boot_message(role, tmux_name)
    except subprocess.CalledProcessError as e:
        return {"ok": False, "detail": f"session came up but the boot message failed to send: {e.stderr.decode(errors='replace')}", "record": None}

    attach_result = attach_role(
        role, tmux=tmux_name, working_dir=working_dir, claude_session=session_id,
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
    a partial restore), rebuilding the IDENTICAL cage/model/effort/
    mcp-config flag set create_role_session() launched with. Uses
    _build_resume_cmd(), which shares _role_cage_args() with
    _build_launch_cmd() (creation's own builder) rather than a second,
    independently-maintained command string -- specifically because "a
    revived session comes back with a weaker security boundary than the
    one it was created with" is the failure this design has to rule out
    by construction, not by remembering to keep two functions in sync.
    Preserves conversation memory -- matters more here than for a team
    lead, since the concierge's whole value includes cross-turn memory
    (SPEC-orchestration.md SS1.2).

    Relaunches at the record's OWN stored working_dir, not a
    hardcoded JARVIS_HOME -- for a role created under this module's
    current create_role_session() that IS JARVIS_HOME (they're the same
    value), but a record attached via the generic Attach flow (any live
    session under ~/Jarvis/, not necessarily at JARVIS_HOME itself) or
    left over from before the 2026-08-20 cwd restructure keeps whatever
    cwd its history actually lives under -- silently relocating an
    existing record's cwd here would risk `--resume` being asked to
    resume a transcript from a directory it was never associated with,
    which was never verified to work and is not this function's call to
    make. A record wanting the new shared cwd gets it by being
    re-attached or recreated, both of which already go through the
    JARVIS_HOME-using paths above."""
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

    cmd = _build_resume_cmd(role, record)

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

    jarvis_home_resolved = _JARVIS_HOME_RESOLVED
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
