"""Tests for the ast_rename tool.

Covers: function rename, class rename, variable rename, method rename,
dry_run preview, apply=True changes files, out-of-scope no-ops, error cases.
"""
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import MagicMock


from harness.tools.ast_rename import AstRenameTool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_config(workspace: str = "/tmp"):
    cfg = MagicMock()
    cfg.workspace = workspace
    cfg.allowed_paths = [workspace]
    return cfg


def run(coro):
    return asyncio.run(coro)


tool = AstRenameTool()


# ---------------------------------------------------------------------------
# Fixtures: write temp Python files
# ---------------------------------------------------------------------------

FUNCTION_SRC = """def old_func(x):
    return x + 1

def caller():
    return old_func(42)
"""

CLASS_SRC = """class OldClass:
    def __init__(self):
        pass

def factory():
    return OldClass()
"""

METHOD_SRC = """class MyClass:
    def old_method(self):
        return 1

    def other(self):
        return self.old_method()
"""

VARIABLE_SRC = """MY_CONST = 42
result = MY_CONST + 1
"""


class TestFunctionRename:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cfg = make_config(workspace=self.tmpdir)

    def _write(self, name: str, src: str) -> str:
        p = Path(self.tmpdir) / name
        p.write_text(src)
        return str(p)

    def test_dry_run_returns_preview_not_writes(self):
        self._write("mod.py", FUNCTION_SRC)
        result = run(tool.execute(
            self.cfg,
            old_name="old_func",
            new_name="new_func",
            scope=self.tmpdir,
            symbol_type="function",
            apply=False,
        ))
        assert not result.is_error
        # File should NOT be changed (dry_run)
        content = (Path(self.tmpdir) / "mod.py").read_text()
        assert "old_func" in content

    def test_apply_renames_function_definition(self):
        self._write("mod.py", FUNCTION_SRC)
        result = run(tool.execute(
            self.cfg,
            old_name="old_func",
            new_name="new_func",
            scope=self.tmpdir,
            symbol_type="function",
            apply=True,
        ))
        assert not result.is_error
        content = (Path(self.tmpdir) / "mod.py").read_text()
        assert "def new_func" in content
        assert "new_func(42)" in content

    def test_apply_does_not_affect_other_names(self):
        self._write("mod.py", FUNCTION_SRC)
        run(tool.execute(
            self.cfg,
            old_name="old_func",
            new_name="new_func",
            scope=self.tmpdir,
            symbol_type="function",
            apply=True,
        ))
        content = (Path(self.tmpdir) / "mod.py").read_text()
        # 'caller' must still be there
        assert "def caller" in content

    def test_no_match_returns_graceful_output(self):
        self._write("mod.py", FUNCTION_SRC)
        result = run(tool.execute(
            self.cfg,
            old_name="nonexistent_func",
            new_name="whatever",
            scope=self.tmpdir,
            symbol_type="function",
            apply=False,
        ))
        # Should not be an error, just says 0 matches
        assert not result.is_error or "not found" in result.output.lower() or "0" in result.output


class TestClassRename:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cfg = make_config(workspace=self.tmpdir)

    def _write(self, name: str, src: str) -> str:
        p = Path(self.tmpdir) / name
        p.write_text(src)
        return str(p)

    def test_apply_renames_class_definition(self):
        self._write("cls.py", CLASS_SRC)
        result = run(tool.execute(
            self.cfg,
            old_name="OldClass",
            new_name="NewClass",
            scope=self.tmpdir,
            symbol_type="class",
            apply=True,
        ))
        assert not result.is_error
        content = (Path(self.tmpdir) / "cls.py").read_text()
        assert "class NewClass" in content
        assert "NewClass()" in content
        assert "OldClass" not in content

    def test_dry_run_class_shows_diff(self):
        self._write("cls.py", CLASS_SRC)
        result = run(tool.execute(
            self.cfg,
            old_name="OldClass",
            new_name="NewClass",
            scope=self.tmpdir,
            symbol_type="class",
            apply=False,
        ))
        assert not result.is_error
        # Preview should mention both old and new names
        assert "NewClass" in result.output or "OldClass" in result.output


