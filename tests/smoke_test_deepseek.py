"""Smoke test for DeepSeek's Anthropic-compat layer.

Verifies the harness's tool-use loop actually works against DeepSeek end-to-end:
the model issues a `read_file` tool call, the harness executes it, the model
consumes the result and produces a final text answer.

Run before bringing up any pipeline against DeepSeek.

Usage:
    HARNESS_API_KEY=<deepseek-key> python tests/smoke_test_deepseek.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from harness.core.config import HarnessConfig
from harness.core.llm import LLM
from harness.tools import build_registry


SELF_PATH = Path(__file__).resolve()


async def main() -> int:
    if not os.environ.get("HARNESS_API_KEY"):
        print("ERROR: HARNESS_API_KEY env var not set (need DeepSeek key)")
        return 2

    config = HarnessConfig(
        model="deepseek-chat",
        max_tokens=2000,
        base_url="https://api.deepseek.com/anthropic",
        workspace=str(REPO_ROOT),
        allowed_paths=[str(REPO_ROOT)],
        allowed_tools=["read_file"],
        max_tool_turns=5,
        log_level="INFO",
    )

    llm = LLM(config)
    registry = build_registry(allowed_tools=["read_file"])

    expected_lines = SELF_PATH.read_text(encoding="utf-8").count("\n") + 1
    prompt = (
        f"Use the read_file tool to read {SELF_PATH.as_posix()!r}, then tell me "
        f"the exact number of lines in that file. Reply only with the number."
    )

    print(f"== DeepSeek smoke test ==")
    print(f"Model:      {config.model}")
    print(f"Endpoint:   {config.base_url}")
    print(f"Target:     {SELF_PATH}")
    print(f"Expected:   {expected_lines} lines")
    print()

    final_text, exec_log = await llm.call_with_tools(
        messages=[{"role": "user", "content": prompt}],
        registry=registry,
    )

    print(f"-- Tool calls --")
    for entry in exec_log:
        print(f"  {entry['tool']}({entry.get('input', {})})")
    print()
    print(f"-- Final text --\n{final_text!r}\n")

    if not exec_log:
        print("FAIL: model never called any tool")
        return 1
    if not any(e["tool"] == "read_file" for e in exec_log):
        print("FAIL: model did not call read_file")
        return 1
    if str(expected_lines) not in final_text:
        print(f"FAIL: final text does not contain expected line count {expected_lines}")
        return 1

    print(f"PASS: tool loop works, model produced correct line count")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
