"""
Thin Ollama HTTP client for the L2.5 concierge. One warm model, used for
both classification-fallback and CHAT/QUERY answer phrasing.

MODEL CHOICE, REVISED (2026-08-17): qwen2.5:7b-instruct-q4_K_M, not 3B.

Original choice was 3B, on a 12-case test where it scored 10/12 to 7B's
11/12 at roughly a third of the latency (~190ms vs ~510ms/call warm).
That held up until the Lead asked for a DANGEROUS-direction-weighted
28-case adversarial set (natural filler/hesitation, question-phrased
instructions, self-correction, implicit targets, instructions buried
after a chat-shaped opener) -- the shape of input a false-negative on
DISPATCH actually looks like in real speech, not a constructed edge case.

On that set: 3B produced 5/20 dangerous-direction failures (25%) --
including a real instruction lost to CHAT on ordinary filler ("So yeah I
was thinking, um, maybe we should have the api gateway run its test
suite") and two implicit-addressee questions read as pure QUERY instead
of an instruction to check something. 7B produced 1/20 (5%), and that one
was a genuinely ambiguous test case both models landed on the same way,
not a clear model failure.

Per the Lead's decision rule stated in advance: if 3B fails in the
dangerous direction anywhere 7B doesn't, the latency difference stops
being "accuracy for speed" and becomes "safety for speed" -- and a slow
correct classification beats a fast silent one by a margin not worth
arguing about. Taking the ~510ms hit deliberately, not optimizing it
away -- classification is a minority of the fast-path total regardless
(Whisper dominates, see l1_wakeword's latency measurements), and the
keyword tier in classifier.py already skips the model call entirely for
CONTROL and many QUERY phrasings.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "l4_controller"))
from latency_log import log_event  # noqa: E402

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "qwen2.5:7b-instruct-q4_K_M"
# Re-sent on every call (not just once at startup) -- keeps the model
# resident between turns without a separate keep-warm daemon/thread.
KEEP_ALIVE = "30m"
REQUEST_TIMEOUT_S = 5.0

# Derived from the phrasing prompts' own "under 20 words" instruction,
# not a bare magic number: English averages roughly 1.3-1.5 tokens/word
# for this kind of BPE tokenizer, so 20 words is ~26-30 tokens. Doubled
# for headroom (punctuation, the model occasionally running a few words
# over, PHRASE_SYSTEM_QUERY's own summarize-not-enumerate instruction
# still needing room to name a count or a pattern) rather than tuned
# tight -- a cut-off mid-word response ("...claude-held") is a real,
# user-facing defect (found when the "what's running" QUERY had to
# summarize 9 sessions and ran past a 40-token cap), and the fix has to
# hold even when the facts given to the model are long, not just for the
# short-answer common case this was originally sized for.
PHRASE_MAX_TOKENS = 64

CLASSIFY_SYSTEM = """You classify a single spoken instruction to a voice assistant named Jarvis into exactly one category. Answer with ONLY the category word, nothing else.

CONTROL - a command about the conversation itself: cancel, never mind, stop, repeat that, say that again.
QUERY - ONLY asking about the state of a running task or session, with no action requested anywhere in the utterance: what's running, what's X doing, how much have I spent, is it done yet.
CHAT - conversational, no action or data requested: greetings, small talk, opinions, thanks.
DISPATCH - an instruction to be carried out by an agent, anywhere in the utterance: tell X to do Y, have X run/fix/deploy/check something. If a question and an instruction both appear in the same utterance -- in either order, even a short one tacked onto the end ("...and also have it restart") -- the whole thing is DISPATCH, never QUERY. An utterance is only QUERY if it asks about state and requests NOTHING else.
UNSURE - anything ambiguous, unclear, or not confidently one of the above. When in doubt, choose UNSURE, never CHAT.

Examples:
"cancel that" -> CONTROL
"never mind" -> CONTROL
"say that again" -> CONTROL
"what's the gateway doing" -> QUERY
"how much have I spent today" -> QUERY
"is shipcheck done yet" -> QUERY
"how's it going" -> CHAT
"thanks" -> CHAT
"good morning" -> CHAT
"tell shipcheck to redeploy the api" -> DISPATCH
"have mobile run the test suite" -> DISPATCH
"what's running right now, and tell the billing session to redeploy" -> DISPATCH
"what's the gateway doing, oh and also have it restart" -> DISPATCH
"""

RECLASSIFY_DISPATCH_OR_QUERY_SYSTEM = """A transcript mentions a real, currently-running session by name, so it cannot be idle chat -- it is either an INSTRUCTION for that session to do something, or a QUESTION asking about that session's current state. Answer with ONLY one word: DISPATCH or QUERY.

