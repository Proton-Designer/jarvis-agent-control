# Conversation Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Ayman hold a spoken conversation with Jarvis without saying "hey Jarvis" before every turn, and let Jarvis tell his voice apart from everyone else's — including its own.

**Architecture:** The wake word still opens the session, but a turn no longer returns to `IDLE`. It goes to `REPLYING` while Jarvis answers, then `FLOOR_OPEN` for 8 seconds where speech starts another turn with no wake word. Silence closes the conversation; `"that's it"` closes it instantly. A speaker-embedding model decides whether a turn may *start*, which both keeps stray room audio out and lets Jarvis ignore its own voice coming back through the speakers.

**Tech Stack:** Python 3.13, `onnxruntime` (already the entire L1 stack — Silero VAD, openWakeWord, Kokoro all ONNX; **there is no torch in `l1_wakeword/.venv`**), `numpy`, `sounddevice`. Tests are canary scripts, not pytest.

**Spec:** `docs/superpowers/specs/2026-08-21-conversation-mode-design.md`

## Global Constraints

- **No torch.** `l1_wakeword/.venv` has `onnxruntime` only. Any model must be ONNX.
- **Audio format is fixed:** 16kHz mono, `FRAME_SAMPLES = 1280` (~80ms), int16 (`listener.py:23-24`).
- **Every canary asserts BOTH directions.** A test that only proves the happy path passes just as happily if everything is accepted.
- **Tests isolate their own state** via `JARVIS_TEST_RUN` and never touch real `~/.jarvis` or `~/Jarvis` files.
- **Anything Ayman is meant to act on ships with its render in the same task** (`SPEC-gaps-and-build-plan.md` §3).
- **Fail toward speech and toward Ayman, never toward silence or truncation.** Every ambiguous case in this plan resolves that way.
- **Run canaries with the L1 venv:** `l1_wakeword/.venv/bin/python3 <path>`.
- Kokoro voice is currently `bm_lewis` (changed from `bm_george` on 2026-08-21). Never hardcode it — always read it.

---

## File Structure

**Create:**
- `l1_wakeword/conversation.py` — the turn-taking state machine as a **pure unit**: no audio, no files, no clock of its own. Events in, state out. This is what makes the whole feature testable without a microphone.
- `l1_wakeword/speaking_state.py` — L1-side reader for `speaking_state.json`, with staleness.
- `l1_wakeword/speaker_verify.py` — embedding + accept/echo/other decision.
- `l1_wakeword/enroll_voice.py` — one-command enrollment CLI.
- `l1_wakeword/conversation_canary.py`
- `l1_wakeword/speaker_verify_canary.py`

**Modify:**
- `l4_controller/say_feedback.py` — worker writes `speaking_state.json`.
- `l1_wakeword/daemon.py` — `LiveController` delegates to `conversation.py`; new states written to `wake_state.json`.
- `l1_wakeword/fetch_models.py` — fetch the speaker-embedding model.
- `l5_console/app/format_helpers.py`, `l5_console/app/console.py` — render new states, floor countdown, voiceprint status.

**Why `conversation.py` is separate from `daemon.py`:** `daemon.py` is already ~1100 lines and owns audio, Whisper, VAD, wake scoring, and delivery. Putting a five-state machine inside it would make the transitions untestable without a mic and unreviewable in context. The state machine is pure logic and belongs in its own file, which is also the only way Task 2's canary can exist.

---

### Task 1: `speaking_state` — knowing when Jarvis stopped talking

The daemon and the speech worker are **different processes** (`jarvis_say` runs inside an MCP server). L1 cannot observe playback directly, so the worker publishes it to a file.

**Files:**
- Create: `l1_wakeword/speaking_state.py`
- Modify: `l4_controller/say_feedback.py` (worker loop, around `_speak_now`)
- Test: `l1_wakeword/conversation_canary.py` (section 1)

**Interfaces:**
- Consumes: nothing.
- Produces: `speaking_state.read(now: float | None = None) -> bool` — True only if the file says speaking AND is fresh. `speaking_state.SPEAKING_STATE_PATH`, `speaking_state.STALE_AFTER_S = 1.0`.

- [ ] **Step 1: Write the failing test**

Create `l1_wakeword/conversation_canary.py`:

