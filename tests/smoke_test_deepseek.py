"""Smoke test for DeepSeek's Anthropic-compat layer.

Verifies the harness's tool-use loop actually works against DeepSeek end-to-end:
the model issues a `read_file` tool call, the harness executes it, the model
consumes the result and produces a final text answer.

Run before bringing up a harness run against DeepSeek.

Usage:
    HARNESS_API_KEY=<deepseek-key> python tests/smoke_test_deepseek.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))  # noqa: E402

from harness.core.config import HarnessConfig  # noqa: E402
from harness.core.llm import LLM  # noqa: E402
from harness.tools import build_registry  # noqa: E402


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

    text = SELF_PATH.read_text(encoding="utf-8")
    expected_lines = len(text.splitlines())
    prompt = (
        f"Use the read_file tool to read {SELF_PATH.as_posix()!r}, then tell me "
        f"the exact number of lines in that file. Reply only with the number."
    )

    print("== DeepSeek smoke test ==")
    print(f"Model:      {config.model}")
    print(f"Endpoint:   {config.base_url}")
    print(f"Target:     {SELF_PATH}")
    print(f"Expected:   {expected_lines} lines")
    print()

    final_text, exec_log, _llm_calls, _conv = await llm.call_with_tools(
        messages=[{"role": "user", "content": prompt}],
        registry=registry,
    )

    print("-- Tool calls --")
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
    # Accept ±1 (file may or may not have a trailing newline depending on how
    # the reader counts).  The point of this test is the tool loop, not exact
    # line counting.
    if not any(str(expected_lines + d) in final_text for d in (-1, 0, 1)):
        print(f"FAIL: final text does not mention {expected_lines}±1 lines")
        return 1

    print("PASS: tool loop works, model produced correct line count")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