DISPATCH - asks the session to DO something: run, check, fix, restart, redeploy, compact, pull changes, etc. Includes indirect/polite phrasing ("can you have it...", "would it be possible to...", "get it to...") and instructions buried after other talk -- if an action is requested anywhere, it's DISPATCH.
QUERY - asks ABOUT the session's current state, without requesting any action: what it's doing, whether it's done, how much it's cost.

Examples:
"can someone check if the api gateway is healthy" -> DISPATCH
"would it be possible to have the billing session check its logs" -> DISPATCH
"so yeah, um, maybe we should have the gateway run its test suite" -> DISPATCH
"what's the gateway up to" -> QUERY
"has shipcheck finished yet" -> QUERY
"""

ASSESS_ADDRESSED_SYSTEM = """A voice assistant named Jarvis was triggered by hearing its own name and captured this transcript. Decide whether it was actually spoken TO Jarvis (a remark, question, or request directed at the assistant) or whether it's AMBIENT speech Jarvis merely overheard (a conversation between other people, a project or product name that happens to contain "Jarvis", talk ABOUT Jarvis rather than TO it). Answer with ONLY one word: ADDRESSED, AMBIENT, or UNSURE. If genuinely unclear, answer UNSURE -- do not guess between the other two.

Examples:
"how's it going Jarvis" -> ADDRESSED
"thanks Jarvis, appreciate it" -> ADDRESSED
"that's true, what's up, we're creating Iron Man IRL Iron Man Jarvis, Dino, Dino Transcripted" -> AMBIENT
"I think Jarvis needs a better wake word detector" -> UNSURE
"""

# Deliberately does NOT include the user's transcript anywhere in this
# prompt -- see phrase_answer's docstring. Only ever fed `facts`, a
# code-computed string from a real provider call (list_sessions,
# session_activity, spend). This template has no place to put a
# transcript even if a caller tried to pass one.
PHRASE_SYSTEM_QUERY = """You are Jarvis, a voice assistant. Phrase the FACTS below as ONE short SPOKEN sentence (under 20 words) reporting them to the user. Use ONLY the facts given -- do not add any detail not present in them. If the facts say nothing relevant or state is unknown, say you don't have that information -- never guess.

This gets read aloud, not displayed as text. If the facts contain a long list (many session names, many items), SUMMARIZE it instead of reading every item -- a count, a pattern, "mostly test sessions" -- nobody wants a list of names read to them one by one. Example: given "currently running sessions: claude-a, claude-b, claude-c, claude-d, claude-e", say something like "You've got 5 sessions running" or "5 sessions running, mostly test ones" -- not a recitation of all 5 names.

Facts:
{facts}

Jarvis says:"""

PHRASE_SYSTEM_CHAT = """You are Jarvis, a voice assistant, acknowledging a casual remark briefly and pleasantly. Reply in ONE short, natural spoken sentence (under 15 words). You have no information about running tasks, sessions, or costs -- never claim any.

