"""Unit tests for harness.tools.batch_write.

Covers:
  - Single file write
  - Multi-file parallel write
  - Parent directory auto-creation
  - Empty path → per-file error
  - Empty files list → error
  - MAX_FILES cap → error
  - MAX_TOTAL_CHARS cap → error
  - Overwrite existing file
  - Partial failure reporting
  - Metadata n_ok / n_err / written_paths
"""

import asyncio
from unittest.mock import Mock


from harness.core.config import HarnessConfig
from harness.tools.batch_write import BatchWriteTool


def _run(coro):
    return asyncio.run(coro)


def _make_config(workspace: str) -> HarnessConfig:
    cfg = Mock(spec=HarnessConfig)
    cfg.workspace = workspace
    cfg.allowed_paths = [workspace]
    return cfg


class TestBatchWriteBasic:
    def test_single_file_write(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        tool = BatchWriteTool()
        cfg = _make_config(str(ws))
        target = ws / "hello.py"

        result = _run(tool.execute(cfg, files=[
            {"path": str(target), "content": "print('hello')"},
        ]))
        assert not result.is_error
        assert "1/1 succeeded" in result.output
        assert target.read_text() == "print('hello')"

    def test_multi_file_write(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        tool = BatchWriteTool()
        cfg = _make_config(str(ws))
        a = ws / "a.txt"
        b = ws / "b.txt"

        result = _run(tool.execute(cfg, files=[
            {"path": str(a), "content": "AAA"},
            {"path": str(b), "content": "BBB"},
        ]))
        assert not result.is_error
        assert "2/2 succeeded" in result.output
        assert a.read_text() == "AAA"
        assert b.read_text() == "BBB"

    def test_parent_directory_auto_created(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        tool = BatchWriteTool()
        cfg = _make_config(str(ws))
        target = ws / "sub" / "deep" / "file.py"

        result = _run(tool.execute(cfg, files=[
            {"path": str(target), "content": "nested"},
        ]))
        assert not result.is_error
        assert target.read_text() == "nested"

    def test_overwrite_existing_file(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        f = ws / "existing.txt"
        f.write_text("old content")

        tool = BatchWriteTool()
        cfg = _make_config(str(ws))
        result = _run(tool.execute(cfg, files=[
            {"path": str(f), "content": "new content"},
        ]))
        assert not result.is_error
        assert f.read_text() == "new content"


class TestBatchWriteValidation:
    def test_empty_files_is_error(self):
        tool = BatchWriteTool()
        cfg = _make_config("/tmp")
        result = _run(tool.execute(cfg, files=[]))
        assert result.is_error
        assert "non-empty" in result.error

    def test_max_files_exceeded(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        tool = BatchWriteTool()
        cfg = _make_config(str(ws))
        files = [{"path": f"f{i}.txt", "content": "x"} for i in range(tool.MAX_FILES + 1)]
        result = _run(tool.execute(cfg, files=files))
        assert result.is_error
        assert str(tool.MAX_FILES) in result.error

    def test_max_total_chars_exceeded(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        tool = BatchWriteTool()
        cfg = _make_config(str(ws))
        big_content = "x" * (tool.MAX_TOTAL_CHARS + 1)
        result = _run(tool.execute(cfg, files=[
            {"path": "big.txt", "content": big_content},
        ]))
        assert result.is_error
        assert "exceeds cap" in result.error

    def test_missing_path_in_file(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        tool = BatchWriteTool()
        cfg = _make_config(str(ws))
        result = _run(tool.execute(cfg, files=[
            {"path": "", "content": "hello"},
        ]))
        assert "missing 'path'" in result.output


class TestBatchWriteMetadata:
    def test_metadata_tracks_counts(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        tool = BatchWriteTool()
        cfg = _make_config(str(ws))
        target = ws / "m.txt"

        result = _run(tool.execute(cfg, files=[
            {"path": str(target), "content": "data"},
        ]))
        assert result.metadata["n_ok"] == 1
        assert result.metadata["n_err"] == 0
        assert len(result.metadata["written_paths"]) == 1


class TestBatchWriteSchema:
    def test_schema_has_required_files(self):
        tool = BatchWriteTool()
        schema = tool.input_schema()
        assert "files" in schema["properties"]
        assert "files" in schema["required"]
        items = schema["properties"]["files"]["items"]
        assert "path" in items["required"]
        assert "content" in items["required"]
