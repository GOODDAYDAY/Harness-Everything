"""Unit tests for harness/pipeline/planner.py."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from harness.pipeline.planner import Planner, _strip_risks_section
from harness.pipeline.three_way import ThreeWayResult


# ---------------------------------------------------------------------------
# _strip_risks_section tests
# ---------------------------------------------------------------------------


class TestStripRisksSection:
    """Tests for _strip_risks_section helper."""

    def test_no_risks_section_unchanged(self) -> None:
        plan = "1. Implement feature A\n2. Write tests\n3. Deploy"
        assert _strip_risks_section(plan) == plan

    def test_strips_risks_section_with_dash_bullets(self) -> None:
        plan = "1. Do X\n2. Do Y\nRISKS:\n- Risk A\n- Risk B\n"
        result = _strip_risks_section(plan)
        assert "RISKS:" not in result
        assert "Risk A" not in result
        assert "1. Do X" in result
        assert "2. Do Y" in result

    def test_strips_risks_section_with_asterisk_bullets(self) -> None:
        plan = "1. Step one\n\nRISKS:\n* performance risk\n* security risk\n"
        result = _strip_risks_section(plan)
        assert "RISKS:" not in result
        assert "Step one" in result

    def test_strips_at_end_of_plan(self) -> None:
        plan = "1. Implementation\nRISKS:\n- could break\n"
        result = _strip_risks_section(plan)
        assert "RISKS:" not in result

    def test_empty_string_unchanged(self) -> None:
        assert _strip_risks_section("") == ""

    def test_only_risks_section_returns_empty(self) -> None:
        plan = "RISKS:\n- something bad\n"
        result = _strip_risks_section(plan)
        assert result.strip() == ""

    def test_risks_at_end_with_trailing_newlines(self) -> None:
        plan = "1. Step\n\nRISKS:\n- Risk X\n- Risk Y\n\n"
        result = _strip_risks_section(plan)
        assert "RISKS:" not in result
        assert result.strip() == "1. Step"

    def test_risks_section_with_bullet_point_char(self) -> None:
        plan = "1. Step\nRISKS:\n\u2022 bullet point risk\n"
        result = _strip_risks_section(plan)
        assert "RISKS:" not in result

    def test_no_strips_inline_risks_word(self) -> None:
        """A line containing 'RISKS' but not a section header is kept."""
        plan = "1. Mitigate security RISKS by patching\n2. Deploy"
        result = _strip_risks_section(plan)
        assert result == plan


# ---------------------------------------------------------------------------
# Planner.plan() tests
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_config() -> MagicMock:
    """Minimal HarnessConfig mock with planner sub-config."""
    config = MagicMock()
    config.planner.conservative_system = None
    config.planner.aggressive_system = None
    config.planner.merge_system = None
    return config


@pytest.fixture
def mock_llm() -> MagicMock:
    return MagicMock()


class TestPlannerPlan:
    """Tests for Planner.plan()."""

    @pytest.mark.asyncio
    async def test_plan_returns_merged_text(self, mock_llm: MagicMock, mock_config: MagicMock) -> None:
        planner = Planner(mock_llm, mock_config)
        merged_text = "1. Write code\n2. Test it\n3. Deploy"
        with patch.object(
            planner.resolver,
            "resolve",
            new=AsyncMock(return_value=ThreeWayResult(
                conservative="conservative plan",
                aggressive="aggressive plan",
                merged=merged_text,
            )),
        ):
            result = await planner.plan("Build feature X")
        assert result == merged_text

    @pytest.mark.asyncio
    async def test_plan_strips_risks_section(self, mock_llm: MagicMock, mock_config: MagicMock) -> None:
        planner = Planner(mock_llm, mock_config)
        merged_with_risks = "1. Implement\n2. Test\nRISKS:\n- might break\n"
        with patch.object(
            planner.resolver,
            "resolve",
            new=AsyncMock(return_value=ThreeWayResult(
                conservative="cons",
                aggressive="aggr",
                merged=merged_with_risks,
            )),
        ):
            result = await planner.plan("Fix the bug")
        assert "RISKS:" not in result
        assert "might break" not in result
        assert "1. Implement" in result

    @pytest.mark.asyncio
    async def test_plan_includes_task_in_user_message(self, mock_llm: MagicMock, mock_config: MagicMock) -> None:
        planner = Planner(mock_llm, mock_config)
        captured_messages: list[str] = []

        async def capture_resolve(user_message: str, **kwargs: object) -> ThreeWayResult:
            captured_messages.append(user_message)
            return ThreeWayResult(conservative="c", aggressive="a", merged="1. step")

        with patch.object(planner.resolver, "resolve", new=capture_resolve):
            await planner.plan("Build feature Y")

        assert len(captured_messages) == 1
        assert "Build feature Y" in captured_messages[0]
        assert "## Task" in captured_messages[0]

    @pytest.mark.asyncio
    async def test_plan_includes_context_when_provided(self, mock_llm: MagicMock, mock_config: MagicMock) -> None:
        planner = Planner(mock_llm, mock_config)
        captured_messages: list[str] = []

        async def capture_resolve(user_message: str, **kwargs: object) -> ThreeWayResult:
            captured_messages.append(user_message)
            return ThreeWayResult(conservative="c", aggressive="a", merged="1. step")

        with patch.object(planner.resolver, "resolve", new=capture_resolve):
            await planner.plan("Do the thing", context="File: foo.py\ndef bar(): pass")

        assert "## Context" in captured_messages[0]
        assert "foo.py" in captured_messages[0]

    @pytest.mark.asyncio
    async def test_plan_no_context_omits_context_section(self, mock_llm: MagicMock, mock_config: MagicMock) -> None:
        planner = Planner(mock_llm, mock_config)
        captured_messages: list[str] = []

        async def capture_resolve(user_message: str, **kwargs: object) -> ThreeWayResult:
            captured_messages.append(user_message)
            return ThreeWayResult(conservative="c", aggressive="a", merged="1. step")

        with patch.object(planner.resolver, "resolve", new=capture_resolve):
            await planner.plan("Do the thing")

        assert "## Context" not in captured_messages[0]

    @pytest.mark.asyncio
    async def test_plan_uses_custom_prompts_from_config(self, mock_llm: MagicMock, mock_config: MagicMock) -> None:
        """Custom system prompts in config override defaults."""
        mock_config.planner.conservative_system = "MY CONSERVATIVE SYS"
        mock_config.planner.aggressive_system = "MY AGGRESSIVE SYS"
        mock_config.planner.merge_system = "MY MERGE SYS"
        planner = Planner(mock_llm, mock_config)
        captured_kwargs: list[dict] = []

        async def capture_resolve(user_message: str, **kwargs: object) -> ThreeWayResult:
            captured_kwargs.append(dict(kwargs))
            return ThreeWayResult(conservative="c", aggressive="a", merged="1. step")

        with patch.object(planner.resolver, "resolve", new=capture_resolve):
            await planner.plan("task")

        assert captured_kwargs[0]["conservative_system"] == "MY CONSERVATIVE SYS"
        assert captured_kwargs[0]["aggressive_system"] == "MY AGGRESSIVE SYS"
        assert captured_kwargs[0]["merge_system"] == "MY MERGE SYS"

    @pytest.mark.asyncio
    async def test_plan_uses_default_prompts_when_config_is_none(self, mock_llm: MagicMock, mock_config: MagicMock) -> None:
        """None in config falls back to harness.prompts.planner defaults."""
        from harness.prompts import planner as default_prompts

        planner = Planner(mock_llm, mock_config)
        captured_kwargs: list[dict] = []

        async def capture_resolve(user_message: str, **kwargs: object) -> ThreeWayResult:
            captured_kwargs.append(dict(kwargs))
            return ThreeWayResult(conservative="c", aggressive="a", merged="1. step")

        with patch.object(planner.resolver, "resolve", new=capture_resolve):
            await planner.plan("task")

        assert captured_kwargs[0]["conservative_system"] == default_prompts.CONSERVATIVE_SYSTEM
        assert captured_kwargs[0]["aggressive_system"] == default_prompts.AGGRESSIVE_SYSTEM
        assert captured_kwargs[0]["merge_system"] == default_prompts.MERGE_SYSTEM


class TestMergeSystemPromptQuality:
    """Guard the quality properties of the MERGE_SYSTEM prompt."""

    def test_merge_system_self_consistency_check_is_concise(self):
        """SELF-CONSISTENCY CHECK must not have redundant 'import additions' item separate from symbol item."""
        from harness.prompts import planner as default_prompts

        merge = default_prompts.MERGE_SYSTEM
        assert "SELF-CONSISTENCY CHECK" in merge, "SELF-CONSISTENCY CHECK section must exist"
        # After merge, import additions should be covered within the symbol item (not as a separate line)
        assert "all import additions paired with the corresponding symbol creation" not in merge, (
            "Redundant 'import additions' item should be merged into the symbol definition item"
        )

    def test_merge_system_critical_output_requirement_is_concise(self):
        """CRITICAL OUTPUT REQUIREMENT must not have redundant 'executor will fail' sentence."""
        from harness.prompts import planner as default_prompts

        merge = default_prompts.MERGE_SYSTEM
        assert "CRITICAL OUTPUT REQUIREMENT" in merge, "CRITICAL OUTPUT REQUIREMENT section must exist"
        # Redundant sentence was removed
        assert "The executor will fail if it receives anything other than" not in merge, (
            "Redundant 'executor will fail' sentence must not be present (covered by first sentence)"
        )
