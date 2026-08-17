"""
Client for L1's cancel-window listener, per the contract agreed with the
L1/L2 owner: a unix domain socket at ~/.jarvis/l1.sock. Request:
{"cmd": "listen_for_cancel", "timeout_s": <float>}. Response:
{"cancelled": <bool>} once the word is heard or the window closes,
whichever is first.

L1 owns the socket; it may not exist yet (built independently, in
parallel). If it's not there or the call fails for any reason, this
degrades to "no cancel heard" rather than raising — a missing L1 must
never turn into a crash on L4's side, and per the loud-refusal principle
elsewhere in this codebase, the caller is responsible for speaking that
the cancel window didn't actually run if that matters to them.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

DEFAULT_SOCKET_PATH = Path.home() / ".jarvis" / "l1.sock"


def listen_for_cancel(timeout_s: float, socket_path: Path = DEFAULT_SOCKET_PATH) -> bool:
    if not socket_path.exists():
        return False
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
            return bool(response.get("cancelled", False))
    except (OSError, json.JSONDecodeError, socket.timeout):
        return False
