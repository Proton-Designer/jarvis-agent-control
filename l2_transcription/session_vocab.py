#!/usr/bin/env python3
"""Build the Whisper --prompt vocabulary string from live tmux sessions.

Deliberately no hardcoded project names anywhere (per the pivot) -- this
re-enumerates tmux on every call so it can be called once per chunk and
stay current even if a session appears mid-dictation.

This is NOT the friendly-name mapping L4 uses for routing (that's
registry.py's job, owned by L4). This only needs textual tokens good
enough to bias Whisper's decoder -- a lightly-cleaned session name is
sufficient for that, no need to duplicate L4's mapping logic.
"""
import subprocess

ALWAYS_INCLUDE = ["Jarvis"]


def live_session_names() -> list[str]:
    try:
        out = subprocess.run(
            ["tmux", "list-sessions", "-F", "#S"],
            capture_output=True, text=True, timeout=2,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    if out.returncode != 0:
        return []  # no server running / no sessions -- not an error
    return [line for line in out.stdout.splitlines() if line.strip()]


def _clean(session_name: str) -> str:
    name = session_name
    if name.startswith("claude-"):
        name = name[len("claude-"):]
    if name.endswith("-test"):
        name = name[: -len("-test")]
    return name.replace("-", " ").replace("_", " ").title()


def build_prompt() -> str:
    names = [_clean(s) for s in live_session_names()]
    return ", ".join(dict.fromkeys(names + ALWAYS_INCLUDE))  # dedupe, keep order


if __name__ == "__main__":
    print(build_prompt())
