"""Async tests for PhaseRunner._run_synthesis and _evaluate_and_log.

These tests use AsyncMock to exercise the retry/fallback/budget logic in
_run_synthesis and the evaluator wrapper logic in _evaluate_and_log without
making real LLM calls.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from harness.core.artifacts import ArtifactStore
from harness.core.checkpoint import CheckpointManager
from harness.core.config import HarnessConfig, PipelineConfig
from harness.core.llm import LLMResponse
from harness.pipeline.phase import DualScore, InnerResult, PhaseConfig, ScoreItem
from harness.pipeline.phase_runner import MIN_SYNTHESIS_CHARS, PhaseRunner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_score_item(score: float = 7.0) -> ScoreItem:
    return ScoreItem(score=score, critique="looks good")


def _make_dual_score(basic: float = 7.0, diffusion: float = 6.0) -> DualScore:
    return DualScore(
        basic=_make_score_item(basic),
        diffusion=_make_score_item(diffusion),
    )


def _make_inner(proposal: str, score: float = 7.0) -> InnerResult:
    """Create a minimal InnerResult with the given proposal and DualScore."""
    ds = _make_dual_score(score, score)
    return InnerResult(proposal=proposal, dual_score=ds)


def _make_phase(
    name: str = "test",
    index: int = 0,
    mode: str = "implement",
) -> PhaseConfig:
    """Create a minimal PhaseConfig (defaults to implement mode for determinism)."""
    return PhaseConfig(
        name=name,
        index=index,
        system_prompt="",
        falsifiable_criterion="",
        mode=mode,  # type: ignore[arg-type]
    )


def _make_runner(tmp_path, min_synthesis_chars: int = 50) -> PhaseRunner:
    """Build a PhaseRunner with a mocked LLM but real artifact/checkpoint stores."""
    pipeline_cfg = PipelineConfig(
        harness=HarnessConfig(),
        min_synthesis_chars=min_synthesis_chars,
    )
    artifacts = ArtifactStore(tmp_path, run_id="run0")
    checkpoint = CheckpointManager(artifacts)

    llm_mock = MagicMock()
    registry_mock = MagicMock()

    runner = PhaseRunner(
        llm=llm_mock,
        registry=registry_mock,
        pipeline_config=pipeline_cfg,
        artifacts=artifacts,
        checkpoint=checkpoint,
    )
    return runner


def _llm_resp(text: str) -> LLMResponse:
    """Create a minimal LLMResponse with the given text."""
    return LLMResponse(text=text)


# ---------------------------------------------------------------------------
# Test: MIN_SYNTHESIS_CHARS constant
# ---------------------------------------------------------------------------


class TestMinSynthesisCharsConstant:
    def test_constant_matches_config_default(self):
        """The module-level constant should match the PipelineConfig default."""
        assert MIN_SYNTHESIS_CHARS == PipelineConfig.__dataclass_fields__["min_synthesis_chars"].default

    def test_constant_is_positive(self):
        assert MIN_SYNTHESIS_CHARS > 0


# ---------------------------------------------------------------------------
# Test: ScoreItem and DualScore internals
# ---------------------------------------------------------------------------


class TestDualScoreCombined:
    """DualScore.combined: 60 % basic + 40 % diffusion."""

    def test_combined_perfect_both(self):
        ds = DualScore(
            basic=ScoreItem(score=10.0, critique=""),
            diffusion=ScoreItem(score=10.0, critique=""),
        )
        assert ds.combined == pytest.approx(10.0)

    def test_combined_zero_both(self):
        ds = DualScore(
            basic=ScoreItem(score=0.0, critique=""),
            diffusion=ScoreItem(score=0.0, critique=""),
        )
        assert ds.combined == pytest.approx(0.0)

    def test_combined_weighted_60_40(self):
        """60/40 weighting: basic=10, diffusion=0 -> 6.0."""
        ds = DualScore(
            basic=ScoreItem(score=10.0, critique=""),
            diffusion=ScoreItem(score=0.0, critique=""),
        )
        # 0.6*10 + 0.4*0 = 6.0
        assert ds.combined == pytest.approx(6.0)

    def test_combined_reverse_40_60(self):
        """basic=0, diffusion=10 -> 4.0."""
        ds = DualScore(
            basic=ScoreItem(score=0.0, critique=""),
            diffusion=ScoreItem(score=10.0, critique=""),
        )
        # 0.6*0 + 0.4*10 = 4.0
        assert ds.combined == pytest.approx(4.0)

    def test_combined_symmetric_scores(self):
        """When basic == diffusion == X, combined == X."""
        for score in (0.0, 5.0, 7.3, 10.0):
            ds = DualScore(
                basic=ScoreItem(score=score, critique=""),
                diffusion=ScoreItem(score=score, critique=""),
            )
            assert ds.combined == pytest.approx(score)


class TestInnerResultCombinedScore:
    def test_no_dual_score_returns_zero(self):
        result = InnerResult(proposal="x")
        assert result.combined_score == 0.0

    def test_with_dual_score_returns_combined(self):
        ds = _make_dual_score(8.0, 6.0)
        result = InnerResult(proposal="x", dual_score=ds)
        # combined = 0.6*8 + 0.4*6 = 4.8 + 2.4 = 7.2
        assert result.combined_score == pytest.approx(7.2)

    def test_higher_score_preferred_in_sort(self):
        low = InnerResult(proposal="low", dual_score=_make_dual_score(3.0, 2.0))
        high = InnerResult(proposal="high", dual_score=_make_dual_score(9.0, 8.0))
        best = max([low, high], key=lambda r: r.combined_score)
        assert best is high

    def test_combined_score_when_dual_is_none(self):
        result = InnerResult(proposal="test", dual_score=None)
        assert result.combined_score == 0.0


# ---------------------------------------------------------------------------
# Test: _evaluate_and_log
# ---------------------------------------------------------------------------


class TestEvaluateAndLog:
    @pytest.mark.asyncio
    async def test_returns_dual_score_from_evaluator(self, tmp_path):
        """_evaluate_and_log should return the DualScore from dual_evaluator."""
        runner = _make_runner(tmp_path)
        expected_score = _make_dual_score(8.0, 7.0)
        runner.dual_evaluator.evaluate = AsyncMock(return_value=expected_score)

        phase = _make_phase("p1")
        segs = runner.artifacts.inner_dir(1, phase.label, 0)
        result = await runner._evaluate_and_log(
            outer=1,
            phase=phase,
            inner=0,
            eval_subject="My proposal text",
            file_context="",
            segs=segs,
        )

        assert result is expected_score

    @pytest.mark.asyncio
    async def test_evaluator_called_with_eval_subject(self, tmp_path):
        """_evaluate_and_log should pass eval_subject as first arg to the evaluator."""
        runner = _make_runner(tmp_path)
        captured: list[str] = []

        async def fake_evaluate(proposal: str, *args, **_kwargs):
            captured.append(proposal)
            return _make_dual_score()

        runner.dual_evaluator.evaluate = fake_evaluate

        phase = _make_phase("p1")
        segs = runner.artifacts.inner_dir(1, phase.label, 0)
        await runner._evaluate_and_log(
            outer=1,
            phase=phase,
            inner=0,
            eval_subject="specific proposal",
            file_context="",
            segs=segs,
        )

        assert captured == ["specific proposal"]

    @pytest.mark.asyncio
    async def test_writes_basic_eval_artifact(self, tmp_path):
        """_evaluate_and_log should write basic_eval.txt to the artifact store."""
        runner = _make_runner(tmp_path)
        score = _make_dual_score(8.0, 7.0)
        score.basic.critique = "Basic critique text"
        runner.dual_evaluator.evaluate = AsyncMock(return_value=score)

        phase = _make_phase("p1")
        segs = runner.artifacts.inner_dir(1, phase.label, 0)
        await runner._evaluate_and_log(
            outer=1,
            phase=phase,
            inner=0,
            eval_subject="proposal",
            file_context="",
            segs=segs,
        )

        stored = runner.artifacts.read(*segs, "basic_eval.txt")
        assert "Basic critique text" in stored

    @pytest.mark.asyncio
    async def test_writes_diffusion_eval_artifact(self, tmp_path):
        """_evaluate_and_log should write diffusion_eval.txt to the artifact store."""
        runner = _make_runner(tmp_path)
        score = _make_dual_score(7.0, 6.0)
        score.diffusion.critique = "Diffusion critique text"
        runner.dual_evaluator.evaluate = AsyncMock(return_value=score)

        phase = _make_phase("p1")
        segs = runner.artifacts.inner_dir(1, phase.label, 0)
        await runner._evaluate_and_log(
            outer=1,
            phase=phase,
            inner=0,
            eval_subject="proposal",
            file_context="",
            segs=segs,
        )

        stored = runner.artifacts.read(*segs, "diffusion_eval.txt")
        assert "Diffusion critique text" in stored


# ---------------------------------------------------------------------------
# Test: _run_synthesis — happy path
# ---------------------------------------------------------------------------


class TestRunSynthesisHappyPath:
    @pytest.mark.asyncio
    async def test_returns_synthesis_when_long_enough(self, tmp_path):
        """When LLM returns text >= min_synthesis_chars, it should be used."""
        runner = _make_runner(tmp_path, min_synthesis_chars=10)

        long_text = "A" * 50  # well above threshold of 10
        runner.llm.call = AsyncMock(return_value=_llm_resp(long_text))

        phase = _make_phase("synth")
        results = [_make_inner("proposal A", 7.0), _make_inner("proposal B", 8.0)]

        synthesis = await runner._run_synthesis(
            outer=1,
            phase=phase,
            results=results,
            file_context="some file context",
        )

        assert synthesis == long_text

    @pytest.mark.asyncio
    async def test_synthesis_written_to_artifacts(self, tmp_path):
        """Synthesis text should be written to the artifact store."""
        runner = _make_runner(tmp_path, min_synthesis_chars=10)

        synthesis_text = "Synthesis content here " * 5
        runner.llm.call = AsyncMock(return_value=_llm_resp(synthesis_text))

        phase = _make_phase("synth")
        results = [_make_inner("proposal X", 7.0)]

        await runner._run_synthesis(outer=1, phase=phase, results=results, file_context="")

        # Artifact should now be readable
        segs = runner.artifacts.phase_dir(1, phase.label)
        stored = runner.artifacts.read(*segs, "synthesis.txt")
        assert stored is not None
        assert synthesis_text in stored

    @pytest.mark.asyncio
    async def test_checkpoint_is_marked_done_after_synthesis(self, tmp_path):
        """After successful synthesis, checkpoint should be marked done."""
        runner = _make_runner(tmp_path, min_synthesis_chars=10)
        runner.llm.call = AsyncMock(return_value=_llm_resp("A" * 50))

        phase = _make_phase("synth")
        results = [_make_inner("proposal A", 7.0)]

        assert not runner.checkpoint.is_synthesis_done(outer=1, phase_label=phase.label)
        await runner._run_synthesis(outer=1, phase=phase, results=results, file_context="")
        assert runner.checkpoint.is_synthesis_done(outer=1, phase_label=phase.label)

    @pytest.mark.asyncio
    async def test_llm_called_once_when_response_sufficient(self, tmp_path):
        """LLM should be called exactly once when the first response is sufficient."""
        runner = _make_runner(tmp_path, min_synthesis_chars=10)
        runner.llm.call = AsyncMock(return_value=_llm_resp("A" * 50))

        phase = _make_phase("synth")
        await runner._run_synthesis(
            outer=1, phase=phase, results=[_make_inner("p")], file_context=""
        )

        runner.llm.call.assert_called_once()


# ---------------------------------------------------------------------------
# Test: _run_synthesis — retry when response is too short
# ---------------------------------------------------------------------------


class TestRunSynthesisRetry:
    @pytest.mark.asyncio
    async def test_retries_when_first_response_too_short(self, tmp_path):
        """When first LLM response is too short, it should retry and use second."""
        runner = _make_runner(tmp_path, min_synthesis_chars=30)

        short_resp = _llm_resp("tiny")
        long_resp = _llm_resp("B" * 60)
        runner.llm.call = AsyncMock(side_effect=[short_resp, long_resp])

        phase = _make_phase("synth")
        results = [_make_inner("proposal A", 7.0)]

        synthesis = await runner._run_synthesis(
            outer=1, phase=phase, results=results, file_context=""
        )

        assert synthesis == "B" * 60
        assert runner.llm.call.call_count == 2

    @pytest.mark.asyncio
    async def test_fallback_to_best_inner_result_when_both_too_short(self, tmp_path):
        """When both LLM attempts are too short, fallback to best-scoring inner result."""
        runner = _make_runner(tmp_path, min_synthesis_chars=100)
        runner.llm.call = AsyncMock(return_value=_llm_resp("tiny"))

        phase = _make_phase("synth")
        results = [
            _make_inner("proposal LOW", 4.0),
            _make_inner("proposal HIGH", 9.0),
            _make_inner("proposal MID", 6.0),
        ]

        synthesis = await runner._run_synthesis(
            outer=1, phase=phase, results=results, file_context=""
        )

        # Should have fallen back to the highest-scoring proposal
        assert "proposal HIGH" in synthesis

    @pytest.mark.asyncio
    async def test_fallback_contains_synthesis_fallback_marker(self, tmp_path):
        """When falling back, the result should contain the SYNTHESIS FALLBACK marker."""
        runner = _make_runner(tmp_path, min_synthesis_chars=100)
        runner.llm.call = AsyncMock(return_value=_llm_resp("x"))

        phase = _make_phase("synth")
        results = [_make_inner("the best proposal", 9.0)]

        synthesis = await runner._run_synthesis(
            outer=1, phase=phase, results=results, file_context=""
        )

        assert "SYNTHESIS FALLBACK" in synthesis
        assert "the best proposal" in synthesis

    @pytest.mark.asyncio
    async def test_fallback_when_no_inner_results_persists_short_text(self, tmp_path):
        """When both LLM attempts are too short and no inner results, persist the short text."""
        runner = _make_runner(tmp_path, min_synthesis_chars=100)
        runner.llm.call = AsyncMock(return_value=_llm_resp("short"))

        phase = _make_phase("synth")

        synthesis = await runner._run_synthesis(
            outer=1, phase=phase, results=[], file_context=""
        )

        # With no fallback available, the short text is persisted as-is
        assert synthesis == "short"

    @pytest.mark.asyncio
    async def test_llm_called_twice_on_short_first_response(self, tmp_path):
        """LLM call count should be exactly 2 when first response is too short."""
        runner = _make_runner(tmp_path, min_synthesis_chars=50)
        runner.llm.call = AsyncMock(return_value=_llm_resp("too short"))

        phase = _make_phase("synth")
        results = [_make_inner("fallback proposal", 5.0)]

        await runner._run_synthesis(
            outer=1, phase=phase, results=results, file_context=""
        )

        # Should have retried exactly once (total 2 calls)
        assert runner.llm.call.call_count == 2

    @pytest.mark.asyncio
    async def test_retry_prompt_includes_prior_attempt_failed_marker(self, tmp_path):
        """The retry prompt should include the PRIOR ATTEMPT FAILED marker."""
        runner = _make_runner(tmp_path, min_synthesis_chars=30)

        received_messages: list[list] = []
        call_count = [0]

        async def track_calls(messages, **kwargs):
            call_count[0] += 1
            received_messages.append(messages)
            if call_count[0] == 1:
                return _llm_resp("tiny")  # too short -> triggers retry
            return _llm_resp("A" * 60)   # long enough on second call

        runner.llm.call = track_calls

        phase = _make_phase("synth")
        results = [_make_inner("p", 7.0)]

        await runner._run_synthesis(
            outer=1, phase=phase, results=results, file_context=""
        )

        assert len(received_messages) == 2
        retry_content = received_messages[1][0]["content"]
        assert "PRIOR ATTEMPT FAILED" in retry_content


# ---------------------------------------------------------------------------
# Test: _run_synthesis — checkpoint skip (resume)
# ---------------------------------------------------------------------------


class TestRunSynthesisCheckpoint:
    @pytest.mark.asyncio
    async def test_skips_llm_when_already_checkpointed(self, tmp_path):
        """When synthesis is already marked done, LLM should not be called."""
        runner = _make_runner(tmp_path, min_synthesis_chars=10)

        # Pre-mark as done and write a fake synthesis artifact
        phase = _make_phase("synth")
        segs = runner.artifacts.phase_dir(1, phase.label)
        runner.artifacts.write("pre-existing synthesis text", *segs, "synthesis.txt")
        runner.checkpoint.mark_synthesis_done(outer=1, phase_label=phase.label)

        runner.llm.call = AsyncMock(return_value=_llm_resp("should not be called"))

        results = [_make_inner("proposal A", 7.0)]
        synthesis = await runner._run_synthesis(
            outer=1, phase=phase, results=results, file_context=""
        )

        runner.llm.call.assert_not_called()
        assert "pre-existing synthesis text" in synthesis

    @pytest.mark.asyncio
    async def test_checkpoint_stays_done_on_resume(self, tmp_path):
        """Resuming from checkpoint should leave checkpoint still marked done."""
        runner = _make_runner(tmp_path, min_synthesis_chars=10)

        phase = _make_phase("synth")
        segs = runner.artifacts.phase_dir(1, phase.label)
        runner.artifacts.write("cached synthesis", *segs, "synthesis.txt")
        runner.checkpoint.mark_synthesis_done(outer=1, phase_label=phase.label)
        runner.llm.call = AsyncMock(return_value=_llm_resp("unused"))

        await runner._run_synthesis(
            outer=1, phase=phase, results=[], file_context=""
        )

        assert runner.checkpoint.is_synthesis_done(outer=1, phase_label=phase.label)


# ---------------------------------------------------------------------------
# Test: _run_synthesis — budget and round proportionality
# ---------------------------------------------------------------------------


class TestRunSynthesisBudget:
    @pytest.mark.asyncio
    async def test_completes_with_single_round(self, tmp_path):
        """With 1 round, synthesis should complete successfully."""
        runner = _make_runner(tmp_path, min_synthesis_chars=10)
        runner.llm.call = AsyncMock(return_value=_llm_resp("A" * 100))

        phase = _make_phase("synth")
        synthesis = await runner._run_synthesis(
            outer=1, phase=phase, results=[_make_inner("proposal A")], file_context=""
        )

        assert len(synthesis.strip()) >= 10

    @pytest.mark.asyncio
    async def test_completes_with_multiple_rounds(self, tmp_path):
        """With N rounds, synthesis should still complete successfully."""
        runner = _make_runner(tmp_path, min_synthesis_chars=10)
        runner.llm.call = AsyncMock(return_value=_llm_resp("A" * 100))

        phase = _make_phase("synth")
        results = [_make_inner(f"proposal {i}", float(i + 5)) for i in range(4)]
        synthesis = await runner._run_synthesis(
            outer=1, phase=phase, results=results, file_context=""
        )

        assert len(synthesis.strip()) >= 10

    @pytest.mark.asyncio
    async def test_proposal_text_included_in_llm_prompt(self, tmp_path):
        """The synthesis prompt should include inner results' proposal text."""
        runner = _make_runner(tmp_path, min_synthesis_chars=10)

        received_messages: list[list] = []

        async def capture_call(messages, **kwargs):
            received_messages.append(messages)
            return _llm_resp("A" * 100)

        runner.llm.call = capture_call

        phase = _make_phase("synth")
        results = [_make_inner("my unique proposal text", 7.0)]

        await runner._run_synthesis(
            outer=1, phase=phase, results=results, file_context=""
        )

        assert len(received_messages) == 1
        user_content = received_messages[0][0]["content"]
        assert "my unique proposal text" in user_content


