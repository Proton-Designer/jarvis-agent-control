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
say_feedback, so JARVIS_MUTE governs it like everything else) the instant
it's called -- before writing the file, before the pointer delivery,
before any orchestrator reasoning has happened. Deliberately generic, not
"routing N instructions": at this point nothing has parsed the dictation
yet, so a specific count would be a guess dressed up as a fact.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from dispatch_state import mark_dispatch_forwarded
from latency_log import log_event
from providers import list_sessions
from say_feedback import speak
from transport import Transport

SERVER_PROCESS_PATTERN = "l4_controller/server.py"

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


def orchestrator_has_tools() -> bool:
    """Whether ANY jarvis-l4 MCP server process is currently running --
    the observable signal that the orchestrator's MCP trust prompt was
    actually answered (see README's note on it). Claude Code only spawns
    this process once its project's .mcp.json entry is approved; declining
    or leaving the prompt unanswered means the process never starts at
    all. Confirmed BOTH directions live (2026-08-17), not assumed from the
    working case: approving a fresh project's prompt spawned a new
    server.py process within seconds; declining it left none, and that
    session's own orchestrator turn independently confirmed "the
    jarvis-l4 MCP tools are not available."

    Matches by command-line substring only (not scoped to one orchestrator
    session's PID) -- with a single orchestrator this is sufficient; if
    multiple orchestrators are ever run concurrently, this would need to
    match a specific server process to its session instead of merely
    confirming SOME jarvis-l4 server is up somewhere."""
    result = subprocess.run(["pgrep", "-f", SERVER_PROCESS_PATTERN], capture_output=True)
    return result.returncode == 0


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
    speak("Got it, working on it.")
    path = write_dictation(text)
    # Pre-inject the live session list rather than making L3 spend a full
    # model turn calling list_sessions() itself before it can even start
    # building a routing plan -- measured live (2026-08-17): ~7.7s of a
    # ~17.9s pointer-to-spoken-plan turn was spent on exactly that round
    # trip. Captured at send time, right here, milliseconds before
    # delivery -- as fresh as a separate list_sessions() call would have
    # been anyway. CLAUDE.md still permits calling list_sessions() as a
    # fallback if this list looks stale or wrong; this only removes the
    # round trip on the common path, it doesn't remove the tool.
    sessions = list_sessions()
    pointer = (
        f"New dictation at {path} — read it and route the instructions inside. "
        f"Live sessions right now (captured at send time): {_format_session_list(sessions)}"
    )
    # Code-driven "forwarded" marker for the concierge's dispatch-in-flight
    # state -- written here, not by L3, because this is the actual moment
    # of handoff and this call already fires exactly once per real
    # forward. See dispatch_state.py for why this must not be an
    # LLM self-report.
    mark_dispatch_forwarded(str(path))
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
    return result
