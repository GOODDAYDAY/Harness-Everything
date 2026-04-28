"""Unit tests for harness.tools.scratchpad.

Covers:
  - Normal note save
  - Empty note → error
  - Whitespace-only note → error
  - Long note truncation at MAX_NOTE_CHARS
  - Metadata contains the note
  - Output prefix format
"""

import asyncio
from unittest.mock import Mock


from harness.core.config import HarnessConfig
from harness.tools.scratchpad import ScratchpadTool


def _run(coro):
    return asyncio.run(coro)


def _make_config() -> HarnessConfig:
    cfg = Mock(spec=HarnessConfig)
    cfg.workspace = "/tmp"
    cfg.allowed_paths = ["/tmp"]
    return cfg


class TestScratchpadBasic:
    def test_save_note(self):
        tool = ScratchpadTool()
        cfg = _make_config()
        result = _run(tool.execute(cfg, note="important finding"))
        assert not result.is_error
        assert "[scratchpad]" in result.output
        assert "note saved" in result.output

    def test_metadata_contains_note(self):
        tool = ScratchpadTool()
        cfg = _make_config()
        result = _run(tool.execute(cfg, note="key detail"))
        assert result.metadata["note"] == "key detail"

    def test_output_shows_char_count(self):
        tool = ScratchpadTool()
        cfg = _make_config()
        note = "x" * 50
        result = _run(tool.execute(cfg, note=note))
        assert "(50 chars)" in result.output


class TestScratchpadValidation:
    def test_empty_note_is_error(self):
        tool = ScratchpadTool()
        cfg = _make_config()
        result = _run(tool.execute(cfg, note=""))
        assert result.is_error
        assert "empty" in result.error.lower()

    def test_whitespace_only_note_is_error(self):
        tool = ScratchpadTool()
        cfg = _make_config()
        result = _run(tool.execute(cfg, note="   \n  "))
        assert result.is_error
        assert "empty" in result.error.lower()


class TestScratchpadTruncation:
    def test_long_note_truncated(self):
        tool = ScratchpadTool()
        cfg = _make_config()
        long_note = "x" * (tool.MAX_NOTE_CHARS + 500)
        result = _run(tool.execute(cfg, note=long_note))
        assert not result.is_error
        saved = result.metadata["note"]
        assert len(saved) <= tool.MAX_NOTE_CHARS + 20  # +20 for "… [truncated]"
        assert "truncated" in saved


class TestScratchpadSchema:
    def test_schema_requires_note(self):
        tool = ScratchpadTool()
        schema = tool.input_schema()
        assert "note" in schema["properties"]
        assert "note" in schema["required"]

    def test_name_and_description(self):
        tool = ScratchpadTool()
        assert tool.name == "scratchpad"
        assert "persistent" in tool.description.lower() or "note" in tool.description.lower()