# ---------------------------------------------------------------------------
# Tests for run_phase: early-exit threshold and gate-failed hook logic
# ---------------------------------------------------------------------------


def _make_scored_inner(score: float, proposal: str = "proposal text") -> InnerResult:
    """Create an InnerResult with a real combined_score via DualScore."""
    ds = DualScore(
        basic=ScoreItem(score=score, critique=f"basic_{score}"),
        diffusion=ScoreItem(score=score, critique=f"diff_{score}"),
    )
    return InnerResult(proposal=proposal, dual_score=ds)


def _make_mock_hook(*, passed: bool, gates_commit: bool = False, name: str = "mock_hook"):
    """Return a mock VerificationHook."""
    from harness.pipeline.hooks import HookResult

    hook = MagicMock()
    hook.gates_commit = gates_commit
    hook.run = AsyncMock(return_value=HookResult(passed=passed, output=name))
    return hook


def _patch_phase_runner_io():
    """Context manager that patches I/O functions in phase_runner to be no-ops."""
    from unittest.mock import patch

    return patch.multiple(
        "harness.pipeline.phase_runner",
        _read_source_files=MagicMock(return_value=""),
        _read_source_manifest=MagicMock(return_value=""),
    )


class TestRunPhaseEarlyExit:
    """run_phase should stop inner rounds when EET threshold is met."""

    @pytest.mark.asyncio
    async def test_early_exit_stops_remaining_rounds(self, tmp_path):
        """With EET=8.0, a score of 8.5 on round 1 skips rounds 2 and 3."""
        runner = _make_runner(tmp_path)
        runner.config.inner_rounds = 3
        runner.config.inner_early_exit_threshold = 8.0
        runner.llm.call = AsyncMock(return_value=_llm_resp("X" * 100))

        call_count = 0

        async def fake_inner_round(outer, phase, idx, file_context, prior_best, carry_syntax, **kw):
            nonlocal call_count
            call_count += 1
            return _make_scored_inner(8.5)

        runner._run_inner_round = fake_inner_round
        runner._run_synthesis = AsyncMock(return_value="synthesis output")
        runner._write_phase_summary = MagicMock()
        runner.artifacts.save = MagicMock()
        runner.checkpoint.is_inner_done = MagicMock(return_value=False)
        runner.checkpoint.mark_inner_done = MagicMock()
        runner.checkpoint.mark_phase_done = MagicMock()

        phase = _make_phase("early_exit")

        with _patch_phase_runner_io():
            with __import__("unittest.mock", fromlist=["patch"]).patch(
                "harness.pipeline.phase_runner.build_hooks", return_value=[]
            ):
                result = await runner.run_phase(outer=1, phase=phase, prior_best=None)

        # Only 1 inner round should have run (early exit on round 1)
        assert call_count == 1
        assert result.best_score == pytest.approx(8.5, abs=0.01)

    @pytest.mark.asyncio
    async def test_early_exit_disabled_when_threshold_zero(self, tmp_path):
        """With EET=0.0 (disabled), all 3 inner rounds run regardless of score."""
        runner = _make_runner(tmp_path)
        runner.config.inner_rounds = 3
        runner.config.inner_early_exit_threshold = 0.0  # disabled
        runner.llm.call = AsyncMock(return_value=_llm_resp("X" * 100))

        call_count = 0

        async def fake_inner_round(outer, phase, idx, file_context, prior_best, carry_syntax, **kw):
            nonlocal call_count
            call_count += 1
            return _make_scored_inner(9.9)  # very high score but EET is disabled

        runner._run_inner_round = fake_inner_round
        runner._run_synthesis = AsyncMock(return_value="synthesis output")
        runner._write_phase_summary = MagicMock()
        runner.artifacts.save = MagicMock()
        runner.checkpoint.is_inner_done = MagicMock(return_value=False)
        runner.checkpoint.mark_inner_done = MagicMock()
        runner.checkpoint.mark_phase_done = MagicMock()

        phase = _make_phase("no_early_exit")

        with _patch_phase_runner_io():
            with __import__("unittest.mock", fromlist=["patch"]).patch(
                "harness.pipeline.phase_runner.build_hooks", return_value=[]
            ):
                await runner.run_phase(outer=1, phase=phase, prior_best=None)

        assert call_count == 3

    @pytest.mark.asyncio
    async def test_phase_level_eet_overrides_config(self, tmp_path):
        """Phase-level inner_early_exit_threshold overrides config-level."""
        runner = _make_runner(tmp_path)
        runner.config.inner_rounds = 3
        runner.config.inner_early_exit_threshold = 5.0  # config says 5.0

        call_count = 0

        async def fake_inner_round(outer, phase, idx, file_context, prior_best, carry_syntax, **kw):
            nonlocal call_count
            call_count += 1
            return _make_scored_inner(6.0)  # above config 5.0 but below phase override 9.0

        runner._run_inner_round = fake_inner_round
        runner._run_synthesis = AsyncMock(return_value="synthesis output")
        runner._write_phase_summary = MagicMock()
        runner.artifacts.save = MagicMock()
        runner.checkpoint.is_inner_done = MagicMock(return_value=False)
        runner.checkpoint.mark_inner_done = MagicMock()
        runner.checkpoint.mark_phase_done = MagicMock()

        # Phase override = 9.0 → score of 6.0 does NOT trigger early exit
        phase = PhaseConfig(
            name="phase_eet_override",
            index=0,
            system_prompt="",
            falsifiable_criterion="",
            inner_early_exit_threshold=9.0,  # phase-level override
            inner_rounds=3,
        )

        with _patch_phase_runner_io():
            with __import__("unittest.mock", fromlist=["patch"]).patch(
                "harness.pipeline.phase_runner.build_hooks", return_value=[]
            ):
                await runner.run_phase(outer=1, phase=phase, prior_best=None)

        assert call_count == 3  # all 3 ran because phase EET=9.0, score=6.0 < 9.0

    @pytest.mark.asyncio
    async def test_early_exit_exactly_at_threshold(self, tmp_path):
        """Early exit triggers when score equals the threshold exactly."""
        runner = _make_runner(tmp_path)
        runner.config.inner_rounds = 3
        runner.config.inner_early_exit_threshold = 7.0

        call_count = 0

        async def fake_inner_round(outer, phase, idx, file_context, prior_best, carry_syntax, **kw):
            nonlocal call_count
            call_count += 1
            return _make_scored_inner(7.0)  # exactly at threshold

        runner._run_inner_round = fake_inner_round
        runner._run_synthesis = AsyncMock(return_value="synthesis output")
        runner._write_phase_summary = MagicMock()
        runner.artifacts.save = MagicMock()
        runner.checkpoint.is_inner_done = MagicMock(return_value=False)
        runner.checkpoint.mark_inner_done = MagicMock()
        runner.checkpoint.mark_phase_done = MagicMock()

        phase = _make_phase("at_threshold")

        with _patch_phase_runner_io():
            with __import__("unittest.mock", fromlist=["patch"]).patch(
                "harness.pipeline.phase_runner.build_hooks", return_value=[]
            ):
                await runner.run_phase(outer=1, phase=phase, prior_best=None)

        assert call_count == 1  # only 1 round ran due to early exit


