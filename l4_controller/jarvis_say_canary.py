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

Runs with say_feedback._speak_now() patched out (the one seam the worker
calls per item regardless of which backend -- Kokoro or the `say`
fallback -- would actually produce the audio, 2026-08-20 Kokoro
rewrite), NOT with JARVIS_MUTE: mute skips the call entirely, which
would make every ordering assertion below vacuous. Same discipline as
speech_queue_canary.py, and the same reason it refuses to run muted.

MUST run via the venv that has kokoro-onnx installed (2026-08-20 Kokoro
rewrite) -- say_feedback.py now imports kokoro_tts at module load, which
needs numpy/onnxruntime:

    l4_controller/.venv/bin/python3 l4_controller/jarvis_say_canary.py
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
    print("The _speak_now() patch below is what keeps it silent; mute skips")
    print("that call entirely and makes every ordering check vacuous.")
    sys.exit(2)

# Isolate the return queue BEFORE anything imports it. Added 2026-08-21:
# return_queue's isolation guard moved into _write(), which this canary
# calls directly at section 3 -- so without these it aborts on an
# unisolated write and the whole file stops running. It failed loudly,
# which is the guard working, but a canary nobody can run is a hole
# whatever the reason.
#
# The worker is disabled rather than left alone because section 3 drives
# flush_now() explicitly: a background flusher racing those calls would
# make the "ONE utterance" assertion nondeterministic, and a flaky
# ordering check teaches people to ignore it.
os.environ["JARVIS_NO_RETURN_QUEUE_WORKER"] = "1"
os.environ.setdefault("JARVIS_TEST_RUN", "jarvis-say-canary")

sys.path.insert(0, str(Path(__file__).parent))
import say_feedback as sf  # noqa: E402
import tools_voice  # noqa: E402

failures: list[str] = []


def check(ok: bool, label: str) -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}")
    if not ok:
        failures.append(label)


spoken: list[str] = []


def _fake_speak_now(text):
    # Records service order and takes real time, so a genuinely
    # concurrent pair would be caught rather than looking sequential.
    spoken.append(text)
    time.sleep(0.05)


print("jarvis_say canary\n")
# Patching sf._speak_now (not sf.subprocess.run) means this canary's own
# probe subprocess call in check 4 is untouched by the fake -- unlike the
# old subprocess.run-wide patch, which silently made that probe return a
# fake CompletedProcess with no output too (found live: surfaced as "the
# security check found nothing" instead of "the security check never
# ran" -- a test that sabotages itself and reports a pass is worse than
# no test). No restore-before-check-4 dance needed any more; the seam is
# specific to speech, not global to every subprocess call in the process.
sf._speak_now = _fake_speak_now

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
# REWRITTEN 2026-08-20 for batching (return_queue.py). These two checks
# used to assert that a blocked_question REACHED speak() before a
# completion did. That measurement point no longer exists: completions
# and blocked questions are now collected and spoken as ONE utterance,
# so neither reaches speak() individually and the old checks failed
# against a system that was working correctly.
#
# The PROPERTY is unchanged and still worth asserting -- a stopped
# session must not be buried behind things that merely finished. It now
# lives inside the composed batch, so that is where it is checked. The
# new first assertion is strictly stronger than what it replaces: not
# just "the blocker came first" but "there was only ONE interruption",
# which is the whole point of the feature.
import return_queue  # noqa: E402

spoken.clear()
return_queue._write([])
tools_voice.jarvis_say("gateway finished its tests", kind="completion")
tools_voice.jarvis_say("billing is asking which database to use", kind="blocked_question")
check(len(spoken) == 0, f"neither reached speak() immediately -- both queued (got {spoken})")
batch = return_queue.flush_now()
time.sleep(0.6)
check(len(spoken) == 1, f"the batch is ONE utterance, not two (got {len(spoken)}: {spoken})")
idx_block = batch["text"].find("billing")
idx_done = batch["text"].find("gateway")
check(idx_block != -1 and idx_done != -1, "both messages are in that one utterance")
check(idx_block < idx_done, f"blocked_question leads the batch (text: {batch['text']!r})")

print("\n4. the read-only surface gained speech but NOT the ability to act")
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
