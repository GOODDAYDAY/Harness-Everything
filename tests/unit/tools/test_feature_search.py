"""Tests for harness/tools/feature_search.py (FeatureSearchTool)."""
from __future__ import annotations

import asyncio
import json
import pathlib
from unittest.mock import MagicMock

import pytest

from harness.tools.feature_search import FeatureSearchTool


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
    return FeatureSearchTool()


@pytest.fixture()
def auth_module(tmp_path):
    """A module with auth-related symbols, comments, and constants."""
    (tmp_path / "auth.py").write_text(
        "# Authentication and authorization module\n"
        "MAX_AUTH_RETRIES = 3\n"
        "\n"
        "class AuthManager:\n"
        "    \"\"\"Manages authentication tokens.\"\"\"\n"
        "    def authenticate(self, user):\n"
        "        pass\n"
        "    def check_auth(self, token):\n"
        "        pass\n"
    )
    (tmp_path / "other.py").write_text(
        "def unrelated():\n"
        "    pass\n"
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Basic discovery
# ---------------------------------------------------------------------------

class TestBasicDiscovery:
    def test_finds_matching_file(self, tool, auth_module):
        r = _run(tool.execute(_cfg(auth_module), keyword="auth", max_results=20,
                               root=str(auth_module)))
        assert not r.is_error
        data = json.loads(r.output)
        files = [f["file"] for f in data.get("files", [])]
        assert any("auth" in f for f in files)

    def test_finds_matching_symbols(self, tool, auth_module):
        r = _run(tool.execute(_cfg(auth_module), keyword="auth", max_results=20,
                               root=str(auth_module)))
        data = json.loads(r.output)
        symbol_names = [s["name"] for s in data.get("symbols", [])]
        assert any("auth" in n.lower() for n in symbol_names)

    def test_finds_matching_comments(self, tool, auth_module):
        r = _run(tool.execute(_cfg(auth_module), keyword="auth", max_results=20,
                               root=str(auth_module)))
        data = json.loads(r.output)
        assert len(data.get("comments", [])) > 0

    def test_finds_matching_config(self, tool, auth_module):
        r = _run(tool.execute(_cfg(auth_module), keyword="auth", max_results=20,
                               root=str(auth_module)))
        data = json.loads(r.output)
        config_names = [c["name"] for c in data.get("config", [])]
        assert any("AUTH" in n for n in config_names)


# ---------------------------------------------------------------------------
# No match case
# ---------------------------------------------------------------------------

class TestNoMatch:
    def test_no_match_returns_empty_lists(self, tool, auth_module):
        r = _run(tool.execute(_cfg(auth_module), keyword="zzznomatch_xyz",
                               max_results=20, root=str(auth_module)))
        assert not r.is_error
        data = json.loads(r.output)
        assert data.get("files", []) == []
        assert data.get("symbols", []) == []


# ---------------------------------------------------------------------------
# Categories filter
# ---------------------------------------------------------------------------

class TestCategoriesFilter:
    def test_symbols_only_category(self, tool, auth_module):
        r = _run(tool.execute(_cfg(auth_module), keyword="auth", max_results=20,
                               root=str(auth_module), categories=["symbols"]))
        data = json.loads(r.output)
        assert "symbols" in data
        assert "files" not in data
        assert "comments" not in data
        assert "config" not in data

    def test_files_only_category(self, tool, auth_module):
        r = _run(tool.execute(_cfg(auth_module), keyword="auth", max_results=20,
                               root=str(auth_module), categories=["files"]))
        data = json.loads(r.output)
        assert "files" in data
        assert "symbols" not in data

    def test_multiple_categories(self, tool, auth_module):
        r = _run(tool.execute(_cfg(auth_module), keyword="auth", max_results=20,
                               root=str(auth_module), categories=["symbols", "config"]))
        data = json.loads(r.output)
        assert "symbols" in data
        assert "config" in data
        assert "files" not in data
        assert "comments" not in data

    def test_all_categories_by_default(self, tool, auth_module):
        r = _run(tool.execute(_cfg(auth_module), keyword="auth", max_results=20,
                               root=str(auth_module)))
        data = json.loads(r.output)
        for cat in ["files", "symbols", "comments", "config"]:
            assert cat in data, f"missing category: {cat}"


# ---------------------------------------------------------------------------
# Output structure
# ---------------------------------------------------------------------------

class TestOutputStructure:
    def test_output_is_valid_json(self, tool, auth_module):
        r = _run(tool.execute(_cfg(auth_module), keyword="auth", max_results=10,
                               root=str(auth_module)))
        data = json.loads(r.output)  # must not raise
        assert isinstance(data, dict)

    def test_keyword_in_output(self, tool, auth_module):
        r = _run(tool.execute(_cfg(auth_module), keyword="auth", max_results=10,
                               root=str(auth_module)))
        data = json.loads(r.output)
        assert data["keyword"] == "auth"

    def test_files_scanned_positive(self, tool, auth_module):
        r = _run(tool.execute(_cfg(auth_module), keyword="auth", max_results=10,
                               root=str(auth_module)))
        data = json.loads(r.output)
        assert data["files_scanned"] >= 1


# ---------------------------------------------------------------------------
# Scoring modes
# ---------------------------------------------------------------------------

class TestScoringModes:
    def test_substring_scoring(self, tool, auth_module):
        r = _run(tool.execute(_cfg(auth_module), keyword="auth", max_results=20,
                               root=str(auth_module), scoring="substring"))
        assert not r.is_error

    def test_token_overlap_scoring(self, tool, auth_module):
        r = _run(tool.execute(_cfg(auth_module), keyword="auth manager", max_results=20,
                               root=str(auth_module), scoring="token_overlap"))
        assert not r.is_error
        data = json.loads(r.output)
        symbol_names = [s["name"] for s in data.get("symbols", [])]
        # AuthManager has both tokens 'auth' and 'manager'
        assert any("auth" in n.lower() or "manager" in n.lower() for n in symbol_names)


# ---------------------------------------------------------------------------
# max_results cap
# ---------------------------------------------------------------------------

class TestMaxResults:
    def test_max_results_caps_symbols(self, tool, tmp_path):
        # Create a module with many auth-related symbols
        lines = ["class Auth{}:\n    pass\n".format(i) for i in range(10)]
        (tmp_path / "many.py").write_text("".join(lines))
        r = _run(tool.execute(_cfg(tmp_path), keyword="auth", max_results=3,
                               root=str(tmp_path)))
        data = json.loads(r.output)
        assert len(data.get("symbols", [])) <= 3
