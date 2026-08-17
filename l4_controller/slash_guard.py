"""
Guards against slash-command payloads that could do something other than
"become text" when typed into a target session.

Three distinct hazards found here, live, not theoretical -- each one is
why this file exists in its current shape:

1. **Partial/unrecognized slash fragments hijack Enter.** Typing "/comp"
   with Claude Code's completion overlay open and pressing Enter submits
   the HIGHLIGHTED SUGGESTION ("/compact"), not the literal text typed.
   A malformed payload could execute a different command than resolved.

2. **Some commands open a persistent, non-input view** (`/cost`, `/usage`,
   `/status`, `/config`, `/help`, ...) rather than running inline. The
   target pane is not accepting normal chat input while one is open.

3. **Inside an interactive view, injected keystrokes are UI INPUT, not
   text.** Confirmed live: sending "/model" while a Config view was still
   open landed on a highlighted settings row, and Enter TOGGLED it --
   disabled a real, global Claude Code preference (Ayman's auto-compact
   setting), not scoped to the throwaway test session. The entire
   transport rests on the assumption that send-keys produces characters
   in an input box; inside a picker view that assumption is false.

So the policy is not just "is this payload well-formed" -- it's "do we
know, from direct verification, what kind of view (if any) this command
opens, and is that kind safe to interact with unsupervised":

- `none`   -- runs inline, no view opens. Always safe to send.
- `readonly` -- opens a persistent view that only displays information,
  nothing selectable that Enter could commit. Safe to send AND safe to
  read back (see server.py's view-read-back flow) -- but the transport
  still owns dismissing it (Escape, verify empty prompt) before treating
  delivery as complete.
- `interactive` -- opens a view containing a selectable/toggleable list.
  NEVER send. The value of a voice-driven settings change is close to
  zero; the risk is a silent, global mutation.
- unknown (not in `allowed` at all) -- default DENY. An unrecognized
  command is exactly what caused finding #3 above; verifying before
  sending is the fix, not sending and hoping.

`known_slash_commands.json`'s `blocked` section records commands
explicitly tested and found dangerous, as a distinct case from "just not
yet verified" -- both refuse, but a `blocked` entry carries a specific,
useful reason.

Per-target custom commands (project-specific `.claude/commands/*.md`,
plugin/skill-provided commands) are a second, separate allowlist source:
see registry.py's discovery of them. They're treated as `view: none`
by default -- they're user-authored prompt templates, not new UI code,
so they structurally can't render an interactive picker the way Claude
Code's own built-ins can. That assumption is about custom commands
specifically; it does not extend to unverified built-ins (rule above).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_KNOWN_COMMANDS_PATH = Path(__file__).parent / "known_slash_commands.json"

# Commands blocked by default for irreversibility (not view-type) that a
# config flag can re-enable individually, once/if a stronger confirmation
# flow exists for them. Distinct from /config and /model, which are
# blocked for a hazard (interactive picker) no flag should ever override.
_REVERSIBILITY_FLAGS = {
    "/clear": "JARVIS_ALLOW_CLEAR",
}


class KnownCommands:
    def __init__(self, allowed: dict[str, str], blocked: dict[str, str]):
        self.allowed = allowed  # command -> view ("none" | "readonly")
        self.blocked = blocked  # command -> reason


def load_known_commands(path: Path = DEFAULT_KNOWN_COMMANDS_PATH) -> KnownCommands:
    data = json.loads(path.read_text())
    allowed = dict(data.get("allowed", {}))
    blocked = dict(data.get("blocked", {}))
    for command, flag in _REVERSIBILITY_FLAGS.items():
        if command in blocked and os.environ.get(flag, "0") == "1":
            allowed[command] = "none"
            del blocked[command]
    return KnownCommands(allowed=allowed, blocked=blocked)


def check_slash_payload(
    payload: str,
    known: KnownCommands,
    extra_allowed: set[str] = frozenset(),
) -> tuple[bool, str, str | None]:
    """
    Returns (is_safe, view, reason). `view` is "none" for anything that
    isn't a slash command at all, or the resolved view type ("none" /
    "readonly") when it is safe. `reason` is set (and is_safe is False)
    for any refusal.
    """
    if not payload.startswith("/"):
        return True, "none", None

    leading = payload.split(" ", 1)[0]

    if leading in known.blocked:
        return False, "blocked", known.blocked[leading]

    if leading in known.allowed:
        return True, known.allowed[leading], None

    if leading in extra_allowed:
        return True, "none", None

    return (
        False,
        "unknown",
        f"'{leading}' is not a verified command for this target -- refusing rather than "
        "guessing what it would do (unverified commands are exactly the case that has "
        "caused real incidents here)",
    )


def is_safe_slash_payload(
    payload: str, known_commands: KnownCommands, extra_allowed: set[str] = frozenset()
) -> bool:
    """Back-compat boolean wrapper around check_slash_payload."""
    is_safe, _view, _reason = check_slash_payload(payload, known_commands, extra_allowed)
    return is_safe