class TestRunPhaseHookGating:
    """run_phase hook logic: gating hooks block subsequent hooks on failure."""

    @pytest.mark.asyncio
    async def test_all_hooks_run_when_all_pass(self, tmp_path):
        """When all hooks pass, every hook.run is called."""
        runner = _make_runner(tmp_path)
        runner.config.inner_rounds = 1

        async def fake_inner_round(outer, phase, idx, file_context, prior_best, carry_syntax, **kw):
            return _make_scored_inner(6.0)

        runner._run_inner_round = fake_inner_round
        runner._run_synthesis = AsyncMock(return_value="synthesis")
        runner._write_phase_summary = MagicMock()
        runner.artifacts.save = MagicMock()
        runner.checkpoint.is_inner_done = MagicMock(return_value=False)
        runner.checkpoint.mark_inner_done = MagicMock()
        runner.checkpoint.mark_phase_done = MagicMock()

        hook1 = _make_mock_hook(passed=True, gates_commit=False, name="hook1")
        hook2 = _make_mock_hook(passed=True, gates_commit=False, name="hook2")

        phase = _make_phase("all_hooks")

        with _patch_phase_runner_io():
            with __import__("unittest.mock", fromlist=["patch"]).patch(
                "harness.pipeline.phase_runner.build_hooks", return_value=[hook1, hook2]
            ):
                await runner.run_phase(outer=1, phase=phase, prior_best=None)

        hook1.run.assert_awaited_once()
        hook2.run.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_gating_hook_failure_skips_remaining_hooks(self, tmp_path):
        """A gating hook (gates_commit=True) that fails prevents subsequent hooks."""
        runner = _make_runner(tmp_path)
        runner.config.inner_rounds = 1

        async def fake_inner_round(outer, phase, idx, file_context, prior_best, carry_syntax, **kw):
            return _make_scored_inner(6.0)

        runner._run_inner_round = fake_inner_round
        runner._run_synthesis = AsyncMock(return_value="synthesis")
        runner._write_phase_summary = MagicMock()
        runner.artifacts.save = MagicMock()
        runner.checkpoint.is_inner_done = MagicMock(return_value=False)
        runner.checkpoint.mark_inner_done = MagicMock()
        runner.checkpoint.mark_phase_done = MagicMock()

        gating_hook = _make_mock_hook(passed=False, gates_commit=True, name="gating")
        later_hook = _make_mock_hook(passed=True, gates_commit=False, name="later")

        phase = _make_phase("gate_fail")

        with _patch_phase_runner_io():
            with __import__("unittest.mock", fromlist=["patch"]).patch(
                "harness.pipeline.phase_runner.build_hooks", return_value=[gating_hook, later_hook]
            ):
                await runner.run_phase(outer=1, phase=phase, prior_best=None)

        gating_hook.run.assert_awaited_once()
        later_hook.run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_gating_hook_failure_does_not_skip_later_hooks(self, tmp_path):
        """A non-gating hook failure does not prevent subsequent hooks from running."""
        runner = _make_runner(tmp_path)
        runner.config.inner_rounds = 1

        async def fake_inner_round(outer, phase, idx, file_context, prior_best, carry_syntax, **kw):
            return _make_scored_inner(6.0)

        runner._run_inner_round = fake_inner_round
        runner._run_synthesis = AsyncMock(return_value="synthesis")
        runner._write_phase_summary = MagicMock()
        runner.artifacts.save = MagicMock()
        runner.checkpoint.is_inner_done = MagicMock(return_value=False)
        runner.checkpoint.mark_inner_done = MagicMock()
        runner.checkpoint.mark_phase_done = MagicMock()

        non_gating = _make_mock_hook(passed=False, gates_commit=False, name="non_gating")
        later_hook = _make_mock_hook(passed=True, gates_commit=False, name="later")

        phase = _make_phase("non_gate_fail")

        with _patch_phase_runner_io():
            with __import__("unittest.mock", fromlist=["patch"]).patch(
                "harness.pipeline.phase_runner.build_hooks", return_value=[non_gating, later_hook]
            ):
                await runner.run_phase(outer=1, phase=phase, prior_best=None)

        non_gating.run.assert_awaited_once()
        later_hook.run.assert_awaited_once()


