"""
Answering a blocked agent by voice (docs/TODO-feature-queue.md #5,
SPEC-blockers.md SS5, stage 2 of SS8's build order). Today escalation is
one-way: Jarvis tells Ayman a session is stuck, and there was no voice
route back. This is that route.

WRITE-SIDE ONLY, DELIBERATELY. pending_questions() (the read half) lives
in providers.py, imported by tools_read.py -- this module holds only
answer_blocked_session(), imported by tools_write.py alone. Splitting it
this way is structural, not cosmetic: server_readonly.py's own docstring
promises "nothing reachable from here can send an arbitrary caller-
supplied payload to an arbitrary target -- that capability lives only in
tools_write.deliver_batch", and that promise is checked by import graph,
not registered-tool list (mcp_surface_canary.py). If this module's write
function lived in the same file tools_read.py imports from, the
Haiku-only process would import it too -- never registered as a tool, so
never actually callable by Haiku, but the STATED guarantee would no
longer be true as literally written. Keeping this file out of
tools_read.py's import graph entirely keeps it true both ways.

DELIBERATELY NOT STAGE 3. This module never invents an answer from
grounded context (SS4.1) or a standing policy (SS4.2) -- it only routes
an answer AYMAN ALREADY SPOKE back to the specific session that asked.
Stage 3 (auto-answer) is explicitly later, after detection has run
against real blocks for a while (the Lead's ruling, 2026-08-20).

WHY A DIGIT KEYSTROKE, NOT TYPED TEXT. Verified live (2026-08-20, real
AskUserQuestion pickers, throwaway tmux sessions): a picker's "N. Type
something." entry does NOT open a free-text field via plain send-keys --
selecting it and pressing Enter DECLINES the question instead. A bare
digit keystroke, with no Enter, DOES immediately and correctly submit
that numbered option. So this module never attempts free text: it
matches Ayman's spoken answer against the CAPTURED option labels
(providers._parse_blocked_question()'s own `options`, which already
excludes "Type something."/"Chat about this" -- see that function's
docstring) and sends only the single digit for an UNAMBIGUOUS match --
see _match_option(). No match, or more than one plausible match,
refuses rather than guessing (SS7 rule 3: "never guess between multiple
pending questions -- hold and ask", applied here to option-matching too,
not just session-matching).

THE ASYMMETRY THIS DESIGN RESTS ON (SPEC-blockers.md SS5.1): an ANSWER
misclassified as DISPATCH is loud and recoverable -- the plan summary
names a target that makes no sense and Ayman notices. A DISPATCH
misclassified as ANSWER would be SILENT -- an instruction typed into a
question prompt as though it were a reply. This module is what makes
that direction structurally hard rather than just discouraged by prompt
wording: answer_blocked_session() only ever succeeds when the spoken
text cleanly, unambiguously matches one of the FEW real captured option
labels for a FRESHLY-reconfirmed BLOCKED_QUESTION pane. Ordinary
dispatch text essentially never satisfies that, so a router that
mistakenly tries this on a real instruction gets a loud refusal back,
not a silent wrong delivery.

HARD BOUNDARY, UNCHANGED (SPEC-blockers.md SS2): this delivers text to a
session that is showing a QUESTION, never an authorisation. That
distinction is enforced upstream, structurally, by pane_state.py's own
classifier -- BLOCKED_QUESTION and PERMISSION_PROMPT are mutually
exclusive, checked via distinct positive footer signatures, and
PERMISSION_PROMPT is checked FIRST (the more dangerous state wins any
future collision). blocked_state.py is only ever populated by the poller
observing BLOCKED_QUESTION specifically, so nothing reachable through
this module ever targets a permission/plan-approval prompt.
"""

from __future__ import annotations

from latency_log import log_event
from providers import pending_questions, transport


