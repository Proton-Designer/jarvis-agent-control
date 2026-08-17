"""
Client for L1's cancel-window listener, per the contract agreed with the
L1/L2 owner: a unix domain socket at ~/.jarvis/l1.sock. Request:
{"cmd": "listen_for_cancel", "timeout_s": <float>}. Response:
{"cancelled": <bool>} once the word is heard or the window closes,
whichever is first.

FAILS CLOSED, not open. An earlier version of this returned a bare bool and
degraded a missing/broken socket to `False` ("not cancelled") — which reads
as a neutral default but is actually the PERMISSIVE one: "not cancelled"
means delivery proceeds. Given target sessions run in auto mode (no
tool-permission prompts at all), the cancel window is the entire
human-in-the-loop control left in the system. A safety control whose
failure mode is "silently stop existing" is worse than no control, because
Ayman calibrates his trust to a window that isn't actually there.

So: `available=False` is a distinct, explicit outcome from `cancelled=False`
and callers MUST branch on it — speak that the window wasn't real, and
refuse delivery to anything that isn't an explicitly-marked throwaway test
target (see registry.py's test-target allowlist). Convenience features fail
open and quietly; safety controls fail closed and audibly. This is a safety
control.
"""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SOCKET_PATH = Path.home() / ".jarvis" / "l1.sock"


@dataclass
class CancelResult:
    cancelled: bool
    available: bool  # False if the socket was missing or the call failed


def cancel_socket_available(socket_path: Path = DEFAULT_SOCKET_PATH) -> bool:
    return socket_path.exists()


def listen_for_cancel(timeout_s: float, socket_path: Path = DEFAULT_SOCKET_PATH) -> CancelResult:
    if not socket_path.exists():
        return CancelResult(cancelled=False, available=False)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout_s + 1.0)  # give L1 the full window plus round-trip slack
            sock.connect(str(socket_path))
            sock.sendall(json.dumps({"cmd": "listen_for_cancel", "timeout_s": timeout_s}).encode())
            sock.shutdown(socket.SHUT_WR)
            data = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
            response = json.loads(data.decode())
            return CancelResult(cancelled=bool(response.get("cancelled", False)), available=True)
    except (OSError, json.JSONDecodeError, socket.timeout):
        return CancelResult(cancelled=False, available=False)
