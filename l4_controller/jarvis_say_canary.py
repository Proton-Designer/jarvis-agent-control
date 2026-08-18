#!/usr/bin/env python3
"""Canary for jarvis_say() -- the return channel.

Guards four properties, in the order that matters:

1. AN UNKNOWN KIND IS REFUSED, never silently downgraded. This is the
   safety property. A tool that quietly treats a misspelled "blocked" as
   normal priority would put a STOPPED session behind three completions,
   which is precisely the failure this channel exists to prevent.
2. Priority actually reorders. A blocked_question submitted AFTER two
   completions is spoken FIRST. Asserted against say_log's service order,
   not against call order -- the log records when the worker processed an
   item, which is the only evidence that reordering really happened
   rather than the calls happening to arrive in the right order already.
3. An empty message is refused rather than producing a silent success.
4. The read-only surface still has no write modules in its import graph
   with jarvis_say added -- i.e. adding a speaking tool to the concierge
   did not smuggle in the ability to act.

Runs with the `say` subprocess patched out, NOT with JARVIS_MUTE: mute
skips the call entirely, which would make every ordering assertion below
vacuous. Same discipline as speech_queue_canary.py, and the same reason
it refuses to run muted.

    python3 l4_controller/jarvis_say_canary.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

if os.environ.get("JARVIS_MUTE") == "1":
    print("REFUSING: do not run this canary with JARVIS_MUTE=1.")
    print("The subprocess patch below is what keeps it silent; mute skips")
    print("the `say` call entirely and makes every ordering check vacuous.")
    sys.exit(2)

sys.path.insert(0, str(Path(__file__).parent))
import say_feedback as sf  # noqa: E402
import tools_voice  # noqa: E402

failures: list[str] = []


def check(ok: bool, label: str) -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}")
    if not ok:
        failures.append(label)


spoken: list[str] = []


def _fake_say(argv, **kwargs):
    # Records service order and takes real time, so a genuinely
    # concurrent pair would be caught rather than looking sequential.
    spoken.append(argv[-1])
    time.sleep(0.05)
    return subprocess.CompletedProcess(argv, 0)


print("jarvis_say canary\n")
# `sf.subprocess` IS the global subprocess module object, so assigning to
# sf.subprocess.run patches subprocess.run for EVERY module in this
# process -- including this canary's own probe call in check 4. That is
# not a hypothetical: it silently made the probe return a fake
# CompletedProcess with no output, which surfaced as "the security check
# found nothing" instead of "the security check never ran." A test that
# sabotages itself and reports a pass is worse than no test.
# Saved here and restored before check 4 runs.
_real_subprocess_run = sf.subprocess.run
sf.subprocess.run = _fake_say

print("1. an unknown kind is REFUSED, never silently downgraded")
r = tools_voice.jarvis_say("something happened", kind="blocked")  # near-miss of blocked_question
check(r["ok"] is False, "unknown kind 'blocked' refused")
check("blocked_question" in r["reason"], "the refusal names the valid kinds")
before = len(spoken)
time.sleep(0.2)
check(len(spoken) == before, "a refused call speaks nothing at all")

print("\n2. an empty message is refused, not silently succeeded")
check(tools_voice.jarvis_say("   ", kind="completion")["ok"] is False, "whitespace-only message refused")

print("\n3. priority reorders: a blocker submitted LAST is spoken FIRST")
spoken.clear()
tools_voice.jarvis_say("filler to occupy the worker", kind="completion")
time.sleep(0.02)  # let the worker pick up the filler so the next two queue together
tools_voice.jarvis_say("gateway finished its tests", kind="completion")
tools_voice.jarvis_say("billing is asking which database to use", kind="blocked_question")
time.sleep(0.6)
idx_block = next((i for i, s in enumerate(spoken) if "billing" in s), -1)
idx_done = next((i for i, s in enumerate(spoken) if "gateway" in s), -1)
check(idx_block != -1 and idx_done != -1, "both messages were spoken")
check(idx_block < idx_done, f"blocked_question preempted completion (order: {spoken})")

print("\n4. the read-only surface gained speech but NOT the ability to act")
sf.subprocess.run = _real_subprocess_run  # see the note at the patch site
probe = (
    "import sys, server_readonly, asyncio;"
    "t=sorted(x.name for x in asyncio.run(server_readonly.app.list_tools()));"
    "bad=[m for m in ('tools_write','dispatch_state') if m in sys.modules];"
    # To STDERR, not stdout: importing an MCP server redirects sys.stdout
    # to keep the stdio protocol stream clean, so a print() here vanishes
    # and the probe returns rc=0 with no output -- which reads as "the
    # check passed and found nothing" rather than "the check never ran."
    # Exactly the silence-as-success shape this project keeps finding, in
    # a test whose whole job is proving a security boundary.
    "import sys as _s;"
    "_s.stderr.write(__import__('json').dumps({'tools': t, 'write_modules': bad}))"
)
# The VENV interpreter explicitly, not sys.executable: this canary is
# runnable under system python (it only needs say_feedback), but the MCP
# servers are not -- `mcp` lives in l4_controller/.venv. Using
# sys.executable here made the probe fail with an import error whose
# stdout was empty, which surfaced as a confusing AttributeError instead
# of a clear "wrong interpreter".
here = Path(__file__).parent
venv_py = here / ".venv" / "bin" / "python3"
if not venv_py.exists():
    print(f"  FAIL  cannot find {venv_py} -- the MCP surface check needs the venv interpreter")
    failures.append("venv interpreter missing")
    out = None
else:
    out = subprocess.run(
        [str(venv_py), "-c", probe], capture_output=True, text=True,
        cwd=str(here), env={**os.environ, "PYTHONPATH": str(here)},
    )
    if out.returncode != 0 or not (out.stderr or "").strip():
        print(f"  FAIL  probe did not run: rc={out.returncode} stdout={(out.stdout or '')[-200:]}")
        failures.append("MCP surface probe failed to execute")
        out = None
data = json.loads(out.stderr.strip().splitlines()[-1]) if out else {"tools": [], "write_modules": ["<probe did not run>"]}
check("jarvis_say" in data["tools"], "jarvis_say IS on the read-only surface")
check("deliver_batch" not in data["tools"], "deliver_batch is still absent from it")
check(data["write_modules"] == [], "no write module entered the import graph")

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("all checks passed")