```python
#!/usr/bin/env python3
"""Canary for conversation mode (docs/superpowers/specs/2026-08-21-conversation-mode-design.md).

BOTH DIRECTIONS ON EVERY PROPERTY. A test that only proves "the floor
closes" would pass just as happily if the floor never opened at all.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("JARVIS_TEST_RUN", "conversation-canary")
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import speaking_state  # noqa: E402

FAILURES: list[str] = []


def check(desc: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ok    {desc}")
    else:
        FAILURES.append(desc)
        print(f"  FAIL  {desc}{('  -- ' + detail) if detail else ''}")


def _write_speaking(speaking: bool, age_s: float) -> None:
    now = time.time()
    speaking_state.SPEAKING_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    speaking_state.SPEAKING_STATE_PATH.write_text(json.dumps({
        "speaking": speaking, "started_at": now - age_s, "updated_at": now - age_s,
    }))


def main() -> int:
    assert "test_runs" in str(speaking_state.SPEAKING_STATE_PATH), \
        f"NOT ISOLATED: {speaking_state.SPEAKING_STATE_PATH}"

    print("1. speaking_state -- fresh is trusted, stale is not")
    _write_speaking(True, age_s=0.0)
    check("a FRESH speaking=true reads as speaking", speaking_state.read() is True)
    _write_speaking(True, age_s=5.0)
    check("a STALE speaking=true reads as NOT speaking -- a dead worker must not strand REPLYING",
          speaking_state.read() is False)
    _write_speaking(False, age_s=0.0)
    check("a fresh speaking=false reads as not speaking", speaking_state.read() is False)
    speaking_state.SPEAKING_STATE_PATH.unlink(missing_ok=True)
    check("a MISSING file reads as not speaking, never as speaking",
          speaking_state.read() is False)
    speaking_state.SPEAKING_STATE_PATH.write_text("{not json")
    check("a MALFORMED file fails toward NOT speaking", speaking_state.read() is False)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run it to verify it fails**

Run: `l1_wakeword/.venv/bin/python3 l1_wakeword/conversation_canary.py`
Expected: `ModuleNotFoundError: No module named 'speaking_state'`

- [ ] **Step 3: Write `l1_wakeword/speaking_state.py`**

```python
"""Is the Jarvis voice currently playing?

L1 cannot ask directly -- jarvis_say runs inside an MCP server process,
not this one. say_feedback's worker publishes playback to a file and
this reads it.

SAME STALENESS DISCIPLINE AS wake_state.py AND return_queue.py, for the
same reason and after the same bug: a writer that dies mid-utterance
leaves speaking=true on disk forever, and a reader that trusts `speaking`
alone strands the daemon in REPLYING permanently -- the conversation
silently never resumes. Opus Lead 3 found exactly this shape in
return_queue's CAPTURING gate on 2026-08-20. It must not be rebuilt one
layer up.

Reimplemented here rather than imported from l5_console/app/wake_state.py
on purpose: this is L1 and that is the console layer, and conversation
mode must keep working with the console closed. Same convention, applied
independently, not a shared import.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from jarvis_paths import jarvis_home  # noqa: E402

SPEAKING_STATE_PATH = jarvis_home() / "speaking_state.json"
STALE_AFTER_S = 1.0


def read(now: float | None = None) -> bool:
    """True only if the file says speaking AND was written within
    STALE_AFTER_S. Every failure -- missing, malformed, stale, wrong
    types -- returns False, which opens the floor. Failing toward
    'not speaking' costs an interruption; failing toward 'speaking'
    costs the conversation."""
    now = time.time() if now is None else now
    try:
        raw = json.loads(SPEAKING_STATE_PATH.read_text())
        if not raw.get("speaking"):
            return False
        return (now - float(raw["updated_at"])) < STALE_AFTER_S
    except Exception:
        return False


def write(speaking: bool) -> None:
    """Called by say_feedback's worker only."""
    try:
        SPEAKING_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        SPEAKING_STATE_PATH.write_text(json.dumps({
            "speaking": bool(speaking), "started_at": now, "updated_at": now,
        }))
    except Exception:
        pass  # never let a status file break speech itself
```

- [ ] **Step 4: Run the canary and verify it passes**

Run: `l1_wakeword/.venv/bin/python3 l1_wakeword/conversation_canary.py`
Expected: `all checks passed`

- [ ] **Step 5: Make `say_feedback` publish it**

In `l4_controller/say_feedback.py`, wrap the body of `_speak_now(text)` so the flag is set before playback and cleared after, including on exception:

```python
def _speak_now(text: str) -> None:
    _speaking_state.write(True)
    try:
        ...existing body unchanged...
    finally:
        _speaking_state.write(False)
```

Add near the other imports:

```python
sys.path.insert(0, str(Path(__file__).parent.parent / "l1_wakeword"))
import speaking_state as _speaking_state  # noqa: E402
```

The `finally` is load-bearing: a Kokoro failure that skipped the clear would leave `speaking=true`, and only the staleness check would save the conversation. Belt and braces, because this is the exact failure class §3 of the spec exists to prevent.

- [ ] **Step 6: Verify end to end with a real utterance**

Run:
```bash
l4_controller/.venv/bin/python3 -c "
import sys, time, threading; sys.path.insert(0,'l4_controller'); sys.path.insert(0,'l1_wakeword')
import say_feedback, speaking_state
seen = []
threading.Thread(target=lambda: [seen.append(speaking_state.read()) or time.sleep(0.1) for _ in range(40)], daemon=True).start()
say_feedback.speak('testing the speaking state file'); time.sleep(4)
print('saw speaking=True at some point:', any(seen))
print('back to False at the end:', speaking_state.read() is False)"
```
Expected: both `True`.

- [ ] **Step 7: Commit**

```bash
git add l1_wakeword/speaking_state.py l1_wakeword/conversation_canary.py l4_controller/say_feedback.py
git commit -m "L1 can see when the Jarvis voice is playing