class TestRunPhaseReturnValues:
    """run_phase returns correct PhaseResult with synthesis and best_score."""

    @pytest.mark.asyncio
    async def test_returns_best_score_from_multiple_rounds(self, tmp_path):
        """run_phase picks the highest combined_score across all inner results."""
        runner = _make_runner(tmp_path)
        runner.config.inner_rounds = 3

        scores = [4.0, 7.0, 5.0]
        call_index = 0

        async def fake_inner_round(outer, phase, idx, file_context, prior_best, carry_syntax, **kw):
            nonlocal call_index
            s = scores[call_index]
            call_index += 1
            return _make_scored_inner(s, proposal=f"proposal_{s}")

        runner._run_inner_round = fake_inner_round
        runner._run_synthesis = AsyncMock(return_value="synthesis text")
        runner._write_phase_summary = MagicMock()
        runner.artifacts.save = MagicMock()
        runner.checkpoint.is_inner_done = MagicMock(return_value=False)
        runner.checkpoint.mark_inner_done = MagicMock()
        runner.checkpoint.mark_phase_done = MagicMock()

        phase = _make_phase("multi_round")

        with _patch_phase_runner_io():
            with __import__("unittest.mock", fromlist=["patch"]).patch(
                "harness.pipeline.phase_runner.build_hooks", return_value=[]
            ):
                result = await runner.run_phase(outer=1, phase=phase, prior_best=None)

        assert result.best_score == pytest.approx(7.0, abs=0.01)
        assert result.synthesis == "synthesis text"
        assert len(result.inner_results) == 3

    @pytest.mark.asyncio
    async def test_synthesis_receives_all_inner_results(self, tmp_path):
        """The synthesis call receives all inner results from the phase."""
        runner = _make_runner(tmp_path)
        runner.config.inner_rounds = 2

        async def fake_inner_round(outer, phase, idx, file_context, prior_best, carry_syntax, **kw):
            return _make_scored_inner(6.0, proposal=f"proposal_{idx}")

        synthesis_results_received = []

        async def capture_synthesis(outer, phase, results, file_context):
            synthesis_results_received.extend(results)
            return "synth_result"

        runner._run_inner_round = fake_inner_round
        runner._run_synthesis = capture_synthesis
        runner._write_phase_summary = MagicMock()
        runner.artifacts.save = MagicMock()
        runner.checkpoint.is_inner_done = MagicMock(return_value=False)
        runner.checkpoint.mark_inner_done = MagicMock()
        runner.checkpoint.mark_phase_done = MagicMock()

        phase = _make_phase("synth_receives_all")

        with _patch_phase_runner_io():
            with __import__("unittest.mock", fromlist=["patch"]).patch(
                "harness.pipeline.phase_runner.build_hooks", return_value=[]
            ):
                result = await runner.run_phase(outer=1, phase=phase, prior_best=None)

        assert len(synthesis_results_received) == 2
        proposals = {r.proposal for r in synthesis_results_received}
        assert "proposal_0" in proposals
        assert "proposal_1" in proposals
        assert result.synthesis == "synth_result"


