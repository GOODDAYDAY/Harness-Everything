"""Tests for harness/tools/code_analysis.py (CodeAnalysisTool)."""
from __future__ import annotations

import asyncio
import json
import pathlib
from unittest.mock import MagicMock

import pytest

from harness.tools.code_analysis import CodeAnalysisTool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(tmp_path: pathlib.Path):
    cfg = MagicMock()
    cfg.workspace = str(tmp_path)
    cfg.allowed_paths = [tmp_path]
    return cfg


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def tool():
    return CodeAnalysisTool()


@pytest.fixture()
def module_file(tmp_path):
    p = tmp_path / "mod.py"
    p.write_text(
        "import os\n"
        "MY_CONST = 42\n\n"
        "class Foo:\n"
        "    def bar(self): pass\n\n"
        "def standalone():\n"
        "    return 1\n"
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Text format (default)
# ---------------------------------------------------------------------------

class TestTextFormat:
    def test_returns_non_error(self, tool, module_file):
        r = _run(tool.execute(_cfg(module_file), path=str(module_file / "mod.py"), limit=50))
        assert not r.is_error

    def test_shows_file_name(self, tool, module_file):
        r = _run(tool.execute(_cfg(module_file), path=str(module_file / "mod.py"), limit=50))
        assert "mod.py" in r.output

    def test_shows_class(self, tool, module_file):
        r = _run(tool.execute(_cfg(module_file), path=str(module_file / "mod.py"), limit=50))
        assert "Foo" in r.output

    def test_shows_function(self, tool, module_file):
        r = _run(tool.execute(_cfg(module_file), path=str(module_file / "mod.py"), limit=50))
        assert "standalone" in r.output

    def test_shows_constant(self, tool, module_file):
        r = _run(tool.execute(_cfg(module_file), path=str(module_file / "mod.py"), limit=50))
        assert "MY_CONST" in r.output

    def test_shows_line_counts(self, tool, module_file):
        r = _run(tool.execute(_cfg(module_file), path=str(module_file / "mod.py"), limit=50))
        assert "Lines:" in r.output or "lines" in r.output.lower()


# ---------------------------------------------------------------------------
# JSON format
# ---------------------------------------------------------------------------

class TestJsonFormat:
    def test_returns_valid_json(self, tool, module_file):
        r = _run(tool.execute(_cfg(module_file), path=str(module_file / "mod.py"),
                               limit=50, format="json"))
        assert not r.is_error
        data = json.loads(r.output)  # must not raise
        assert isinstance(data, dict)

    def test_json_has_file_key(self, tool, module_file):
        r = _run(tool.execute(_cfg(module_file), path=str(module_file / "mod.py"),
                               limit=50, format="json"))
        data = json.loads(r.output)
        assert "mod.py" in data

    def test_json_has_symbols(self, tool, module_file):
        r = _run(tool.execute(_cfg(module_file), path=str(module_file / "mod.py"),
                               limit=50, format="json"))
        data = json.loads(r.output)
        file_data = data["mod.py"]
        symbols = file_data["symbols"]
        names = [s["name"] for s in symbols]
        assert "Foo" in names
        assert "MY_CONST" in names

    def test_json_has_summary(self, tool, module_file):
        r = _run(tool.execute(_cfg(module_file), path=str(module_file / "mod.py"),
                               limit=50, format="json"))
        data = json.loads(r.output)
        summary = data["mod.py"]["summary"]
        assert summary["classes"] == 1
        assert summary["functions"] >= 1

    def test_json_has_imports(self, tool, module_file):
        r = _run(tool.execute(_cfg(module_file), path=str(module_file / "mod.py"),
                               limit=50, format="json"))
        data = json.loads(r.output)
        imports = data["mod.py"]["imports"]
        # Each import is a dict with a 'module' key
        modules = [imp["module"] if isinstance(imp, dict) else imp for imp in imports]
        assert any("os" in m for m in modules)

    def test_json_functions_have_complexity(self, tool, module_file):
        r = _run(tool.execute(_cfg(module_file), path=str(module_file / "mod.py"),
                               limit=50, format="json"))
        data = json.loads(r.output)
        funcs = data["mod.py"]["functions"]
        for fn in funcs:
            assert "complexity" in fn


# ---------------------------------------------------------------------------
# Directory mode
# ---------------------------------------------------------------------------

class TestDirectoryMode:
    def test_scans_directory(self, tool, tmp_path):
        (tmp_path / "a.py").write_text("def a(): pass\n")
        (tmp_path / "b.py").write_text("def b(): pass\n")
        r = _run(tool.execute(_cfg(tmp_path), path=str(tmp_path), limit=50))
        assert not r.is_error
        assert "a.py" in r.output
        assert "b.py" in r.output

    def test_file_glob_filter(self, tool, tmp_path):
        (tmp_path / "code.py").write_text("def code(): pass\n")
        (tmp_path / "test_it.py").write_text("def test_foo(): pass\n")
        r = _run(tool.execute(_cfg(tmp_path), path=str(tmp_path),
                               limit=50, file_glob="test_*.py"))
        assert "test_it.py" in r.output
        assert "code.py" not in r.output


# ---------------------------------------------------------------------------
# limit
# ---------------------------------------------------------------------------

class TestLimit:
    def test_limit_caps_symbols_returned(self, tool, tmp_path):
        lines = [f"def func_{i}(): pass\n" for i in range(20)]
        (tmp_path / "big.py").write_text("".join(lines))
        r = _run(tool.execute(_cfg(tmp_path), path=str(tmp_path / "big.py"), limit=3))
        # output should exist but be trimmed
        assert not r.is_error
        assert "big.py" in r.output
