"""Intelligence-metric infrastructure for Harness-Everything.

This module is imported lazily by both ``pipeline_loop.py`` (to run the
eval probe after each round) and ``phase_runner.py`` (to build the
``intel_metric_block`` that appears in the framework_improvement executor
prompt).  Both callers wrap the import in a broad ``except`` so any
failure here is non-fatal.

Public API
----------
format_trajectory(path: str) -> dict
    Read *path* (a JSONL file of probe results) and return a summary dict
    with keys ``current``, ``delta``, ``trajectory``, and
    ``regressions_in_last_5``.

run_eval_probe(harness_config, pipeline_config_path: str | None = None)
    -> dict | None
    Run an evaluator-discrimination probe against the benchmark proposals
    found in ``{workspace}/benchmarks/evaluator_calibration/proposals/``.
    Returns ``None`` when no benchmarks are present or when the venv lacks
    the required dependencies, so that callers can skip gracefully.
"""

from __future__ import annotations

import json
import logging
import pathlib
import time
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Spearman ρ (pure-stdlib implementation — no scipy dependency)
# ---------------------------------------------------------------------------


def _rank(values: list[float]) -> list[float]:
    """Return dense average ranks (1-based) for *values*."""
    n = len(values)
    if n == 0:
        return []
    sorted_with_idx = sorted(enumerate(values), key=lambda x: x[1])
    ranks: list[float] = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j < n - 1 and sorted_with_idx[j + 1][1] == sorted_with_idx[i][1]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1  # 1-based
        for k in range(i, j + 1):
            ranks[sorted_with_idx[k][0]] = avg_rank
        i = j + 1
    return ranks


def spearman_rho(x: list[float], y: list[float]) -> float | None:
    """Compute Spearman rank-correlation coefficient for *x* and *y*.

    Returns ``None`` when the inputs are too short or constant.
    """
    if len(x) != len(y) or len(x) < 2:  # noqa: PLR2004
        return None
    rx = _rank(list(x))
    ry = _rank(list(y))
    n = len(rx)
    mx = sum(rx) / n
    my = sum(ry) / n
    cov = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    sx = (sum((r - mx) ** 2 for r in rx)) ** 0.5
    sy = (sum((r - my) ** 2 for r in ry)) ** 0.5
    if sx == 0 or sy == 0:
        return None
    return cov / (sx * sy)


# ---------------------------------------------------------------------------
# format_trajectory
# ---------------------------------------------------------------------------

_TRAJ_MAX = 20  # keep only the most recent N probe records in the trajectory


def format_trajectory(path: str) -> dict[str, Any]:
    """Read a probe-results JSONL file at *path* and return a summary.

    Each line in the JSONL file must be a JSON object containing at least
    one of the keys ``rho``, ``rho_basic``, or ``rho_diffusion``.  The
    combined ``rho`` key (average of basic + diffusion) is preferred; if
    absent, the mean of whichever sub-scores are present is used.

    Return value keys
    -----------------
    ``current``
        Most-recent combined rho, or ``None`` when the file is absent /
        empty / unparseable.
    ``delta``
        Difference between the last two rounds, or ``None`` when < 2 rows.
    ``trajectory``
        List of up to ``_TRAJ_MAX`` most-recent rho values (oldest first).
    ``regressions_in_last_5``
        Number of rounds in the last five where rho decreased vs the
        previous round.
    """
    p = pathlib.Path(path)
    if not p.exists():
        return {"current": None, "delta": None, "trajectory": [], "regressions_in_last_5": 0}

    rows: list[float] = []
    try:
        for raw in p.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            rho = _extract_rho(obj)
            if rho is not None:
                rows.append(rho)
    except OSError as exc:
        log.warning("intel_metrics: cannot read %s: %s", path, exc)
        return {"current": None, "delta": None, "trajectory": [], "regressions_in_last_5": 0}

    if not rows:
        return {"current": None, "delta": None, "trajectory": [], "regressions_in_last_5": 0}

    recent = rows[-_TRAJ_MAX:]
    current = recent[-1]
    delta = (recent[-1] - recent[-2]) if len(recent) >= 2 else None  # noqa: PLR2004
    last5 = recent[-6:]  # up to 6 entries so we can compare 5 consecutive pairs
    regressions = sum(1 for i in range(1, len(last5)) if last5[i] < last5[i - 1])
    return {
        "current": current,
        "delta": delta,
        "trajectory": recent,
        "regressions_in_last_5": regressions,
    }


def _extract_rho(obj: dict[str, Any]) -> float | None:
    """Extract a combined Spearman-ρ value from a probe-result JSON object."""
    if "rho" in obj:
        v = obj["rho"]
        if isinstance(v, (int, float)):
            return float(v)
    # Fall back to average of sub-scores.
    subs: list[float] = []
    for key in ("rho_basic", "rho_diffusion"):
        v = obj.get(key)
        if isinstance(v, (int, float)):
            subs.append(float(v))
    if subs:
        return sum(subs) / len(subs)
    return None


# ---------------------------------------------------------------------------
# run_eval_probe
# ---------------------------------------------------------------------------

#: Expected file that holds ground-truth rankings for the benchmark proposals.
_GROUND_TRUTH_FILENAME = "ground_truth.json"
#: Directory under workspace that holds proposal subdirectories.
_PROPOSALS_SUBDIR = "benchmarks/evaluator_calibration/proposals"
#: JSONL file where probe results are appended.
_PROBE_RESULTS_FILENAME = "benchmarks/evaluator_calibration/probe_results.jsonl"


