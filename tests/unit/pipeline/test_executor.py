"""Tests for harness.pipeline.executor module."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from harness.pipeline.executor import ExecutionResult, Executor, executor_system_with_workspace


# ---------------------------------------------------------------------------
# executor_system_with_workspace
# ---------------------------------------------------------------------------

class TestExecutorSystemWithWorkspace:
    """Tests for executor_system_with_workspace(workspace)."""

    def test_returns_string(self):
        """Return type is always a string."""
        result = executor_system_with_workspace("/some/path")
        assert isinstance(result, str)

    def test_workspace_path_embedded(self):
        """The provided workspace path appears in the output."""
        ws = "/my/project/root"
        result = executor_system_with_workspace(ws)
        assert ws in result

    def test_different_workspaces_differ(self):
        """Two different workspace paths produce different prompts."""
        a = executor_system_with_workspace("/path/a")
        b = executor_system_with_workspace("/path/b")
        assert a != b

    def test_same_workspace_stable(self):
        """Same workspace path produces the same prompt (deterministic)."""
        ws = "/deterministic/path"
        assert executor_system_with_workspace(ws) == executor_system_with_workspace(ws)

    def test_contains_batch_read_guidance(self):
        """The prompt must mention batch_read (critical tool guidance)."""
        result = executor_system_with_workspace("/tmp")
        assert "batch_read" in result

    def test_contains_never_cat_guidance(self):
        """The prompt must discourage using bash to read source files."""
        result = executor_system_with_workspace("/tmp")
        # Should have NEVER or strong discouragement for cat/bash-reading
        assert "NEVER" in result or "never" in result

    def test_not_empty(self):
        """The returned system prompt is not empty."""
        result = executor_system_with_workspace("/tmp")
        assert result.strip()

    def test_substantial_length(self):
        """The system prompt is substantial — at least 500 chars."""
        result = executor_system_with_workspace("/some/workspace")
        assert len(result) > 500, (
            f"Expected substantial system prompt, got {len(result)} chars"
        )

    def test_empty_workspace_path(self):
        """Empty workspace path is handled without error."""
        result = executor_system_with_workspace("")
        assert isinstance(result, str)

    def test_workspace_with_spaces(self):
        """Workspace path with spaces is embedded correctly."""
        ws = "/path with spaces/project"
        result = executor_system_with_workspace(ws)
        assert ws in result


# ---------------------------------------------------------------------------
# ExecutionResult  dataclass
# ---------------------------------------------------------------------------

class TestExecutionResult:
    """Tests for the ExecutionResult dataclass."""

    def test_default_construction(self):
        """Default constructor sets sensible empty defaults."""
        r = ExecutionResult()
        assert r.text == ""
        assert r.log == []
        assert r.files_changed == []

    def test_explicit_fields(self):
        """All fields can be set explicitly."""
        log = [{"role": "tool", "name": "bash"}]
        r = ExecutionResult(
            text="done",
            log=log,
            files_changed=["a.py", "b.py"],
        )
        assert r.text == "done"
        assert r.log == log
        assert r.files_changed == ["a.py", "b.py"]

    def test_log_defaults_are_independent(self):
        """Each default-constructed result has its own list (no shared state)."""
        r1 = ExecutionResult()
        r2 = ExecutionResult()
        r1.log.append({"role": "tool"})
        assert r2.log == [], "Mutating r1.log should not affect r2.log"

    def test_files_changed_defaults_are_independent(self):
        """Each default-constructed result has its own files_changed list."""
        r1 = ExecutionResult()
        r2 = ExecutionResult()
        r1.files_changed.append("foo.py")
        assert r2.files_changed == [], "Mutating r1.files_changed should not affect r2"

    def test_text_can_be_multiline(self):
        """text field handles multi-line strings."""
        text = "line1\nline2\nline3"
        r = ExecutionResult(text=text)
        assert r.text == text

    def test_many_files_changed(self):
        """files_changed supports many entries."""
        files = [f"file_{i}.py" for i in range(100)]
        r = ExecutionResult(files_changed=files)
        assert len(r.files_changed) == 100

    def test_log_can_hold_arbitrary_dicts(self):
        """log field accepts list of arbitrary dicts."""
        log = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "tool", "name": "bash", "output": "ok"},
        ]
        r = ExecutionResult(log=log)
        assert len(r.log) == 3
        assert r.log[2]["name"] == "bash"


# ---------------------------------------------------------------------------
# Executor class
# ---------------------------------------------------------------------------

class TestExecutorInit:
    """Tests for Executor.__init__."""

    def _make_executor(self, max_tool_turns=60):
        from harness.core.config import HarnessConfig
        llm = MagicMock()
        registry = MagicMock()
        config = HarnessConfig()
        config.max_tool_turns = max_tool_turns
        return Executor(llm, registry, config), llm, registry, config

    def test_stores_llm(self):
        e, llm, _, _ = self._make_executor()
        assert e.llm is llm

    def test_stores_registry(self):
        e, _, registry, _ = self._make_executor()
        assert e.registry is registry

    def test_stores_config(self):
        e, _, _, config = self._make_executor()
        assert e.config is config

    def test_different_instances_independent(self):
        e1, llm1, _, _ = self._make_executor()
        e2, llm2, _, _ = self._make_executor()
        assert e1.llm is not e2.llm


class TestExecutorExecute:
    """Tests for Executor.execute (async)."""

    def _make_executor(self, return_text="output", return_log=None, max_tool_turns=60):
        from harness.core.config import HarnessConfig
        if return_log is None:
            return_log = []
        llm = MagicMock()
        llm.call_with_tools = AsyncMock(return_value=(return_text, return_log))
        registry = MagicMock()
        config = HarnessConfig()
        config.max_tool_turns = max_tool_turns
        return Executor(llm, registry, config), llm

    @pytest.mark.asyncio
    async def test_returns_execution_result(self):
        e, _ = self._make_executor()
        result = await e.execute("do something")
        assert isinstance(result, ExecutionResult)

    @pytest.mark.asyncio
    async def test_result_text_equals_llm_text(self):
        e, _ = self._make_executor(return_text="done with task")
        result = await e.execute("plan")
        assert result.text == "done with task"

    @pytest.mark.asyncio
    async def test_result_log_equals_llm_log(self):
        log = [{"tool": "bash", "output": "ok"}]
        e, _ = self._make_executor(return_log=log)
        result = await e.execute("plan")
        assert result.log is log

    @pytest.mark.asyncio
    async def test_no_context_message_format(self):
        e, llm = self._make_executor()
        await e.execute("step 1\nstep 2")
        messages = llm.call_with_tools.call_args[0][0]
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        content = messages[0]["content"]
        assert "## Plan to Execute" in content
        assert "step 1" in content
        assert "step 2" in content

    @pytest.mark.asyncio
    async def test_no_context_does_not_include_additional_context_header(self):
        e, llm = self._make_executor()
        await e.execute("my plan")
        messages = llm.call_with_tools.call_args[0][0]
        content = messages[0]["content"]
        assert "## Additional Context" not in content

    @pytest.mark.asyncio
    async def test_with_context_included_in_message(self):
        e, llm = self._make_executor()
        await e.execute("my plan", context="extra info here")
        messages = llm.call_with_tools.call_args[0][0]
        content = messages[0]["content"]
        assert "## Plan to Execute" in content
        assert "my plan" in content
        assert "## Additional Context" in content
        assert "extra info here" in content

    @pytest.mark.asyncio
    async def test_max_turns_passed_to_llm(self):
        e, llm = self._make_executor(max_tool_turns=42)
        await e.execute("plan")
        kwargs = llm.call_with_tools.call_args[1]
        assert kwargs["max_turns"] == 42

    @pytest.mark.asyncio
    async def test_registry_passed_to_llm(self):
        e, llm = self._make_executor()
        await e.execute("plan")
        args = llm.call_with_tools.call_args[0]
        assert args[1] is e.registry

    @pytest.mark.asyncio
    async def test_system_prompt_is_string(self):
        e, llm = self._make_executor()
        await e.execute("plan")
        kwargs = llm.call_with_tools.call_args[1]
        assert isinstance(kwargs["system"], str)

    @pytest.mark.asyncio
    async def test_system_prompt_contains_workspace(self):
        e, llm = self._make_executor()
        await e.execute("plan")
        kwargs = llm.call_with_tools.call_args[1]
        assert e.config.workspace in kwargs["system"]

    @pytest.mark.asyncio
    async def test_files_changed_empty_when_no_path_ops(self):
        log = [{"tool": "bash", "success": True, "input": {"command": "ls"}}]
        e, _ = self._make_executor(return_log=log)
        result = await e.execute("plan")
        assert result.files_changed == []

    @pytest.mark.asyncio
    async def test_files_changed_from_edit_file(self):
        log = [
            {
                "tool": "edit_file",
                "success": True,
                "input": {"path": "src/foo.py", "old_str": "a", "new_str": "b"},
            }
        ]
        e, _ = self._make_executor(return_log=log)
        result = await e.execute("plan")
        assert "src/foo.py" in result.files_changed

    @pytest.mark.asyncio
    async def test_files_changed_from_batch_write(self):
        log = [
            {
                "tool": "batch_write",
                "success": True,
                "input": {
                    "files": [
                        {"path": "a.py", "content": "x"},
                        {"path": "b.py", "content": "y"},
                    ]
                },
            }
        ]
        e, _ = self._make_executor(return_log=log)
        result = await e.execute("plan")
        assert "a.py" in result.files_changed
        assert "b.py" in result.files_changed

    @pytest.mark.asyncio
    async def test_empty_plan_is_accepted(self):
        e, _ = self._make_executor()
        result = await e.execute("")
        assert isinstance(result, ExecutionResult)

    @pytest.mark.asyncio
    async def test_plan_embedded_verbatim_in_message(self):
        plan = "UNIQUE_PLAN_MARKER_XYZ"
        e, llm = self._make_executor()
        await e.execute(plan)
        messages = llm.call_with_tools.call_args[0][0]
        assert plan in messages[0]["content"]
