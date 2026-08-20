"""
Revive-everything (SPEC-gaps-and-build-plan.md SS1.7): one action that
revives every attached engine role and every registered team, and
reports a per-item result -- never a bare bool.

Reuses engine_roles.activate_role() and reconnect.reconnect_team()
exclusively; this file adds no third launch path (the cage-drift
argument from SPEC-engine-sessions applies here too -- a role or team
member revived through a different code path than its own Activate/[r]
button could come back with a different security boundary than the one
it was created with, silently).

Two-function split (list_revive_targets / revive_target), not one
do-everything call: the console owns the loop (awaiting each item via
asyncio.to_thread and rendering as results land), this module owns
per-item correctness. "It will take tens of seconds -- silence during
that is the failure mode this project keeps hitting" (the Lead's own
framing) is exactly what a single blocking call would reproduce.

list_revive_targets() ALWAYS returns the complete set -- every role
(attached or not) and every registered team (needing reconnection or
not). Omitting an already-fine item would make the final "N of M" count
mean something different depending on what happened to already be
running, which is the same silent-partial-success shape this whole
feature exists to rule out. revive_target() is what decides, per item,
whether anything needed doing at all (skipped=True, ok=True) versus was
attempted and failed (skipped=False, ok=False) -- "ok" only ever means
"an attempted revival failed" when it's False; never conflated with
"there was nothing to do."
"""

from __future__ import annotations

from dataclasses import dataclass

from engine_roles import ROLES, activate_role, role_liveness
from models import LIVENESS_RUNNING
from reconnect import reconnect_team
from teams import discover_teams_and_unassigned, load_registry

# Engineer 2's window-revival seam (SPEC-gaps-and-build-plan.md SS1.6):
# idempotent, no-ops if the team isn't visible or already has a window.
# Called once per team below, right after that team's reconnect_team()
# call, so a freshly-revived team's window comes back the same way a
# freshly-created one gets one -- one function, two callers, per the
# Lead's ruling (2026-08-20) not to write a second window-opening path
# here. NOT built defensively against this not existing yet -- the Lead
# is sequencing the merge order, not this module (2026-08-20 ruling).
from team_actions import ensure_window_for_team  # noqa: E402


@dataclass
class ReviveTarget:
    kind: str  # "role" | "team"
    id: str    # role name ("concierge"/"orchestrator") or team id
    label: str  # display name


def list_revive_targets() -> list[ReviveTarget]:
    """Every attached-or-not role, then every registered team, in a
    stable order. Roles first: they're the front door (SS1's start
    precondition needs both up before the mic can even open), and there
    are always exactly two of them regardless of how many teams exist."""
    targets = [ReviveTarget(kind="role", id=role, label=role.capitalize()) for role in ROLES]
    for entry in load_registry():
        targets.append(ReviveTarget(kind="team", id=entry["id"], label=entry["id"]))
    return targets


def revive_target(target: ReviveTarget) -> dict:
    """Revives ONE target. Returns {"kind", "id", "label", "ok",
    "skipped", "detail", "sub_results"}. sub_results is reconnect_team()'s
    own per-member list for a team, None for a role. skipped=True means
    nothing needed doing (already running / not attached / no stopped
    members) and ok is still True in that case -- ok=False is reserved
    for "a revival was attempted and it failed," never for "there was
    nothing to revive." Blocking -- call via asyncio.to_thread from the
    console, same as engine_roles.activate_role() already is there."""
    if target.kind == "role":
        return _revive_role(target)
    return _revive_team(target)


def _revive_role(target: ReviveTarget) -> dict:
    liveness = role_liveness(target.id)
    if not liveness["attached"]:
        return {
            "kind": "role", "id": target.id, "label": target.label,
            "ok": True, "skipped": True,
            "detail": "no session attached -- nothing to revive", "sub_results": None,
        }
    if liveness["liveness"] == LIVENESS_RUNNING:
        return {
            "kind": "role", "id": target.id, "label": target.label,
            "ok": True, "skipped": True,
            "detail": "already running", "sub_results": None,
        }
    result = activate_role(target.id)
    return {
        "kind": "role", "id": target.id, "label": target.label,
        "ok": result["ok"], "skipped": False,
        "detail": result["detail"], "sub_results": None,
    }


def _revive_team(target: ReviveTarget) -> dict:
    result = reconnect_team(target.id)
    if "error" in result:
        # Registry entry vanished between list_revive_targets() building
        # the target list and this call reaching it (e.g. removed mid-run
        # from another screen) -- report it as a failed item, never drop
        # it from the count silently.
        return {
            "kind": "team", "id": target.id, "label": target.label,
            "ok": False, "skipped": False,
            "detail": result["error"], "sub_results": None,
        }

    if not result["results"]:
        item = {
            "kind": "team", "id": target.id, "label": target.label,
            "ok": True, "skipped": True,
            "detail": "nothing needed reconnecting", "sub_results": None,
        }
    else:
        n = len(result["results"])
        if result["ok"]:
            detail = f"all {n} member(s) reconnected"
        else:
            failed = [r["tmux"] for r in result["results"] if not r["ok"]]
            detail = f"{n - len(failed)} of {n} member(s) reconnected -- FAILED: {', '.join(failed)}"
        item = {
            "kind": "team", "id": target.id, "label": target.label,
            "ok": result["ok"], "skipped": False,
            "detail": detail, "sub_results": result["results"],
        }

    # Window revival, AFTER reconnecting -- must read post-revival
    # liveness (ensure_window_for_team picks the lead's session, falling
    # back to first running member; evaluating that against pre-revival
    # state would pick a session that may no longer be the right one, or
    # may not be running yet). Best-effort: a window failing to open is
    # not a revival failure -- the session itself is back, which is the
    # thing this feature is actually responsible for -- so this never
    # flips `item["ok"]`, only appends to `detail` if it's worth knowing.
    if not item["skipped"] and item["ok"]:
        team_list, _unassigned = discover_teams_and_unassigned()
        team = next((t for t in team_list if t.id == target.id), None)
        if team is not None:
            try:
                ensure_window_for_team(team)
            except Exception as e:  # noqa: BLE001
                item["detail"] += f" (window not opened: {e})"

    return item


def summarize(results: list[dict]) -> str:
    """The final count, phrased so '5 of 5' and '4 of 5' cannot be
    confused for each other -- the denominator is only items that
    actually NEEDED reviving (skipped=False); an item that was already
    fine was never at risk of partial success, so folding it into the
    headline ratio would let a bad run round toward looking better than
    it was. Never claims success while `results` contains any ok=False
    item -- callers should treat this string as spoken/rendered verbatim,
    not just a debug label."""
    needed = [r for r in results if not r["skipped"]]
    if not needed:
        return f"Nothing needed reviving -- all {len(results)} already up."
    succeeded = [r for r in needed if r["ok"]]
    if len(succeeded) == len(needed):
        return f"Revived {len(succeeded)} of {len(needed)} (of {len(results)} total, rest already up)."
    failed = [r for r in needed if not r["ok"]]
    names = "; ".join(f"{r['label']}: {r['detail']}" for r in failed)
    return f"Revived {len(succeeded)} of {len(needed)} -- FAILED: {names}."
