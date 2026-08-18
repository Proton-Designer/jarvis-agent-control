"""
L2 -> L3 handoff: deliver_transcript(text) -> DeliveryResult | None.
None means the handoff never reached the transport at all -- currently
only when the orchestrator's jarvis-l4 MCP tools aren't connected (see
orchestrator_has_tools()); the failure is spoken directly in that case,
same as every other failure path this function handles.

Contract agreed with the L1/L2 owner: a plain importable function, called
from their daemon after VAD-endpointed capture + STT. Signature is stable;
what happens inside it changed with the architecture pivot to long-form
dictation (up to 5-6 minutes, ~5,000 characters, multiple instructions for
multiple targets in one utterance):

  Do NOT type a multi-thousand-character transcript into a tmux input box.
  Write it to a durably-logged file instead, and deliver a short pointer
  instruction telling the orchestrator where to read it. This also gives
  the system a forensic trail — auto-mode target sessions have no
  permission prompts to fall back on, so the dictation log is the record
  of what was actually said if a misroute ever happens.

L1/L2 does not need to change anything on their end for this — same
function, same signature, same busy/rejected DeliveryResult contract they
already agreed to.

IMMEDIATE ACK: the real end-to-end pipeline run (2026-08-17) measured
~2 minutes from Ayman finishing speaking to the last delivery landing,
dominated by orchestrator reasoning, not audio processing. The actual UX
failure isn't that duration -- it's the SILENCE: Ayman has no signal the
system heard him until routing finishes, which risks him re-triggering
the wake word and starting a second, overlapping dictation. So
deliver_transcript speaks a short, generic acknowledgement (via
say_feedback, so JARVIS_MUTE governs it like everything else) as soon as
the dictation is durably enqueued (file written, dispatch state marked
forwarded) -- before the pointer delivery, before any router reasoning
has happened, but AFTER the enqueue is a confirmed fact rather than an
assumption (SPEC-orchestration.md SS1.4 -- see the try/except around the
enqueue in deliver_transcript() below: an enqueue failure is spoken
explicitly and this ack is never reached). Deliberately generic, not
"routing N instructions": at this point nothing has parsed the dictation
yet, so a specific count would be a guess dressed up as a fact.

THE HANDOFF SEAM, under the two-tier architecture (Haiku concierge /
Sonnet router): whatever calls deliver_transcript() -- directly, or via a
thin wrapper Haiku's own turn invokes -- gets a budget of roughly "how
long a file write and a tmux send-keys call take," not "how long the
router's reasoning takes." That's not a promise this function makes by
being fast; it's a fact already true of what it does (no model call
anywhere in this function) plus the enqueue-then-ack ordering above. The
one thing that must never happen is this function's caller being built
as fire-and-forget, ignoring the return value, on the theory that "it's
fast, it won't fail" -- every failure path in this function (preflight,
enqueue, pointer delivery) already speaks explicitly, so the audible-
failure guarantee is self-contained here and does not depend on the
caller checking anything. See handoff_seam_canary.py.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

from dispatch_state import mark_dispatch_failed, mark_dispatch_forwarded
from latency_log import log_event
from providers import list_sessions
from say_feedback import speak
from transport import Transport

SERVER_PROCESS_PATTERN = "l4_controller/server.py"

# `pgrep -f` matches this pattern as a plain substring anywhere in a
# process's full, flattened command line -- the exact hazard already
# found live in wake.py's is_running() (a throwaway `python -c "...#
# l4_controller/... daemon.py placeholder..."` test fixture matched and
# was briefly read as the real daemon). SPEC-orchestration.md's Known
# Gaps flags this function specifically: 0.2 introduced a genuinely
# second jarvis-l4-shaped process (server_readonly.py, Haiku's surface)
# alongside this one (server.py, the router's), so a bare substring check
# is no longer just theoretically fragile -- there is now a second, real,
# similarly-named process type in the same process table. A plain
# substring wouldn't actually collide with server_readonly.py today
# ("server.py" isn't a substring of "server_readonly.py"), but it would
# still collide with any unrelated process that happens to mention this
# path in a `-c` script body, a comment, or a test fixture -- same class
# of bug, same fix: require the real invocation shape (server.py as its
# OWN trailing argument, not embedded in a larger string) and reject
# anything only found inside a `-c` argument's body.
_REAL_SERVER_INVOCATION = re.compile(r"(?:^|\s)\S*python\S*\s+\S*l4_controller/server\.py(?:\s|$)")

sys.path.insert(0, str(Path(__file__).parent.parent))
from jarvis_paths import jarvis_home  # noqa: E402

DICTATIONS_DIR = jarvis_home() / "dictations"

# Naming convention matches sessions.json's "claude-<project>" pattern.
# Exposed as a named constant for real callers to import and pass
# explicitly (daemon.py's default_deliver, concierge.py's _forward both
# do) -- deliberately NOT deliver_transcript()'s own default (see below).
DEFAULT_ORCHESTRATOR_TARGET = "claude-orchestrator"


def _format_session_list(sessions: list[dict]) -> str:
    if not sessions:
        return "No sessions are currently running."
    parts = []
    for s in sessions:
        bit = f"{s['session_id']} (cwd: {s['working_dir']}"
        if s["alias"]:
            bit += f", alias: {s['alias']}"
        if s["custom_commands"]:
            bit += f", custom commands: {', '.join(s['custom_commands'])}"
        bit += ")"
        parts.append(bit)
    return "; ".join(parts)


def _looks_like_real_server_process(cmdline: str) -> bool:
    if " -c " in cmdline or cmdline.rstrip().endswith(" -c"):
        return False
    return bool(_REAL_SERVER_INVOCATION.search(cmdline))


def orchestrator_has_tools() -> bool:
    """Whether the router's own jarvis-l4 MCP FULL-surface server process
    (server.py, not server_readonly.py -- see _REAL_SERVER_INVOCATION) is
    currently running -- the observable signal that the router's MCP
    trust prompt was actually answered (see README's note on it). Claude
    Code only spawns this process once its project's .mcp.json entry is
    approved; declining or leaving the prompt unanswered means the
    process never starts at all. Confirmed BOTH directions live
    (2026-08-17), not assumed from the working case: approving a fresh
    project's prompt spawned a new server.py process within seconds;
    declining it left none, and that session's own turn independently
    confirmed "the jarvis-l4 MCP tools are not available."

    Uses `pgrep -f` for just the candidate PIDs (one per line, unambiguous
    -- PIDs are numeric, never contain embedded newlines), then
    `ps -p <pid>` per PID for that specific process's own command line --
    same shape as wake.is_running(), and for the same reason: a naive
    `pgrep -fl` breaks on a multi-line `-c "<script>"` command line.

    Still scoped to "SOME real server.py process is up," not to one
    specific router session's PID -- SPEC-orchestration.md's own "one
    router, not a pool" ruling means that's still sufficient today. If
    that ever changes, this would need to match a specific server process
    to its session rather than confirming any real one is running
    somewhere."""
    pids = subprocess.run(["pgrep", "-f", SERVER_PROCESS_PATTERN], capture_output=True, text=True)
    if pids.returncode != 0:
        return False
    for pid in pids.stdout.split():
        cmd = subprocess.run(["ps", "-o", "command=", "-p", pid], capture_output=True, text=True)
        if cmd.returncode == 0 and _looks_like_real_server_process(cmd.stdout):
            return True
    return False


def write_dictation(text: str) -> Path:
    DICTATIONS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%dT%H%M%S")
    path = DICTATIONS_DIR / f"{timestamp}.txt"
    # Exclusive-create with a numeric suffix on collision, rather than
    # silently overwriting an existing dictation from the same second.
    n = 1
    while path.exists():
        path = DICTATIONS_DIR / f"{timestamp}-{n}.txt"
        n += 1
    path.write_text(text)
    return path


def deliver_transcript(
    text: str,
    transport: Transport,
    orchestrator_target: str,
):
    """orchestrator_target has no default, deliberately -- this is a bare,
    directly-importable function, not gated behind daemon.py's
    --live-deliver or concierge.py's live_deliver flag the way a real
    dispatch is. A default here (it used to be DEFAULT_ORCHESTRATOR_TARGET,
    the real production session name) meant any ad-hoc call -- a quick
    script, a REPL check, a CLI smoke test -- silently hit the live
    orchestrator unless the caller happened to think to override it.
    Confirmed the hard way, 2026-08-17: exactly this shape, one directory
    over, sent a concierge CLI smoke test to the real orchestrator by
    omission. Both real callers (daemon.py, concierge.py) already pass
    this explicitly on every call, so removing the default cost them
    nothing and closes the footgun for whoever writes the next one."""
    log_event("handoff_received", chars=len(text))
    if not orchestrator_has_tools():
        # Preflight, before the ack -- not after. Confirmed live
        # (2026-08-17, cold-start validation): an orchestrator whose
        # jarvis-l4 MCP prompt was never answered reasons through the
        # whole dictation correctly and even declines to fabricate a
        # delivery -- but every bit of that stays in its pane's text,
        # because ALL spoken output routes through the now-unavailable
        # MCP tools. From a voice-only user's side that's total silence.
        # This is the one channel that can speak here, because it's the
        # only code in the path that isn't itself downstream of those
        # missing tools -- speaking "Got it, working on it" first would
        # just be a confusing lie immediately followed by this, so this
        # check comes before it, not after.
        log_event("handoff_no_tools", chars=len(text))
        speak("The orchestrator has no tools connected -- check its MCP prompt.")
        return None

    # SPEC-orchestration.md SS1.4: the handoff's fast-path ack must
    # SYNCHRONOUSLY CONFIRM THE ENQUEUE SUCCEEDED -- not be spoken on the
    # assumption it will. Under the two-tier architecture this function
    # (or its equivalent) is what a Haiku-side caller's "~20ms hand off
    # and return" budget is actually paying for; if that budget were spent
    # on a fire-and-forget call that never checks whether the write
    # actually landed, a disk-full or permissions failure here would
    # previously have propagated as an uncaught exception AFTER "Got it,
    # working on it" was already spoken -- a truthful-sounding ack
    # immediately followed by a silent crash, which is worse than never
    # acking at all. So: attempt the enqueue (write the dictation file,
    # mark dispatch state as forwarded) FIRST, catch any failure and speak
    # it explicitly, and only speak the ack once the enqueue is a
    # confirmed fact, not a hope.
    try:
        path = write_dictation(text)
        # Code-driven "forwarded" marker for the router's dispatch-in-
        # flight state -- written here, not by the router, because this
        # is the actual moment of handoff and this call already fires
        # exactly once per real forward. dictation_ref is this dictation's
        # own file path (SPEC-orchestration.md SS0.1) -- see
        # dispatch_state.py for why this must not be an LLM self-report,
        # and why it's keyed rather than a global singleton (a second
        # dictation can be handed off before this one's router work
        # finishes; each must close out only its own record).
        mark_dispatch_forwarded(str(path))
    except Exception as e:  # noqa: BLE001
        log_event("handoff_enqueue_failed", chars=len(text), error=str(e))
        speak("I couldn't record that dictation -- it wasn't handed off.")
        return None

    speak("Got it, working on it.")

    # Pre-inject the live session list rather than making the router spend
    # a full model turn calling list_sessions() itself before it can even
    # start building a routing plan -- measured live (2026-08-17): ~7.7s
    # of a ~17.9s pointer-to-spoken-plan turn was spent on exactly that
    # round trip. Captured at send time, right here, milliseconds before
    # delivery -- as fresh as a separate list_sessions() call would have
    # been anyway. CLAUDE.md still permits calling list_sessions() as a
    # fallback if this list looks stale or wrong; this only removes the
    # round trip on the common path, it doesn't remove the tool.
    sessions = list_sessions()
    pointer = (
        f"New dictation at {path} — read it and route the instructions inside. "
        f"Live sessions right now (captured at send time): {_format_session_list(sessions)}"
    )
    result = transport.deliver(orchestrator_target, pointer)
    log_event("pointer_delivered", ok=result.ok, target=orchestrator_target)
    if not result.ok:
        # Confirmed live (2026-08-17, cold-start validation): without this,
        # a missing/not-ready orchestrator session produced total silence
        # after "Got it, working on it" -- nothing downstream ever checked
        # this DeliveryResult, so a failed handoff looked identical to one
        # that was still being worked on, forever. Worse than a stack
        # trace: at least a stack trace shows up somewhere. This is the
        # one place that can speak the failure, because it's the only
        # code that ever sees it -- L3 never gets a turn to report a
        # pointer it was never sent.
        if result.reason == "no_session":
            speak("I couldn't reach the orchestrator session -- is it running?")
        else:
            speak(f"I couldn't hand that off to the orchestrator: {result.detail}.")
        # The enqueue (file + mark_dispatch_forwarded above) succeeded,
        # but the router was never actually notified -- correct the
        # record to say so immediately rather than leaving it as
        # "forwarded" (implying active work) for up to
        # DISPATCH_ABANDONED_AFTER_S until abandonment self-heals it to a
        # fact this function already knows right now.
        mark_dispatch_failed(str(path), result.reason or result.detail)
    return result
