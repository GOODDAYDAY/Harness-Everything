"""Tests for harness/tools/symbol_extractor.py (SymbolExtractorTool)."""
from __future__ import annotations

import asyncio
import json
import pathlib
from unittest.mock import MagicMock

import pytest

from harness.tools.symbol_extractor import SymbolExtractorTool


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
    return SymbolExtractorTool()


@pytest.fixture()
def module_file(tmp_path):
    p = tmp_path / "mod.py"
    p.write_text(
        "class Foo:\n"
        "    def bar(self): pass\n"
        "    def baz(self): return 42\n\n"
        "def standalone():\n"
        "    return 1\n"
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Basic extraction
# ---------------------------------------------------------------------------

class TestBasicExtraction:
    def test_extracts_class(self, tool, module_file):
        r = _run(tool.execute(_cfg(module_file),
                               path=str(module_file / "mod.py"),
                               symbols="Foo", context_lines=0, limit=10))
        assert not r.is_error
        assert "Foo" in r.output
        assert "class" in r.output.lower() or "method" in r.output.lower()

    def test_extracts_method(self, tool, module_file):
        r = _run(tool.execute(_cfg(module_file),
                               path=str(module_file / "mod.py"),
                               symbols="Foo.bar", context_lines=0, limit=10))
        assert not r.is_error
        assert "bar" in r.output
        assert "def bar" in r.output

    def test_extracts_function(self, tool, module_file):
        r = _run(tool.execute(_cfg(module_file),
                               path=str(module_file / "mod.py"),
                               symbols="standalone", context_lines=0, limit=10))
        assert not r.is_error
        assert "def standalone" in r.output

    def test_not_found_reports_gracefully(self, tool, module_file):
        r = _run(tool.execute(_cfg(module_file),
                               path=str(module_file / "mod.py"),
                               symbols="NonExistent", context_lines=0, limit=10))
        assert not r.is_error
        assert "No symbols" in r.output or "not found" in r.output.lower()

    def test_reports_found_count(self, tool, module_file):
        r = _run(tool.execute(_cfg(module_file),
                               path=str(module_file / "mod.py"),
                               symbols="standalone", context_lines=0, limit=10))
        assert "Found 1" in r.output


# ---------------------------------------------------------------------------
# Glob pattern
# ---------------------------------------------------------------------------

class TestGlobPattern:
    def test_glob_matches_methods(self, tool, module_file):
        r = _run(tool.execute(_cfg(module_file),
                               path=str(module_file / "mod.py"),
                               symbols="Foo.*", context_lines=0, limit=10))
        assert not r.is_error
        assert "bar" in r.output
        assert "baz" in r.output
        assert "Found 2" in r.output

    def test_glob_no_match(self, tool, module_file):
        r = _run(tool.execute(_cfg(module_file),
                               path=str(module_file / "mod.py"),
                               symbols="NoClass.*", context_lines=0, limit=10))
        assert "No symbols" in r.output or "0" in r.output


# ---------------------------------------------------------------------------
# Context lines
# ---------------------------------------------------------------------------

class TestContextLines:
    def test_context_lines_includes_preceding_lines(self, tool, module_file):
        r = _run(tool.execute(_cfg(module_file),
                               path=str(module_file / "mod.py"),
                               symbols="standalone", context_lines=2, limit=10))
        # With context=2, lines before 'def standalone' should appear
        lines = r.output.splitlines()
        # Should include more than just the function definition
        assert len(lines) > 3

    def test_zero_context_no_extra(self, tool, module_file):
        r = _run(tool.execute(_cfg(module_file),
                               path=str(module_file / "mod.py"),
                               symbols="standalone", context_lines=0, limit=10))
        # With context=0, output should not include class body
        assert "def bar" not in r.output


# ---------------------------------------------------------------------------
# JSON format
# ---------------------------------------------------------------------------

class TestJsonFormat:
    def test_json_format_returns_parseable_output(self, tool, module_file):
        r = _run(tool.execute(_cfg(module_file),
                               path=str(module_file / "mod.py"),
                               symbols="Foo.bar", context_lines=0, limit=10,
                               format="json"))
        assert not r.is_error
        data = json.loads(r.output)  # must not raise
        assert isinstance(data, (dict, list))

    def test_json_contains_symbol_name(self, tool, module_file):
        r = _run(tool.execute(_cfg(module_file),
                               path=str(module_file / "mod.py"),
                               symbols="Foo.bar", context_lines=0, limit=10,
                               format="json"))
        assert "bar" in r.output


# ---------------------------------------------------------------------------
# Directory scan
# ---------------------------------------------------------------------------

class TestDirectoryScan:
    def test_scans_directory_for_symbol(self, tool, tmp_path):
        (tmp_path / "a.py").write_text("def target(): return 1\n")
        (tmp_path / "b.py").write_text("def other(): return 2\n")
        r = _run(tool.execute(_cfg(tmp_path), path=str(tmp_path),
                               symbols="target", context_lines=0, limit=10))
        assert not r.is_error
        assert "target" in r.output
        assert "a.py" in r.output

    def test_file_glob_restricts_scan(self, tool, tmp_path):
        (tmp_path / "a.py").write_text("def target(): return 1\n")
        (tmp_path / "a_test.py").write_text("def target(): return 2\n")
        r = _run(tool.execute(_cfg(tmp_path), path=str(tmp_path),
                               symbols="target", context_lines=0, limit=10,
                               file_glob="a.py"))
        assert not r.is_error
        assert "a.py" in r.output
        # a_test.py should not be in the results
        assert "a_test.py" not in r.output


# ---------------------------------------------------------------------------
# Multiple symbols
# ---------------------------------------------------------------------------

class TestMultipleSymbols:
    def test_list_of_symbols(self, tool, module_file):
        r = _run(tool.execute(_cfg(module_file),
                               path=str(module_file / "mod.py"),
                               symbols=["Foo.bar", "standalone"],
                               context_lines=0, limit=10))
        assert not r.is_error
        assert "bar" in r.output
        assert "standalone" in r.output
