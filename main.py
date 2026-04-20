"""Harness-Everything entry point.

Two modes:
  Simple:   python main.py "task description" [config.json]
  Pipeline: python main.py --pipeline pipeline_config.json
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

from harness.core.config import HarnessConfig, PipelineConfig
from harness.pipeline.simple_loop import HarnessLoop
from harness.pipeline.pipeline_loop import PipelineLoop


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


# ===========================================================================
# Simple mode
# ===========================================================================


async def run_simple(task: str, config: HarnessConfig) -> None:
    loop = HarnessLoop(config)
    result = await loop.run(task)

    print("\n" + "=" * 60)
    if result.success:
        print(f"SUCCESS after {len(result.iterations)} iteration(s)")
        print(f"Total tool calls: {result.total_tool_calls}")
        if result.final_result:
            print(f"Files changed: {result.final_result.files_changed}")
    else:
        print(f"FAILED after {len(result.iterations)} iteration(s)")
        last = result.iterations[-1] if result.iterations else None
        if last:
            print(f"Last verdict: {last.verdict.reason}")
            print(f"Last feedback: {last.verdict.feedback}")
    print("=" * 60)


# ===========================================================================
# Pipeline mode
# ===========================================================================


async def run_pipeline(config: PipelineConfig, config_path: str | None = None) -> int:
    """Run the pipeline. Returns the process exit code.

    Exit codes:
      0 — pipeline produced real work (best_score >= 1.0 OR fewer than 3 rounds
          ran, which is too few to call catastrophic).
      2 — zero-work catastrophe: rounds>=3 completed but every phase crashed
          (total_phases_run == 0) OR best_score stayed at 0. Distinguishable
          from natural clean exit so systemd (Restart=on-failure) restarts
          and heartbeat can treat it as a fault. See 2026-04-19 incident
          where a missing function silently tanked 6 rounds × ~4 phases.
    """
    loop = PipelineLoop(config)
    # Pipeline config path threads through to the intelligence probe so the
    # probe subprocess uses the same base_url/api_key/model as the pipeline.
    if config_path:
        loop._intel_probe_pipeline_cfg_path = config_path
    result = await loop.run()

    print("\n" + "=" * 60)
    if result.success:
        print(f"Pipeline completed: {result.rounds_completed} round(s)")
    else:
        print("Pipeline failed")
    if result.final_proposal:
        print(f"Final proposal: {result.final_proposal[:200]}...")
    print("=" * 60)

    zero_work = (
        result.rounds_completed >= 3
        and (result.total_phases_run == 0 or result.best_score < 1.0)
    )
    if zero_work:
        print(
            f"ZERO-WORK CATASTROPHE: rounds={result.rounds_completed} "
            f"phases_run={result.total_phases_run} best_score={result.best_score:.1f} "
            f"— exiting with code 2 so systemd treats this as a failure."
        )
        return 2
    return 0


# ===========================================================================
# CLI
# ===========================================================================


def main() -> None:
    setup_logging()
    args = sys.argv[1:]

    # Pipeline mode
    if "--pipeline" in args:
        idx = args.index("--pipeline")
        if idx + 1 >= len(args):
            print("Usage: python main.py --pipeline <config.json>")
            sys.exit(1)
        config_path = Path(args[idx + 1])
        with open(config_path) as f:
            config = PipelineConfig.from_dict(json.load(f))
        exit_code = asyncio.run(run_pipeline(config, config_path=str(config_path)))
        sys.exit(exit_code)

    # Simple mode
    if not args:
        print("Usage:")
        print('  python main.py "task description" [config.json]')
        print("  python main.py --pipeline pipeline_config.json")
        sys.exit(1)

    task = args[0]
    if len(args) >= 2 and not args[1].startswith("--"):
        config_path = Path(args[1])
        with open(config_path) as f:
            config = HarnessConfig.from_dict(json.load(f))
    else:
        config = HarnessConfig()

    asyncio.run(run_simple(task, config))


if __name__ == "__main__":
    main()
