"""
Visible terminal windows per team (SPEC-gaps-and-build-plan.md SS1.6,
Ayman's requirement, 2026-08-20): project agents are visible by default,
one window per team/directory, and the concierge/orchestrator stay
invisible always. This module is the PLATFORM layer only -- open, or
confirm-already-open, a window for ONE tmux session. Which session
represents a team, and whether that team wants a window at all
(Team.visible), is teams.py/team_actions.py's job -- kept separate so
engine_roles.py can structurally never reach this module (nothing there
imports it), rather than relying on a convention not to call it.

REUSE, APP-AGNOSTIC: `tmux list-clients -t <session>` is ground truth for
"is ANY terminal window/tab anywhere currently attached to this session,"
regardless of which app opened it -- Terminal.app, iTerm, Ghostty, or a
human's own `tmux attach` all show up as an ordinary tmux client. Checking
this instead of enumerating each app's own windows by title sidesteps a
much harder, app-specific problem (GUI window enumeration via AppleScript
System Events, which is real automation and commonly prompts for
Accessibility access) in favor of one plain subprocess call with no new
permission surface at all.

DETECTION, NOT ASSUMPTION: measured live 2026-08-20 on this machine --
Ghostty is the terminal actually running (`ps aux`, `$TERM_PROGRAM`),
Terminal.app is also installed and running, and iTerm is NOT installed
(`osascript -e 'exists application "iTerm"'` -> false). The build
instruction named "Terminal.app or iTerm"; neither is what's actually in
daily use here, which is exactly why this module detects instead of
hardcoding one. Ghostty needs a different mechanism than the other two:
it has no AppleScript "do script" dictionary the way Terminal.app/iTerm2
do (`ghostty --help` documents no such action), but it does support
`open -na Ghostty.app --args -e <command...>` to launch a new window
running a specific command -- verified live: opens a real window
attached to a real tmux session, with NO permission dialog at all.
Terminal.app/iTerm fall back to the standard osascript `do script`
pattern, which commonly prompts for a one-time Automation permission the
first time it runs on a machine -- that prompt is Ayman's call to grant,
per the Lead's explicit instruction not to route around it, so this
module does not attempt to detect or suppress it.

VERIFIED LIVE, 2026-08-20 (scratch tmux session, `visible_windows_canary.py`
covers this as a standing assertion): opened a Ghostty window attached to
a real session, confirmed via list-clients; killed the Ghostty process
outright (simulating the user closing the window); the tmux session and
the process inside it were still alive and unaffected. That is tmux's own
default behavior (no `destroy-unattached`, no `-A`/exit-on-detach anywhere
in this codebase -- checked) rather than anything this module has to
implement: a window is a view, not the process, by construction of how
the session was created.
"""

from __future__ import annotations

import subprocess

# Order matters: prefer whichever is confirmed actually in use, over
# merely installed -- a running instance reflects what Ayman actually has
# open today. iTerm ranks above Terminal.app in the "installed" fallback
# because when someone has bothered to install it, it is normally the
# deliberate choice over the OS default; Terminal.app is the final
# fallback since macOS always has it.
_CANDIDATE_APPS = ["Ghostty", "iTerm", "Terminal"]

_OSASCRIPT_TIMEOUT_S = 5.0
_OPEN_TIMEOUT_S = 10.0


