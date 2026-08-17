"""
L2.5 concierge core (SPEC-L2.5-concierge.md). Sits between L2 (daemon.py,
a finished dictation transcript) and L3 (the orchestrator): classifies
every transcript and either answers directly (CONTROL/QUERY/CHAT, target
<800ms fast path, spoken via say_feedback.speak()) or forwards to L3
unchanged (DISPATCH/UNSURE) through the same deliver_transcript() call
daemon.py used to call directly.

Called synchronously, in-process, from daemon.py's dictation-end
handling -- not a separate OS process. "Never blocks on L3" (requirement
5) is satisfied by deliver_transcript()/transport.deliver() already being
a fast keystroke-send that returns before L3 finishes reasoning, not by
anything new here; L3's actual reasoning happens in its own tmux
session/process regardless of what calls deliver_transcript(). This
module blocking daemon.py briefly for its OWN fast-path work (classify +
at most one local model call + a non-blocking speak()) before daemon.py
returns to listening is the deliberate, measured-latency design -- same
precedent as verify_wake_trigger's ~0.5s block on the START side.

NOT YET WIRED: the NOT_ADDRESSED intent class and the retention decision
it drives (discard an ambient-conversation transcript instead of writing
it to ~/.jarvis/dictations/) -- sequenced after this core classifier
per the Lead's explicit instruction ("don't let it complicate the first
version"). daemon.py's chunk-log write is unconditional today and stays
that way until that follow-up lands.
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "l4_controller"))
from dispatch_state import dispatch_state  # noqa: E402
from l2_l3_handoff import deliver_transcript, DEFAULT_ORCHESTRATOR_TARGET  # noqa: E402
from latency_log import log_event  # noqa: E402
from providers import list_sessions, session_activity, spend  # noqa: E402
from say_feedback import speak  # noqa: E402
from transport import TmuxTransport  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from classifier import classify, Classification, CONTROL, QUERY, CHAT, DISPATCH, UNSURE  # noqa: E402
from ollama_client import phrase_answer  # noqa: E402

# In-process only, one dictation handled fully before the next starts
# (same non-overlap assumption latency_log.py's docstring already states
# for this project) -- no persistence needed for "repeat that" to reach
# across a single daemon.py run.
_last_utterance: dict[str, str | None] = {"text": None}


def _session_tokens(session_id: str, alias: str | None) -> set[str]:
    name = session_id[len("claude-"):] if session_id.lower().startswith("claude-") else session_id
    tokens = {t for t in re.split(r"[-_\s]+", name.lower()) if t}
    if alias:
        tokens.add(alias.lower())
    return tokens


def _resolve_session(text: str) -> dict | None:
    """Best-effort match of a spoken session reference ("the gateway",
    "shipcheck") against real, currently-running sessions -- token
    overlap against session_id (minus the "claude-" prefix) and alias,
    never a guess. Returns None on no match OR an ambiguous multiple
    match; callers must treat that as "don't know," not pick one."""
    words = {w for w in re.findall(r"[a-z0-9]+", text.lower())}
    matches = [
        s for s in list_sessions()
        if _session_tokens(s["session_id"], s.get("alias")) & words
    ]
    return matches[0] if len(matches) == 1 else None


def _phrase(kind: str, text: str, facts: str = "") -> str:
    response, elapsed_ms = phrase_answer(kind, text, facts)
    log_event("concierge_phrase", kind=kind, elapsed_ms=round(elapsed_ms, 1))
    return response


def _handle_control(text: str) -> str:
    lowered = text.lower()
    if "again" in lowered or "repeat" in lowered or "what did you say" in lowered:
        return _last_utterance["text"] or "I haven't said anything yet."
    # Real cancellation is L1's acoustic CANCEL_ARMED window during L4's
    # confirm prompt (see l1_wakeword/README.md's governing asymmetry) --
    # by the time a standalone CONTROL utterance reaches here, a dictation
    # has already fully ended, so there's nothing in-flight at THIS layer
    # to cancel. Acknowledge without inventing an action.
    return "Okay, nothing to cancel right now."


def _handle_query(text: str) -> str:
    """Deterministic data first (requirement 1), always -- the model
    (via _phrase) only phrases what's already been fetched here, it never
    originates a fact about session state or cost."""
    lowered = text.lower()

    if any(k in lowered for k in ("spend", "spent", "cost")):
        session = _resolve_session(text)
        if session is None:
            return "I'm not sure which session's cost you mean -- which one?"
        result = spend(session["session_id"])
        facts = (
            f"cost data for {session['session_id']}: {result['summary']}"
            if result["ok"] and result["summary"]
            else f"no cost data currently available for {session['session_id']}"
        )
        return _phrase(QUERY, text, facts)

    if any(k in lowered for k in ("running", "sessions", "going on")):
        sessions = list_sessions()
        if not sessions:
            return "Nothing's running right now."
        names = ", ".join(s.get("alias") or s["session_id"] for s in sessions)
        return _phrase(QUERY, text, f"currently running sessions: {names}")

    session = _resolve_session(text)
    if session is not None:
        activity = session_activity(session["session_id"])
        facts = f"session {session['session_id']}: state={activity['state']}, activity={activity['activity']}"
        return _phrase(QUERY, text, facts)

    # No named session resolved -- most likely asking about the last
    # dispatch ("is it done yet", "what's it doing"). dispatch_state() is
    # code-written (see dispatch_state.py), never an L3 self-report; the
    # live activity poll below is the same deterministic classifier
    # session_activity() always uses, not an inference either.
    state = dispatch_state()
    if state is None:
        return "Nothing's been dispatched yet."
    if state["stage"] == "complete":
        return "Yes, that finished already."
    activity = session_activity(DEFAULT_ORCHESTRATOR_TARGET)
    if activity["activity"]:
        return f"Still working on it -- {activity['activity']}."
    return "Still working on it."


def _forward(text: str, orchestrator_target: str, live_deliver: bool) -> dict | None:
    """live_deliver=False (the default) never touches a real tmux session
    -- mirrors daemon.py's own --live-deliver pattern exactly, for the
    same reason it exists there: a CLI smoke test of this module hitting
    DEFAULT_ORCHESTRATOR_TARGET, which resolves to whatever real
    "claude-orchestrator" session happens to be running, is production by
    omission otherwise. Confirmed the hard way -- see the incident this
    fixed."""
    if not live_deliver:
        print(f"(live_deliver=False: NOT forwarding to {orchestrator_target!r} -- would send: {text!r})")
        return None
    return deliver_transcript(text, TmuxTransport(), orchestrator_target=orchestrator_target)


def handle_transcript(
    text: str,
    orchestrator_target: str = DEFAULT_ORCHESTRATOR_TARGET,
    live_deliver: bool = False,
) -> dict:
    """Entry point daemon.py calls once per finished dictation. Never
    raises for an ordinary classification/handling failure: a bug in this
    module's local-answer path forwards to L3 instead of silently eating
    a turn that might have been a real instruction -- same asymmetry as
    everything else on this project (a forward that turns out unnecessary
    costs L3 a wasted turn; a dropped real instruction is silent and
    worse).

    live_deliver defaults to False, same as daemon.py's --live-deliver --
    daemon.py's real integration passes its own --live-deliver value
    straight through here, so the whole L1->L2.5 pipeline is gated by one
    consistent, explicitly-opted-in flag rather than each layer defaulting
    differently."""
    t_start = time.monotonic()
    try:
        result = classify(text)
    except Exception as e:
        log_event("concierge_classify_error", error=str(e))
        result = Classification(UNSURE, tier="error")

    log_event("concierge_classified", label=result.label, tier=result.tier, chars=len(text))

    if result.label in (DISPATCH, UNSURE):
        delivery = _forward(text, orchestrator_target, live_deliver)
        elapsed_ms = (time.monotonic() - t_start) * 1000
        log_event("concierge_fast_path_done", label=result.label, elapsed_ms=round(elapsed_ms, 1), forwarded=live_deliver)
        return {"label": result.label, "forwarded": live_deliver, "delivery": delivery}

    try:
        if result.label == CONTROL:
            response = _handle_control(text)
        elif result.label == QUERY:
            response = _handle_query(text)
        else:  # CHAT
            response = _phrase(CHAT, text)
    except Exception as e:
        log_event("concierge_local_handling_error", label=result.label, error=str(e))
        delivery = _forward(text, orchestrator_target, live_deliver)
        elapsed_ms = (time.monotonic() - t_start) * 1000
        log_event(
            "concierge_fast_path_done", label=result.label, elapsed_ms=round(elapsed_ms, 1),
            forwarded=live_deliver, fell_back_to_forward=True,
        )
        return {"label": result.label, "forwarded": live_deliver, "delivery": delivery, "error": str(e)}

    _last_utterance["text"] = response
    speak(response)
    elapsed_ms = (time.monotonic() - t_start) * 1000
    log_event(
        "concierge_fast_path_done", label=result.label, elapsed_ms=round(elapsed_ms, 1),
        forwarded=False, response_chars=len(response),
    )
    return {"label": result.label, "forwarded": False, "response": response}


if __name__ == "__main__":
    import argparse

    from ollama_client import warm_up

    ap = argparse.ArgumentParser()
    ap.add_argument("text", help="transcript to classify/handle, as if it were a finished dictation")
    ap.add_argument("--target", default=DEFAULT_ORCHESTRATOR_TARGET)
    ap.add_argument("--no-warmup", action="store_true")
    ap.add_argument(
        "--live-deliver", action="store_true",
        help="actually call deliver_transcript() on a DISPATCH/UNSURE classification (real tmux send-keys "
             "into --target, which defaults to the REAL claude-orchestrator session if it's running). "
             "Never the default -- see daemon.py's identical flag and the incident that made this one "
             "match it: a CLI smoke test without this delivered fake test text into a real live orchestrator "
             "session by accident.",
    )
    args = ap.parse_args()

    if not args.no_warmup:
        warm_up()
    t0 = time.monotonic()
    out = handle_transcript(args.text, orchestrator_target=args.target, live_deliver=args.live_deliver)
    print(f"{out}\n({(time.monotonic() - t0) * 1000:.0f}ms total)")