Jarvis says:"""


def _generate(prompt: str, num_predict: int, temperature: float = 0.0) -> tuple[str, float]:
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"num_predict": num_predict, "temperature": temperature},
        "keep_alive": KEEP_ALIVE,
    }
    t0 = time.monotonic()
    req = urllib.request.Request(
        OLLAMA_URL, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
        data = json.loads(resp.read())
    elapsed_ms = (time.monotonic() - t0) * 1000
    return data.get("response", "").strip(), elapsed_ms


def warm_up() -> bool:
    """Fire a trivial call at concierge startup so the model is resident
    before the first real turn -- a cold load measured ~1.7s for this
    model, which alone blows the entire 800ms fast-path budget. Returns
    True on success; failure is logged, not raised, since the concierge
    should still start and simply eat the cold-load cost on turn one
    rather than fail to start entirely over Ollama being slow to answer
    once."""
    try:
        _, elapsed_ms = _generate("Reply with the word OK.", num_predict=3)
        log_event("concierge_model_warm_up", model=MODEL, elapsed_ms=round(elapsed_ms, 1))
        return True
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        log_event("concierge_model_warm_up_failed", model=MODEL, error=str(e))
        return False


def classify_with_model(text: str) -> str:
    prompt = CLASSIFY_SYSTEM + f'\n"{text}" -> '
    response, elapsed_ms = _generate(prompt, num_predict=6)
    log_event("concierge_classify", tier="model", elapsed_ms=round(elapsed_ms, 1), raw_response=response)
    if not response.strip():
        return ""
    return response.strip().split()[0].upper().rstrip(".,:;")


def reclassify_dispatch_or_query(text: str) -> str:
    """Called only when a transcript names a live session AND the
    5-way classifier said CHAT -- see classifier.py's hard rule. A
    second, constrained call rather than silently forcing UNSURE, so a
    real QUERY ("what's the gateway doing") that happens to also
    mis-trigger CHAT still gets answered locally instead of being
    forwarded needlessly. Only ever returns "DISPATCH", "QUERY", or
    something the caller must treat as neither (garbage/timeout) --
    CONTROL/CHAT/UNSURE are not valid answers to this prompt by
    construction, so classifier.py doesn't need to re-check for them."""
    prompt = RECLASSIFY_DISPATCH_OR_QUERY_SYSTEM + f'\n"{text}" -> '
    response, elapsed_ms = _generate(prompt, num_predict=6)
    log_event("concierge_reclassify_dispatch_or_query", elapsed_ms=round(elapsed_ms, 1), raw_response=response)
    if not response.strip():
        return ""
    return response.strip().split()[0].upper().rstrip(".,:;")


def assess_addressed(text: str) -> str:
    """Called only for a CHAT-classified transcript, by
    classifier.assess_retention() -- the retention half of NOT_ADDRESSED.
    Deliberately three-way (ADDRESSED/AMBIENT/UNSURE), not binary: a
    forced binary choice on a genuinely hard case just means the model
    guesses, and this decision's whole design (see assess_retention) only
    discards on AMBIENT specifically so it can treat "the model isn't
    sure" as its own real answer rather than rounding it to one side."""
    prompt = ASSESS_ADDRESSED_SYSTEM + f'\n"{text}" -> '
    response, elapsed_ms = _generate(prompt, num_predict=6)
    log_event("concierge_assess_addressed", elapsed_ms=round(elapsed_ms, 1), raw_response=response)
    if not response.strip():
        return "UNSURE"
    label = response.strip().split()[0].upper().rstrip(".,:;")
    return label if label in ("ADDRESSED", "AMBIENT", "UNSURE") else "UNSURE"


def phrase_answer(kind: str, facts: str = "") -> tuple[str, float]:
    """kind: "CHAT" or "QUERY". `facts` is the ONLY source of truth the
    model may draw on for a QUERY answer -- e.g. a provider function's
    literal output -- never invented. Ignored for CHAT (nothing to
    report; the model is explicitly told it has no state access).

    Deliberately takes no `text` parameter -- the raw transcript is never
    part of this prompt, structurally, not just by instruction. Found
    live (gu2s6tnt's review): when this used to also receive the full
    original transcript, a compound utterance that slipped a DISPATCH
    clause past the QUERY keyword tier ("what's running right now, and
    tell the billing session to redeploy") made the model pick up the
    redeploy clause from `text` and FABRICATE that it happened --
    "Currently running: claude-orchestrator. Billing session redeployed."
    -- despite the prompt saying to use only the facts given. A confident
    false confirmation of an action that never occurred, with no record
    of the real instruction anywhere, is strictly worse than silence.
    Telling a model not to use information it can see is not a control;
    not showing it the information is. If `text` isn't in this function's
    signature, it can't leak into the prompt no matter what a future
    caller passes or what a future prompt edit forgets to restate."""
    template = PHRASE_SYSTEM_QUERY if kind == "QUERY" else PHRASE_SYSTEM_CHAT
    prompt = template.format(facts=facts)
    response, elapsed_ms = _generate(prompt, num_predict=PHRASE_MAX_TOKENS, temperature=0.3)
    log_event("concierge_phrase_model_call", kind=kind, elapsed_ms=round(elapsed_ms, 1))
    return response, elapsed_ms
