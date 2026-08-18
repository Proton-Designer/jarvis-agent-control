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

NOT_ADDRESSED (SPEC-L2.5's sixth intent class) is wired as a decision on
top of CHAT, not a 7th label. It answers two questions from one model
call: whether the transcript survives to disk, and -- since 2026-08-18 --
whether Jarvis answers it aloud. A CHAT transcript still never forwards
to L3. See classifier.assess_retention() for the (deliberately
asymmetric) retention bar and the speech gate's rationale, and daemon.py's
_report_and_deliver -- the chunk-log write happens AFTER this decision,
not before, so a discarded transcript is never written in the first place
rather than being written then deleted.
"""
from __future__ import annotations

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
from classifier import assess_retention, classify, Classification, CONTROL, QUERY, CHAT, DISPATCH, UNSURE  # noqa: E402
from ollama_client import phrase_answer, phrase_chat_reply  # noqa: E402
from session_match import resolve_session as _resolve_session  # noqa: E402

# In-process only, one dictation handled fully before the next starts
# (same non-overlap assumption latency_log.py's docstring already states
# for this project) -- no persistence needed for "repeat that" to reach
# across a single daemon.py run.
_last_utterance: dict[str, str | None] = {"text": None}


def _phrase(kind: str, facts: str = "") -> str:
    """Deliberately does NOT take the raw transcript -- see
    ollama_client.phrase_answer's docstring for why. The phrasing model
    only ever sees `facts` (code-computed, from a real provider call),
    never anything Ayman said."""
    response, elapsed_ms = phrase_answer(kind, facts)
    log_event("concierge_phrase", kind=kind, elapsed_ms=round(elapsed_ms, 1))
    return response


def _chat_facts() -> str:
    """The deterministic-before-model rule (SPEC-L2.5 requirement 1),
    applied to small talk. Everything here is code-computed from a real
    provider call; the phrasing model sees ONLY this string and never
    the transcript, exactly as in _handle_query.

    Why CHAT gets facts at all, when it used to be told it had none:
    "how's it going" is the single most natural thing to say to a system
    like this, and a Jarvis that cannot answer it with anything real is
    a text-to-speech toy. The anti-fabrication rule was never "the model
    must know nothing" -- it is "the model must not be the SOURCE of a
    fact." Passing it a code-computed fact string satisfies that rule as
    completely here as it does for QUERY.

    A provider failure must produce "unknown," never "nothing" -- an
    empty session list and an unreachable tmux look identical from the
    model's side, and silently reporting the second as the first is the
    exact silence-read-as-success failure this project keeps finding.
    See PHRASE_SYSTEM_CHAT, which is told not to round unknown down to
    nothing."""
    parts: list[str] = []
    try:
        sessions = list_sessions()
    except Exception as e:
        log_event("concierge_chat_facts_error", source="list_sessions", error=str(e))
        parts.append("current session state is UNKNOWN (could not be read)")
    else:
        if sessions:
            names = ", ".join(s.get("alias") or s["session_id"] for s in sessions)
            parts.append(f"{len(sessions)} agent session(s) running: {names}")
        else:
            parts.append("no agent sessions are running right now")

    try:
        state = dispatch_state()
    except Exception as e:
        log_event("concierge_chat_facts_error", source="dispatch_state", error=str(e))
        state = None
    else:
        if state is None:
            parts.append("nothing has been dispatched yet this session")
        elif state.get("stage") == "complete":
            parts.append("the last dispatch has finished")
        else:
            parts.append("a dispatch is still in progress")

    return "\n".join(parts)


def _handle_chat(text: str) -> str:
    """The one handler that passes the transcript to a model, via
    phrase_chat_reply() -- see its docstring for the three upstream
    gates that make that safe and the residual risk it does not close.
    _handle_query deliberately still does not, and phrase_answer() now
    asserts QUERY-only so the two paths cannot converge by accident.

    A reply to a greeting has to have heard the greeting; the first
    build of this spoke deterministic state at Ayman no matter what he
    said, which answered nothing."""
    response, elapsed_ms = phrase_chat_reply(text, _chat_facts())
    log_event("concierge_phrase", kind=CHAT, elapsed_ms=round(elapsed_ms, 1))
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
    originates a fact about session state or cost.

    `text` is used ONLY to route to the right deterministic lookup below
    (spend vs. list vs. per-session activity vs. dispatch status) -- it
    is never passed to _phrase/phrase_answer. Found live (gu2s6tnt's
    review): a compound utterance ("what's running right now, and tell
    the billing session to redeploy") that slipped past the keyword tier
    into here would previously reach _phrase with the FULL original text
    plus partial facts, and the model would pick up the redeploy clause
    and fabricate that it happened -- "Currently running: claude-
    orchestrator. Billing session redeployed." despite the prompt saying
    to use only the given facts. A prompt telling the model not to do
    something is not a control; not showing it the transcript at all is.
    (The keyword-tier hole itself is fixed separately, see
    classifier.py's anchored _QUERY_PATTERNS -- this fix stands on its
    own regardless, since text could still reach here via the model
    tier.)"""
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
        return _phrase(QUERY, facts)

    if any(k in lowered for k in ("running", "sessions", "going on")):
        sessions = list_sessions()
        if not sessions:
            return "Nothing's running right now."
        names = ", ".join(s.get("alias") or s["session_id"] for s in sessions)
        return _phrase(QUERY, f"currently running sessions: {names}")

    session = _resolve_session(text)
    if session is not None:
        activity = session_activity(session["session_id"])
        facts = f"session {session['session_id']}: state={activity['state']}, activity={activity['activity']}"
        return _phrase(QUERY, facts)

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
    wake_score: float | None = None,
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
    differently.

    wake_score is the acoustic START trigger's score (from L1, purely for
    the NOT_ADDRESSED discard-event log -- measuring the real false-
    trigger rate needs it correlated with the outcome). Optional and
    never required for correctness: a caller that doesn't have it (the
    CLI below, any other test harness) just gets a log entry with
    wake_score=None rather than a broken retention decision."""
    t_start = time.monotonic()
    try:
        result = classify(text)
    except Exception as e:
        log_event("concierge_classify_error", error=str(e))
        result = Classification(UNSURE, tier="error")

    log_event("concierge_classified", label=result.label, tier=result.tier, chars=len(text))

    if result.label in (DISPATCH, UNSURE):
        delivery = _forward(text, orchestrator_target, live_deliver)
        # deliver_transcript() can now return None on a real failure (no
        # jarvis-l4 tools connected -- gu2s6tnt's preflight), not only
        # when live_deliver=False skipped the call entirely. Those are
        # different things: one never tried, the other tried and failed
        # (and already spoke the failure itself). `forwarded` here means
        # "actually delivered," not "attempted" -- echoing live_deliver
        # unconditionally would have reported a failed delivery as a
        # success to everything downstream (this function's own caller,
        # the l1_concierge_round_trip log).
        delivered = live_deliver and delivery is not None and getattr(delivery, "ok", False)
        elapsed_ms = (time.monotonic() - t_start) * 1000
        log_event(
            "concierge_fast_path_done", label=result.label, elapsed_ms=round(elapsed_ms, 1),
            forwarded=delivered, live_deliver_requested=live_deliver,
        )
        return {"label": result.label, "forwarded": delivered, "delivery": delivery}

    if result.label == CHAT:
        # GATE: speak on CHAT only when the transcript was positively
        # judged ADDRESSED. This replaced an unconditional never-speak
        # guard on 2026-08-18, and the distinction is the entire point.
        #
        # The original guard was protecting against one specific thing:
        # a false wake-word trigger on ambient conversation making Jarvis
        # audibly interject into a conversation that was never addressed
        # to it -- not recoverable the way staying silent is. That
        # concern was and remains correct. What was wrong was the
        # discriminator: keying on the CHAT *label* answers "is this
        # small talk?", when the question the guard actually needed
        # answered is "was this said to me?" Those come apart exactly at
        # the case that matters, and Ayman hit it live -- he said "What's
        # up? How's it going?" straight at the microphone, the system
        # ran assess_addressed(), got ADDRESSED in 624ms, and stayed
        # silent anyway, because the verdict was only ever consumed by
        # the retention decision. It computed the right answer and threw
        # it away.
        #
        # THRESHOLD: silent on AMBIENT, and on the imperative
        # short-circuit's None. UNSURE speaks. That is a deliberately
        # LOWER bar than the retention decision applies to the very same
        # verdict, and the two differing is the design, not an
        # inconsistency -- assess_addressed() returns three-way evidence
        # and each consumer sets its own threshold from its own cost
        # asymmetry:
        #   - Retention discards only on AMBIENT, because a wrong
        #     discard is irreversible: there is no artifact left to
        #     diagnose from.
        #   - Speech stays silent only on AMBIENT, because a wrong
        #     sentence is one recoverable utterance, while a wrong
        #     silence is the failure Ayman actually hit and had no way
        #     to tell from a crash.
        # Measured, not assumed: "Hello. How are you doing?" -- his own
        # example -- verdicts UNSURE, every time. A bare greeting IS
        # ambiguous in isolation and the model is right to say so; a
        # gate that required ADDRESSED would have stayed mute on the
        # exact sentence this whole change exists to answer.
        #
        # What still protects the original concern: reaching here at all
        # required daemon.py's verify_wake_trigger() to re-transcribe
        # the trigger audio and positively confirm "hey jarvis" was
        # spoken (a rejected trigger never enters CAPTURING, so no
        # transcript is produced at all). A false acoustic fire is
        # already filtered upstream. AMBIENT then catches the residual
        # case verification cannot -- the name really was said, but ABOUT
        # Jarvis rather than TO it.
        #
        # One model call still serves both decisions (see
        # assess_retention's docstring): the speech gate adds no latency
        # to the classification path, only the phrase call it enables.
        #
        # Logged as an EVENT, never the content: the decision, the reason
        # (itself never the transcript text), char count, and the
        # acoustic wake score for measuring the real false-trigger rate
        # over time. response_chars, not the response -- same rule.
        retain, reason, verdict = assess_retention(text)
        response = None
        if verdict in ("ADDRESSED", "UNSURE"):
            try:
                response = _handle_chat(text)
            except Exception as e:
                # Deliberately does NOT fall back to forwarding, unlike
                # the CONTROL/QUERY path below. A failed chat reply is
                # small talk that went unanswered; forwarding it would
                # put "how's it going" in front of the orchestrator as
                # though it were an instruction. Silence is the correct
                # failure here, and it is the pre-2026-08-18 behaviour.
                log_event("concierge_chat_response_error", error=str(e))
                response = None
        if response:
            _last_utterance["text"] = response
            speak(response)
        elapsed_ms = (time.monotonic() - t_start) * 1000
        log_event(
            "concierge_chat_handled", chars=len(text), elapsed_ms=round(elapsed_ms, 1),
            retain=retain, retention_reason=reason, wake_score=wake_score,
            verdict=verdict, spoken=bool(response),
            response_chars=len(response) if response else 0,
        )
        return {
            "label": result.label, "forwarded": False, "response": response,
            "spoken": bool(response), "retain": retain,
        }

    try:
        if result.label == CONTROL:
            response = _handle_control(text)
        else:  # QUERY
            response = _handle_query(text)
    except Exception as e:
        log_event("concierge_local_handling_error", label=result.label, error=str(e))
        delivery = _forward(text, orchestrator_target, live_deliver)
        delivered = live_deliver and delivery is not None and getattr(delivery, "ok", False)
        elapsed_ms = (time.monotonic() - t_start) * 1000
        log_event(
            "concierge_fast_path_done", label=result.label, elapsed_ms=round(elapsed_ms, 1),
            forwarded=delivered, live_deliver_requested=live_deliver, fell_back_to_forward=True,
        )
        return {"label": result.label, "forwarded": delivered, "delivery": delivery, "error": str(e)}

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
