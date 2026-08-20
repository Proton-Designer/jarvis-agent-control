#!/usr/bin/env python3
"""Canary for return-channel batching (SPEC-orchestration.md SS2.3).

Three agents finishing together used to be three interruptions. This
asserts they become one -- and, just as importantly, that the things
which must NOT be batched still are not.

BOTH DIRECTIONS ON EVERY PROPERTY. A test that only proves "completions
batch" would pass just as happily if EVERYTHING batched, including
refusals and errors -- and that is strictly worse than the bug it
replaced, because a delayed refusal is equivalent to a lost instruction
(say_feedback's own docstring). So every batching check has a
must-stay-immediate counterpart.

Runs with the flush worker DISABLED (JARVIS_NO_RETURN_QUEUE_WORKER=1)
and speak() patched -- flushes are driven explicitly here so timing is
asserted, not raced against.

    l4_controller/.venv/bin/python3 l4_controller/return_queue_canary.py
"""
from __future__ import annotations

import os
import sys
import time
import unittest.mock as mock
from pathlib import Path

os.environ["JARVIS_NO_RETURN_QUEUE_WORKER"] = "1"
os.environ.setdefault("JARVIS_TEST_RUN", "return-queue-canary")
sys.path.insert(0, str(Path(__file__).parent))

import return_queue as rq  # noqa: E402
import tools_voice  # noqa: E402

FAILURES: list[str] = []


def check(desc: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ok    {desc}")
    else:
        FAILURES.append(desc)
        print(f"  FAIL  {desc}{('  -- ' + detail) if detail else ''}")


def run() -> int:
    assert "test_runs" in str(rq.QUEUE_PATH), f"NOT ISOLATED: {rq.QUEUE_PATH}"
    rq._write([])

    print("batching -- three become one")
    rq.enqueue("completion", "Gateway finished its tests")
    rq.enqueue("completion", "Billing deployed")
    rq.enqueue("completion", "Mobile is done")
    with mock.patch("say_feedback.speak") as sp:
        res = rq.flush_now()
    check("three items flush as ONE speak() call, not three",
          sp.call_count == 1, f"speak called {sp.call_count}x")
    check("...and all three are in that one utterance",
          all(w in res["text"] for w in ("Gateway", "Billing", "Mobile")), res["text"])
    check("the queue is EMPTY afterwards -- content-keyed, not id()-keyed",
          rq.pending() == [], str(rq.pending()))
    with mock.patch("say_feedback.speak") as sp2:
        again = rq.flush_now()
    check("a second flush speaks NOTHING -- no repeat batch",
          sp2.call_count == 0 and again["spoken"] == 0)

    print()
    print("tier order -- a blocked question is never buried behind completions")
    rq._write([])
    rq.enqueue("completion", "Gateway finished")
    time.sleep(0.01)
    rq.enqueue("blocked_question", "The API session is asking about staging")
    time.sleep(0.01)
    rq.enqueue("completion", "Billing finished")
    order = [i["kind"] for i in rq.pending()]
    check("blocked_question sorts FIRST despite arriving second",
          order[0] == "blocked_question", str(order))
    with mock.patch("say_feedback.speak") as sp3:
        r = rq.flush_now()
    check("the spoken text leads with the blocked question",
          r["text"].startswith("The API session"), r["text"][:60])
    check("a batch containing a blocked question speaks at HIGH priority",
          sp3.call_args.kwargs.get("priority") == 0, str(sp3.call_args))
    rq._write([])
    rq.enqueue("completion", "only a completion")
    with mock.patch("say_feedback.speak") as sp4:
        rq.flush_now()
    check("...and a completions-only batch does NOT claim high priority",
          sp4.call_args.kwargs.get("priority") == 1, str(sp4.call_args))

    print()
    print("the other direction -- what must NEVER be batched")
    check("enqueue REFUSES 'error' rather than accepting it",
          rq.enqueue("error", "something broke")["ok"] is False)
    check("'error' is not in BATCHABLE_KINDS at all",
          "error" not in rq.BATCHABLE_KINDS, str(sorted(rq.BATCHABLE_KINDS)))
    # Patch tools_voice.speak, NOT say_feedback.speak. tools_voice does
    # `from say_feedback import speak` at import, so it holds its own
    # reference and patching the source module leaves it untouched.
    # Found here: the error check reported speak=0 and looked like the
    # error had VANISHED, when in fact the real speak() was called and
    # this file was watching a function nobody calls. A canary measuring
    # the wrong object reports a catastrophe that isn't happening -- and
    # would equally miss one that is.
    rq._write([])
    with mock.patch("tools_voice.speak") as sp5:
        tools_voice.jarvis_say("the deploy failed", kind="error")
    check("jarvis_say(kind='error') speaks IMMEDIATELY, bypassing the queue",
          sp5.call_count == 1 and rq.pending() == [],
          f"speak={sp5.call_count} pending={rq.pending()}")
    rq._write([])
    with mock.patch("tools_voice.speak") as sp6:
        tools_voice.jarvis_say("gateway finished", kind="completion")
    check("jarvis_say(kind='completion') QUEUES instead of speaking now",
          sp6.call_count == 0 and len(rq.pending()) == 1,
          f"speak={sp6.call_count} pending={len(rq.pending())}")

    print()
    print("flush gating -- and failing toward speech, never toward silence")
    rq._write([])
    rq.enqueue("completion", "just arrived")
    ready, why = rq.ready_to_flush()
    check("a just-arrived item is NOT flushed yet (settle delay is what makes it a batch)",
          ready is False and why == "collecting", f"{ready} {why!r}")
    ready2, _ = rq.ready_to_flush(now=time.time() + rq.SETTLE_S + 1)
    check("...and IS flushed once the settle delay has passed", ready2 is True)
    with mock.patch("dispatch_state.any_forwarded", return_value=True):
        ready3, why3 = rq.ready_to_flush(now=time.time() + rq.SETTLE_S + 1)
    check("a dictation in flight holds the batch -- never interrupt Ayman mid-sentence",
          ready3 is False and "dictation" in why3, f"{ready3} {why3!r}")
    with mock.patch("dispatch_state.any_forwarded", side_effect=OSError("unreadable")):
        ready4, _ = rq.ready_to_flush(now=time.time() + rq.SETTLE_S + 1)
    check("an UNREADABLE dispatch state fails toward SPEAKING, not toward silence",
          ready4 is True)
    rq._write([])
    check("an empty queue is never 'ready' (nothing to say is not a reason to speak)",
          rq.ready_to_flush()[0] is False)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("all 17 checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(run())
