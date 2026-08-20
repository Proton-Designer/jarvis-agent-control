"""
Shared, layout-independent formatting logic -- used by both rail.py
(condensed) and console.py (fuller detail) so the two densities can't
silently disagree about what a liveness icon means or how a team's
overall state is derived. Factored out once needed in two places, same
reasoning as l2_5_concierge/session_match.py's own extraction.
"""
from __future__ import annotations

import time

import sys
from pathlib import Path

from rich.text import Text

sys.path.insert(0, str(Path(__file__).parent.parent / "state"))
from models import LIVENESS_RUNNING, LIVENESS_STOPPED, LIVENESS_LOST  # noqa: E402

# Same palette as docs/console-design-studies.html's mockups (the Lead's
# "match the design, don't approximate it" instruction) -- exact hex
# values, not Textual theme tokens, because these are used inside Rich
# Text objects passed to Static.update(), which render independent of
# the app's CSS theme resolution.
COLOR_OK = "#46C07A"
COLOR_WARN = "#E0A34A"
COLOR_ERR = "#EE6055"
COLOR_ACCENT = "#4FD6E0"
COLOR_DIM = "#6B7688"
# A step below COLOR_DIM -- for a control that is currently inert, not
# merely secondary. Footer's "[space] stop listening" needs this
# distinction specifically: the binding is correct (space IS stop-only,
# by ruling), but showing it at the same weight as every other always-
# live key when wake isn't running reads as "this will do something" --
# the Lead's live finding, 2026-08-18.
COLOR_MUTED = "#4A5262"
COLOR_INK = "#C9D2DE"


def build_hint_line(hints: list[tuple[str, str]]) -> Text:
    """A row of "[key] effect" action hints -- bold accent key, dim
    effect label, matching Footer's own keybind-bar styling exactly
    (widgets.py). Shared so the global footer and any per-panel hint
    area (the Lead's ruling, 2026-08-18: action hints belong with the
    panel they act on, not only in the global footer) can never drift
    on what a hint looks like -- one styling decision, not two places
    to keep in sync by hand. Every hint names the EFFECT, not just the
    key ("[a] add team", never a bare "[a]") -- the Lead's explicit
    requirement, same reasoning as never-a-UUID: a label that requires
    the reader to already know what it does isn't a label."""
    text = Text()
    for i, (key, label) in enumerate(hints):
        if i:
            text.append("   ")
        text.append(f"[{key}]", style=f"bold {COLOR_ACCENT}")
        text.append(f" {label}", style=COLOR_DIM)
    return text


def liveness_icon(liveness: str) -> str:
    return {LIVENESS_RUNNING: "●", LIVENESS_STOPPED: "○", LIVENESS_LOST: "✕"}.get(liveness, "?")


def compact_model_name(model: str | None) -> str:
    """A RoleSlot attached via Create carries a clean "haiku"/"sonnet"/
    "opus" (engine_roles.MODELS). One attached via Attach instead carries
    whatever /status reported verbatim (e.g. "haiku
    (claude-haiku-4-5-20251001)", found live attaching a real concierge
    session) -- both are real, correct values, but the long form doesn't
    fit "at a glance" (SPEC-engine-roles.md SS7) in either density.
    Display-only: the stored value is never truncated, only what's
    rendered here."""
    if not model:
        return "?"
    return model.split()[0].split("(")[0].strip() or model


def liveness_color(liveness: str) -> str:
    return {LIVENESS_RUNNING: COLOR_OK, LIVENESS_STOPPED: COLOR_DIM, LIVENESS_LOST: COLOR_ERR}.get(liveness, COLOR_DIM)


def blocked_for(member) -> str:
    """How long a member has been blocked, e.g. "blocked 4m". Empty
    string when it isn't blocked.

    Duration is part of the signal, not decoration: a session blocked
    twenty seconds ago may resolve itself, one blocked twenty minutes ago
    has been burning wall-clock with nobody watching. That difference is
    what tells Ayman whether to walk over to it."""
    if not getattr(member, "blocked_question", None):
        return ""
    since = getattr(member, "blocked_since", None)
    if not since:
        return "blocked"
    secs = max(0, int(time.time() - since))
    if secs < 60:
        return f"blocked {secs}s"
    if secs < 3600:
        return f"blocked {secs // 60}m"
    return f"blocked {secs // 3600}h{(secs % 3600) // 60:02d}m"


