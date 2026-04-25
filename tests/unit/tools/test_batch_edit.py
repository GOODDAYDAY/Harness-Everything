"""Unit tests for harness.tools.batch_edit.

Covers:
  - Single-file single edit
  - Multi-file batch
  - old_str not found → error
  - old_str not unique → error (replace_all=false)
  - replace_all=true replaces every occurrence
  - Empty edits list → error
  - MAX_EDITS cap → error
  - Missing path / empty old_str → per-edit error
  - Partial failure: good edits succeed even when one fails
  - Same-path edits applied serially (order preserved)
"""

import asyncio
from unittest.mock import Mock


from harness.core.config import HarnessConfig
from harness.tools.batch_edit import BatchEditTool


def _run(coro):
    return asyncio.run(coro)


def _make_config(workspace: str) -> HarnessConfig:
    cfg = Mock(spec=HarnessConfig)
    cfg.workspace = workspace
    cfg.allowed_paths = [workspace]
    return cfg


class TestBatchEditBasic:
    def test_single_edit(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        f = ws / "a.py"
        f.write_text("foo = 1\nbar = 2\n")

        tool = BatchEditTool()
        cfg = _make_config(str(ws))
        result = _run(tool.execute(cfg, edits=[
            {"path": str(f), "old_str": "foo = 1", "new_str": "foo = 42"},
        ]))
        assert not result.is_error
        assert "1/1 succeeded" in result.output
        assert f.read_text() == "foo = 42\nbar = 2\n"

    def test_multi_file_batch(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        a = ws / "a.py"
        a.write_text("alpha")
        b = ws / "b.py"
        b.write_text("beta")

        tool = BatchEditTool()
        cfg = _make_config(str(ws))
        result = _run(tool.execute(cfg, edits=[
            {"path": str(a), "old_str": "alpha", "new_str": "ALPHA"},
            {"path": str(b), "old_str": "beta", "new_str": "BETA"},
        ]))
        assert not result.is_error
        assert "2/2 succeeded" in result.output
        assert a.read_text() == "ALPHA"
        assert b.read_text() == "BETA"

    def test_old_str_not_found(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        f = ws / "a.py"
        f.write_text("hello world")

        tool = BatchEditTool()
        cfg = _make_config(str(ws))
        result = _run(tool.execute(cfg, edits=[
            {"path": str(f), "old_str": "nonexistent", "new_str": "x"},
        ]))
        assert "old_str not found" in result.output
        assert "0/1 succeeded" in result.output

    def test_old_str_not_unique(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        f = ws / "a.py"
        f.write_text("dup dup dup")

        tool = BatchEditTool()
        cfg = _make_config(str(ws))
        result = _run(tool.execute(cfg, edits=[
            {"path": str(f), "old_str": "dup", "new_str": "x"},
        ]))
        assert "appears 3 times" in result.output
        assert "replace_all=true" in result.output


class TestBatchEditReplaceAll:
    def test_replace_all_replaces_every_occurrence(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        f = ws / "a.py"
        f.write_text("foo foo foo")

        tool = BatchEditTool()
        cfg = _make_config(str(ws))
        result = _run(tool.execute(cfg, edits=[
            {"path": str(f), "old_str": "foo", "new_str": "bar", "replace_all": True},
        ]))
        assert not result.is_error
        assert "3 replacements" in result.output
        assert f.read_text() == "bar bar bar"


class TestBatchEditValidation:
    def test_empty_edits_is_error(self):
        tool = BatchEditTool()
        cfg = _make_config("/tmp")
        result = _run(tool.execute(cfg, edits=[]))
        assert result.is_error
        assert "non-empty" in result.error

    def test_max_edits_exceeded(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        tool = BatchEditTool()
        cfg = _make_config(str(ws))
        edits = [{"path": "x.py", "old_str": "a", "new_str": "b"}] * (tool.MAX_EDITS + 1)
        result = _run(tool.execute(cfg, edits=edits))
        assert result.is_error
        assert str(tool.MAX_EDITS) in result.error

    def test_missing_path_in_edit(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        tool = BatchEditTool()
        cfg = _make_config(str(ws))
        result = _run(tool.execute(cfg, edits=[
            {"path": "", "old_str": "a", "new_str": "b"},
        ]))
        assert "missing 'path'" in result.output

    def test_empty_old_str_in_edit(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        f = ws / "a.py"
        f.write_text("hello")
        tool = BatchEditTool()
        cfg = _make_config(str(ws))
        result = _run(tool.execute(cfg, edits=[
            {"path": str(f), "old_str": "", "new_str": "x"},
        ]))
        assert "old_str" in result.output and "non-empty" in result.output


class TestBatchEditPartialFailure:
    def test_good_edits_succeed_despite_bad_ones(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        a = ws / "a.py"
        a.write_text("good content")
        b = ws / "b.py"
        b.write_text("also good")

        tool = BatchEditTool()
        cfg = _make_config(str(ws))
        result = _run(tool.execute(cfg, edits=[
            {"path": str(a), "old_str": "good content", "new_str": "great content"},
            {"path": str(b), "old_str": "MISSING", "new_str": "x"},
            {"path": str(b), "old_str": "also good", "new_str": "also great"},
        ]))
        assert "2/3 succeeded" in result.output
        assert "1 failed" in result.output
        assert a.read_text() == "great content"
        assert b.read_text() == "also great"


class TestBatchEditSamePathSerial:
    def test_same_path_edits_applied_in_order(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        f = ws / "a.py"
        f.write_text("line1\nline2\nline3\n")

        tool = BatchEditTool()
        cfg = _make_config(str(ws))
        result = _run(tool.execute(cfg, edits=[
            {"path": str(f), "old_str": "line1", "new_str": "LINE_ONE"},
            {"path": str(f), "old_str": "line2", "new_str": "LINE_TWO"},
        ]))
        assert "2/2 succeeded" in result.output
        assert f.read_text() == "LINE_ONE\nLINE_TWO\nline3\n"


class TestBatchEditMetadata:
    def test_metadata_tracks_counts_and_paths(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        f = ws / "a.py"
        f.write_text("old")

        tool = BatchEditTool()
        cfg = _make_config(str(ws))
        result = _run(tool.execute(cfg, edits=[
            {"path": str(f), "old_str": "old", "new_str": "new"},
        ]))
        assert result.metadata["n_ok"] == 1
        assert result.metadata["n_err"] == 0
        assert len(result.metadata["changed_paths"]) == 1


class TestBatchEditSchema:
    def test_schema_has_required_edits(self):
        tool = BatchEditTool()
        schema = tool.input_schema()
        assert "edits" in schema["properties"]
        assert "edits" in schema["required"]
        items = schema["properties"]["edits"]["items"]
        assert "path" in items["required"]
        assert "old_str" in items["required"]
        assert "new_str" in items["required"]
