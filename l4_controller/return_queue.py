"""
The return channel's batching queue (SPEC-orchestration.md §2.3).

Three agents finishing within a few seconds of each other is three
interruptions today. This collects them and speaks once.

WHY THIS IS NOT INSIDE say_feedback.speak()
-------------------------------------------
speak() has twelve callers, and they split into two groups that must not
be treated alike:

  IMMEDIATE  refusals, the instant ack, cancel-window confirmations.
             Delaying any of these breaks the exact property it exists
             for -- say_feedback's own docstring already establishes that
             a delayed refusal is equivalent to a lost instruction.

  BATCHABLE  return traffic FROM agents: completions and blocked-question
             escalations. These are the ones that pile up.

Batching inside speak() would hand all twelve callers a delay they never
asked for, and force the immediate group to opt out one at a time. That
is a convention, and conventions are what keep failing in this codebase.
So batching lives here, at the return channel, and speak() is untouched.

jarvis_say already carries typed kinds and REFUSES an unknown one rather
than downgrading it. That type is the batching key -- which is what the
typing was for. `error` is not batchable and never enters this queue.

FLUSH POLICY
------------
Collected, then spoken when BOTH hold:
  - he is not mid-sentence (the wake daemon's own state is not
    CAPTURING), so a completion never interrupts him; and
  - SETTLE_S has passed since the last arrival, so a burst of three
    agents finishing together becomes one utterance rather than three.

The settle delay is what turns "collect" into "batch". Without it the
first completion would flush alone and the other two would still arrive
separately -- the exact behaviour this module exists to remove.

Tier order, not arrival order: blocked questions outrank completions.
A stopped session needs Ayman more than a finished one does, and
speaking them in arrival order would bury the blocker behind two
"finished" messages.

DRIVEN BY ITS OWN THREAD, NOT THE CONSOLE
-----------------------------------------
The flush loop runs here, in a daemon thread, exactly like
say_feedback's worker. It deliberately does NOT depend on the console
poller: the console is an observer and may not be running, and speech
that only works while a UI happens to be open is not a return channel.

PERSISTED, because the queue is answerable state. "What did I miss"
needs the items to exist after a restart, and an unflushed batch that
evaporates on crash is a silently lost message -- the failure class this
project keeps finding.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import threading
import time
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).parent.parent))
from jarvis_paths import jarvis_home  # noqa: E402
from latency_log import log_event  # noqa: E402

QUEUE_PATH = jarvis_home() / "return_queue.json"
FLUSH_LOCK_PATH = jarvis_home() / "return_queue.lock"

# Long enough that a burst of agents finishing together lands in one
# utterance; short enough that a single completion doesn't feel delayed.
SETTLE_S = 2.5
_TICK_S = 0.5

# HARD BACKSTOP. Nothing may be held longer than this, for any reason.
#
# Added 2026-08-20 after the queue swallowed a real reply. The concierge
# answered Ayman's "hello, how are you doing" with "Good. Ready for
# whatever you need." -- and it sat unspoken, because the flush gate
# asked dispatch_state.any_forwarded() and that stayed True forever. A
# conversational answer has no dispatch lifecycle to complete, so the
# condition that was supposed to mean "he is still talking" became
# "never speak again".
#
# The gate itself is fixed below, but the gate was not the real problem.
# ANY hold condition that can get stuck turns this queue into a place
# messages go to disappear, silently, with every layer reporting
# success. So the backstop is unconditional: past this age it speaks,
# whatever the gate thinks. Being interrupted is recoverable; never
# hearing the answer is not.
MAX_HOLD_S = 12.0

# Same staleness discipline as l5_console/app/wake_state.py's
# read_wake_state() (STALE_AFTER_S=1.0 there) -- daemon.py writes
# wake_state.json at ~10Hz while live(), so missing more than a second
# of writes means the writer is gone, not just between updates. Reused
# as a literal constant here rather than importing that module: this is
# an L4 file and wake_state.py lives under l5_console/app -- the console
# layer -- and this queue must keep working with the console closed, so
# it must not depend on anything under l5_console/app at all. Same
# convention, applied independently, not a shared import.
#
# WHY THIS MATTERS (found live, 2026-08-20, Opus Lead 3): the CAPTURING
# check below used to trust wake_state.json's `state` field with no
# regard for `updated_at` at all. A daemon that crashes mid-capture
# leaves "state": "CAPTURING" on disk FOREVER -- every batchable message
# after that reads "he is still talking" permanently, and MAX_HOLD_S
# becomes not a backstop but an unconditional 12s tax on every message,
# forever, with nothing on screen explaining why.
WAKE_STATE_STALE_AFTER_S = 1.0

# Speech order. Lower speaks first. Mirrors say_feedback's priority
# meaning so the two orderings can never disagree about what outranks
# what.
_TIER = {"blocked_question": 0, "completion": 1}

BATCHABLE_KINDS = frozenset(_TIER)

_lock = threading.RLock()
_worker_started = False


@contextlib.contextmanager
def _cross_process_lock():
    """docs/PLAN-silence-and-ux.md SS1 R3: `_lock` above is a
    threading.RLock -- it only ever serializes threads INSIDE one
    process. jarvis_say is registered on BOTH MCP surfaces
    (server.py and server_readonly.py), so the concierge and the router
    are two SEPARATE processes, each perfectly capable of running its
    own _worker_loop() thread with its own, unrelated in-memory _lock --
    each one correctly believing it is the only flusher, while
    flush_now()'s read -> speak -> re-read -> write is not atomic across
    two such processes. Two workers ticking at once can both read the
    same pending items, both speak them (duplicate audio), and then race
    on whose write of the remainder actually lands.

    fcntl.flock() is a real OS-level, cross-process advisory lock
    (unlike threading.Lock, which cannot see across a process boundary
    at all) on FLUSH_LOCK_PATH. BLOCKING (no LOCK_NB): every operation
    guarded by this lock (one enqueue, one check-and-maybe-flush tick)
    is a brief, bounded file read/write plus enqueueing onto
    say_feedback's OWN queue -- never the actual multi-second spoken
    playback, since say_feedback.speak() itself is non-blocking (it only
    enqueues; the real audio happens later, asynchronously, on
    say_feedback's own worker thread). So blocking briefly here is cheap
    and never risks holding this lock for as long as an utterance takes
    to actually play. A crashed process cannot wedge this lock forever
    either -- an OS-level flock is tied to the open file descriptor and
    is released automatically by the kernel the moment that process dies
    or the fd closes, `with` block or not.

    THIS FIXES THE CONCURRENCY BUG, NOT STALENESS. A lock guarantees at
    most one process acts on the queue at a time; it says nothing about
    whether that process is running current code. That is a different
    problem (see docs/PLAN-silence-and-ux.md SS4's standing rule) with a
    different fix: R5 surfaces "this role is older than your code" so
    Ayman knows to Activate it, which already respawns the process
    cleanly. A dedicated always-on flusher process was considered and
    rejected here -- it would only relocate the exact same staleness
    risk to a new, equally-restartable component, not remove it; the
    only real fix for a process running stale code is restarting that
    process, which is what R5 makes visible and Activate already does."""
    FLUSH_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(FLUSH_LOCK_PATH, "a") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _read() -> list[dict]:
    try:
        with QUEUE_PATH.open() as f:
            items = json.load(f)
        return items if isinstance(items, list) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def _write(items: list[dict]) -> None:
    # The guard belongs HERE, not only in enqueue(). Found by a sweep
    # 2026-08-20: the real ~/.jarvis/return_queue.json reappeared
    # containing "[]", written by a canary calling _write([]) to reset
    # itself without isolation. enqueue() was refused correctly; the
    # reset path went straight through, because I had protected the
    # function I was thinking about rather than the operation that
    # actually touches the file.
    #
    # Harmless this time -- an empty list is not a message that gets
    # spoken. But "the guard covers one of two writers" is the same
    # incompleteness as the denylist that missed /bin/rm, and it is only
    # harmless by accident: _write() is also how flush_now() persists the
    # REMAINDER after speaking, so an unisolated test hitting that path
    # could discard real queued messages.
    _refuse_if_unisolated_test()
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = QUEUE_PATH.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        json.dump(items, f, indent=2)
    tmp.replace(QUEUE_PATH)


def _sorted(items: list[dict]) -> list[dict]:
    """Tier first, then arrival. This is the SPOKEN order, and pending()
    returns it too -- the panel must show what will actually happen, not
    the order things arrived, or glancing at it would mislead."""
    return sorted(items, key=lambda i: (_TIER.get(i.get("kind"), 99), i.get("queued_at", 0)))


# The only processes that legitimately write the REAL queue. Everything
# else must isolate itself with JARVIS_TEST_RUN.
_PRODUCTION_ENTRYPOINTS = frozenset({"server.py", "server_readonly.py", "daemon.py"})


def _refuse_if_unisolated_test() -> None:
    """Refuse to touch the REAL queue from anything that isn't a
    production entry point.

    An ALLOWLIST, not a heuristic, and it got here in two steps worth
    recording because the first step was wrong.

    Engineer 2 smoke-tested enqueue() without JARVIS_TEST_RUN and wrote
    two fake items into the live file. They caught it in a minute and
    nothing was spoken -- but only because their process exited before
    the 2.5s settle. enqueue() starts the flush worker, so a test living
    a few seconds longer would have had Kokoro read fake items aloud.

    My first guard refused when JARVIS_MUTE was set or argv[0] ended in
    "_canary.py". That is a denylist of test SHAPES, and it missed the
    very next case: a `python3 -c` smoke test is neither, so it wrote the
    real file anyway -- found in the same sweep that added the guard.

    Denylisting the shapes tests happen to take can only ever enumerate
    the ones already seen. The set of PRODUCTION writers, by contrast, is
    small, known, and changes when someone deliberately changes it. Same
    reasoning as _SAFE_SESSION_NAME in terminal_window.py: everything
    real fits the allowlist, so anything that does not is either a
    mistake or hostile, and refusing is right in both cases.

    Escape hatch is explicit and loud (JARVIS_ALLOW_REAL_QUEUE=1) rather
    than implicit, so using it is a decision someone typed."""
    if "test_runs" in str(QUEUE_PATH):
        return  # isolated -- nothing to protect
    if os.environ.get("JARVIS_ALLOW_REAL_QUEUE") == "1":
        return
    entry = Path(sys.argv[0]).name
    if entry in _PRODUCTION_ENTRYPOINTS:
        return
    raise RuntimeError(
        f"return_queue: refusing to write the REAL queue from {entry!r}. "
        "Only the MCP servers and the wake daemon may. Set "
        "JARVIS_TEST_RUN=<name> to isolate, plus "
        "JARVIS_NO_RETURN_QUEUE_WORKER=1 to stop the flush thread -- "
        "otherwise a forgotten test item gets SPOKEN aloud."
    )


def _key(item: dict) -> tuple:
    """Stable identity for one queued item, across separate file reads.
    Content-based deliberately -- see flush_now()."""
    return (item.get("kind"), item.get("text"), item.get("queued_at"))


def enqueue(kind: str, text: str, team: str | None = None) -> dict:
    """Adds one batchable item. Refuses a non-batchable kind rather than
    accepting it -- `error` must reach Ayman immediately and silently
    queueing it would be the same class of bug as downgrading an unknown
    kind, which jarvis_say already refuses to do."""
    _refuse_if_unisolated_test()
    if kind not in BATCHABLE_KINDS:
        return {"ok": False, "reason": f"{kind!r} is not batchable; speak it immediately"}
    text = (text or "").strip()
    if not text:
        return {"ok": False, "reason": "empty text"}
    # BOTH locks: _lock (intra-process -- a second thread in THIS same
    # process, e.g. the worker thread racing a direct enqueue() call) AND
    # _cross_process_lock() (a second PROCESS -- the concierge and the
    # router each have their own copy of this module loaded, see that
    # lock's own docstring). Neither alone is sufficient; they guard
    # different boundaries and compose safely (distinct resources, same
    # thread holds both throughout).
    with _lock, _cross_process_lock():
        items = _read()
        items.append({"kind": kind, "text": text, "team": team, "queued_at": time.time()})
        _write(items)
    log_event("return_queue_enqueued", kind=kind, team=(team or "")[:60], depth=len(items))
    _ensure_worker()
    return {"ok": True, "reason": ""}


def pending() -> list[dict]:
    """Everything waiting, in the order it will be SPOKEN. Cheap enough
    to call on every console tick -- one small file read."""
    with _lock:
        return _sorted(_read())


def compose(items: list[dict]) -> str:
    """One utterance from many items.

    Joined into a single spoken block rather than several queued strings:
    the point of batching is ONE interruption, and three items handed to
    speak() separately would still be three, just closer together.

    Each item already names its own subject ("Gateway finished its
    tests") per SPEC-orchestration §2.2's identification-by-grammar rule,
    so no prefix is added here -- adding one would turn a single voice
    into a phone tree, which that rule exists to prevent."""
    parts = []
    for it in _sorted(items):
        t = it["text"].strip()
        parts.append(t if t.endswith((".", "!", "?")) else t + ".")
    return " ".join(parts)


def flush_now() -> dict:
    """Speaks everything waiting, as one utterance, and clears the queue.

    Clears only what it actually took, not the file wholesale: an item
    enqueued between the read and the write would otherwise be dropped
    without ever being spoken."""
    from say_feedback import speak, PRIORITY_HIGH, PRIORITY_NORMAL

    # docs/PLAN-silence-and-ux.md SS1 R3: BOTH locks, same reasoning as
    # enqueue(). This is the span that used to race across processes --
    # two workers (concierge's and the router's, each in its own process)
    # could both read the same pending items here, both speak them, and
    # then race on whose write of the remainder actually landed. The
    # cross-process lock makes this whole read -> take -> write span
    # atomic with respect to every OTHER process's flush_now() too, not
    # just this one's own thread.
    with _lock, _cross_process_lock():
        items = _sorted(_read())
        if not items:
            return {"ok": True, "spoken": 0, "text": ""}
        # Keyed on CONTENT, not id(). My first version used id(), which
        # cannot work: _read() re-parses the file and returns fresh dict
        # objects every call, so no identity ever matches, nothing would
        # be removed, and the queue would speak the same batch forever.
        # Caught before it shipped only because the failure is loud;
        # written down because the next person reaching for id() across
        # two reads deserves the warning.
        #
        # Still a re-read rather than writing [] wholesale: an item
        # enqueued between the read above and this write would otherwise
        # be dropped without ever being spoken.
        taken = {_key(i) for i in items}
        remaining = [i for i in _read() if _key(i) not in taken]
        _write(remaining)

    text = compose(items)
    # The batch speaks at its HIGHEST tier, not an average: if a blocked
    # question is in there, the whole utterance is urgent, because the
    # blocked question is.
    priority = PRIORITY_HIGH if any(i["kind"] == "blocked_question" for i in items) else PRIORITY_NORMAL
    speak(text, priority=priority)
    log_event("return_queue_flushed", count=len(items), chars=len(text), priority=priority)
    return {"ok": True, "spoken": len(items), "text": text}


def ready_to_flush(now: float | None = None) -> tuple[bool, str]:
    """(ready, why_not). Returned as a reason rather than a bare bool so
    the console can say WHY something is waiting -- "will speak when you
    stop talking" is actionable where "3 pending" is not."""
    now = now if now is not None else time.time()
    items = pending()
    if not items:
        return False, "nothing queued"

    # Backstop first, so no gate below can override it.
    oldest = min(i.get("queued_at", 0) for i in items)
    if now - oldest >= MAX_HOLD_S:
        return True, ""

    # "Is he MID-SENTENCE right now", read from the wake daemon's own
    # state file -- not dispatch_state.any_forwarded(), which was the
    # original gate and was wrong. any_forwarded() means "a dispatch was
    # forwarded and has not been marked complete", which is a different
    # question and stays True indefinitely for a conversational reply
    # that has no dispatch to complete. It read as "he is still talking"
    # forever, and swallowed a real answer.
    try:
        with (jarvis_home() / "wake_state.json").open() as f:
            wake_data = json.load(f)
        if wake_data.get("state") == "CAPTURING":
            # STALENESS, not just presence -- found live 2026-08-20 (Opus
            # Lead 3): without this check, a daemon that crashes mid-
            # capture leaves "state": "CAPTURING" on disk forever, and
            # this gate would then read "he is still talking" permanently
            # -- every batchable message pays the full MAX_HOLD_S, always,
            # with nothing on screen explaining why. daemon.py writes
            # this file at ~10Hz while live() runs (WAKE_STATE_WRITE_
            # INTERVAL_S=0.1), so a write more than WAKE_STATE_STALE_
            # AFTER_S old means the writer is gone, not just between
            # ticks -- same convention l5_console/app/wake_state.py's
            # read_wake_state() already uses (STALE_AFTER_S=1.0),
            # reapplied here directly rather than imported (this file
            # must keep working with the console closed, so it must not
            # depend on anything under l5_console/app).
            updated_at = float(wake_data.get("updated_at", 0))
            if time.time() - updated_at <= WAKE_STATE_STALE_AFTER_S:
                return False, "waiting until you stop talking"
            # Stale: fail toward SPEAKING, same direction as the
            # FileNotFoundError case below -- a dead daemon's last word
            # must not silently gate speech forever.
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        # No daemon running, or unreadable: nothing is being captured, so
        # there is nothing to interrupt. Fail toward SPEAKING -- silence
        # is the failure this channel exists to prevent.
        pass
    newest = max(i.get("queued_at", 0) for i in items)
    if now - newest < SETTLE_S:
        return False, "collecting"
    return True, ""


def _worker_loop() -> None:
    while True:
        time.sleep(_TICK_S)
        try:
            ready, _ = ready_to_flush()
            if ready:
                flush_now()
        except Exception as e:  # never let the loop die
            log_event("return_queue_worker_error", error=str(e))


def _ensure_worker() -> None:
    global _worker_started
    with _lock:
        if _worker_started or os.environ.get("JARVIS_NO_RETURN_QUEUE_WORKER") == "1":
            return
        _worker_started = True
    threading.Thread(target=_worker_loop, daemon=True, name="return-queue").start()