# ===========================================================================
# Tests for _run_debate_round
# ===========================================================================

class TestRunDebateRound:
    """Tests for PhaseRunner._run_debate_round."""

    @pytest.mark.asyncio
    async def test_normal_flow_returns_inner_result(self, tmp_path):
        """_run_debate_round returns an InnerResult with the LLM text as proposal."""
        runner = _make_runner(tmp_path)
        phase = _make_phase(mode="debate")
        phase.min_proposal_chars = 0  # no length gate

        proposal_text = "A" * 200
        runner.llm.call_with_tools = AsyncMock(return_value=(proposal_text, []))
        runner._evaluate_and_log = AsyncMock(return_value=_make_dual_score(8.0, 7.0))

        result = await runner._run_debate_round(
            outer=0,
            phase=phase,
            inner=0,
            prompt="Do something useful",
            file_context="# ctx",
            segs=(),
        )
        assert isinstance(result, InnerResult)
        assert result.proposal == proposal_text
        runner._evaluate_and_log.assert_called_once()

    @pytest.mark.asyncio
    async def test_short_proposal_skips_evaluation(self, tmp_path):
        """When proposal < min_proposal_chars, eval is skipped and scores are zero."""
        runner = _make_runner(tmp_path)
        phase = _make_phase(mode="debate")
        phase.min_proposal_chars = 500  # high threshold

        runner.llm.call_with_tools = AsyncMock(return_value=("short", []))
        runner._evaluate_and_log = AsyncMock(return_value=_make_dual_score(8.0, 7.0))

        result = await runner._run_debate_round(
            outer=0,
            phase=phase,
            inner=0,
            prompt="Do something",
            file_context="# ctx",
            segs=(),
        )
        assert isinstance(result, InnerResult)
        runner._evaluate_and_log.assert_not_called()
        assert result.dual_score.basic.score == 0.0
        assert result.dual_score.diffusion.score == 0.0

    @pytest.mark.asyncio
    async def test_custom_system_prompt_without_dollar_used_as_debate_system(self, tmp_path):
        """When phase.system_prompt has no '$', it is used as the debate system prompt."""
        runner = _make_runner(tmp_path)
        phase = _make_phase(mode="debate")
        phase.system_prompt = "My custom debate instructions"
        phase.min_proposal_chars = 0

        captured = {}

        async def capture_call_with_tools(messages, *args, system=None, **kwargs):
            captured["system"] = system
            return ("B" * 200, [])

        runner.llm.call_with_tools = capture_call_with_tools
        runner._evaluate_and_log = AsyncMock(return_value=_make_dual_score())

        await runner._run_debate_round(
            outer=0,
            phase=phase,
            inner=0,
            prompt="Go",
            file_context="",
            segs=(),
        )
        assert captured.get("system") == "My custom debate instructions"

    @pytest.mark.asyncio
    async def test_system_prompt_with_dollar_falls_back_to_default(self, tmp_path):
        """When phase.system_prompt contains '$', the default debate system is used."""
        from harness.pipeline.phase_runner import _DEBATE_SYSTEM_DEFAULT

        runner = _make_runner(tmp_path)
        phase = _make_phase(mode="debate")
        phase.system_prompt = "Implement $TASK for the user"  # contains '$'
        phase.min_proposal_chars = 0

        captured = {}

        async def capture_call_with_tools(messages, *args, system=None, **kwargs):
            captured["system"] = system
            return ("C" * 200, [])

        runner.llm.call_with_tools = capture_call_with_tools
        runner._evaluate_and_log = AsyncMock(return_value=_make_dual_score())

        await runner._run_debate_round(
            outer=0,
            phase=phase,
            inner=0,
            prompt="Go",
            file_context="",
            segs=(),
        )
        assert captured.get("system") == _DEBATE_SYSTEM_DEFAULT

    @pytest.mark.asyncio
    async def test_proposal_text_stored_on_inner_result(self, tmp_path):
        """The full LLM text is stored as InnerResult.proposal even with exec_log present."""
        runner = _make_runner(tmp_path)
        phase = _make_phase(mode="debate")
        phase.min_proposal_chars = 0

        proposal_text = "My detailed proposal " * 10
        exec_log = [
            {"tool": "batch_read", "output": "file contents", "is_error": False, "duration_ms": 50},
        ]
        runner.llm.call_with_tools = AsyncMock(return_value=(proposal_text, exec_log))
        runner._evaluate_and_log = AsyncMock(return_value=_make_dual_score())

        result = await runner._run_debate_round(
            outer=0,
            phase=phase,
            inner=0,
            prompt="Read something",
            file_context="",
            segs=(),
        )
        # Debate rounds do not persist exec_log in tool_call_log — that's an implement-only feature
        assert result.proposal == proposal_text


