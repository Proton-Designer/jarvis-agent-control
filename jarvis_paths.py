"""
Shared runtime-path resolution for Jarvis. Two functions -- jarvis_home()
and jarvis_project_home() -- that every file currently hardcoding
`Path.home() / ".jarvis"` or `Path.home() / "Jarvis"` for a runtime path
constant should call instead.

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
the real paths, zero risk to anything already relying on them.

jarvis_project_home() ADDED 2026-08-18 after a second, more serious
collision: a canary (team_actions_canary.py) backed up ~/Jarvis/teams.json,
replaced it with test data mid-run, and restored it in a `finally` block
-- a real registration (the Lead's, mid a live end-to-end voice test) was
read as EMPTY during that window, and the restore's own correctness
depended entirely on nothing else writing the file between the backup and
the restore. It happened to restore correctly this time; it did not have
to. Backup-and-restore is a promise a canary keeps only if nothing else
touches the file meanwhile and the canary itself never crashes before its
`finally` runs -- a redirected path is a guarantee that holds even if the
canary is killed outright. Every module that persists to ~/Jarvis
(teams.json, engine.json, the context/ directory) must resolve its path
through jarvis_project_home(), never hardcode `Path.home() / "Jarvis"`
directly, or this protection is opt-in per file instead of structural --
exactly the gap that let this collision happen (TEAMS_REGISTRY_PATH
predated this function and nothing forced a canary to isolate it).
"""
from __future__ import annotations

import os
from pathlib import Path


def jarvis_home() -> Path:
    test_run = os.environ.get("JARVIS_TEST_RUN")
    base = Path.home() / ".jarvis"
    return (base / "test_runs" / test_run) if test_run else base


def jarvis_project_home() -> Path:
    """~/Jarvis -- the persistent, human-meaningful config/registry
    directory (teams.json, engine.json, context/, and each Engine role's
    working subdirectory), distinct from jarvis_home()'s ~/.jarvis
    ephemeral runtime artifacts. Same JARVIS_TEST_RUN isolation, same
    reasoning, applied to the tree that actually got clobbered."""
    test_run = os.environ.get("JARVIS_TEST_RUN")
    base = Path.home() / "Jarvis"
    return (base / "test_runs" / test_run) if test_run else base