def is_blocked(member) -> bool:
    """One predicate, shared by every surface, so Rail and Console can
    never disagree about whether a member is blocked."""
    return bool(getattr(member, "blocked_question", None))


# SPEC-teams.md SS2: how long a stale identity confirmation is trusted
# before it's worth flagging. member_identity.py fires AT DISPATCH TIME
# (not on a poll), so this isn't a poll-cadence threshold -- it's "how
# long since I last confirmed this is genuinely the conversation I think
# it is." 30 minutes: long enough that back-to-back dispatches during
# active work never flicker this on (two dispatches ten minutes apart
# stay silent), short enough that returning to a team after a genuine gap
# -- the exact window in which someone could manually kill and restart a
# session without a new dispatch happening yet to catch it -- gets
# flagged before Ayman acts on a false-confident "active".
IDENTITY_STALE_THRESHOLD_S = 30 * 60


def identity_staleness_note(member) -> str:
    """"" when there's nothing worth showing, else "unverified since Xm"
    / "unverified since XhYYm".

    JUDGMENT CALL, made explicit per the Lead's ask (2026-08-20):
    identity_verified_at is None means "never dispatched to" -- the
    ordinary starting state for any freshly adopted/created member, and
    for every member Ayman simply hasn't sent anything to yet. There is
    no PRIOR claim of "this is still the same agent" for a restart to
    have quietly falsified, so showing anything for None would be noise
    on every brand-new team, not signal -- exactly the "should not be
    visually noisy" case the Lead flagged. Only a STALE verification --
    one that WAS established and has since gone unrefreshed past the
    threshold -- is the false-confident-active case SPEC-teams.md SS2
    names: a member that looked verified a while ago could have been
    restarted since, with nothing yet to have caught it.

    PURE: a TeamMember in, a string out -- same shape as blocked_for(),
    so a canary can assert on it directly without mounting a Textual
    app."""
    verified_at = getattr(member, "identity_verified_at", None)
    if verified_at is None:
        return ""
    elapsed = time.time() - verified_at
    if elapsed < IDENTITY_STALE_THRESHOLD_S:
        return ""
    mins = int(elapsed // 60)
    if mins < 60:
        return f"unverified since {mins}m"
    hours = mins // 60
    return f"unverified since {hours}h{mins % 60:02d}m"


def is_identity_stale(member) -> bool:
    """One predicate, shared by every surface (same reasoning as
    is_blocked()) -- Rail's aggregate count and Console/team_flow's
    per-member note can never disagree about which members count."""
    return bool(identity_staleness_note(member))


def pending_speech_header(count: int) -> str:
    """"3 waiting -- will speak when you stop talking" (SPEC-orchestration.md
    SS2.3's flush policy, in plain language). Shared so Console's full
    panel and Rail's count line can't drift on wording -- same reasoning
    as every other function here. WHY it is waiting, not just that it is
    (the Lead's explicit requirement, TODO-feature-queue.md item 4):
    a bare "3 waiting" tells Ayman nothing actionable; this tells him the
    batch is the policy working as designed, and exactly when it resolves
    -- the moment he stops talking -- so it is never a surprise
    afterward."""
    return f"{count} waiting -- will speak when you stop talking"


def pending_speech_kind_glyph(kind: str) -> str:
    """"❓" for a blocked-question escalation (the higher-priority tier,
    SPEC-orchestration.md SS2.3), "✓" for anything else (completions,
    today's only other real kind -- "error" is never queued, per the one
    priority rule that survives unchanged)."""
    return "❓" if kind == "blocked_question" else "✓"


def model_effort_suffix(member) -> str:
    """"sonnet · high" / "opus" / "" -- model and effort folded together
    (TODO-feature-queue.md item 2, SPEC-teams.md SS5's "model folds into
    the existing activity column as compact text," extended to effort).
    Shared by console.py's TeamsPanel and team_flow.py's detail view so
    they can't render two different suffixes for the same member, same
    reasoning as every other function here.

    Either half is OMITTED, never a placeholder, when unknown -- an
    ADOPTED member's effort is permanently None (/status does not expose
    it, verified live 2026-08-20: a real session launched with
    `--effort high`, queried immediately after, no Effort field anywhere
    in the raw view), and that is a real fact about what's knowable, not
    a gap to paper over with "unknown"."""
    model = compact_model_name(member.model) if getattr(member, "model", None) else None
    effort = getattr(member, "effort", None)
    return " · ".join(v for v in (model, effort) if v)


def is_prompt_pending(slot) -> bool:
    """One predicate, shared by every surface (same reasoning as
    is_blocked()) -- Console's full ENGINE panel and Rail's compact line
    can never disagree about whether a role is stuck on a permission
    prompt (TODO-feature-queue.md item 5, the 2026-08-20 stuck-concierge
    incident)."""
    return bool(getattr(slot, "prompt_pending", False))


def prompt_preview_text(slot) -> str:
    """"" when nothing pending, else the verbatim (untrusted, same
    discipline as TeamMember.blocked_question) captured prompt preview."""
    return getattr(slot, "prompt_preview", None) or ""


def role_status_icon(slot) -> str:
    """The glyph beside role_status_phrase(). Its own function, and NOT
    liveness_icon(slot.liveness), because the two disagree in exactly one
    case.

    Found 2026-08-20 by looking at the rendered row rather than at the
    values behind it: a cwd-mismatched role rendered "✕ found running
    elsewhere", and ✕ is defined in the panel's own legend as "lost -- no
    saved history". One row asserting both that a session is running
    somewhere AND that it has no saved history. The phrase was right; the
    glyph next to it was the lie, and no test compared them because the
    phrase assertion passed.

    Why liveness really is LOST here, and correctly so: _has_history()
    looks for the transcript under the record's OWN working_dir, which in
    a mismatch is the stale one -- so it searches the directory we
    already know is wrong, finds nothing, and reports LOST. That verdict
    is right for the attach/activate contract, because a stale record
    genuinely cannot be Activated; it has to be re-attached first. What
    was wrong was borrowing LOST's icon, whose established meaning is a
    claim about history rather than about resumability.

    So the mismatch gets its own marker, matching the warning colour the
    phrase and Stream's formatter already use, instead of overloading a
    glyph whose meaning is already spoken for."""
    if getattr(slot, "cwd_mismatch", False):
        return "⚠"
    return liveness_icon(slot.liveness)


def role_status_phrase(slot) -> str:
    """Plain-language ENGINE-row status word for one RoleSlot -- shared
    by Rail (compact) and Console (fuller) so the two densities can't
    render two different messages for the same underlying state, same
    reasoning as every other function in this module.

    A cwd_mismatch slot MUST NOT render identically to a genuinely-
    stopped one (SPEC-engine-roles.md, 2026-08-20): that silence -- a
    live session found under this role's tmux name, reported as plain
    "inactive" with no hint anything was found at all -- is the exact
    failure this project keeps re-discovering in new clothes (the CHAT
    verdict, the swallowed [orchestrator] flag, the dropped instant ack,
    per the Lead). liveness itself is UNCHANGED by a mismatch (still
    STOPPED/LOST, never RUNNING -- the attach/activate contract); this
    function is what makes that case visibly different on screen without
    touching that contract.

    Deliberately no path in the returned phrase -- found_cwd is real
    diagnostic content, but the Lead's ruling was "legible... in plain
    language, not a path dump": the raw cwd belongs in Stream's
    diagnostic feed (stream.py's engine_role_cwd_mismatch formatter),
    not on the at-a-glance ENGINE row.

    PURE: a RoleSlot in, a phrase out -- no widget access, so a canary
    can assert on this directly without mounting a Textual app."""
    if slot.liveness == LIVENESS_RUNNING:
        return "active"
    if slot.cwd_mismatch:
        return "found running elsewhere"
    return "inactive"


def team_liveness(team) -> str:
    """Derived from members, never stored -- per the Lead's ruling: a
    team-level liveness field that could disagree with its own members
    is a second source of truth that can drift from the first. running
    if ANY member is running (the team is reachable through at least one
    path); otherwise stopped if any member has resumable history, else
    lost."""
    if not team.members:
        return LIVENESS_LOST
    if any(m.liveness == LIVENESS_RUNNING for m in team.members):
        return LIVENESS_RUNNING
    if any(m.liveness == LIVENESS_STOPPED for m in team.members):
        return LIVENESS_STOPPED
    return LIVENESS_LOST
