"""test_runner — run pytest and return structured, parsed results.

Unlike the generic ``bash`` tool, this tool:
* Invokes pytest with ``-v --tb=short --no-header`` plus an optional
  ``pytest-json-report`` JSON file (falls back to stdout parsing when the
  plugin is absent).
* Returns a structured summary: total/passed/failed/error/skipped counts,
  per-test outcomes, condensed failure tracebacks — sized to fit in an LLM
  context window.
* Respects ``config.workspace`` as the cwd and validates that ``test_path``
  falls within ``config.allowed_paths``.
* Supports extra ``pytest_args`` (e.g. ``["-x", "-k", "test_login"]``).
* Configurable ``timeout`` (default 120 s).

Output format ``format="text"`` (default)::

    pytest  tests/  [3 passed, 1 failed, 0 error, 0 skipped / 4 total]  (1.23s)  [FAIL]

      ✓  tests/test_foo.py::test_one
      ✓  tests/test_foo.py::test_two
      ✗  tests/test_bar.py::test_broken
      s  tests/test_foo.py::test_skip

    ── Failures ─────────────────────────────────────────────────
    FAILED  tests/test_bar.py::test_broken
        AssertionError: assert 1 == 2
        ...

Output format ``format="json"`` — a JSON object with keys:
``passed``, ``failed``, ``error``, ``skipped``, ``total``, ``duration``,
``exit_code``, ``tests`` (list of per-test dicts), ``failures``
(list of {name, short_tb} dicts).
"""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
from pathlib import Path
from typing import Any

from harness.core.config import HarnessConfig
from harness.tools.base import Tool, ToolResult

# ---------------------------------------------------------------------------
# Regex patterns for parsing pytest -v --tb=short stdout
# ---------------------------------------------------------------------------

# Compact per-line format from -v: "tests/test_foo.py::test_bar PASSED"
_COMPACT_OUTCOME_RE = re.compile(
    r"^(.+?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?:\s+\[[\d\s%]+\])?\s*$"
)

# Per-count summary extractors (used individually — avoids non-greedy multi-group issues)
_SUM_PASSED_RE  = re.compile(r"(\d+)\s+passed",  re.IGNORECASE)
_SUM_FAILED_RE  = re.compile(r"(\d+)\s+failed",  re.IGNORECASE)
_SUM_ERROR_RE   = re.compile(r"(\d+)\s+error",   re.IGNORECASE)
_SUM_SKIPPED_RE = re.compile(r"(\d+)\s+skipped", re.IGNORECASE)
_SUM_DURATION_RE = re.compile(r"in\s+([\d.]+)s",  re.IGNORECASE)

# Failure section header: "____ test_name ____"
_FAILED_HEADER_RE = re.compile(r"^_{5,}\s+(.+?)\s+_{5,}$")


# ---------------------------------------------------------------------------
# Stdout parser
# ---------------------------------------------------------------------------