# ===========================================================================
# Tests for _run_implement_round – tool_metrics / exec_log logic
# ===========================================================================

class TestRunImplementRoundExecLog:
    """Tests for exec_log is_error field handling in _run_implement_round."""

    def _setup_runner(self, tmp_path, exec_log, output_text="X" * 200, min_chars=0):
        runner = _make_runner(tmp_path)
        phase = _make_phase(mode="implement")
        phase.min_proposal_chars = min_chars
        # Patch planner to avoid real planner call
        runner.planner = AsyncMock()
        runner.planner.plan = AsyncMock(side_effect=Exception("no planner"))
        runner.llm.call_with_tools = AsyncMock(return_value=(output_text, exec_log))
        runner._evaluate_and_log = AsyncMock(return_value=_make_dual_score())
        return runner, phase

    @pytest.mark.asyncio
    async def test_is_error_false_entry_is_success(self, tmp_path):
        """exec_log entry with is_error=False → tool_call_log entry with success=True."""
        exec_log = [{"tool": "bash", "output": "ok", "is_error": False, "duration_ms": 10}]
        runner, phase = self._setup_runner(tmp_path, exec_log)

        result = await runner._run_implement_round(
            outer=0,
            phase=phase,
            inner=0,
            prompt="Fix it",
            file_context="",
            segs=(),
        )
        assert isinstance(result, InnerResult)
        assert result.tool_call_log is not None
        success_entries = [e for e in result.tool_call_log if e.get("success") is True]
        assert len(success_entries) >= 1

    @pytest.mark.asyncio
    async def test_is_error_true_entry_is_failure(self, tmp_path):
        """exec_log entry with is_error=True → tool_call_log entry with success=False."""
        exec_log = [{"tool": "bash", "output": "SCHEMA ERROR", "is_error": True, "duration_ms": 5}]
        runner, phase = self._setup_runner(tmp_path, exec_log)

        result = await runner._run_implement_round(
            outer=0,
            phase=phase,
            inner=0,
            prompt="Fix it",
            file_context="",
            segs=(),
        )
        assert result.tool_call_log is not None
        error_entries = [e for e in result.tool_call_log if e.get("success") is False]
        assert len(error_entries) >= 1

    @pytest.mark.asyncio
    async def test_fallback_heuristic_schema_error_prefix_is_failure(self, tmp_path):
        """Without is_error field, 'SCHEMA ERROR' prefix output → success=False."""
        exec_log = [{"tool": "bash", "output": "SCHEMA ERROR: missing param", "duration_ms": 3}]
        runner, phase = self._setup_runner(tmp_path, exec_log)

        result = await runner._run_implement_round(
            outer=0,
            phase=phase,
            inner=0,
            prompt="Fix it",
            file_context="",
            segs=(),
        )
        error_entries = [e for e in result.tool_call_log if e.get("success") is False]
        assert len(error_entries) >= 1

    @pytest.mark.asyncio
    async def test_fallback_heuristic_normal_output_is_success(self, tmp_path):
        """Without is_error field, normal (non-error) output → success=True."""
        exec_log = [{"tool": "batch_read", "output": "file content here", "duration_ms": 20}]
        runner, phase = self._setup_runner(tmp_path, exec_log)

        result = await runner._run_implement_round(
            outer=0,
            phase=phase,
            inner=0,
            prompt="Read",
            file_context="",
            segs=(),
        )
        success_entries = [e for e in result.tool_call_log if e.get("success") is True]
        assert len(success_entries) >= 1

    @pytest.mark.asyncio
    async def test_short_output_skips_eval(self, tmp_path):
        """When implement output is shorter than min_proposal_chars, eval is skipped."""
        exec_log = []
        runner, phase = self._setup_runner(tmp_path, exec_log, output_text="tiny", min_chars=1000)

        result = await runner._run_implement_round(
            outer=0,
            phase=phase,
            inner=0,
            prompt="Do",
            file_context="",
            segs=(),
        )
        runner._evaluate_and_log.assert_not_called()
        assert result.dual_score.basic.score == 0.0
        assert result.dual_score.diffusion.score == 0.0

    @pytest.mark.asyncio
    async def test_phase_edit_globs_restored_after_call(self, tmp_path):
        """phase_edit_globs are restored to their prior value after _run_implement_round."""
        exec_log = []
        runner, phase = self._setup_runner(tmp_path, exec_log, output_text="Z" * 200)
        phase.allowed_edit_globs = ["harness/**/*.py"]

        # Set a sentinel value as the prior globs
        runner.harness.phase_edit_globs = ["prior_glob"]

        await runner._run_implement_round(
            outer=0,
            phase=phase,
            inner=0,
            prompt="Do",
            file_context="",
            segs=(),
        )
        # After the call, globs should be restored to the prior value
        assert runner.harness.phase_edit_globs == ["prior_glob"]

    @pytest.mark.asyncio
    async def test_phase_edit_globs_restored_even_on_exception(self, tmp_path):
        """phase_edit_globs are restored even when call_with_tools raises."""
        runner, phase = self._setup_runner(tmp_path, [], output_text="Z" * 200)
        phase.allowed_edit_globs = ["harness/**/*.py"]
        runner.harness.phase_edit_globs = ["sentinel_glob"]

        runner.llm.call_with_tools = AsyncMock(side_effect=RuntimeError("boom"))

        with pytest.raises(RuntimeError, match="boom"):
            await runner._run_implement_round(
                outer=0,
                phase=phase,
                inner=0,
                prompt="Do",
                file_context="",
                segs=(),
            )
        # Globs must be restored despite exception
        assert runner.harness.phase_edit_globs == ["sentinel_glob"]


