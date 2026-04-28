"""Pilot — daily improvement daemon with Feishu human-in-the-loop approval.

Usage:
  python pilot.py <pilot_config.json>

Starts a long-running daemon that connects to Feishu via WebSocket,
schedules daily diagnosis runs, and manages the improvement lifecycle.

Requires the 'pilot' optional dependency:
  pip install 'harness-everything[pilot]'
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

from harness.pilot.config import PilotConfig
from harness.pilot.loop import PilotLoop


def setup_logging(level: str = "INFO") -> None:
    """Configure structured logging for the pilot daemon."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


async def run_pilot(config: PilotConfig, run_now: bool = False) -> None:
    """Create and run the pilot loop.

    Args:
        run_now: If True, run one improvement cycle immediately instead of
                 waiting for the daily schedule.  Useful for testing.
    """
    loop = PilotLoop(config)
    if run_now:
        await loop._connect_feishu()
        await loop._run_improvement_cycle()
        await loop._shutdown()
    else:
        await loop.run()


def main() -> None:
    """Parse config and start the pilot daemon."""
    setup_logging()
    log = logging.getLogger(__name__)

    args = sys.argv[1:]
    if not args:
        print("Usage: python pilot.py <pilot_config.json> [--now]")
        sys.exit(1)

    run_now = "--now" in args
    config_args = [a for a in args if a != "--now"]

    # 1. Load config file
    config_path = Path(config_args[0])
    if not config_path.exists():
        print(f"Config file not found: {config_path}")
        sys.exit(1)

    # 2. Parse pilot config
    with open(config_path, encoding="utf-8") as f:
        raw = json.load(f)
    config = PilotConfig.from_dict(raw)

    # 3. Start
    mode = "immediate" if run_now else "daemon"
    log.info("Pilot starting, config=%s, mode=%s", config_path, mode)
    asyncio.run(run_pilot(config, run_now=run_now))
    log.info("Pilot exited")


if __name__ == "__main__":
    main()