Cross-process, file-based, with the same staleness discipline as
wake_state and return_queue -- a speech worker that dies mid-utterance
must not strand the daemon waiting for an end that never comes."
```

---

### Task 2: The conversation state machine, as a pure unit

**Files:**
- Create: `l1_wakeword/conversation.py`
- Test: `l1_wakeword/conversation_canary.py` (section 2)

**Interfaces:**
- Consumes: `speaking_state.read()` (injected, not imported directly — see below).
- Produces:
  - `conversation.IDLE / CAPTURING / REPLYING / FLOOR_OPEN` (str constants)
  - `Conversation(now: float)` with `.state -> str`
  - `.on_wake(now)`, `.on_turn_sent(now, closing: bool)`, `.on_speech_onset(now) -> bool`, `.tick(now, jarvis_speaking: bool) -> str`
  - `conversation.FLOOR_S = 8.0`, `conversation.REPLY_WAIT_MAX_S = 10.0`
  - `.floor_remaining_s(now) -> float | None`

`jarvis_speaking` is **passed into `tick()`** rather than read inside. That keeps the whole state machine pure, so the canary drives 8-second floors and stale-worker cases in microseconds instead of waiting in real time.

- [ ] **Step 1: Write the failing tests**

Append to `l1_wakeword/conversation_canary.py`, before the `if FAILURES` block:

```python
    import conversation as cv

    print()
    print("2. the floor -- opens on end of speech, closes on silence")
    c = cv.Conversation(now=0.0)
    check("starts IDLE", c.state == cv.IDLE)
    c.on_wake(now=1.0)
    check("wake -> CAPTURING", c.state == cv.CAPTURING)
    c.on_turn_sent(now=5.0, closing=False)
    check("turn sent -> REPLYING", c.state == cv.REPLYING)

    c.tick(now=5.5, jarvis_speaking=True)
    check("still REPLYING while Jarvis is speaking", c.state == cv.REPLYING)
    c.tick(now=7.0, jarvis_speaking=False)
    check("Jarvis stops -> FLOOR_OPEN", c.state == cv.FLOOR_OPEN)

    c.tick(now=7.0 + cv.FLOOR_S - 0.1, jarvis_speaking=False)
    check("floor is STILL OPEN at 7.9s -- both directions", c.state == cv.FLOOR_OPEN)
    c.tick(now=7.0 + cv.FLOOR_S + 0.01, jarvis_speaking=False)
    check("floor CLOSES at 8s -> IDLE, silently", c.state == cv.IDLE)

    print()
    print("   the floor opens on END OF SPEECH, not on turn sent")
    c = cv.Conversation(now=0.0)
    c.on_wake(now=0.0); c.on_turn_sent(now=1.0, closing=False)
    c.tick(now=1.1, jarvis_speaking=True)
    c.tick(now=9.0, jarvis_speaking=True)   # a long reply
    check("a long reply does NOT consume the floor", c.state == cv.REPLYING)
    c.tick(now=9.1, jarvis_speaking=False)
    check("...and the floor opens fresh when it ends", c.state == cv.FLOOR_OPEN)
    check("...with the FULL 8s, not what's left of it",
          abs(c.floor_remaining_s(now=9.1) - cv.FLOOR_S) < 0.01,
          str(c.floor_remaining_s(now=9.1)))

    print()
    print("   REPLYING can never hang")
    c = cv.Conversation(now=0.0)
    c.on_wake(now=0.0); c.on_turn_sent(now=1.0, closing=False)
    c.tick(now=1.0 + cv.REPLY_WAIT_MAX_S - 0.1, jarvis_speaking=False)
    check("before the backstop, silence still waits for a reply", c.state == cv.REPLYING)
    c.tick(now=1.0 + cv.REPLY_WAIT_MAX_S + 0.01, jarvis_speaking=False)
    check("past REPLY_WAIT_MAX_S with NO speech at all -> floor opens anyway",
          c.state == cv.FLOOR_OPEN)

    print()
    print("   taking the floor costs no wake word")
    c = cv.Conversation(now=0.0)
    c.on_wake(now=0.0); c.on_turn_sent(now=1.0, closing=False)
    c.tick(now=2.0, jarvis_speaking=False)
    check("floor is open", c.state == cv.FLOOR_OPEN)
    took = c.on_speech_onset(now=3.0)
    check("speech during the floor starts a turn", took is True and c.state == cv.CAPTURING)

    c = cv.Conversation(now=0.0)
    check("speech while IDLE does NOT start a turn -- the wake word is still the guard",
          c.on_speech_onset(now=1.0) is False and c.state == cv.IDLE)

    print()
    print("   Jarvis speaking during the floor RESETS it")
    c = cv.Conversation(now=0.0)
    c.on_wake(now=0.0); c.on_turn_sent(now=1.0, closing=False)
    c.tick(now=2.0, jarvis_speaking=False)
    c.tick(now=6.0, jarvis_speaking=True)
    check("a completion arriving during the floor -> REPLYING", c.state == cv.REPLYING)
    c.tick(now=7.0, jarvis_speaking=False)
    check("...and the floor reopens with the full window",
          c.state == cv.FLOOR_OPEN and abs(c.floor_remaining_s(7.0) - cv.FLOOR_S) < 0.01)

    print()
    print("3. \"that's it\" -- closes instead of reopening")
    c = cv.Conversation(now=0.0)
    c.on_wake(now=0.0); c.on_turn_sent(now=1.0, closing=True)
    c.tick(now=1.1, jarvis_speaking=True)
    check("a closing turn still gets its reply spoken", c.state == cv.REPLYING)
    c.tick(now=3.0, jarvis_speaking=False)
    check("...then goes to IDLE, NOT to the floor", c.state == cv.IDLE)
    check("floor_remaining_s is None when there is no floor",
          c.floor_remaining_s(now=3.0) is None)
```

- [ ] **Step 2: Run to verify it fails**

Run: `l1_wakeword/.venv/bin/python3 l1_wakeword/conversation_canary.py`
Expected: `ModuleNotFoundError: No module named 'conversation'`

- [ ] **Step 3: Write `l1_wakeword/conversation.py`**

```python
"""The turn-taking state machine. Pure logic -- no audio, no files, no
clock of its own.

Every method takes `now` and `tick()` takes `jarvis_speaking` as an
ARGUMENT rather than reading speaking_state itself. That is what lets
conversation_canary drive an 8-second floor in microseconds and test a
dead speech worker without killing one. A state machine that reads the
world cannot be tested; one that is handed the world can.

WHY THE FLOOR OPENS ON END-OF-SPEECH, NOT ON TURN-SENT: if it opened when
the turn was sent, a dispatch would eat it -- the concierge takes ~2.8s to
answer, so a third of the window would be gone before Ayman heard a word.
Opening at end-of-speech means the 8 seconds are always 8 seconds of HIS
time, and it composes with the slow path: a dispatch gets a fast "passing
that on", the floor opens, and he can add another instruction while the
router is still thinking.
"""
from __future__ import annotations

IDLE = "IDLE"
CAPTURING = "CAPTURING"
REPLYING = "REPLYING"
FLOOR_OPEN = "FLOOR_OPEN"

FLOOR_S = 8.0

# REPLYING must not be able to hang. There is a known live case where a
# dispatch produces NO speech at all, and waiting on an end-of-speech
# that never comes would silently kill the conversation. Unconditional,
# like return_queue's MAX_HOLD_S and for the same reason.
REPLY_WAIT_MAX_S = 10.0


