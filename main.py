"""Harness-Everything — autonomous agent for codebase improvement.

Usage:
  python main.py <config.json>
  python main.py --agent <config.json>   (legacy form, still supported)
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

from harness.agent import AgentConfig, AgentLoop


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


async def run_agent(config: AgentConfig) -> int:
    """Run the autonomous agent. Returns the process exit code.

    Exit codes:
      0 — mission complete OR at least one cycle produced work
      2 — nothing happened at all (zero cycles, or first cycle crashed
          before any tool call)
    """
    loop = AgentLoop(config)
    result = await loop.run()

    print("\n" + "=" * 60)
    print(f"Agent: mission_status={result.mission_status}")
    print(f"Cycles run: {result.cycles_run}")
    print(f"Total tool calls: {result.total_tool_calls}")
    print(f"Run dir: {result.run_dir}")
    if result.summary:
        print(f"\nFinal summary (first 500 chars):\n{result.summary[:500]}")
    print("=" * 60)

    # Catastrophic exit when the agent didn't even get a full cycle done.
    if result.cycles_run >= 1 and result.total_tool_calls == 0:
        print(
            "ZERO-WORK CATASTROPHE: agent completed cycles but made no tool calls "
            "— exiting 2 so systemd treats this as a failure."
        )
        return 2
    return 0


def main() -> None:
    setup_logging()
    args = sys.argv[1:]

    # Strip legacy --agent flag if present
    if "--agent" in args:
        args.remove("--agent")

    if not args:
        print("Usage: python main.py <config.json>")
        sys.exit(1)

    config_path = Path(args[0])
    with open(config_path, encoding="utf-8") as f:
        agent_cfg = AgentConfig.from_dict(json.load(f))

    exit_code = asyncio.run(run_agent(agent_cfg))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