def _parse_pytest_stdout(stdout: str) -> dict[str, Any]:
    """Parse pytest verbose stdout into a structured dict.

    Strategy:
    * Per-test outcomes are read from the ``<nodeid> PASSED/FAILED/…`` lines
      emitted by ``-v``.
    * Counts (passed/failed/error/skipped) and duration come from the final
      summary line ``N passed, M failed in Xs``, which is the authoritative
      source (handles xpass/xfail edge cases correctly).
    * Failure tracebacks are collected from ``___ test_name ___`` blocks.
    """
    tests: list[dict[str, str]] = []
    failures: list[dict[str, str]] = []
    duration = 0.0

    lines = stdout.splitlines()

    # --- per-test outcomes ---
    for line in lines:
        m = _COMPACT_OUTCOME_RE.match(line.strip())
        if m:
            node_id, outcome = m.group(1).strip(), m.group(2)
            tests.append({"nodeid": node_id, "outcome": outcome})

    # --- failure tracebacks ---
    current_failure: str | None = None
    tb_lines: list[str] = []

    for line in lines:
        sep = _FAILED_HEADER_RE.match(line)
        if sep:
            if current_failure is not None and tb_lines:
                failures.append(
                    {"name": current_failure, "short_tb": "\n".join(tb_lines)}
                )
            current_failure = sep.group(1)
            tb_lines = []
            continue
        if current_failure is not None:
            stripped = line.rstrip()
            if "short test summary info" in stripped.lower():
                # Save and stop collecting
                if tb_lines:
                    failures.append(
                        {"name": current_failure, "short_tb": "\n".join(tb_lines[:40])}
                    )
                current_failure = None
                tb_lines = []
                continue
            if stripped:
                tb_lines.append(stripped)

    if current_failure is not None and tb_lines:
        failures.append(
            {"name": current_failure, "short_tb": "\n".join(tb_lines[:40])}
        )

    # --- summary line (authoritative counts) ---
    # Scan from the end to find the canonical "N passed … in Xs" line.
    passed = failed = errors = skipped = 0
    for line in reversed(lines):
        # Must contain "in Xs" to be the real summary, not an intermediate line
        if not _SUM_DURATION_RE.search(line):
            continue
        m_p = _SUM_PASSED_RE.search(line)
        m_f = _SUM_FAILED_RE.search(line)
        m_e = _SUM_ERROR_RE.search(line)
        m_s = _SUM_SKIPPED_RE.search(line)
        m_d = _SUM_DURATION_RE.search(line)
        passed   = int(m_p.group(1)) if m_p else 0
        failed   = int(m_f.group(1)) if m_f else 0
        errors   = int(m_e.group(1)) if m_e else 0
        skipped  = int(m_s.group(1)) if m_s else 0
        duration = float(m_d.group(1)) if m_d else 0.0
        break

    total = passed + failed + errors + skipped
    return {
        "passed": passed,
        "failed": failed,
        "error": errors,
        "skipped": skipped,
        "total": total,
        "duration": duration,
        "tests": tests,
        "failures": failures,
    }


def _parse_json_report(report_path: Path) -> dict[str, Any] | None:
    """Parse a pytest-json-report file into our standard schema.

    Returns ``None`` when the file is absent or malformed.
    """
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception:
        return None

    summary = data.get("summary", {})
    tests_raw = data.get("tests", [])

    tests: list[dict[str, str]] = []
    failures: list[dict[str, str]] = []

    for t in tests_raw:
        nodeid = t.get("nodeid", "")
        outcome = t.get("outcome", "").upper()
        tests.append({"nodeid": nodeid, "outcome": outcome})
        if outcome in ("FAILED", "ERROR"):
            longrepr = (
                t.get("call", {}).get("longrepr", "")
                or t.get("longrepr", "")
                or ""
            )
            snippet = "\n".join(str(longrepr).splitlines()[:40])
            failures.append({"name": nodeid, "short_tb": snippet})

    return {
        "passed": summary.get("passed", 0),
        "failed": summary.get("failed", 0),
        "error": summary.get("error", 0),
        "skipped": summary.get("skipped", 0),
        "total": summary.get("total", len(tests)),
        "duration": data.get("duration", 0.0),
        "tests": tests,
        "failures": failures,
    }


# ---------------------------------------------------------------------------
# Text formatter
# ---------------------------------------------------------------------------

_OUTCOME_SYMBOL: dict[str, str] = {
    "PASSED": "✓",
    "FAILED": "✗",
    "ERROR": "E",
    "SKIPPED": "s",
    "XFAIL": "x",
    "XPASS": "X",
}

_MAX_TESTS_IN_LIST = 60


