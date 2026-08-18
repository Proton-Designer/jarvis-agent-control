"""
Public entry point for l5_console/app/ (the Textual console). This is the
whole boundary agreed with ue6rruxg -- see models.py for the contract
shape and poller.py for why get_state() is cheap/side-effect-free.

Flat sys.path import, matching this codebase's established convention
(l2_5_concierge/concierge.py, l4_controller's own modules, etc. -- bare
module names via sys.path.insert, not package-relative imports) rather
than mixing two different import styles in one project:

    import sys
    sys.path.insert(0, ".../l5_console/state")
    from api import start, get_state

    start()  # once, at app startup -- launches the background poller threads
    ...
    current = get_state()  # anywhere, anytime, on every render -- instant
"""

from __future__ import annotations

from models import (  # noqa: F401 -- re-exported for consumers
    JarvisState,
    LIVENESS_LOST,
    LIVENESS_RUNNING,
    LIVENESS_STOPPED,
    OrchestratorState,
    RuntimeState,
    Team,
    TeamMember,
    UnassignedSession,
    WakeDaemonState,
)
from poller import Poller

_poller: Poller | None = None


def start() -> None:
    """Idempotent -- calling twice does not spawn a second set of
    threads. Call once at console startup."""
    global _poller
    if _poller is not None:
        return
    _poller = Poller()
    _poller.start()


def get_state() -> JarvisState:
    """Cheap, side-effect-free, safe on every render -- see poller.py's
    module docstring for why this must never do real work. Calling this
    before start() raises rather than silently returning a placeholder,
    since that's a real programming error in the caller, not a runtime
    condition to paper over."""
    if _poller is None:
        raise RuntimeError("state.start() must be called before state.get_state()")
    return _poller.get_state()


def stop() -> None:
    global _poller
    if _poller is not None:
        _poller.stop()
        _poller = None
