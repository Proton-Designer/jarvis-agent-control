"""
Runtime state (SPEC-TUI.md SS3.5): model warmth, memory, spend --
"ambient, not primary." Two clocks per SS6.1: models_resident + memory
are cheap (a couple hundred ms each), spend is the explicit
expensive/backed-off case (a real /cost round-trip through a READY-gated
pane).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "l4_controller"))
from l2_l3_handoff import DEFAULT_ORCHESTRATOR_TARGET  # noqa: E402
from providers import spend as _spend  # noqa: E402


def resident_models() -> tuple[list[str], str | None]:
    """Returns (models, error). `ollama ps` output, one name per resident
    model -- empty list legitimately means nothing loaded; error is set
    only when the command itself failed (ollama unreachable/not
    installed), so the two cases stay distinguishable per the contract's
    per-section error field."""
    try:
        result = subprocess.run(["ollama", "ps"], capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return [], f"ollama unreachable: {e}"
    if result.returncode != 0:
        return [], f"ollama ps failed: {result.stderr.strip()}"
    lines = [ln for ln in result.stdout.splitlines()[1:] if ln.strip()]  # skip header row
    return [ln.split()[0] for ln in lines], None


def memory_free_pct() -> tuple[float | None, str | None]:
    """memory_pressure's own summary line, not vm_stat's raw free-pages
    count -- vm_stat reads alarming on a healthy Mac (macOS deliberately
    keeps free pages low, using spare RAM for cache/compression), which
    is exactly the kind of number that would misrepresent a fine system
    as one under pressure."""
    try:
        result = subprocess.run(["memory_pressure"], capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return None, f"memory_pressure unreachable: {e}"
    m = re.search(r"System-wide memory free percentage:\s+(\d+)%", result.stdout)
    if not m:
        return None, "memory_pressure output didn't parse -- unexpected format"
    return float(m.group(1)), None


def orchestrator_spend() -> dict:
    """providers.spend()'s existing shape verbatim -- {"ok", "summary",
    "raw"} -- scoped to the orchestrator's own session for v1 (SS3.5's
    "ambient" framing, not a per-team-member breakdown). This is the
    explicit expensive/backed-off call; the poller is responsible for not
    running it on the cheap clock."""
    return _spend(DEFAULT_ORCHESTRATOR_TARGET)