def _format_results(
    results: dict[str, Any],
    test_path: str,
    exit_code: int,
    max_failures: int,
) -> str:
    """Render parsed results as a compact, LLM-friendly text block."""
    p = results["passed"]
    f = results["failed"]
    e = results["error"]
    s = results["skipped"]
    total = results["total"]
    dur = results["duration"]

    status = "PASS" if exit_code == 0 else "FAIL"
    header = (
        f"pytest  {test_path}  "
        f"[{p} passed, {f} failed, {e} error, {s} skipped / {total} total]"
        f"  ({dur:.2f}s)  [{status}]"
    )

    parts: list[str] = [header]

    tests = results.get("tests", [])
    if tests:
        parts.append("")
        shown = tests[:_MAX_TESTS_IN_LIST]
        for t in shown:
            sym = _OUTCOME_SYMBOL.get(t["outcome"], "?")
            parts.append(f"  {sym}  {t['nodeid']}")
        if len(tests) > _MAX_TESTS_IN_LIST:
            parts.append(f"  … and {len(tests) - _MAX_TESTS_IN_LIST} more")

    failures = results.get("failures", [])
    if failures:
        parts.append("")
        parts.append("── Failures " + "─" * 48)
        for fail in failures[:max_failures]:
            parts.append(f"\nFAILED  {fail['name']}")
            tb = fail.get("short_tb", "").strip()
            if tb:
                for ln in tb.splitlines()[:30]:
                    parts.append(f"    {ln}")
        if len(failures) > max_failures:
            parts.append(
                f"\n  … {len(failures) - max_failures} more failure(s) not shown"
            )

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class TestRunnerTool(Tool):
    """Run pytest and return structured, parsed results.

    Invokes ``python -m pytest`` in the workspace directory and parses the
    output into pass/fail/error/skip counts, a per-test outcome list, and
    condensed failure tracebacks — all sized to fit in an LLM context window.

    When ``pytest-json-report`` is installed the tool uses its
    ``--json-report`` flag for precise structured data; otherwise pytest's
    verbose stdout is parsed with regex and the same schema is returned.
    """

    name = "test_runner"
    description = (
        "Run pytest on the specified path and return structured results: "
        "pass/fail/error/skip counts, per-test outcomes, and condensed failure "
        "tracebacks.  More structured than bare 'bash' for programmatic use. "
        "Set format='json' for machine-readable output."
    )
    requires_path_check = True
    tags = frozenset({"testing"})

    # Memoised: None = unknown, True = available, False = unavailable
    _json_report_available: bool | None = None

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "test_path": {
                    "type": "string",
                    "description": (
                        "Path to the test file or directory, relative to the "
                        "workspace (default: 'tests/')."
                    ),
                    "default": "tests/",
                },
                "pytest_args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Extra pytest arguments, e.g. "
                        "[\"-x\", \"-k\", \"test_login\", \"--ignore=tests/slow\"]."
                    ),
                    "default": [],
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default: 120).",
                    "default": 120,
                },
                "format": {
                    "type": "string",
                    "enum": ["text", "json"],
                    "description": "Output format: 'text' (default) or 'json'.",
                    "default": "text",
                },
                "max_failures": {
                    "type": "integer",
                    "description": (
                        "Maximum number of failure tracebacks to include in "
                        "text output (default: 10)."
                    ),
                    "default": 10,
                },
            },
            "required": [],
        }

    async def execute(
        self,
        config: HarnessConfig,
        *,
        test_path: str = "tests/",
        pytest_args: list[str] | None = None,
        timeout: int = 120,
        format: str = "text",  # noqa: A002
        max_failures: int = 10,
    ) -> ToolResult:
        # ---- path validation ----
        abs_test_path = str((Path(config.workspace) / test_path).resolve())
        resolved = self._check_path(config, abs_test_path)
        if isinstance(resolved, ToolResult):
            return resolved

        # ---- build command ----
        base_args: list[str] = [
            "python", "-m", "pytest",
            test_path,
            "-v", "--tb=short", "--no-header",
        ]
        extra_args: list[str] = list(pytest_args or [])

        # Attempt to use pytest-json-report for richer structured output.
        use_json_report = False
        json_report_file: str | None = None

        if TestRunnerTool._json_report_available is not False:
            try:
                tmp = tempfile.NamedTemporaryFile(
                    suffix=".json",
                    delete=False,
                    dir=config.workspace,
                )
                tmp.close()
                json_report_file = tmp.name
                base_args += [
                    "--json-report",
                    f"--json-report-file={json_report_file}",
                ]
                use_json_report = True
            except Exception:
                use_json_report = False

        cmd = base_args + extra_args

        # ---- launch pytest ----
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=config.workspace,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            if proc is not None:
                proc.kill()
                await proc.wait()
            if json_report_file:
                try:
                    Path(json_report_file).unlink(missing_ok=True)
                except Exception:
                    pass
            return ToolResult(
                error=f"pytest timed out after {timeout}s", is_error=True
            )
        except FileNotFoundError:
            if json_report_file:
                try:
                    Path(json_report_file).unlink(missing_ok=True)
                except Exception:
                    pass
            return ToolResult(
                error=(
                    "pytest not found — is it installed in the current environment?"
                ),
                is_error=True,
            )
        except Exception as exc:
            if json_report_file:
                try:
                    Path(json_report_file).unlink(missing_ok=True)
                except Exception:
                    pass
            return ToolResult(error=f"Failed to launch pytest: {exc}", is_error=True)

        exit_code: int = proc.returncode  # type: ignore[assignment]
        stdout_text = stdout_bytes.decode(errors="replace")
        stderr_text = stderr_bytes.decode(errors="replace")

        # ---- detect json-report availability from this run ----
        if use_json_report:
            if (
                "--json-report" in stderr_text
                or "unrecognized arguments" in stderr_text
                or "no such option" in stderr_text.lower()
            ):
                TestRunnerTool._json_report_available = False
                use_json_report = False
                # Clean up leftover temp file
                if json_report_file:
                    try:
                        Path(json_report_file).unlink(missing_ok=True)
                    except Exception:
                        pass
                # Re-run without --json-report so the output is actually usable.
                cmd_plain = [
                    "python", "-m", "pytest",
                    test_path,
                    "-v", "--tb=short", "--no-header",
                ] + list(pytest_args or [])
                try:
                    proc2 = await asyncio.wait_for(
                        asyncio.create_subprocess_exec(
                            *cmd_plain,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                            cwd=config.workspace,
                        ),
                        timeout=timeout,
                    )
                    stdout_bytes, stderr_bytes = await asyncio.wait_for(
                        proc2.communicate(), timeout=timeout
                    )
                    exit_code = proc2.returncode or 0
                    stdout_text = stdout_bytes.decode(errors="replace")
                    stderr_text = stderr_bytes.decode(errors="replace")
                except Exception:
                    pass  # keep the original (failed) output for best-effort parsing
            else:
                TestRunnerTool._json_report_available = True

        # ---- parse results ----
        results: dict[str, Any] | None = None

        if use_json_report and json_report_file:
            results = _parse_json_report(Path(json_report_file))
            try:
                Path(json_report_file).unlink(missing_ok=True)
            except Exception:
                pass

        if results is None:
            combined = stdout_text
            if stderr_text.strip():
                combined += "\n" + stderr_text
            results = _parse_pytest_stdout(combined)

        results["exit_code"] = exit_code

        # ---- format output ----
        if format == "json":
            return ToolResult(output=json.dumps(results, indent=2))

        # text
        text_output = _format_results(results, test_path, exit_code, max_failures)

        # Append stderr when it contains useful info (import errors, collection errors)
        if stderr_text.strip():
            stderr_trimmed = "\n".join(stderr_text.splitlines()[:30])
            text_output += f"\n\n── stderr ──\n{stderr_trimmed}"

        # exit codes: 0 = all passed, 1 = test failures (NOT a tool error — caller
        # should check counts), 2 = interrupted, 3 = internal error, 4 = usage error,
        # 5 = no tests collected
        is_error = exit_code in (2, 3, 4)
        return ToolResult(output=text_output, is_error=is_error)