async def run_eval_probe(
    harness_config: Any,
    pipeline_config_path: str | None = None,
) -> dict[str, Any] | None:
    """Run an evaluator-discrimination probe and persist the result.

    Looks for benchmark proposals in::

        {workspace}/benchmarks/evaluator_calibration/proposals/<name>/

    Each proposal sub-directory must contain a ``proposal.md`` file (the
    text that would be shown to the evaluator) and there must be a sibling
    ground-truth file at::

        {workspace}/benchmarks/evaluator_calibration/ground_truth.json

    The ground-truth file must be a JSON object mapping proposal names
    (directory basenames) to numeric ground-truth scores.

    When benchmarks are absent the function returns ``None`` without
    logging any warning — the caller is expected to treat ``None`` as
    "no probe data available".

    When a probe is run successfully the result dict is appended as a
    JSON line to ``probe_results.jsonl`` and returned to the caller.

    Parameters
    ----------
    harness_config:
        A ``HarnessConfig`` instance (or any object with a ``.workspace``
        attribute).
    pipeline_config_path:
        Unused for now; reserved for future multi-pipeline probing.

    Returns
    -------
    dict or None
        Keys: ``rho``, ``rho_basic``, ``rho_diffusion``, ``n``,
        ``elapsed_s``, ``timestamp``.
    """
    ws = pathlib.Path(getattr(harness_config, "workspace", "."))
    proposals_dir = ws / _PROPOSALS_SUBDIR
    ground_truth_path = ws / "benchmarks/evaluator_calibration" / _GROUND_TRUTH_FILENAME

    if not proposals_dir.is_dir() or not ground_truth_path.exists():
        return None

    try:
        ground_truth: dict[str, float] = _load_ground_truth(ground_truth_path)
        proposals = _load_proposals(proposals_dir, ground_truth)
    except Exception as exc:
        log.warning("intel_probe: failed to load benchmark data: %s", exc)
        return None

    if len(proposals) < 3:  # noqa: PLR2004 — need at least 3 points for rho
        log.debug("intel_probe: skipped (only %d proposals)", len(proposals))
        return None

    # Lazy import to avoid hard dependency on the evaluator.
    try:
        from harness.evaluation import evaluator as _ev
    except ImportError as exc:
        log.debug("intel_probe: evaluator not importable: %s", exc)
        return None

    t0 = time.monotonic()
    names = list(proposals.keys())
    gt_scores = [proposals[n]["gt"] for n in names]
    texts = [proposals[n]["text"] for n in names]

    try:
        basic_scores, diffusion_scores = await _score_proposals(_ev, texts)
    except Exception as exc:
        log.warning("intel_probe: scoring error: %s", exc)
        return None

    rho_basic = spearman_rho(gt_scores, basic_scores)
    rho_diffusion = spearman_rho(gt_scores, diffusion_scores)
    rho_parts = [r for r in (rho_basic, rho_diffusion) if r is not None]
    rho = sum(rho_parts) / len(rho_parts) if rho_parts else None
    elapsed = time.monotonic() - t0

    result: dict[str, Any] = {
        "rho": rho,
        "rho_basic": rho_basic,
        "rho_diffusion": rho_diffusion,
        "n": len(names),
        "elapsed_s": round(elapsed, 2),
        "timestamp": _utc_iso(),
    }

    _append_probe_result(ws / _PROBE_RESULTS_FILENAME, result)
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_ground_truth(path: pathlib.Path) -> dict[str, float]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"ground_truth.json must be a JSON object, got {type(raw).__name__}")
    return {str(k): float(v) for k, v in raw.items()}


def _load_proposals(
    proposals_dir: pathlib.Path,
    ground_truth: dict[str, float],
) -> dict[str, dict[str, Any]]:
    """Return ``{name: {gt: float, text: str}}`` for proposals that have GT."""
    result: dict[str, dict[str, Any]] = {}
    for subdir in sorted(proposals_dir.iterdir()):
        if not subdir.is_dir():
            continue
        name = subdir.name
        if name not in ground_truth:
            continue
        proposal_file = subdir / "proposal.md"
        if not proposal_file.exists():
            continue
        result[name] = {
            "gt": ground_truth[name],
            "text": proposal_file.read_text(encoding="utf-8"),
        }
    return result


async def _score_proposals(
    ev_module: Any,
    texts: list[str],
) -> tuple[list[float], list[float]]:
    """Score *texts* with both the BASIC and DIFFUSION evaluators.

    Returns ``(basic_scores, diffusion_scores)`` — parallel lists.
    """
    import asyncio

    dummy_task = "Rate the quality of this implementation proposal."

    async def _score_one(text: str) -> tuple[float, float]:
        # Import lazily to avoid hard dependency when module is imported.
        from harness.evaluation.evaluator import Evaluator

        try:
            evaluator = Evaluator()
            result = await evaluator.evaluate(
                task_description=dummy_task,
                proposal=text,
                prior_best_score=None,
            )
            basic = result.get("basic", {}).get("score", 5.0)
            diffusion = result.get("diffusion", {}).get("score", 5.0)
            return float(basic), float(diffusion)
        except Exception as exc:
            log.debug("intel_probe: _score_one error: %s", exc)
            return 5.0, 5.0

    pairs = await asyncio.gather(*[_score_one(t) for t in texts])
    basic_scores = [p[0] for p in pairs]
    diffusion_scores = [p[1] for p in pairs]
    return basic_scores, diffusion_scores


def _append_probe_result(path: pathlib.Path, result: dict[str, Any]) -> None:
    """Append *result* as a JSON line to *path*, creating parents as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(result, separators=(",", ":")) + "\n")


def _utc_iso() -> str:
    """Return current UTC time as an ISO-8601 string."""
    import datetime

    return datetime.datetime.now(tz=datetime.timezone.utc).isoformat()
