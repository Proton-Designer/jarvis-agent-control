"""
Shared runtime-path resolution for Jarvis. One function, jarvis_home(),
that every file currently hardcoding `Path.home() / ".jarvis"` for a
runtime path constant should call instead.

Exists because of a real collision (2026-08-17): two agents on the same
machine, testing independently, both wrote to
~/.jarvis/latency_log.jsonl, ~/.jarvis/dictations/, and
~/.jarvis/dispatch_state.json, and one truncated the shared log mid-run
of the other's test.

JARVIS_TEST_RUN isolates BOTH prod-vs-test AND engineer-vs-engineer --
set it to a value unique per test session (a name, a timestamp, a random
suffix), not a fixed flag like "1". A fixed flag only separates test
from prod; the collision that actually happened was test-vs-test between
two engineers, which a fixed flag would not have prevented.

Unset (the default): identical behavior to before this module existed --
the real ~/.jarvis path, zero risk to anything already relying on it.
"""
from __future__ import annotations

import os
from pathlib import Path


def jarvis_home() -> Path:
    test_run = os.environ.get("JARVIS_TEST_RUN")
    base = Path.home() / ".jarvis"
    return (base / "test_runs" / test_run) if test_run else base
