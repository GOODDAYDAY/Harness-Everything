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


async def run_pilot(config: PilotConfig) -> None:
    """Create and run the pilot loop until shutdown signal."""
    loop = PilotLoop(config)
    await loop.run()


def main() -> None:
    """Parse config and start the pilot daemon."""
    setup_logging()
    log = logging.getLogger(__name__)

    args = sys.argv[1:]
    if not args:
        print("Usage: python pilot.py <pilot_config.json>")
        sys.exit(1)

    # 1. Load config file
    config_path = Path(args[0])
    if not config_path.exists():
        print(f"Config file not found: {config_path}")
        sys.exit(1)

    # 2. Parse pilot config
    with open(config_path, encoding="utf-8") as f:
        raw = json.load(f)
    config = PilotConfig.from_dict(raw)

    # 3. Start daemon
    log.info("Pilot starting, config=%s", config_path)
    asyncio.run(run_pilot(config))
    log.info("Pilot exited")


if __name__ == "__main__":
    main()