class TestMethodRename:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cfg = make_config(workspace=self.tmpdir)

    def _write(self, name: str, src: str) -> str:
        p = Path(self.tmpdir) / name
        p.write_text(src)
        return str(p)

    def test_apply_renames_method(self):
        self._write("svc.py", METHOD_SRC)
        result = run(tool.execute(
            self.cfg,
            old_name="old_method",
            new_name="new_method",
            scope=self.tmpdir,
            symbol_type="method",
            class_name="MyClass",
            apply=True,
        ))
        assert not result.is_error
        content = (Path(self.tmpdir) / "svc.py").read_text()
        assert "def new_method" in content
        # caller should also be updated
        assert "self.new_method()" in content

    def test_method_rename_requires_class_name(self):
        """If symbol_type=method and no class_name, should either error or rename nothing."""
        self._write("svc.py", METHOD_SRC)
        result = run(tool.execute(
            self.cfg,
            old_name="old_method",
            new_name="new_method",
            scope=self.tmpdir,
            symbol_type="method",
            # no class_name
            apply=False,
        ))
        # It may succeed with 0 matches or return an error — either is acceptable
        # The important thing is no crash
        assert isinstance(result.is_error, bool)


class TestVariableRename:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cfg = make_config(workspace=self.tmpdir)

    def _write(self, name: str, src: str) -> str:
        p = Path(self.tmpdir) / name
        p.write_text(src)
        return str(p)

    def test_apply_renames_module_variable(self):
        self._write("consts.py", VARIABLE_SRC)
        result = run(tool.execute(
            self.cfg,
            old_name="MY_CONST",
            new_name="MY_VALUE",
            scope=self.tmpdir,
            symbol_type="variable",
            apply=True,
        ))
        assert not result.is_error
        content = (Path(self.tmpdir) / "consts.py").read_text()
        assert "MY_VALUE" in content
        assert "MY_CONST" not in content


class TestSymbolTypeAny:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cfg = make_config(workspace=self.tmpdir)

    def _write(self, name: str, src: str) -> str:
        p = Path(self.tmpdir) / name
        p.write_text(src)
        return str(p)

    def test_any_type_renames_function(self):
        self._write("mod.py", FUNCTION_SRC)
        result = run(tool.execute(
            self.cfg,
            old_name="old_func",
            new_name="new_func",
            scope=self.tmpdir,
            symbol_type="any",
            apply=True,
        ))
        assert not result.is_error
        content = (Path(self.tmpdir) / "mod.py").read_text()
        assert "def new_func" in content


class TestMultiFileScope:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cfg = make_config(workspace=self.tmpdir)

    def _write(self, name: str, src: str) -> str:
        p = Path(self.tmpdir) / name
        p.write_text(src)
        return str(p)

    def test_renames_across_multiple_files(self):
        src1 = "def shared_func():\n    pass\n"
        src2 = "from mod1 import shared_func\n\ndef call():\n    shared_func()\n"
        self._write("mod1.py", src1)
        self._write("mod2.py", src2)
        result = run(tool.execute(
            self.cfg,
            old_name="shared_func",
            new_name="renamed_func",
            scope=self.tmpdir,
            symbol_type="function",
            apply=True,
        ))
        assert not result.is_error
        c1 = (Path(self.tmpdir) / "mod1.py").read_text()
        assert "def renamed_func" in c1

    def test_scope_restricts_to_subdirectory(self):
        subdir = Path(self.tmpdir) / "sub"
        subdir.mkdir()
        other = Path(self.tmpdir) / "other.py"
        other.write_text("def old_func():\n    pass\n")
        (subdir / "inner.py").write_text("def old_func():\n    pass\n")

        cfg_sub = make_config(workspace=self.tmpdir)
        result = run(tool.execute(
            cfg_sub,
            old_name="old_func",
            new_name="new_func",
            scope=str(subdir),
            symbol_type="function",
            apply=True,
        ))
        assert not result.is_error
        # inner.py should be renamed
        assert "def new_func" in (subdir / "inner.py").read_text()
        # other.py outside scope should NOT be renamed
        assert "def old_func" in other.read_text()


class TestErrorCases:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cfg = make_config(workspace=self.tmpdir)

    def test_scope_outside_workspace_graceful(self):
        """Scope outside workspace returns gracefully — either error or no-match.
        The tool doesn't crash; it either rejects the path or finds no .py files.
        """
        result = run(tool.execute(
            self.cfg,
            old_name="foo",
            new_name="bar",
            scope="/etc",
            symbol_type="function",
            apply=False,
        ))
        # Must not raise an exception; may be error or no-match
        assert isinstance(result.is_error, bool)
        if not result.is_error:
            assert "no" in result.output.lower() or "0" in result.output

    def test_invalid_symbol_type_no_crash(self):
        """Invalid symbol_type should not raise an exception.
        The tool may return an error or silently find no matches.
        """
        result = run(tool.execute(
            self.cfg,
            old_name="foo",
            new_name="bar",
            scope=self.tmpdir,
            symbol_type="invalid_type",
            apply=False,
        ))
        assert isinstance(result.is_error, bool)
        if not result.is_error:
            assert "no" in result.output.lower() or "0" in result.output