class Conversation:
    def __init__(self, now: float):
        self.state = IDLE
        self._closing = False
        self._floor_until: float | None = None
        self._replying_since: float | None = None
        self._heard_jarvis = False

    def on_wake(self, now: float) -> None:
        """Wake word verified. Only ever called from IDLE."""
        self.state = CAPTURING
        self._closing = False
        self._floor_until = None

    def on_speech_onset(self, now: float) -> bool:
        """Speech started. Returns True if it opened a turn.

        Only the floor grants this. In IDLE the wake word is still the
        only way in -- that boundary is what keeps a remark to someone
        else in the room from reaching an agent."""
        if self.state != FLOOR_OPEN:
            return False
        self.state = CAPTURING
        self._floor_until = None
        return True

    def on_turn_sent(self, now: float, closing: bool) -> None:
        self.state = REPLYING
        self._closing = closing
        self._replying_since = now
        self._heard_jarvis = False
        self._floor_until = None

    def tick(self, now: float, jarvis_speaking: bool) -> str:
        if self.state == REPLYING:
            if jarvis_speaking:
                self._heard_jarvis = True
                return self.state
            if self._heard_jarvis:
                self._end_reply(now)          # normal: speech finished
            elif now - self._replying_since >= REPLY_WAIT_MAX_S:
                self._end_reply(now)          # backstop: nothing was ever said
            return self.state

        if self.state == FLOOR_OPEN:
            if jarvis_speaking:
                # A batched completion arriving mid-floor. Jarvis is
                # talking again, so this is a reply -- and Ayman gets a
                # fresh full floor after it rather than the remains of
                # this one.
                self.state = REPLYING
                self._replying_since = now
                self._heard_jarvis = True
                self._floor_until = None
            elif now >= self._floor_until:
                self.state = IDLE
                self._floor_until = None
            return self.state

        return self.state

    def _end_reply(self, now: float) -> None:
        if self._closing:
            self.state = IDLE
            self._floor_until = None
        else:
            self.state = FLOOR_OPEN
            self._floor_until = now + FLOOR_S

    def floor_remaining_s(self, now: float) -> float | None:
        if self.state != FLOOR_OPEN or self._floor_until is None:
            return None
        return max(0.0, self._floor_until - now)
```

- [ ] **Step 4: Run and verify it passes**

Run: `l1_wakeword/.venv/bin/python3 l1_wakeword/conversation_canary.py`
Expected: `all checks passed`

- [ ] **Step 5: Commit**

```bash
git add l1_wakeword/conversation.py l1_wakeword/conversation_canary.py
git commit -m "The conversation state machine, as a pure testable unit

Five states. The floor opens when Jarvis stops SPEAKING, not when the
turn is sent -- otherwise a dispatch eats the window before Ayman hears
a word. Two independent backstops so REPLYING can never hang."
```

---

### Task 3: `"that's it"` alone must send nothing

**Files:**
- Modify: `l1_wakeword/daemon.py` (`DictationSession.strip_stop_phrase`, `:608`, and `_report_and_deliver`, `:718`)
- Test: `l1_wakeword/conversation_canary.py` (section 4)

**Interfaces:**
- Consumes: existing `match_stop_phrase(text) -> tuple[bool, str]`, `_normalize_for_stop_match(text) -> str`.
- Produces: `DictationSession.is_empty_after_stop() -> bool`.

Ayman's rule, exactly: `"that's it"` **after a message** sends it, lets Jarvis answer, then closes. `"that's it"` **alone** closes with no send and no reply.

- [ ] **Step 1: Write the failing tests**

Append to `conversation_canary.py`:

```python
    print()
    print("4. \"that's it\" alone sends NOTHING")
    import daemon_text as dt   # thin import shim, see Step 3
    check("'that's it' alone is empty after stripping",
          dt.transcript_after_stop("that's it") == "")
    check("'thats it' (no apostrophe) too",
          dt.transcript_after_stop("thats it") == "")
    check("BOTH DIRECTIONS: content + 'that's it' KEEPS the content",
          dt.transcript_after_stop("tell gateway to run the tests, that's it")
          == "tell gateway to run the tests,")
    check("a bare instruction is untouched",
          dt.transcript_after_stop("tell gateway to run the tests")
          == "tell gateway to run the tests")
    check("'wait for the deploy to finish' is NOT a close -- the word appears mid-sentence",
          dt.transcript_after_stop("wait for the deploy to finish")
          == "wait for the deploy to finish")
```

- [ ] **Step 2: Run to verify it fails**

Expected: `ModuleNotFoundError: No module named 'daemon_text'`

- [ ] **Step 3: Create `l1_wakeword/daemon_text.py`**

`daemon.py` imports `openwakeword` at module scope, so a canary cannot import it without a mic stack (this already broke `instant_ack_canary` once). Move the pure text helpers into their own module and have `daemon.py` import them from there.

```python
"""Pure text helpers shared by daemon.py and its canaries.

Split out because daemon.py imports openwakeword at module scope, so
importing it from a test pulls in the entire acoustic stack. These
functions have no dependencies at all and are the only part canaries
need.
"""
from __future__ import annotations

import re

STOP_PHRASE_VARIANTS = {"that's it", "thats it", "that is it"}