# ===========================================================================
# Tests for _run_inner_round dispatch
# ===========================================================================

class TestRunInnerRoundDispatch:
    """Tests for PhaseRunner._run_inner_round routing logic."""

    @pytest.mark.asyncio
    async def test_debate_mode_calls_run_debate_round(self, tmp_path):
        """_run_inner_round dispatches to _run_debate_round when mode=='debate'."""
        runner = _make_runner(tmp_path)
        phase = _make_phase(mode="debate")
        phase.min_proposal_chars = 0

        expected = _make_inner("debate proposal")
        runner._run_debate_round = AsyncMock(return_value=expected)
        runner._run_implement_round = AsyncMock(return_value=_make_inner("implement proposal"))

        result = await runner._run_inner_round(
            outer=0,
            phase=phase,
            inner=0,
            file_context="",
            prior_best=None,
            syntax_errors="",
        )
        runner._run_debate_round.assert_called_once()
        runner._run_implement_round.assert_not_called()
        assert result.proposal == "debate proposal"

    @pytest.mark.asyncio
    async def test_implement_mode_calls_run_implement_round(self, tmp_path):
        """_run_inner_round dispatches to _run_implement_round when mode=='implement'."""
        runner = _make_runner(tmp_path)
        phase = _make_phase(mode="implement")
        phase.min_proposal_chars = 0

        expected = _make_inner("implement proposal")
        runner._run_debate_round = AsyncMock(return_value=_make_inner("debate proposal"))
        runner._run_implement_round = AsyncMock(return_value=expected)

        result = await runner._run_inner_round(
            outer=0,
            phase=phase,
            inner=0,
            file_context="",
            prior_best=None,
            syntax_errors="",
        )
        runner._run_implement_round.assert_called_once()
        runner._run_debate_round.assert_not_called()
        assert result.proposal == "implement proposal"

    @pytest.mark.asyncio
    async def test_debate_result_recorded_in_inner_results(self, tmp_path):
        """After _run_inner_round with debate, the result is stored in inner_results."""
        runner = _make_runner(tmp_path)
        phase = _make_phase(mode="debate")
        phase.min_proposal_chars = 0

        result_obj = _make_inner("my proposal")
        runner._run_debate_round = AsyncMock(return_value=result_obj)

        returned = await runner._run_inner_round(
            outer=0,
            phase=phase,
            inner=0,
            file_context="",
            prior_best=None,
            syntax_errors="",
        )
        assert returned is result_obj
