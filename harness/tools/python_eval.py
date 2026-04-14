"""python_eval — run a Python snippet in a subprocess and return structured output.

Unlike the generic ``bash`` tool, this tool:

* Automatically prepends the workspace to ``sys.path`` so workspace modules
  are importable without manual ``PYTHONPATH`` juggling.
* Captures ``stdout``, ``stderr``, and the **return value** of the last
  expression separately, giving structured, LLM-parseable output.
* Runs in a fresh subprocess (never ``exec()`` in-process) so it cannot
  corrupt the harness process's state or block the asyncio event loop.
* Enforces a tight default timeout (30 s) to prevent runaway scripts from
  stalling the tool loop.
* Truncates output to ``max_output_chars`` (default 4 000) so a verbose
  script cannot flood the context window.

Return-value extraction
-----------------------
When the snippet ends with an *expression statement* (not an assignment),
the tool wraps that expression in ``repr()`` and prints it to a dedicated
``__return_value__`` channel so the caller gets both the display output and
the structured value.  Example::

    snippet = "import harness.config; harness.config.HarnessConfig()"
    # Output contains:
    #   RETURN: HarnessConfig(model='bedrock/...', max_tokens=8096, ...)

This is more useful than requiring the LLM to parse ``print()`` statements
or infer values from stdout noise.

Isolation
---------
The snippet runs as ``python -c "..."`` with:
* ``cwd`` = workspace (so relative file paths resolve correctly)
* ``PYTHONPATH`` = workspace (prepended to existing ``PYTHONPATH``)
* ``PYTHONUTF8=1`` (consistent encoding)
* No stdin (connected to ``/dev/null``)
* ``sys.argv = ['<harness_snippet>']`` (avoids accidental argparse confusion)

Security note
-------------
This tool executes arbitrary Python code.  It is subject to the same
``allowed_paths`` checks as every file tool (workspace must be set), but it
does **not** sandbox the Python interpreter itself.  Do not use in
untrusted-user contexts.
"""

from __future__ import annotations

import asyncio
import os
import sys
import textwrap
from typing import Any

from harness.config import HarnessConfig
from harness.tools.base import Tool, ToolResult

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_TIMEOUT: int = 30          # seconds — tight enough to catch infinite loops
_DEFAULT_MAX_OUTPUT: int = 4_000    # chars — keeps context window clean
_MAX_HARD_OUTPUT: int = 20_000      # absolute ceiling regardless of caller preference

def _build_wrapper(snippet: str) -> str:
    """Return a self-contained Python script that executes *snippet*.

    The script:
    1. Sets ``sys.argv`` to a neutral value to avoid confusing argparse-using code.
    2. Parses the snippet with ``ast`` to detect whether the last statement is an
       expression.  If so, the expression is evaluated and its ``repr()`` is
       written to stderr under the ``__return_value__:`` marker so the caller can
       extract a structured return value without parsing stdout.
    3. Uses a shared namespace dict so multi-statement snippets (e.g. an assignment
       followed by an expression) see each other's bindings.

    We build the wrapper via string concatenation rather than ``str.format()`` /
    ``str.Template`` to avoid accidental collision between format-field braces
    in the template and literal braces in the generated dict literals.
    """
    # Use repr() so any string content in the snippet is safely escaped as a
    # Python literal — newlines, quotes, backslashes all survive round-trip.
    snippet_repr = repr(textwrap.dedent(snippet))

    return (
        "import sys as _sys\n"
        "_sys.argv = ['<harness_snippet>']\n"
        "import ast as _ast\n"
        "\n"
        "_snippet = " + snippet_repr + "\n"
        "\n"
        "# Detect whether the last statement is a bare expression so we can\n"
        "# capture its repr() as the structured return value.\n"
        "_tree = _ast.parse(_snippet, mode='exec')\n"
        "_last_is_expr = (\n"
        "    isinstance(_tree, _ast.Module)\n"
        "    and _tree.body\n"
        "    and isinstance(_tree.body[-1], _ast.Expr)\n"
        ")\n"
        "\n"
        "# Shared namespace for the snippet so assignments are visible to\n"
        "# subsequent expressions in the same snippet.\n"
        "_ns = {'__name__': '__harness_snippet__'}\n"
        "\n"
        "if _last_is_expr:\n"
        "    _body = _ast.Module(body=_tree.body[:-1], type_ignores=[])\n"
        "    _last_expr = _tree.body[-1].value\n"
        "    exec(compile(_body, '<snippet>', 'exec'), _ns)\n"
        "    _retval = eval(compile(_ast.Expression(body=_last_expr), '<snippet>', 'eval'), _ns)\n"
        "    print('\\n__return_value__:', repr(_retval), file=_sys.stderr)\n"
        "else:\n"
        "    exec(compile(_tree, '<snippet>', 'exec'), _ns)\n"
    )


def _format_output(
    stdout: str,
    stderr_raw: str,
    exit_code: int,
    max_chars: int,
) -> tuple[str, str, bool]:
    """Parse stdout + stderr into (output_text, return_value_line, is_error).

    The return value is extracted from the ``__return_value__: ...`` marker
    written to stderr by the wrapper; remaining stderr is shown as ``[stderr]``.
    """
    # Extract return-value marker from stderr
    return_line = ""
    stderr_lines: list[str] = []
    for line in stderr_raw.splitlines():
        if line.startswith("__return_value__:"):
            return_line = line[len("__return_value__:"):].strip()
        else:
            stderr_lines.append(line)
    stderr_clean = "\n".join(stderr_lines).rstrip()

    parts: list[str] = []

    if stdout.strip():
        parts.append(stdout.rstrip())

    if stderr_clean.strip():
        parts.append(f"[stderr]\n{stderr_clean}")

    if return_line:
        parts.append(f"[return value]\n{return_line}")

    parts.append(f"[exit code: {exit_code}]")

    full = "\n\n".join(parts)

    # Truncate if necessary, preserving the exit-code trailer
    trailer = f"\n\n[exit code: {exit_code}]"
    if len(full) > max_chars:
        cap = max_chars - len(trailer) - 60
        full = full[:max(0, cap)] + f"\n... [output truncated to {max_chars} chars]{trailer}"

    is_error = exit_code != 0
    return full, return_line, is_error


