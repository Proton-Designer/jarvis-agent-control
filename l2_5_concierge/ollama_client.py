"""
Thin Ollama HTTP client for the L2.5 concierge. One warm model
(qwen2.5:3b-instruct-q4_K_M), used for both classification-fallback and
CHAT/QUERY answer phrasing.

Single-model choice is measured, not assumed. Ran the same 12-case
classification test against both locally-pulled qwen2.5 variants:

  qwen2.5:3b-instruct-q4_K_M   ~190ms/call warm, 10/12 correct
  qwen2.5:7b-instruct-q4_K_M   ~510ms/call warm (one cold-load outlier
                                excluded), 11/12 correct -- the extra
                                correct case was exactly the ambient-
                                speech "Iron Man Jarvis" transcript
                                (7B said UNSURE, the safe answer per the
                                governing rule; 3B said CHAT, the unsafe
                                one -- see classifier.py and concierge.py
                                for how this gap gets covered elsewhere,
                                not by the model)

7B's ~510ms average is already over budget for classification alone
(spec: <150ms) before whisper/delivery/say latency is added on top, and
its one real cold-load in this test took ~3.6s -- reloading a 4.7GB model
mid-conversation would blow the entire fast path by itself. 3B stays
comfortably under budget everywhere and one warm model is simpler to keep
resident than two. If phrasing quality specifically (not classification)
ever needs the difference, revisit 7B for that call only -- not measured
as necessary yet.
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
MODEL = "qwen2.5:3b-instruct-q4_K_M"
# Re-sent on every call (not just once at startup) -- keeps the model
# resident between turns without a separate keep-warm daemon/thread.
KEEP_ALIVE = "30m"
REQUEST_TIMEOUT_S = 5.0

CLASSIFY_SYSTEM = """You classify a single spoken instruction to a voice assistant named Jarvis into exactly one category. Answer with ONLY the category word, nothing else.

CONTROL - a command about the conversation itself: cancel, never mind, stop, repeat that, say that again.
QUERY - asking about the state of a running task or session: what's running, what's X doing, how much have I spent, is it done yet.
CHAT - conversational, no action or data requested: greetings, small talk, opinions, thanks.
DISPATCH - an instruction to be carried out by an agent: tell X to do Y, have X run/fix/deploy/check something.
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
"""

PHRASE_SYSTEM_QUERY = """You are Jarvis, a voice assistant. Answer the user's question in ONE short spoken sentence (under 20 words), using ONLY the facts given below. Do not add any detail not present in the facts. If the facts say nothing relevant or state is unknown, say you don't have that information -- never guess.

Facts:
{facts}

User asked: "{text}"
Jarvis says:"""

PHRASE_SYSTEM_CHAT = """You are Jarvis, a voice assistant, replying to a casual remark (not a question about system state). Reply in ONE short, natural spoken sentence (under 15 words). Do not claim any information about running tasks, sessions, or costs -- you have none available. If the remark seems to need real information, say you're not sure rather than guessing.

User said: "{text}"
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


def phrase_answer(kind: str, text: str, facts: str = "") -> tuple[str, float]:
    """kind: "CHAT" or "QUERY". `facts` is the ONLY source of truth the
    model may draw on for a QUERY answer -- e.g. a provider function's
    literal output -- never invented. Ignored for CHAT (nothing to
    report; the model is explicitly told it has no state access)."""
    template = PHRASE_SYSTEM_QUERY if kind == "QUERY" else PHRASE_SYSTEM_CHAT
    prompt = template.format(facts=facts, text=text)
    response, elapsed_ms = _generate(prompt, num_predict=40, temperature=0.3)
    log_event("concierge_phrase_model_call", kind=kind, elapsed_ms=round(elapsed_ms, 1))
    return response, elapsed_ms
