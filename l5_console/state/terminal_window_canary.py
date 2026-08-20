#!/usr/bin/env python3
"""Canary for terminal_window.py's injection guards.

Flagged by automated security review 2026-08-20 (AppleScript command
injection + argv flag smuggling) and confirmed real, with a worse path
than the review described: session names are not all ours. A team ADOPTED
from existing sessions takes its names from `tmux list-sessions`, and
agents in this system create tmux sessions -- the orchestrator runs with
--dangerously-skip-permissions. So a session named

    x"; do shell script "curl ..."; --

created by a prompt-injected agent, adopted into a team, then given a
window, would have executed as AppleScript: arbitrary code OUTSIDE the
tool cage the engine design exists to maintain. That is escalation from
"an agent can type into a pane" to "an agent can run anything," which is
the bright line SPEC-blockers SS2 draws.

WHY THIS FILE EXISTS RATHER THAN TRUSTING THE FIX: the first version of
the allowlist permitted "-" anywhere, so the session name "-e" passed it
AND OPENED A REAL WINDOW -- the exact argv-smuggling case the guard was
written for. The code even carried a comment asserting a leading dash was
excluded. It was not. A guard is a claim about inputs it has never seen,
and the only way to hold it is to keep feeding it those inputs.

Runs NOTHING: every case asserts a refusal, so no window is ever opened
and no AppleScript ever executes. If a case regressed, the assertion
fails rather than the payload running -- deliberately, so a broken guard
is caught by a red test rather than by whatever the payload does.

    l5_console/app/.venv/bin/python3 l5_console/state/terminal_window_canary.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import terminal_window as tw  # noqa: E402

FAILURES: list[str] = []


def check(desc: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ok    {desc}")
    else:
        FAILURES.append(desc)
        print(f"  FAIL  {desc}{('  -- ' + detail) if detail else ''}")


HOSTILE = [
    ('x"; do shell script "curl evil"; --', "AppleScript injection via embedded quote"),
    ("x`id`", "backtick command substitution"),
    ("x$(id)", "dollar command substitution"),
    ("x;rm -rf /", "shell metacharacter"),
    ("ok\nrm -rf /", "embedded newline -- why the pattern uses \\A/\\Z, not ^/$"),
    ("-e", "argv flag smuggling: the case that slipped past the FIRST version of this guard"),
    ("--args", "long-flag smuggling"),
    ("-", "bare dash"),
    ("a b", "space"),
    (".hidden", "leading dot"),
    ("", "empty"),
]

REAL = ["claude-concierge-5", "jarvis-orchestrator", "claude-gateway-scratch", "team_1.a", "a"]


def run() -> int:
    print("hostile names are REFUSED, and nothing is opened")
    for name, why in HOSTILE:
        r = tw.open_window_for_session(name, app="Terminal")
        check(
            f"refused ({why})",
            r["ok"] is False and r["opened"] is False and "refusing" in r["detail"],
            f"{name!r} -> {r}",
        )

    print()
    print("...and the read path refuses them too, not just the write path")
    for name, why in HOSTILE[:4]:
        check(f"has_attached_client refuses ({why})", tw.has_attached_client(name) is False, name)

    print()
    print("real session names still work -- a guard that blocks everything is not a guard")
    for name in REAL:
        check(f"accepted: {name}", bool(tw._SAFE_SESSION_NAME.match(name)))

    print()
    print("the boundary guards are the RIGHT ones for each boundary")
    src = Path(tw.__file__).read_text()
    # These two assertions used to demand "--" before every session name,
    # and they were WRONG -- my own fix, corrected 2026-08-20 by Engineer
    # 2 and then reproduced directly:
    #
    #   tmux list-clients -t -- dashtest
    #   -> command list-clients: too many arguments (need at most 0)
    #
    # tmux's subcommand parser does not honour "--" as end-of-options.
    # "-t" binds the very NEXT token verbatim, so "-t --" sets the target
    # to the literal string "--" and the real name becomes a stray
    # positional. So the separator I added as a hardening measure
    # silently broke has_attached_client(): it began failing on every
    # call, meaning "is a window already open" answered no forever, and
    # window REUSE stopped working -- the security fix quietly broke the
    # feature.
    #
    # The correct rule is per-boundary, not one blanket habit:
    #   - tmux via subprocess list form: no shell, no re-parse. The
    #     allowlist (first char alnum/underscore) is what makes a
    #     dash-leading name impossible, and it is sufficient.
    #   - AppleScript: a real string-building boundary, so the name goes
    #     in as argv and AppleScript quotes it.
    check("the tmux calls do NOT pass -- (tmux would read it as the target)",
          '"list-clients", "-t", tmux_session' in src and '"-t", "--"' not in src)
    check("AppleScript takes the name as argv, never string-interpolated",
          "quoted form of (item 1 of argv)" in src
          and 'do script "tmux attach -t {' not in src)
    check("a dash-leading name is still impossible, which is what -- was for",
          not tw._SAFE_SESSION_NAME.match("-e") and not tw._SAFE_SESSION_NAME.match("--args"))

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"all {len(HOSTILE) + 4 + len(REAL) + 3} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(run())