class PythonEvalTool(Tool):
    """Run a Python code snippet in a subprocess and return structured output.

    Automatically adds the workspace to ``sys.path`` so local modules are
    importable.  Captures stdout, stderr, and the return value of the last
    expression separately.

    Differences from ``bash``
    -------------------------
    * ``bash`` runs arbitrary shell commands; this runs Python specifically.
    * The workspace is on ``sys.path`` automatically — no need to set
      ``PYTHONPATH`` manually or use ``python -c 'import sys; sys.path.insert(0, ...)'``.
    * The last expression's ``repr()`` is shown under ``[return value]``,
      making it easy to inspect object state without adding ``print()`` calls.
    * Output is clearly segmented into stdout / stderr / return-value sections.
    * Default timeout is 30 s (tighter than bash's 60 s) to catch infinite loops fast.

    Typical uses
    ------------
    * Verify an import succeeds after adding a new module::

        from harness.tools.python_eval import PythonEvalTool
        # snippet: "import harness.memory; harness.memory.MemoryStore"

    * Check a function's return type or value::

        # snippet: "from harness.config import HarnessConfig; HarnessConfig()"

    * Run a quick unit-level assertion without full pytest overhead::

        # snippet: "assert 1 + 1 == 2, 'arithmetic broken'"

    * Evaluate an expression from a module::

        # snippet: "import re; re.findall(r'SCORE:(\\\\d+)', 'SCORE:7 SCORE:9')"
    """

    name = "python_eval"
    description = (
        "Run a Python code snippet in a subprocess with the workspace on sys.path. "
        "Captures stdout, stderr, and the return value of the last expression separately. "
        "Use this to verify imports, check function return values, or run quick assertions "
        "without spinning up full pytest. "
        "The workspace directory is automatically prepended to sys.path so local modules "
        "are importable without extra setup. "
        "Default timeout: 30 s. Output truncated to max_output_chars (default 4 000)."
    )
    # No path check on the snippet itself; workspace membership is enforced via
    # cwd + PYTHONPATH pointing only at the workspace.
    requires_path_check = False

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "snippet": {
                    "type": "string",
                    "description": (
                        "Python source code to execute. "
                        "May be multi-line. "
                        "If the last statement is an expression (not an assignment), "
                        "its repr() is shown under [return value]. "
                        "The workspace is automatically on sys.path."
                    ),
                },
                "timeout": {
                    "type": "integer",
                    "description": "Execution timeout in seconds (default: 30, max: 120).",
                    "default": _DEFAULT_TIMEOUT,
                },
                "max_output_chars": {
                    "type": "integer",
                    "description": (
                        "Maximum characters of combined output to return "
                        f"(default: {_DEFAULT_MAX_OUTPUT}, hard max: {_MAX_HARD_OUTPUT})."
                    ),
                    "default": _DEFAULT_MAX_OUTPUT,
                },
            },
            "required": ["snippet"],
        }

    async def execute(
        self,
        config: HarnessConfig,
        *,
        snippet: str,
        timeout: int = _DEFAULT_TIMEOUT,
        max_output_chars: int = _DEFAULT_MAX_OUTPUT,
    ) -> ToolResult:
        if not snippet.strip():
            return ToolResult(error="snippet must not be empty", is_error=True)

        # Clamp timeout and output size to safe ranges
        timeout = max(1, min(timeout, 120))
        max_output_chars = max(100, min(max_output_chars, _MAX_HARD_OUTPUT))

        # Build the wrapper script text
        wrapper_code = _build_wrapper(snippet)

        # Build environment: inherit current env, prepend workspace to PYTHONPATH
        env = os.environ.copy()
        workspace = config.workspace
        existing_pythonpath = env.get("PYTHONPATH", "")
        if existing_pythonpath:
            env["PYTHONPATH"] = f"{workspace}{os.pathsep}{existing_pythonpath}"
        else:
            env["PYTHONPATH"] = workspace
        env["PYTHONUTF8"] = "1"

        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,        # same Python interpreter as the harness
                "-c",
                wrapper_code,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
                cwd=workspace,
                env=env,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            if proc is not None:
                proc.kill()
                await proc.wait()
            return ToolResult(
                error=(
                    f"Python snippet timed out after {timeout}s. "
                    "Check for infinite loops, blocking I/O, or overly expensive operations."
                ),
                is_error=True,
            )
        except Exception as exc:
            return ToolResult(
                error=f"Failed to launch Python subprocess: {type(exc).__name__}: {exc}",
                is_error=True,
            )

        exit_code: int = proc.returncode  # type: ignore[assignment]
        stdout_text = stdout_bytes.decode(errors="replace")
        stderr_text = stderr_bytes.decode(errors="replace")

        output, _return_val, is_error = _format_output(
            stdout_text, stderr_text, exit_code, max_output_chars
        )

        if is_error:
            return ToolResult(output=output, error=output, is_error=True)
        return ToolResult(output=output)
