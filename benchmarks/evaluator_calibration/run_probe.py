"""Evaluator calibration probe.

Feeds 20 fixed synthetic proposals (ranked 1..20 by a ground-truth tier
scheme) through the current DualEvaluator and reports the Spearman rank
correlation between evaluator scores and ground-truth ranks. Higher ρ =
the evaluator discriminates better.

The probe is the first of four "intelligence metric" loops (see
docs/HARNESS_RUNBOOK.md §X once written). It exists so the self-improvement
LLM has an observable, quantitative signal for whether its evaluator
changes actually made the evaluator smarter — rather than drifting toward
whatever the evaluator happens to reward.

Usage:
    python benchmarks/evaluator_calibration/run_probe.py [pipeline_config.json]

If no config given, reads HARNESS_BASE_URL and HARNESS_API_KEY from env.
Writes one JSONL line per invocation to probe_results.jsonl in this
directory.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import sys
import time
from pathlib import Path

# Make repo root importable when invoked as a script.
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
sys.path.insert(0, str(_REPO))

from harness.core.config import HarnessConfig, PipelineConfig  # noqa: E402
from harness.core.llm import LLM  # noqa: E402
from harness.evaluation.dual_evaluator import DualEvaluator  # noqa: E402

log = logging.getLogger("eval_probe")


PROPOSALS_PATH = _HERE / "proposals.jsonl"
RESULTS_PATH = _HERE / "probe_results.jsonl"

# A small, stable snippet of real harness code so the exemplar proposals'
# references to real file paths are actually "in context" for the evaluator.
# Kept short to bound token cost at ~400 tokens/proposal.
SOURCE_CONTEXT = """\
## harness/pipeline/hooks.py (excerpt)
class ImportSmokeHook(VerificationHook):
    name = "import_smoke"
    gates_commit = True
    async def run(self, config, context):
        # ... subprocess import + build_registry() + smoke_calls ...

class StaticCheckHook(VerificationHook):
    name = "static_check"
    gates_commit = True
    RUFF_RULES = "F821,F811,F401"

## harness/pipeline/memory.py (excerpt)
class MemoryStore:
    def format_context(self, phase, max_entries=8):
        # returns formatted str of prior-round learnings

## harness/evaluation/dual_evaluator.py (excerpt)
def validate_evaluator_output(text, evaluator_type="basic", mode=None):
    # returns (bool_valid, list_of_issues)

def parse_score(text, pattern=r"SCORE[:\\s]+([0-9.]+)") -> float:
    # returns [0, 10] score extracted from evaluator text

## harness/pipeline/pipeline_loop.py (excerpt)
def _write_run_summary(self, ...):
    # writes summary.json at end of run
