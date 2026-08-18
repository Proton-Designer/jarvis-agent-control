#!/usr/bin/env python3
"""Canary for intent classification. Companion to chat_gate_canary.py
(which guards whether Jarvis SPEAKS); this one guards where an utterance
GOES.

Exists because of a live finding, 2026-08-18. Ayman asked "What sessions
are running right now? Tell me" and it classified DISPATCH instead of
QUERY, which read as the model being too weak to connect two phrasings of
the same question -- and reasonably raised "then what is this model even
for?"

It wasn't a capability failure. A 16-paraphrase sweep scored 13/16, and
all three failures contained the same two words: "tell me". The cause was
in CLASSIFY_SYSTEM, which taught the model that "tell" means DISPATCH
("tell X to do Y") AND that an instruction tacked onto the end overrides
a question. The model applied both rules exactly as written. "Tell me"
(the user asking Jarvis) versus "tell gateway" (an instruction for an
agent) was a distinction the prompt never drew. After drawing it: 23/23.

The lesson worth keeping: when this model looks stupid, check the prompt
first. Its real failure modes are arithmetic and multi-step reasoning
(see the measurements in the Lead's notes) -- NOT paraphrase, which it
handles well. A paraphrase miss is nearly always a prompt bug wearing a
capability bug's clothes.

Both directions are asserted, and that matters more than the QUERY rows
on their own: it would be trivial to fix "tell me" by teaching the model
that "tell" is never dispatch, and that would silently break every real
instruction. The DISPATCH block is what makes the QUERY block safe to
change.

Needs Ollama. Speaks nothing, touches no tmux session.

    python3 l2_5_concierge/classify_canary.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ["JARVIS_MUTE"] = "1"
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "l4_controller"))

from classifier import classify  # noqa: E402

# Must reach QUERY: asking about state, directing nothing at any agent.
QUERY_CASES = [
    "what's running right now",
    "what all sessions are running right now",      # Ayman's phrasing
    "what sessions are running right now",
    "what sessions are running right now? tell me",  # the live failure
    "tell me what's running",                        # "tell me" leading
    "can you tell me which sessions are active",     # "can you" + "tell me"
    "show me what's running",
    "give me a list of running sessions",
    "which sessions are up",
    "how many sessions do i have running",
    "what agents are alive at the moment",
    "list the running sessions",
    "who's running right now",
    "what do i have going on right now",
    "anything running?",
    "what's currently active",
    "tell me how much i've spent",
    "show me what the gateway is doing",
]

# Must reach DISPATCH (UNSURE also acceptable -- it forwards too, which
# is the safe direction; CHAT/QUERY/CONTROL are real failures).
DISPATCH_CASES = [
    "tell the gateway to restart",
    "have billing run the payout job",
    "tell gateway to run the tests and let me know when it's done",
    "get shipcheck to pull the latest",
    "what's the gateway doing, oh and also have it restart",
    "go ahead and redeploy the api",
]


def run() -> int:
    failures = []
    print("intent-classification canary\n")

    for text in QUERY_CASES:
        got = classify(text).label
        ok = got == "QUERY"
        print(f"  {'ok  ' if ok else 'FAIL'}  {got:8}  {text!r}")
        if not ok:
            failures.append((text, "QUERY", got))

    print()
    for text in DISPATCH_CASES:
        got = classify(text).label
        ok = got in ("DISPATCH", "UNSURE")
        print(f"  {'ok  ' if ok else 'FAIL'}  {got:8}  {text!r}")
        if not ok:
            failures.append((text, "DISPATCH-or-UNSURE", got))

    total = len(QUERY_CASES) + len(DISPATCH_CASES)
    print(f"\n{total - len(failures)}/{total} correct")
    if failures:
        for text, want, got in failures:
            print(f"  wanted {want}, got {got}: {text!r}")
        if any(w.startswith("DISPATCH") for _, w, _ in failures):
            print("\n  *** A DISPATCH row failed -- a real instruction would be")
            print("  *** answered conversationally instead of reaching an agent.")
            print("  *** That is silent loss. Fix before shipping.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run())
