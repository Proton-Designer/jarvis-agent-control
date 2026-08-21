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

Also covers R4 (docs/PLAN-silence-and-ux.md SS1, Opus Lead 3's finding):
the CAPTURING gate must distinguish a FRESH wake_state.json write from a
STALE one (a daemon that crashed mid-capture), never trust `state` alone.
R3 (cross-process flush locking) is NOT covered here -- that needs real
separate OS processes to mean anything, see return_queue_race_canary.py.

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
    print("R2 -- a reply is not a report (docs/PLAN-silence-and-ux.md SS1)")
    # The bug this closes: the concierge had no way to say "I am
    # answering the question you just asked", so a live reply went out as
    # `completion` and inherited a policy written for asynchronous agent
    # news -- held SETTLE_S, and eligible to merge into one blob with an
    # unrelated completion. Measured consequence: reply latency crossed
    # instant_ack's 3.0s fallback, so Ayman heard "Okay, one sec" before
    # nearly every answer (Engineer 1, 2026-08-21: 18/19 conversational
    # replies typed `completion`, ack fired 7/8 times).
    check("'answer' is an accepted kind at all",
          "answer" in tools_voice.KINDS, str(sorted(tools_voice.KINDS)))
    check("'answer' is NOT batchable",
          "answer" not in rq.BATCHABLE_KINDS, str(sorted(rq.BATCHABLE_KINDS)))
    check("enqueue REFUSES 'answer' rather than accepting it",
          rq.enqueue("answer", "doing well, ready to help")["ok"] is False)
    rq._write([])
    with mock.patch("tools_voice.speak") as sp7:
        tools_voice.jarvis_say("doing well, ready to help", kind="answer")
    check("jarvis_say(kind='answer') speaks IMMEDIATELY, bypassing the queue",
          sp7.call_count == 1 and rq.pending() == [],
          f"speak={sp7.call_count} pending={rq.pending()}")
    check("...at HIGH priority -- he is waiting on it, it outranks queued news",
          sp7.call_args.kwargs.get("priority") == 0, str(sp7.call_args))

    # Both directions on the property that actually bit: an answer must
    # not be absorbed into a batch that is already waiting. A test that
    # only checked an answer on an empty queue would pass even if the
    # answer were appended to a pending completion and spoken as one
    # blob -- which is precisely the "Still good. Good. I'm here."
    # failure in the log.
    rq._write([])
    rq.enqueue("completion", "Gateway finished its tests")
    with mock.patch("tools_voice.speak") as sp8:
        tools_voice.jarvis_say("five sessions running", kind="answer")
    check("an answer overtakes a WAITING completion instead of joining it",
          sp8.call_count == 1
          and sp8.call_args.args[0] == "five sessions running"
          and len(rq.pending()) == 1
          and rq.pending()[0]["kind"] == "completion",
          f"speak={sp8.call_count} args={sp8.call_args} pending={rq.pending()}")
    check("...and the completion is still queued, not lost to the answer",
          "Gateway finished its tests" in rq.pending()[0]["text"],
          str(rq.pending()))

    print()
    print("flush gating -- and failing toward speech, never toward silence")
    rq._write([])
    rq.enqueue("completion", "just arrived")
    ready, why = rq.ready_to_flush()
    check("a just-arrived item is NOT flushed yet (settle delay is what makes it a batch)",
          ready is False and why == "collecting", f"{ready} {why!r}")
    ready2, _ = rq.ready_to_flush(now=time.time() + rq.SETTLE_S + 1)
    check("...and IS flushed once the settle delay has passed", ready2 is True)
    # The gate is the wake daemon's OWN state, not dispatch_state. That
    # was the original gate and it swallowed a real reply: the concierge
    # answered Ayman and any_forwarded() stayed True forever, because a
    # conversational answer has no dispatch to complete. "He is still
    # talking" became "never speak again".
    import json  # noqa: E402
    from jarvis_paths import jarvis_home  # noqa: E402
    wake_path = jarvis_home() / "wake_state.json"
    wake_path.parent.mkdir(parents=True, exist_ok=True)

    # updated_at IS the point here -- a FRESH write, matching what a
    # real, live daemon actually writes (~10Hz). Without it, the gate
    # below (docs/PLAN-silence-and-ux.md SS1 R4) would already, correctly,
    # treat this as stale and fail toward speaking -- which would make
    # THIS check (mid-sentence holds the batch) pass for the wrong
    # reason, or fail outright once R4 landed. Precise freshness is the
    # setup this check actually needs.
    wake_path.write_text(json.dumps({"state": "CAPTURING", "updated_at": time.time()}))
    ready3, why3 = rq.ready_to_flush(now=time.time() + rq.SETTLE_S + 1)
    check("mid-sentence (FRESH write) holds the batch -- never interrupt him while he is talking",
          ready3 is False and "talking" in why3, f"{ready3} {why3!r}")

    wake_path.write_text(json.dumps({"state": "IDLE"}))
    ready4, _ = rq.ready_to_flush(now=time.time() + rq.SETTLE_S + 1)
    check("...and once he stops, it speaks", ready4 is True)

    wake_path.unlink(missing_ok=True)
    ready5, _ = rq.ready_to_flush(now=time.time() + rq.SETTLE_S + 1)
    check("no daemon at all fails toward SPEAKING -- nothing to interrupt",
          ready5 is True)

    print()
    print("R4 -- a STALE CAPTURING (crashed mid-capture) must not gate speech forever")
    # docs/PLAN-silence-and-ux.md SS1 R4 (Opus Lead 3's finding): a daemon
    # that dies mid-capture leaves "state": "CAPTURING" on disk permanently.
    # Without a staleness check this reads as "he is still talking" forever
    # -- indistinguishable from check R4-fresh above, which is exactly the
    # bug: the ONLY thing that should tell these two apart is updated_at.
    rq._write([])
    rq.enqueue("completion", "test item behind a stale CAPTURING flag")
    stale_at = time.time() - rq.WAKE_STATE_STALE_AFTER_S - 5.0  # comfortably past the threshold
    wake_path.write_text(json.dumps({"state": "CAPTURING", "updated_at": stale_at}))
    ready_stale, why_stale = rq.ready_to_flush(now=time.time() + rq.SETTLE_S + 1)
    check("a STALE CAPTURING write does NOT hold the batch -- fails toward speaking",
          ready_stale is True, f"{ready_stale} {why_stale!r}")

    # The negative control, same data shape, only updated_at differs --
    # proves the fresh case above wasn't passing by some other accident
    # (e.g. missing-file fallthrough) once this file also exercises stale.
    rq._write([])
    rq.enqueue("completion", "test item behind a fresh CAPTURING flag")
    wake_path.write_text(json.dumps({"state": "CAPTURING", "updated_at": time.time()}))
    ready_fresh, why_fresh = rq.ready_to_flush(now=time.time() + rq.SETTLE_S + 1)
    check("...while a FRESH CAPTURING write (same run) still correctly holds it",
          ready_fresh is False and "talking" in why_fresh, f"{ready_fresh} {why_fresh!r}")

    # Malformed updated_at (not a number) must fail toward speaking too,
    # same as a missing file -- never let a parse error read as "trust
    # CAPTURING forever."
    rq._write([])
    rq.enqueue("completion", "test item behind a malformed updated_at")
    wake_path.write_text(json.dumps({"state": "CAPTURING", "updated_at": "not-a-number"}))
    ready_bad, _ = rq.ready_to_flush(now=time.time() + rq.SETTLE_S + 1)
    check("a malformed updated_at fails toward SPEAKING, not toward trusting CAPTURING",
          ready_bad is True)
    wake_path.unlink(missing_ok=True)

    # THE BACKSTOP, and it is the assertion that matters most here: the
    # bug was not that one gate was wrong, it was that a stuck gate could
    # hold a message forever with every layer reporting success. Nothing
    # may outrank this. Explicitly self-contained (its own enqueue, not
    # relying on an item left over from an earlier check) -- the R4
    # checks above clear the queue several times, and a test that depends
    # on state left behind by unrelated earlier checks is exactly the
    # kind of fragile ordering this file should not have.
    rq._write([])
    rq.enqueue("completion", "test item for the MAX_HOLD_S backstop")
    wake_path.write_text(json.dumps({"state": "CAPTURING"}))
    ready6, _ = rq.ready_to_flush(now=time.time() + rq.MAX_HOLD_S + 1)
    check("past MAX_HOLD_S it speaks even mid-sentence -- no gate may hold a "
          "message indefinitely, because that is how a reply disappears silently",
          ready6 is True)
    wake_path.unlink(missing_ok=True)
    rq._write([])
    check("an empty queue is never 'ready' (nothing to say is not a reason to speak)",
          rq.ready_to_flush()[0] is False)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(run())