"""

FALSIFIABLE_CRITERION = (
    "The evaluator must assign higher scores to more specific, testable, "
    "correct-and-complete proposals and lower scores to vague, hallucinated, "
    "or rule-violating ones. A proposal that cites real symbols from the "
    "source context AND provides a concrete test AND respects the "
    "SELF-IMPROVEMENT LOOP PROTECTION scores higher than one that does not."
)


def _spearman(xs: list[float], ys: list[float]) -> float:
    """Spearman rank correlation, tie-aware, pure stdlib."""
    n = len(xs)
    if n < 2 or len(ys) != n:
        return 0.0

    def _rank(vs: list[float]) -> list[float]:
        order = sorted(range(n), key=lambda i: vs[i])
        ranks = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and vs[order[j + 1]] == vs[order[i]]:
                j += 1
            avg_rank = (i + j) / 2 + 1  # 1-indexed, ties get average rank
            for k in range(i, j + 1):
                ranks[order[k]] = avg_rank
            i = j + 1
        return ranks

    rx = _rank(xs)
    ry = _rank(ys)
    mx = sum(rx) / n
    my = sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    denom = (
        sum((rx[i] - mx) ** 2 for i in range(n))
        * sum((ry[i] - my) ** 2 for i in range(n))
    ) ** 0.5
    return num / denom if denom else 0.0


def _load_proposals() -> list[dict]:
    out: list[dict] = []
    with open(PROPOSALS_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _load_harness_config(cfg_path: str | None) -> HarnessConfig:
    """Resolve the LLM config for the probe.

    If a pipeline config path is given, reuse its harness block. Otherwise
    build a minimal HarnessConfig from env, without validating workspace
    (the probe doesn't write files — workspace existence is irrelevant).
    """
    if cfg_path:
        with open(cfg_path, encoding="utf-8") as f:
            pcfg = PipelineConfig.from_dict(json.load(f))
        return pcfg.harness

    # Env-driven fallback — bypass __post_init__ workspace check by passing
    # a real directory we know exists.
    return HarnessConfig(
        workspace=str(_REPO),
        base_url=os.environ.get("HARNESS_BASE_URL", ""),
        api_key=os.environ.get("HARNESS_API_KEY", ""),
        model=os.environ.get("HARNESS_MODEL", "deepseek-chat"),
    )


async def _score_one(
    evaluator: DualEvaluator, proposal_row: dict,
) -> tuple[float, float]:
    """Return (basic_score, diffusion_score) for one proposal row."""
    subject = proposal_row["proposal"]
    try:
        dual = await evaluator.evaluate(
            subject=subject,
            context=SOURCE_CONTEXT,
            mode="debate",
        )
        return dual.basic.score, dual.diffusion.score
    except Exception as exc:
        log.warning("probe: proposal %s failed: %s", proposal_row["id"], exc)
        # Assign 5.0/5.0 so failure doesn't skew ranking wildly — we lose
        # discrimination signal on that sample but don't crash the whole probe.
        return 5.0, 5.0


async def run_probe(config: HarnessConfig) -> dict:
    """Evaluate all 20 proposals and return the metrics dict."""
    proposals = _load_proposals()
    llm = LLM(config)
    evaluator = DualEvaluator(llm)

    t0 = time.monotonic()
    pairs = await asyncio.gather(*(_score_one(evaluator, p) for p in proposals))
    elapsed = time.monotonic() - t0

    basic_scores = [b for b, _ in pairs]
    diffusion_scores = [d for _, d in pairs]
    combined_scores = [b + d for b, d in pairs]
    gt_ranks = [p["ground_truth_rank"] for p in proposals]

    # Higher evaluator score SHOULD correspond to LOWER rank (rank 1 = best).
    # To make a positive ρ mean "better discrimination", flip the ground-truth:
    # use (21 - rank) so rank 1 -> 20, rank 20 -> 1, and higher value = better.
    gt_inv = [21 - r for r in gt_ranks]

    rho_basic = _spearman(basic_scores, [float(x) for x in gt_inv])
    rho_diffusion = _spearman(diffusion_scores, [float(x) for x in gt_inv])
    rho_combined = _spearman(combined_scores, [float(x) for x in gt_inv])

    return {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "rho": round(rho_combined, 4),
        "rho_basic": round(rho_basic, 4),
        "rho_diffusion": round(rho_diffusion, 4),
        "n": len(proposals),
        "elapsed_s": round(elapsed, 2),
        "per_proposal": [
            {
                "id": p["id"],
                "tier": p["tier"],
                "gt_rank": p["ground_truth_rank"],
                "basic": basic_scores[i],
                "diffusion": diffusion_scores[i],
                "combined": combined_scores[i],
            }
            for i, p in enumerate(proposals)
        ],
    }


def _append_result(result: dict) -> None:
    # Write a compact one-line JSON (strip per_proposal to keep file small;
    # the full detail is returned from run_probe for callers that want it).
    compact = {k: v for k, v in result.items() if k != "per_proposal"}
    with open(RESULTS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(compact) + "\n")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else None
    config = _load_harness_config(cfg_path)
    result = asyncio.run(run_probe(config))
    _append_result(result)
    print(
        f"rho_combined={result['rho']:.4f}  "
        f"rho_basic={result['rho_basic']:.4f}  "
        f"rho_diffusion={result['rho_diffusion']:.4f}  "
        f"n={result['n']}  elapsed={result['elapsed_s']}s"
    )


if __name__ == "__main__":
    main()