def _match_option(answer_text: str, options: list[str]) -> int | None:
    """1-based index of the SINGLE option `answer_text` unambiguously
    matches, or None on zero or multiple matches -- never guesses.
    Case-insensitive, bidirectional substring ("staging" matches "Use
    the staging environment", and vice versa), so both a terse and a
    fuller spoken answer land -- but a match shorter than
    MIN_MATCH_CHARS on the shorter side is discarded first, so a
    one-or-two-letter accidental overlap (e.g. "no" inside "Norway")
    can't manufacture a false single match."""
    MIN_MATCH_CHARS = 3
    answer_norm = " ".join(answer_text.strip().lower().split())
    matches = []
    for i, opt in enumerate(options, start=1):
        opt_norm = " ".join(opt.strip().lower().split())
        if not opt_norm or not answer_norm:
            continue
        shorter = opt_norm if len(opt_norm) <= len(answer_norm) else answer_norm
        if len(shorter) < MIN_MATCH_CHARS:
            continue
        if opt_norm in answer_norm or answer_norm in opt_norm:
            matches.append(i)
    if len(matches) == 1:
        return matches[0]
    return None


def answer_blocked_session(answer_text: str, target: str | None = None) -> dict:
    """Routes `answer_text` (Ayman's own spoken words, verbatim) back to
    the specific blocked session it answers. Returns {"ok", "detail",
    "team_id"}.

    target: a team id/alias/tmux hint, when the caller already knows
    which session this answers (e.g. Ayman named it: "tell gateway to
    use staging"). Omitted or empty: resolved from pending_questions()
    ONLY when there is exactly one -- 0 pending or 2+ pending both
    refuse (SS5.4/SS7 rule 3: never guess between multiple pending
    questions, hold and ask). A caller that gets ok=False for the
    "N sessions waiting" reason should relay `detail` back to Ayman
    verbatim-ish and wait for a disambiguating answer, never pick one.

    Never invents an answer (stage 3, deliberately not built here) --
    always requires answer_text to unambiguously match one of the
    question's own captured option labels. See module docstring for why
    that's also what keeps a misclassified DISPATCH from silently
    landing here."""
    pending = pending_questions()

    if target:
        target_norm = target.strip().lower()
        pending = [
            p for p in pending
            if target_norm in (p["team_id"].lower(), p["tmux"].lower())
        ]
        if not pending:
            return {"ok": False, "detail": f"no pending question found for {target!r}", "team_id": None}

    if not pending:
        return {"ok": False, "detail": "no session is currently waiting on an answer", "team_id": None}

    if len(pending) > 1:
        names = ", ".join(f"{p['team_id']} ({p['question']!r})" for p in pending)
        return {
            "ok": False,
            "detail": f"{len(pending)} sessions are waiting on an answer -- which one? {names}",
            "team_id": None,
        }

    entry = pending[0]
    option_index = _match_option(answer_text, entry["options"])
    if option_index is None:
        return {
            "ok": False,
            "detail": (
                f"{entry['team_id']}'s question ({entry['question']!r}) offers {entry['options']!r} -- "
                f"{answer_text!r} doesn't clearly match exactly one of them, refusing rather than guessing"
            ),
            "team_id": entry["team_id"],
        }

    # Fresh re-check right at delivery time, not trusted from the
    # pending_questions() snapshot a moment ago -- re-running
    # pending_questions() is the same "session died -> drop immediately"
    # self-heal, applied again at the actual moment of use.
    still_pending = {p["claude_session"] for p in pending_questions()}
    if entry["claude_session"] not in still_pending:
        return {
            "ok": False,
            "detail": f"{entry['team_id']}'s session is no longer waiting on an answer -- it may have moved on or died",
            "team_id": entry["team_id"],
        }

    result = transport.answer_blocked_question(entry["tmux"], option_index)
    log_event(
        "blocked_answer_delivered", team_id=entry["team_id"], ok=result.ok,
        question=entry["question"][:200], matched_option=entry["options"][option_index - 1],
        answer_chars=len(answer_text), reason=result.reason,
    )
    if not result.ok:
        return {"ok": False, "detail": f"couldn't deliver the answer to {entry['team_id']}: {result.detail}", "team_id": entry["team_id"]}
    return {
        "ok": True,
        "detail": f"told {entry['team_id']}: {entry['options'][option_index - 1]}",
        "team_id": entry["team_id"],
    }