def _app_installed(name: str) -> bool:
    try:
        result = subprocess.run(
            ["osascript", "-e", f'exists application "{name}"'],
            capture_output=True, text=True, timeout=_OSASCRIPT_TIMEOUT_S,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"
    except (subprocess.TimeoutExpired, OSError):
        return False


def _app_running(name: str) -> bool:
    try:
        result = subprocess.run(
            ["osascript", "-e", f'application "{name}" is running'],
            capture_output=True, text=True, timeout=_OSASCRIPT_TIMEOUT_S,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"
    except (subprocess.TimeoutExpired, OSError):
        return False


def detect_terminal_app() -> str | None:
    """"Ghostty" | "iTerm" | "Terminal" | None (nothing detected -- e.g.
    not on macOS, or osascript itself is unavailable). Checked fresh on
    every call, deliberately not cached: this is a cheap, infrequent
    operation (once per window-open, not once per poll), and caching a
    "which app is running" answer is exactly the kind of intent-as-truth
    this project's own governing rule (SPEC-TUI.md SS6.2) already refuses
    to do for liveness."""
    installed = [a for a in _CANDIDATE_APPS if _app_installed(a)]
    running = [a for a in installed if _app_running(a)]
    if running:
        return running[0]
    if installed:
        return installed[0]
    return None


def has_attached_client(tmux_session: str) -> bool:
    """Ground truth for "is a window already open for this session" --
    see module docstring. Empty stdout (no error) means no client, not a
    failure -- tmux's own documented behavior for list-clients on a
    session with nothing attached."""
    result = subprocess.run(
        ["tmux", "list-clients", "-t", tmux_session],
        capture_output=True, text=True,
    )
    return bool(result.stdout.strip())


def _open_ghostty(tmux_session: str) -> dict:
    # Args passed as separate list elements, NOT one joined string --
    # found live 2026-08-20 that `-e "tmux attach -t X"` (one string)
    # makes Ghostty try to execute a single binary literally named
    # "tmux attach -t X" and silently fail to attach anything, where
    # `-e tmux attach -t X` (four separate argv entries after -e) is what
    # actually works. `open -na`, not `open -a`: -n forces a NEW
    # instance/window every time rather than reusing Ghostty's existing
    # window (which would just retarget the window this very console
    # might be running in) -- reuse is handled at a higher level, by
    # has_attached_client(), not by letting `open` decide.
    try:
        result = subprocess.run(
            ["open", "-na", "Ghostty.app", "--args", "-e", "tmux", "attach", "-t", tmux_session],
            capture_output=True, text=True, timeout=_OPEN_TIMEOUT_S,
        )
        if result.returncode != 0:
            return {"ok": False, "detail": f"Ghostty refused to open a window: {result.stderr.strip()}"}
        return {"ok": True, "detail": f"opened a Ghostty window for {tmux_session!r}"}
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"ok": False, "detail": f"couldn't open a Ghostty window for {tmux_session!r}: {e}"}


def _open_via_applescript(app: str, tmux_session: str) -> dict:
    # Terminal.app and iTerm both understand a plain "attach a running
    # command in a new window," but the exact verb differs -- this is the
    # standard, widely-used pattern for each, not a novel approach.
    # UNVERIFIED LIVE: iTerm isn't installed on this machine (see module
    # docstring); this branch is written to the documented AppleScript
    # dictionary shape but has not been driven against a real iTerm.
    if app == "Terminal":
        script = f'tell application "Terminal" to do script "tmux attach -t {tmux_session}"'
    else:
        script = f'tell application "{app}" to create window with default profile command "tmux attach -t {tmux_session}"'
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=_OPEN_TIMEOUT_S,
        )
        if result.returncode != 0:
            return {"ok": False, "detail": f"{app} refused to open a window: {result.stderr.strip()}"}
        return {"ok": True, "detail": f"opened a {app} window for {tmux_session!r}"}
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"ok": False, "detail": f"couldn't open a {app} window for {tmux_session!r}: {e}"}


def open_window_for_session(tmux_session: str, app: str | None = None) -> dict:
    """Opens a terminal window attached to `tmux_session`, UNLESS one is
    already attached -- "reuse an already-open window rather than
    stacking duplicates" (SPEC-gaps-and-build-plan.md SS1.6). Returns
    {"ok", "opened", "detail"} -- opened=False on the reuse path is not a
    failure and callers should not report it as one; ok=False means the
    open genuinely failed (no supported app, or the app refused).

    `app`: override for testing/callers that already know which app to
    use; production callers should leave this None and let
    detect_terminal_app() decide."""
    if has_attached_client(tmux_session):
        return {"ok": True, "opened": False, "detail": f"{tmux_session!r} already has a window open"}

    app = app or detect_terminal_app()
    if app is None:
        return {"ok": False, "opened": False, "detail": "no supported terminal app detected on this machine"}

    result = _open_ghostty(tmux_session) if app == "Ghostty" else _open_via_applescript(app, tmux_session)
    result["opened"] = result["ok"]
    return result