def normalize_for_stop_match(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s']", "", text)
    return re.sub(r"\s+", " ", text).strip()


def transcript_after_stop(text: str) -> str:
    """The transcript with a TRAILING stop phrase removed.

    Trailing only -- "wait for the deploy to finish" must not close a
    conversation just because it contains a word. Returns "" when the
    whole utterance was the phrase, which is Ayman's "close without
    sending" case and is the caller's signal to send nothing at all.
    """
    norm = normalize_for_stop_match(text)
    for variant in STOP_PHRASE_VARIANTS:
        if norm == variant:
            return ""
        if norm.endswith(" " + variant):
            cut = len(text)
            words = variant.split()
            # walk back exactly len(words) whitespace-separated tokens
            for _ in range(len(words)):
                cut = text.rstrip()[:cut].rstrip().rfind(" ")
                if cut == -1:
                    return ""
            return text[:cut].rstrip()
    return text.strip()
```

Then in `daemon.py`, replace the local `STOP_PHRASE_VARIANTS` and `_normalize_for_stop_match` definitions with `from daemon_text import STOP_PHRASE_VARIANTS, normalize_for_stop_match, transcript_after_stop` and keep the existing call sites pointing at the imported names.

- [ ] **Step 4: Run and verify it passes**

Run: `l1_wakeword/.venv/bin/python3 l1_wakeword/conversation_canary.py`
Expected: `all checks passed`

- [ ] **Step 5: Verify the existing canaries still pass**

Run:
```bash
l1_wakeword/.venv/bin/python3 l1_wakeword/hold_phrase_canary.py
l1_wakeword/.venv/bin/python3 l1_wakeword/wake_verify_canary.py
```
Expected: both pass. If either fails, the extraction changed behaviour — fix it, do not adjust the test.

- [ ] **Step 6: Commit**

```bash
git add l1_wakeword/daemon_text.py l1_wakeword/daemon.py l1_wakeword/conversation_canary.py
git commit -m "\"that's it\" alone closes without sending

Also splits daemon.py's pure text helpers into daemon_text.py -- daemon.py
imports openwakeword at module scope, so canaries could not reach them."
```

---

### Task 4: Wire the state machine into the live daemon

**Files:**
- Modify: `l1_wakeword/daemon.py` — `LiveController.__init__`, `on_frame` (`:1055`), `_to_idle` (`:1012`)
- Test: manual live drive (this task is the one that cannot be canaried)

**Interfaces:**
- Consumes: `conversation.Conversation`, `speaking_state.read()`, `daemon_text.transcript_after_stop`.
- Produces: `LiveController.conversation` attribute for Task 5's state reporting.

- [ ] **Step 1: Replace `self.state` with the Conversation object**

In `LiveController.__init__`:

```python
self.conversation = conversation.Conversation(now=time.time())
```

Replace every `self.state` read with `self.conversation.state`, and every assignment with the corresponding `on_*` call. `CANCEL_ARMED` stays a separate flag on the controller — it is orthogonal to the conversation and folding it in would couple two unrelated lifecycles.

- [ ] **Step 2: Drive `tick()` every frame**

At the top of `on_frame`:

```python
self.conversation.tick(now=now, jarvis_speaking=speaking_state.read())
```

`speaking_state.read()` is a small JSON read at ~12Hz. If profiling shows it matters, cache it for 100ms — but measure first, and do not pre-optimise a file read that the console already does at 10Hz.

- [ ] **Step 3: Let the floor start a turn**

In the branch that currently handles `IDLE`, add a `FLOOR_OPEN` branch **before** wake scoring:

```python
elif self.conversation.state == conversation.FLOOR_OPEN:
    if self._vad_speech_onset(frame_i16):
        if self.conversation.on_speech_onset(now):
            self._begin_session(now, wake_score=None)
```

`_vad_speech_onset` uses the Silero VAD instance already in the controller. Speaker verification is **not** wired here yet — that is Task 10. Until then the floor accepts any speech, which is exactly the behaviour to test in Step 5.

- [ ] **Step 4: Route end-of-turn through the machine**

Where the daemon currently returns to IDLE after delivery, call instead:

```python
stripped = daemon_text.transcript_after_stop(session.full_transcript())
closing = stripped != session.full_transcript().strip()
if not stripped:
    self.conversation.on_turn_sent(now, closing=True)
    self.conversation.tick(now, jarvis_speaking=False)  # nothing to say -> straight to IDLE
    log_event("l1_conversation_closed", reason="stop_phrase_alone")
else:
    _report_and_deliver(stripped, ...)
    self.conversation.on_turn_sent(now, closing=closing)
```

Note the empty case calls `tick` with `jarvis_speaking=False` and `_heard_jarvis` False, which the `REPLY_WAIT_MAX_S` path would otherwise hold for 10s. Set `_closing=True` **and** short-circuit: add `Conversation.close_now(now)` that sets `IDLE` directly, and use it here.

- [ ] **Step 5: Drive it live**

```bash
l1_wakeword/run_daemon.sh --live-deliver
```

Say: "hey Jarvis, how are you doing" → wait for the reply → **without a wake word** say "what's running right now" → wait → say nothing for 8 seconds.

Expected in `~/.jarvis/latency_log.jsonl`: two `l1_dictation_end` events, the second with no `wake_verified` before it, then silence.

Then repeat and end the second turn with "that's it" — expect the conversation to close after the reply rather than reopening.

Then say "hey Jarvis" followed only by "that's it" — expect no `pointer_delivered` at all.

- [ ] **Step 6: Commit**

```bash
git add l1_wakeword/daemon.py
git commit -m "Conversation mode is live: a turn no longer returns to IDLE"
```

---

### Task 5: Render the new states (ships with the feature, not after)

**Files:**
- Modify: `l1_wakeword/daemon.py` (`_write_wake_state_file`, `:662`)
- Modify: `l5_console/app/format_helpers.py`, `l5_console/app/console.py`
- Test: `l5_console/app/pending_speech_canary.py` pattern → new checks in `format_helpers`' canary

**Interfaces:**
- Consumes: `Conversation.state`, `Conversation.floor_remaining_s(now)`.
- Produces: `format_helpers.conversation_status(state: str, floor_remaining_s: float | None) -> str`.

Standing rule from `SPEC-gaps-and-build-plan.md` §3: anything Ayman is meant to act on ships with its render **in the same change**. A live microphone he cannot see on screen is precisely the class of bug that list exists to catch.

- [ ] **Step 1: Write the failing test**

In the console canary:

```python
check("FLOOR_OPEN renders the countdown -- he must be able to see the mic is live",
      "8" in fh.conversation_status("FLOOR_OPEN", 8.0))
check("...and counts down", "3" in fh.conversation_status("FLOOR_OPEN", 3.2))
check("IDLE renders no countdown at all",
      fh.conversation_status("IDLE", None) == "idle")
check("REPLYING is distinguishable from CAPTURING -- they mean opposite things for the mic",
      fh.conversation_status("REPLYING", None) != fh.conversation_status("CAPTURING", None))
```

- [ ] **Step 2: Run to verify it fails**

Run: `l5_console/app/.venv/bin/python3 l5_console/app/pending_speech_canary.py`
Expected: `AttributeError: module 'format_helpers' has no attribute 'conversation_status'`

- [ ] **Step 3: Add `conversation_status` to `format_helpers.py`**

```python
def conversation_status(state: str, floor_remaining_s: float | None) -> str:
    """One short line for the console header.

    FLOOR_OPEN shows a countdown because it is the only state where the
    microphone is live and Ayman did not just say something -- the one
    case where the screen tells him something his ears cannot."""
    if state == "FLOOR_OPEN" and floor_remaining_s is not None:
        return f"listening -- {floor_remaining_s:.0f}s"
    return {
        "CAPTURING": "hearing you",
        "REPLYING": "answering",
        "CANCEL_ARMED": "cancel armed",
    }.get(state, "idle")
```

- [ ] **Step 4: Write the new states into `wake_state.json`**

In `_write_wake_state_file`, add `floor_remaining_s` alongside `state` and `level`. The console already reads this file at 10Hz, so no new plumbing.

- [ ] **Step 5: Render it in `console.py`**

Put `conversation_status(...)` in the header next to the existing meter, using `PlainLabel` (markup off — `[orchestrator]` was eaten by Rich markup once already).

- [ ] **Step 6: Run the canary and verify it passes, then look at the screen**

Run the console, start the mic, hold a two-turn conversation, and confirm the countdown visibly ticks 8→0 during the floor.

- [ ] **Step 7: Commit**

```bash
git add l1_wakeword/daemon.py l5_console/app/format_helpers.py l5_console/app/console.py l5_console/app/pending_speech_canary.py
git commit -m "Show conversation state and the floor countdown"
```

---

### Task 6: Fetch and run a speaker-embedding model

**Files:**
- Modify: `l1_wakeword/fetch_models.py`
- Create: `l1_wakeword/speaker_verify.py` (embedding half only)
- Test: `l1_wakeword/speaker_verify_canary.py` (section 1)

**Interfaces:**
- Produces: `speaker_verify.embed(audio_f32: np.ndarray) -> np.ndarray` (L2-normalised, shape `(DIM,)`), `speaker_verify.DIM`, `speaker_verify.MIN_EMBED_S = 0.8`, `speaker_verify.cosine(a, b) -> float`.

Model selection is a **decision with a criterion, not a placeholder**: pick a WeSpeaker or ECAPA-TDNN ONNX export, run Step 4's measurement, and keep the first one that clears a 0.25 margin between Ayman's self-similarity and its similarity to `bm_lewis`. Record the chosen name and measured margin in the commit message, exactly as Kokoro's voice choice was.

- [ ] **Step 1: Write the failing test**

```python
#!/usr/bin/env python3
"""Canary for speaker verification.

BOTH DIRECTIONS, and the second one is the whole point: a test that only
proves "Ayman is accepted" passes just as happily if EVERYTHING is
accepted -- which is the Jarvis-hears-itself feedback loop.
"""
import os, sys, numpy as np
from pathlib import Path
os.environ.setdefault("JARVIS_TEST_RUN", "speaker-verify-canary")
sys.path.insert(0, str(Path(__file__).parent))
import speaker_verify as sv

FAILURES = []
def check(desc, ok, detail=""):
    print(f"  {'ok   ' if ok else 'FAIL '} {desc}" + (f"  -- {detail}" if not ok and detail else ""))
    if not ok: FAILURES.append(desc)

def main():
    print("1. embedding")
    a = np.zeros(16000, dtype=np.float32)
    e = sv.embed(a)
    check("returns the declared dimension", e.shape == (sv.DIM,), str(e.shape))
    check("is L2-normalised, so cosine is a dot product",
          abs(float(np.linalg.norm(e)) - 1.0) < 1e-3)
    check("audio shorter than MIN_EMBED_S returns None, never a garbage vector",
          sv.embed(np.zeros(int(16000 * 0.3), dtype=np.float32)) is None)
    check("cosine of a vector with itself is 1", abs(sv.cosine(e, e) - 1.0) < 1e-4)
    print()
    if FAILURES: print(f"{len(FAILURES)} FAILED"); return 1
    print("all checks passed"); return 0

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run to verify it fails**

Expected: `ModuleNotFoundError: No module named 'speaker_verify'`

- [ ] **Step 3: Add the model to `fetch_models.py`**

Follow the existing entries' pattern exactly (URL, destination in `l1_wakeword/models/`, checksum verification if the existing ones do it).

- [ ] **Step 4: Write the embedding half of `speaker_verify.py`**

```python
"""Speaker embeddings, ONNX.

ONNX is forced, not preferred: l1_wakeword/.venv has onnxruntime and NO
torch, same as every other model in this layer (silero_vad.onnx,
hey_jarvis_v0.1.onnx, melspectrogram.onnx). That rules out Resemblyzer
and anything else torch-based.
"""
from __future__ import annotations

import numpy as np
import onnxruntime as ort
from pathlib import Path

MODEL_PATH = Path(__file__).parent / "models" / "speaker_embedding.onnx"
DIM = 192
SAMPLE_RATE = 16000

# Below this there is not enough signal for a meaningful embedding.
# Returning None rather than a low-confidence vector is deliberate --
# callers decide what to do with "cannot tell", and every caller in this
# system decides it in Ayman's favour.
MIN_EMBED_S = 0.8

_session = None


def _get_session():
    global _session
    if _session is None:
        _session = ort.InferenceSession(str(MODEL_PATH), providers=["CPUExecutionProvider"])
    return _session


def embed(audio_f32: np.ndarray) -> np.ndarray | None:
    if len(audio_f32) < int(SAMPLE_RATE * MIN_EMBED_S):
        return None
    sess = _get_session()
    out = sess.run(None, {sess.get_inputs()[0].name: audio_f32[None, :].astype(np.float32)})[0]
    vec = np.asarray(out).reshape(-1).astype(np.float32)
    norm = float(np.linalg.norm(vec))
    return vec / norm if norm > 0 else vec


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b))   # both are L2-normalised
```

- [ ] **Step 5: Run and verify it passes**

Run: `l1_wakeword/.venv/bin/python3 l1_wakeword/speaker_verify_canary.py`

- [ ] **Step 6: Commit** with the chosen model name and its measured margin in the message.

---

### Task 7: The accept / echo / other decision

**Files:**
- Modify: `l1_wakeword/speaker_verify.py`
- Test: `l1_wakeword/speaker_verify_canary.py` (section 2)

**Interfaces:**
- Produces: `speaker_verify.decide(emb, print_data, barge_in: bool) -> str` returning `"accept" | "echo" | "other"`, and `speaker_verify.load_print() -> dict | None`.

- [ ] **Step 1: Write the failing tests**

```python
    print()
    print("2. the decision -- all three branches, plus the failure directions")
    ay = np.zeros(sv.DIM, dtype=np.float32); ay[0] = 1.0
    jv = np.zeros(sv.DIM, dtype=np.float32); jv[1] = 1.0
    p = {"ayman": {"centroid": ay.tolist(), "accept_threshold": 0.6},
         "jarvis": {"centroid": jv.tolist(), "reject_threshold": 0.55, "voice": "bm_lewis"}}

    check("Ayman's own voice ACCEPTS", sv.decide(ay, p, barge_in=False) == "accept")
    check("Jarvis's own voice is ECHO, not merely 'other'",
          sv.decide(jv, p, barge_in=False) == "echo")
    mid = (ay * 0.4 + np.random.RandomState(0).randn(sv.DIM).astype(np.float32) * 0.1)
    mid /= np.linalg.norm(mid)
    check("a third person is OTHER", sv.decide(mid, p, barge_in=False) == "other")

    print("   barge-in demands PROOF, ordinary turns only demand 'not obviously someone else'")
    near = (ay * 0.62 + jv * 0.02); near /= np.linalg.norm(near)
    check("a marginal match accepts on the open floor",
          sv.decide(near, p, barge_in=False) == "accept")
    check("...and does NOT accept as a barge-in -- a false barge-in is a feedback loop",
          sv.decide(near, p, barge_in=True) != "accept")

    print("   a missing print never silently accepts everything")
    check("no print at all -> 'other', never 'accept'",
          sv.decide(ay, None, barge_in=False) == "other")
```

- [ ] **Step 2: Run to verify it fails**

Expected: `AttributeError: module 'speaker_verify' has no attribute 'decide'`

- [ ] **Step 3: Implement**

```python
import json, sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from jarvis_paths import jarvis_home

VOICEPRINT_PATH = jarvis_home() / "voiceprint.json"

# Barge-in -- starting a turn while Jarvis is mid-sentence -- demands a
# positive match at a raised bar. The two errors are not symmetric: a
# missed barge-in means Ayman talks over Jarvis for a moment and can
# repeat himself; a FALSE barge-in means Jarvis heard itself, answered
# itself, and does it again forever.
BARGE_IN_MARGIN = 0.12


def load_print() -> dict | None:
    try:
        return json.loads(VOICEPRINT_PATH.read_text())
    except Exception:
        return None


def decide(emb, print_data: dict | None, barge_in: bool) -> str:
    """accept | echo | other. A missing or broken print returns "other"
    for everything -- degraded, but it can never open the feedback loop."""
    if not print_data:
        return "other"
    try:
        ay = np.asarray(print_data["ayman"]["centroid"], dtype=np.float32)
        jv = np.asarray(print_data["jarvis"]["centroid"], dtype=np.float32)
        accept_t = float(print_data["ayman"]["accept_threshold"])
        reject_t = float(print_data["jarvis"]["reject_threshold"])
    except Exception:
        return "other"

    sim_j = cosine(emb, jv)
    sim_a = cosine(emb, ay)
    if sim_j >= reject_t and sim_j > sim_a:
        return "echo"
    if sim_a >= (accept_t + BARGE_IN_MARGIN if barge_in else accept_t):
        return "accept"
    return "other"
```

- [ ] **Step 4: Run and verify it passes**

- [ ] **Step 5: Commit**

---

### Task 8: Enrollment

**Files:**
- Create: `l1_wakeword/enroll_voice.py`
- Test: run it (this task's deliverable is an interactive tool)

**Interfaces:**
- Consumes: `speaker_verify.embed`, `speaker_verify.cosine`, `speaker_verify.VOICEPRINT_PATH`, Kokoro synthesis from `l4_controller/kokoro_tts.py`.
- Produces: `~/.jarvis/voiceprint.json` in the §4.4 schema.

- [ ] **Step 1: Write the enrollment script**

Structure, in order:
1. Print the paragraph from spec §5.1 and wait for Enter.
2. Record via `sounddevice` at 16kHz mono until Enter again, showing a level meter.
3. **Reject a bad take and ask for another** — under 45s of voiced audio, peak below -30dBFS, or more than 1% clipped samples. Say which. Building the system's identity check on a bad recording is worse than asking him to read it twice.
4. Window at 3.0s with a 1.5s hop, `embed()` each, drop `None`s, mean → L2-normalise → `ayman.centroid`.
5. **Calibrate rather than hardcode:** compute each window's cosine to the centroid; set `accept_threshold = max(0.45, mean - 2 * std)`. The floor of 0.45 stops a very consistent reading from producing an unusably strict threshold.
6. Synthesise the same paragraph with Kokoro at the **currently configured** voice, embed identically, → `jarvis.centroid`, and record `jarvis.voice`. Set `reject_threshold = 0.55`.
7. Compute and **print the margin** between Ayman's mean self-similarity and his similarity to the Jarvis print.
8. **If the margin is under 0.25, refuse to write a print that enables full-duplex** — write the file with `"full_duplex": false` and say why, out loud and on screen. A number he can see, not a promise.

- [ ] **Step 2: Run it**

```bash
l1_wakeword/.venv/bin/python3 l1_wakeword/enroll_voice.py
```

- [ ] **Step 3: Verify the artifact**

```bash
l1_wakeword/.venv/bin/python3 -c "
import json,sys; sys.path.insert(0,'l1_wakeword')
from speaker_verify import VOICEPRINT_PATH
p=json.loads(VOICEPRINT_PATH.read_text())
print('voice:', p['jarvis']['voice'])
print('accept_threshold:', p['ayman']['accept_threshold'])
print('windows:', p['ayman']['n_windows'])
print('full_duplex:', p.get('full_duplex'))"
```
Expected: `voice: bm_lewis`, a threshold between 0.45 and 0.8, 30+ windows.

- [ ] **Step 4: Commit** (the script only — `voiceprint.json` lives in `~/.jarvis` and must never be committed)

---

### Task 9: The voice-change trap

**Files:**
- Modify: `l1_wakeword/speaker_verify.py`, `l1_wakeword/daemon.py` (startup)
- Test: `l1_wakeword/speaker_verify_canary.py` (section 3)

**Interfaces:**
- Produces: `speaker_verify.full_duplex_ok(print_data, configured_voice: str | None) -> tuple[bool, str]`.

`jarvis.centroid` fingerprints one specific Kokoro voice. Ayman changed `bm_george → bm_lewis` today. A voice change silently invalidates echo rejection, and silent invalidation is this project's signature failure.

- [ ] **Step 1: Write the failing tests**

```python
    print()
    print("3. a voice change must DEGRADE AUDIBLY, never fail silently")
    ok, why = sv.full_duplex_ok(p, "bm_lewis")
    check("matching voice -> full duplex", ok is True)
    ok, why = sv.full_duplex_ok(p, "bm_george")
    check("CHANGED voice -> refuses full duplex", ok is False)
    check("...and says which voice it expected", "bm_lewis" in why and "bm_george" in why, why)
    ok, why = sv.full_duplex_ok(p, None)
    check("an UNREADABLE voice is treated as a mismatch, not as a match", ok is False)
    ok, why = sv.full_duplex_ok(None, "bm_lewis")
    check("no print at all -> no full duplex", ok is False)
```

- [ ] **Step 2: Run to verify it fails**

- [ ] **Step 3: Implement, and wire it into daemon startup**

On mismatch the daemon must `speak()` the reason at `PRIORITY_HIGH` and fall back to half-duplex (mic ignored entirely while `speaking_state.read()` is True). Degraded and audible.

- [ ] **Step 4: Run and verify it passes**

- [ ] **Step 5: Commit**

---

### Task 10: Wire verification into the floor and into barge-in

**Files:**
- Modify: `l1_wakeword/daemon.py` (`LiveController.on_frame`, the `FLOOR_OPEN` branch from Task 4)

**Interfaces:**
- Consumes: everything above.

The §4.6 ordering is the whole task and getting it backwards clips the first word of every turn.

- [ ] **Step 1: Buffer from VAD onset, unconditionally**

Capture starts the instant VAD fires. Audio buffers while verification runs on the leading ~1s. If it verifies, the buffer is already there and nothing was lost. If it does not, discard and return to `FLOOR_OPEN` with the remaining time intact.

- [ ] **Step 2: Accept what is too short to judge**

`embed()` returns `None` under `MIN_EMBED_S`. On the open floor that means **accept** — during `FLOOR_OPEN` the overwhelmingly likely speaker is the person who was just talking, and "yes" / "no" / "stop" must work. The floor being open at all is the guard; the model is not asked to carry a decision it cannot make.

- [ ] **Step 3: Barge-in is the exception**

During `REPLYING`, `None` means **reject** — a short unverifiable chunk there is more likely Jarvis's own tail than a barge-in. Pass `barge_in=True`.

- [ ] **Step 4: Drop `"echo"` chunks inside `CAPTURING` too**

Verification gates turn *starts*, not words — except this one bounded case, because barging in overlaps both voices. Only `"echo"` is dropped mid-turn; `"other"` is kept, because dropping it would delete part of Ayman's sentence and that is the failure §4.3 exists to prevent.

- [ ] **Step 5: Live drive, both ways**

On headphones: hold a four-turn conversation, then interrupt Jarvis mid-sentence and confirm it stops and listens.

On laptop speakers: repeat. Confirm `~/.jarvis/latency_log.jsonl` contains **zero** turns whose transcript matches something Jarvis just said. That is the feedback loop, and its absence is the acceptance criterion.

Then have someone else in the room speak while the floor is open, and confirm no turn opens.

- [ ] **Step 6: Commit**

---

### Task 11: Documentation

**Files:**
- Modify: `docs/SPEC-orchestration.md`, `l1_wakeword/README.md`, `docs/TODO-feature-queue.md`

- [ ] **Step 1: Document the state machine** in `l1_wakeword/README.md`, including the enrollment command and what to do after changing the Kokoro voice (re-run enrollment; the Jarvis half is synthesis and needs no human).

- [ ] **Step 2: Record the rejected alternatives** so nobody rediscovers them as oversights: output-device detection (§4.1), continuous semantic endpointing (§9), multi-speaker enrollment (§9).

- [ ] **Step 3: Commit**

---

## Self-Review

**Spec coverage:** §1 floor semantics → Task 2. §2 states/transitions → Tasks 2, 4. §3 speaking_state → Task 1. §4.1 no device detection → Task 11 Step 2. §4.2 ONNX model → Task 6. §4.3 gates starts not words → Task 10 Step 4. §4.4 two prints → Tasks 7, 8. §4.5 voice-change trap → Task 9. §4.6 onset buffering → Task 10 Steps 1-3. §4.7 reading the configured voice → Task 9. §5 enrollment + paragraph → Task 8. §6 render → Task 5. §7 failure directions → asserted across Tasks 2, 7, 9, 10. §8 testing → every task. §9 not-in-this-build → Task 11.

**Gap found and closed during review:** Task 4 Step 4's empty-transcript path would have sat in `REPLYING` for the full `REPLY_WAIT_MAX_S` before closing, because nothing ever speaks. `Conversation.close_now(now)` is called out explicitly in that step.

**Type consistency:** `Conversation.state` is a `str` matching the module constants throughout. `embed()` returns `np.ndarray | None` and every caller handles `None` explicitly (Task 10 Steps 2-3). `decide()` returns one of exactly three strings, checked in Tasks 7 and 10. `full_duplex_ok()` returns `(bool, str)` in Task 9 only.
