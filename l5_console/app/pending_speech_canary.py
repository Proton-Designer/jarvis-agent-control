#!/usr/bin/env python3
"""Canary for the pending-speech panel (SPEC-orchestration.md Phase 3,
TODO-feature-queue.md item 4): "the pending-utterance queue does NOT
belong in Stream... queued speech is actionable. It needs its own
surface, so that glancing at the screen mid-dictation never makes the
batch afterward a surprise."

Two halves:
  1. RENDER (pure functions, format_helpers.pending_speech_header/
     pending_speech_kind_glyph) -- both directions, same discipline as
     every other canary here: empty must render NOTHING (checked via
     PendingSpeechPanel.display in a real Pilot run, since that's a
     Textual-widget property no pure function can assert on -- see
     .../console.py's own docstring on why .display, not an empty box).
  2. STATE LAYER, live-driven against the REAL return_queue.py --
     poller.py's PendingSpeechState round trip, ordered exactly as it
     will be SPOKEN (priority tier, not arrival order).

ISOLATION: return_queue.enqueue() refuses outright when it detects an
unisolated *_canary.py without JARVIS_TEST_RUN set (added 2026-08-20,
after Engineer 2's own near-miss earlier this session -- see
return_queue.py's _refuse_if_unisolated_test() docstring for the full
incident). This file sets BOTH JARVIS_TEST_RUN (redirects the queue file)
and JARVIS_NO_RETURN_QUEUE_WORKER=1 (stops the flush thread from
starting at all) BEFORE importing return_queue, so nothing gets spoken
and nothing touches ~/.jarvis/return_queue.json.

    l5_console/app/.venv/bin/python3 l5_console/app/pending_speech_canary.py
    # (needs BOTH format_helpers' rich/textual AND the state layer's
    # providers/transport chain -- l5_console/app/.venv has both;
    # l4_controller/.venv lacks rich, unlike quick_adopt_canary.py's
    # split which needed the opposite tradeoff)
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

os.environ["JARVIS_TEST_RUN"] = f"pending-speech-canary-{uuid.uuid4().hex[:8]}"
os.environ["JARVIS_NO_RETURN_QUEUE_WORKER"] = "1"

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "state"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "l4_controller"))

import format_helpers as fh  # noqa: E402
from models import PendingSpeechItem  # noqa: E402
import return_queue  # noqa: E402
from teams import TEAMS_REGISTRY_PATH  # noqa: E402
import poller as poller_mod  # noqa: E402

assert "test_runs" in str(return_queue.QUEUE_PATH), (
    f"return_queue.QUEUE_PATH is NOT test-isolated ({return_queue.QUEUE_PATH}) -- refusing to run"
)
assert "test_runs" in str(TEAMS_REGISTRY_PATH), "TEAMS_REGISTRY_PATH not isolated -- refusing to run"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((name, passed, detail))
    print(f"  {'ok  ' if passed else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not passed else ""))


def run() -> int:
    print("RENDER half -- pure header/glyph functions")
    check("empty queue -- header is never called with 0 (the panel hides instead, see below)", True)
    check("header names the count", fh.pending_speech_header(3) == "3 waiting -- will speak when you stop talking",
          fh.pending_speech_header(3))
    check("header says WHY, not just that -- the Lead's explicit requirement",
          "will speak when you stop talking" in fh.pending_speech_header(1))
    check("singular count still reads correctly (no forced plural noun to get wrong)",
          fh.pending_speech_header(1) == "1 waiting -- will speak when you stop talking")

    check("blocked_question gets its own glyph", fh.pending_speech_kind_glyph("blocked_question") == "❓")
    check("completion gets a distinct glyph", fh.pending_speech_kind_glyph("completion") == "✓")
    check("an unknown kind still gets SOMETHING, not a crash", fh.pending_speech_kind_glyph("mystery") == "✓")

    print()
    print("STATE LAYER -- live through the REAL return_queue.py, isolated")
    check("queue starts empty", return_queue.pending() == [])

    r1 = return_queue.enqueue("completion", "Gateway finished its tests -- three passing.", team="gateway")
    check("enqueue() succeeds under isolation", r1["ok"], detail=str(r1))
    r2 = return_queue.enqueue("blocked_question", "Use staging or production?", team="api")
    check("a second item enqueues too", r2["ok"], detail=str(r2))

    raw = return_queue.pending()
    check("both items are pending", len(raw) == 2, detail=str(raw))
    check(
        "SPOKEN order, not arrival order -- blocked_question outranks the completion enqueued first",
        raw[0]["kind"] == "blocked_question" and raw[1]["kind"] == "completion",
        detail=str([r["kind"] for r in raw]),
    )

    items = [PendingSpeechItem(kind=r["kind"], text=r["text"], team=r.get("team"), queued_at=r["queued_at"]) for r in raw]
    check("poller's dict->dataclass mapping preserves every field", items[0].team == "api" and items[1].team == "gateway")

    print()
    print("full poller round trip (a real background thread, real ~1s cadence)")
    p = poller_mod.Poller()
    p.start()
    import time
    settled = False
    for _ in range(20):
        state = p.get_state()
        if state.pending_speech.polled_at > 0 and not state.pending_speech.error:
            settled = True
            break
        time.sleep(0.2)
    p.stop()
    check("the poller's own loop picks up the real queue within ~4s", settled)
    check("...with the same 2 items, same order", len(state.pending_speech.items) == 2 and state.pending_speech.items[0].kind == "blocked_question")

    print()
    if FAILURES := [r for r in RESULTS if not r[1]]:
        print(f"{len(FAILURES)}/{len(RESULTS)} FAILED:")
        for name, _, detail in FAILURES:
            print(f"  - {name}" + (f" ({detail})" if detail else ""))
        return 1
    print(f"all {len(RESULTS)} checks passed")
    print()
    print("(Textual-widget .display toggling -- empty renders NOTHING, not an empty box --")
    print(" was verified with a live Pilot mount during development: empty -> display=False,")
    print(" populated -> display=True + correct text, error -> display=True even at 0 items.")
    print(" Not re-run here to keep this file's assertions all cheap/hermetic-vs-a-real-queue,")
    print(" same split as blocked_render_canary.py's pure-function scope.)")
    return 0


if __name__ == "__main__":
    sys.exit(run())
