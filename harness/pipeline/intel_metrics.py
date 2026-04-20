"""Intelligence metrics — per-round probe orchestration.

Runs the evaluator-calibration probe at the end of each outer round and
surfaces a Spearman ρ trajectory that downstream prompts (framework_improvement
specifically) can consume to know whether evaluator changes moved the needle.

Design notes:
  * The probe lives at benchmarks/evaluator_calibration/run_probe.py as a
    standalone subprocess. Running it in-process would couple this module
    to the probe's (heavy) LLM imports and force a single event loop.
  * We verify the probe's SHA-256 before each run (anti-tamper) so a
    self-improving LLM cannot silently modify the benchmark to inflate ρ.
  * All probe artifacts live under benchmarks/ which is excluded from every
    phase's allowed_edit_globs — the write-scope gate in Tool._check_phase_scope
    blocks mutations from the executor LLM.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
from pathlib import Path

from harness.core.config import HarnessConfig

log = logging.getLogger(__name__)

# Paths are resolved relative to the harness workspace at call time, not at
# import time — the workspace may differ between dev and deployment.
_BENCH_SUBPATH = Path("benchmarks") / "evaluator_calibration"
_PROBE_SCRIPT = _BENCH_SUBPATH / "run_probe.py"
_PROPOSALS = _BENCH_SUBPATH / "proposals.jsonl"
_HASH_FILE = _BENCH_SUBPATH / ".sha256"
_RESULTS = _BENCH_SUBPATH / "probe_results.jsonl"

# How long to wait for the probe subprocess before giving up. 40 API calls
# × ~5s latency = 200s worst case; 360s gives 1.8× headroom for cold starts.
_PROBE_TIMEOUT_S = 360


def _compute_hash(workspace: str) -> str:
    """SHA-256 of (proposals.jsonl || run_probe.py) — covers both the data
    and the logic. Any tamper changes the digest."""
    h = hashlib.sha256()
    for rel in (_PROPOSALS, _PROBE_SCRIPT):
        path = Path(workspace) / rel
        try:
            h.update(path.read_bytes())
        except FileNotFoundError:
            return ""  # caller will detect missing files via verify_hash
    return h.hexdigest()


def verify_hash(workspace: str) -> tuple[bool, str]:
    """Return (ok, reason).

    ok=True when the on-disk benchmark matches the pinned hash in .sha256.
    ok=False with a human-readable reason otherwise — caller should log
    WARNING and skip the probe run rather than crash the pipeline.

    Missing .sha256 is treated as ok=True with a "no pin yet" reason so
    first-time runs on a fresh checkout don't break. The pinner (update_hash)
    writes it after a successful baseline probe.
    """
    pin_path = Path(workspace) / _HASH_FILE
    if not pin_path.exists():
        return True, "no hash pin yet — treated as first-run baseline"

    try:
        pinned = pin_path.read_text(encoding="utf-8").strip().split()[0]
    except (OSError, IndexError):
        return False, f"{_HASH_FILE} unreadable or empty"

    current = _compute_hash(workspace)
    if not current:
        return False, "benchmark files missing"
    if current != pinned:
        return False, f"hash mismatch: on-disk={current[:12]}… pinned={pinned[:12]}…"
    return True, "hash matches"


def update_hash(workspace: str) -> str:
    """Write current hash to .sha256. Call once from a trusted baseline
    (e.g. right after human curation). Returns the digest written."""
    digest = _compute_hash(workspace)
    if not digest:
        raise FileNotFoundError("cannot hash — benchmark files missing")
    pin_path = Path(workspace) / _HASH_FILE
    pin_path.write_text(digest + "  benchmarks/evaluator_calibration\n", encoding="utf-8")
    return digest


async def run_eval_probe(
    config: HarnessConfig, pipeline_config_path: str | None = None,
) -> dict | None:
    """Invoke the probe subprocess and return its parsed summary.

    Returns None when the probe cannot run — the pipeline treats None as
    "skip intelligence metric for this round" rather than an error.
    """
    workspace = config.workspace
    script = Path(workspace) / _PROBE_SCRIPT
    if not script.exists():
        log.info("intel_probe: %s absent — skipping (benchmark not installed)", script)
        return None

    ok, reason = verify_hash(workspace)
    if not ok:
        log.warning("intel_probe: hash check failed (%s) — skipping", reason)
        return None

    # Compute hash to pass to subprocess for dual-layer verification
    current_hash = _compute_hash(workspace)
    if not current_hash:
        log.warning("intel_probe: cannot compute hash — skipping")
        return None

    argv = [sys.executable, str(script)]
    if pipeline_config_path:
        # Add validation as per Round 1, item #3
        config_path = Path(pipeline_config_path)
        workspace_path = Path(workspace)
        try:
            config_path.resolve().relative_to(workspace_path.resolve())
        except ValueError:
            log.warning("intel_probe: config path outside workspace — skipping")
            return None
        if config_path.suffix != '.json':
            log.warning("intel_probe: config must be JSON file — skipping")
            return None
        argv.append(str(config_path))

    # Pass hash via environment variable for subprocess self-verification
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "HARNESS_PROBE_HASH": current_hash}
    
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workspace,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_PROBE_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        if proc is not None:
            proc.kill()
            await proc.wait()
        log.warning("intel_probe: timed out after %ds — skipping", _PROBE_TIMEOUT_S)
        return None
    except Exception as exc:
        log.warning("intel_probe: subprocess error: %s", exc)
        return None

    if proc.returncode != 0:
        err = (stderr or b"").decode(errors="replace")[:500]
        log.warning("intel_probe: non-zero exit %d: %s", proc.returncode, err)
        return None

    # The probe appended one compact JSON line to probe_results.jsonl; read it.
    results_path = Path(workspace) / _RESULTS
    try:
        with open(results_path, encoding="utf-8") as f:
            last = None
            for line in f:
                line = line.strip()
                if line:
                    last = line
        if last is None:
            return None
        return json.loads(last)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("intel_probe: could not read results: %s", exc)
        return None


def format_trajectory(
    latest_results_path: str, max_points: int = 8,
) -> dict:
    """Return a summary dict for the last N probe results — suitable for
    injecting into prompts or memory entries.

    Schema: {"trajectory": [ρ, ρ, ρ, ...], "current": ρ, "delta": Δ,
             "target": 0.85, "regressions_in_last_5": int}
    """
    path = Path(latest_results_path)
    if not path.exists():
        return {"trajectory": [], "current": None, "delta": None, "target": 0.85,
                "regressions_in_last_5": 0}
    rows: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    if not rows:
        return {"trajectory": [], "current": None, "delta": None, "target": 0.85,
                "regressions_in_last_5": 0}
    trajectory = [r.get("rho", 0.0) for r in rows[-max_points:]]
    current = trajectory[-1] if trajectory else None
    delta = (
        round(trajectory[-1] - trajectory[-2], 4)
        if len(trajectory) >= 2 else None
    )
    # Count rounds in last 5 where delta < -0.05
    regressions = 0
    window = trajectory[-6:]
    for i in range(1, len(window)):
        if window[i] - window[i - 1] < -0.05:
            regressions += 1
    return {
        "trajectory": trajectory,
        "current": current,
        "delta": delta,
        "target": 0.85,
        "regressions_in_last_5": regressions,
    }
