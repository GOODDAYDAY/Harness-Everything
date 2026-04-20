"""Tests for PhaseConfig validation."""

import pytest
from harness.pipeline.phase import PhaseConfig


def test_phase_config_glob_validation_raises_on_path_traversal():
    """Test that PhaseConfig raises ValueError for glob patterns with '..'."""
    with pytest.raises(ValueError, match="contains '..'.*path traversal"):
        PhaseConfig(
            name="test",
            index=0,
            system_prompt="test",
            allowed_edit_globs=["../*.py"]  # Should raise
        )


def test_phase_config_glob_validation_raises_on_absolute_path():
    """Test that PhaseConfig raises ValueError for absolute path globs."""
    with pytest.raises(ValueError, match="absolute path"):
        PhaseConfig(
            name="test",
            index=0,
            system_prompt="test",
            allowed_edit_globs=["/etc/passwd"]  # Should raise
        )


def test_phase_config_glob_validation_passes_valid_pattern():
    """Test that PhaseConfig accepts valid glob patterns."""
    config = PhaseConfig(
        name="test",
        index=0,
        system_prompt="test",
        allowed_edit_globs=["src/**/*.py", "tests/*.py", "*.md"]
    )
    assert config.allowed_edit_globs == ["src/**/*.py", "tests/*.py", "*.md"]


def test_phase_config_glob_validation_empty_list():
    """Test that PhaseConfig works with empty allowed_edit_globs."""
    config = PhaseConfig(
        name="test",
        index=0,
        system_prompt="test",
        allowed_edit_globs=[]
    )
    assert config.allowed_edit_globs == []


def test_phase_config_glob_validation_windows_absolute_path():
    """Test that PhaseConfig raises ValueError for Windows absolute paths."""
    with pytest.raises(ValueError, match="absolute Windows path"):
        PhaseConfig(
            name="test",
            index=0,
            system_prompt="test",
            allowed_edit_globs=["C:\\Windows\\*.py"]  # Should raise
        )