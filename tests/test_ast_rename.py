"""Tests for the ast_rename tool."""

from __future__ import annotations

import asyncio

from harness.core.config import HarnessConfig
from harness.tools.ast_rename import AstRenameTool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(tool, config, **kwargs):
    """Synchronously execute the tool."""
    return asyncio.run(tool.execute(config, **kwargs))


def _make_config(tmp_path):
    ws = str(tmp_path)
    return HarnessConfig(workspace=ws, allowed_paths=[ws])


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

class TestAstRenameInit:
    def test_tool_name(self):
        assert AstRenameTool().name == "ast_rename"

    def test_description_mentions_symbol_types(self):
        desc = AstRenameTool().description.lower()
        assert "function" in desc
        assert "class" in desc
        assert "method" in desc

    def test_description_mentions_preview(self):
        desc = AstRenameTool().description.lower()
        assert "preview" in desc

    def test_schema_required_fields(self):
        schema = AstRenameTool().input_schema()
        required = schema["required"]
        assert "old_name" in required
        assert "new_name" in required
        assert "scope" in required

    def test_schema_has_apply_property(self):
        schema = AstRenameTool().input_schema()
        props = schema["properties"]
        assert "apply" in props

    def test_schema_has_symbol_type_property(self):
        schema = AstRenameTool().input_schema()
        props = schema["properties"]
        assert "symbol_type" in props

    def test_schema_has_class_name_property(self):
        schema = AstRenameTool().input_schema()
        props = schema["properties"]
        assert "class_name" in props


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

class TestAstRenameValidation:
    def setup_method(self):
        self.tool = AstRenameTool()

    def test_invalid_old_name_rejected(self, tmp_path):
        config = _make_config(tmp_path)
        result = _run(self.tool, config, old_name="123invalid", new_name="valid", scope=".")
        assert result.is_error
        assert "invalid" in result.error.lower() or "identifier" in result.error.lower()

    def test_invalid_new_name_rejected(self, tmp_path):
        config = _make_config(tmp_path)
        result = _run(self.tool, config, old_name="valid", new_name="also-invalid", scope=".")
        assert result.is_error
        assert "identifier" in result.error.lower() or "invalid" in result.error.lower()

    def test_same_name_rejected(self, tmp_path):
        config = _make_config(tmp_path)
        result = _run(self.tool, config, old_name="foo", new_name="foo", scope=".")
        assert result.is_error
        assert "same" in result.error.lower()

    def test_method_without_class_name_rejected(self, tmp_path):
        config = _make_config(tmp_path)
        result = _run(
            self.tool, config,
            old_name="my_method", new_name="new_method",
            scope=".", symbol_type="method"
        )
        assert result.is_error
        assert "class_name" in result.error.lower() or "class" in result.error.lower()

    def test_nonexistent_scope_rejected(self, tmp_path):
        config = _make_config(tmp_path)
        result = _run(
            self.tool, config,
            old_name="foo", new_name="bar",
            scope="does_not_exist_subdir"
        )
        assert result.is_error
        assert "not found" in result.error.lower() or "path" in result.error.lower()

    def test_empty_old_name_rejected(self, tmp_path):
        config = _make_config(tmp_path)
        result = _run(self.tool, config, old_name="", new_name="bar", scope=".")
        assert result.is_error
        assert "identifier" in result.error.lower() or "invalid" in result.error.lower()

    def test_empty_new_name_rejected(self, tmp_path):
        config = _make_config(tmp_path)
        result = _run(self.tool, config, old_name="foo", new_name="", scope=".")
        assert result.is_error
        assert "identifier" in result.error.lower() or "invalid" in result.error.lower()


# ---------------------------------------------------------------------------
# No Python files in scope
# ---------------------------------------------------------------------------

class TestAstRenameNoPythonFiles:
    def setup_method(self):
        self.tool = AstRenameTool()

    def test_no_python_files_returns_informative_message(self, tmp_path):
        """When scope contains no .py files, return informative message (not error)."""
        (tmp_path / "data.txt").write_text("not python\n")
        config = _make_config(tmp_path)
        result = _run(
            self.tool, config,
            old_name="foo", new_name="bar",
            scope="."
        )
        assert not result.is_error
        assert "no python files" in result.output.lower() or "not found" in result.output.lower()

    def test_no_occurrences_returns_informative_message(self, tmp_path):
        """When symbol not found, return an informative message."""
        (tmp_path / "empty.py").write_text(
            "def unrelated():\n    pass\n"
        )
        config = _make_config(tmp_path)
        result = _run(
            self.tool, config,
            old_name="nonexistent_symbol", new_name="doesnt_matter",
            scope="."
        )
        assert not result.is_error
        assert "nonexistent_symbol" in result.output or "no occurrences" in result.output.lower()


# ---------------------------------------------------------------------------
# Preview mode (apply=False, the default)
# ---------------------------------------------------------------------------

class TestAstRenamePreview:
    def setup_method(self):
        self.tool = AstRenameTool()

    def test_preview_does_not_modify_file(self, tmp_path):
        """Preview mode leaves files untouched."""
        py_file = tmp_path / "mod.py"
        original = "def old_func():\n    pass\n\ndef caller():\n    old_func()\n"
        py_file.write_text(original)
        config = _make_config(tmp_path)
        result = _run(
            self.tool, config,
            old_name="old_func", new_name="new_func",
            scope=".", symbol_type="function"
        )
        assert not result.is_error
        # File must NOT be modified in preview mode
        assert py_file.read_text() == original

    def test_preview_output_says_preview(self, tmp_path):
        """Preview mode output starts with 'Preview:'."""
        (tmp_path / "p.py").write_text(
            "def target():\n    pass\n"
        )
        config = _make_config(tmp_path)
        result = _run(
            self.tool, config,
            old_name="target", new_name="new_target",
            scope="."
        )
        assert not result.is_error
        assert "Preview" in result.output

    def test_preview_output_shows_occurrence_count(self, tmp_path):
        """Preview output mentions occurrence count."""
        (tmp_path / "occ.py").write_text(
            "def old_func():\n    pass\n\ndef caller():\n    old_func()\n"
        )
        config = _make_config(tmp_path)
        result = _run(
            self.tool, config,
            old_name="old_func", new_name="new_func",
            scope="."
        )
        assert not result.is_error
        assert "occurrence" in result.output.lower()

    def test_preview_output_mentions_file_path(self, tmp_path):
        """Preview output lists affected files."""
        (tmp_path / "q.py").write_text(
            "def old_helper():\n    return 42\n"
        )
        config = _make_config(tmp_path)
        result = _run(
            self.tool, config,
            old_name="old_helper", new_name="new_helper",
            scope="."
        )
        assert not result.is_error
        assert "q.py" in result.output

    def test_preview_shows_line_numbers(self, tmp_path):
        """Preview output contains line numbers for each rename site."""
        (tmp_path / "ln.py").write_text(
            "def func_to_rename():\n    pass\n"
        )
        config = _make_config(tmp_path)
        result = _run(
            self.tool, config,
            old_name="func_to_rename", new_name="renamed_func",
            scope="."
        )
        assert not result.is_error
        # Line number should appear in output
        assert "line 1" in result.output.lower() or "1:" in result.output

    def test_preview_mentions_both_old_and_new_names(self, tmp_path):
        """Preview shows both old and new names."""
        (tmp_path / "names.py").write_text("def alpha():\n    pass\n")
        config = _make_config(tmp_path)
        result = _run(
            self.tool, config,
            old_name="alpha", new_name="beta",
            scope="."
        )
        assert not result.is_error
        assert "alpha" in result.output
        assert "beta" in result.output

    def test_apply_false_is_default(self, tmp_path):
        """Not passing apply should default to preview (no file changes)."""
        py_file = tmp_path / "default_apply.py"
        original = "def original_name():\n    pass\n"
        py_file.write_text(original)
        config = _make_config(tmp_path)
        result = _run(
            self.tool, config,
            old_name="original_name", new_name="changed_name",
            scope="."
        )
        assert not result.is_error
        # File should be unchanged since apply defaults to False
        assert py_file.read_text() == original


# ---------------------------------------------------------------------------
# Apply mode (apply=True)
# ---------------------------------------------------------------------------

class TestAstRenameApply:
    def setup_method(self):
        self.tool = AstRenameTool()

    def test_apply_renames_function_definition(self, tmp_path):
        """apply=True should rename function definition in file."""
        py_file = tmp_path / "apply_test.py"
        py_file.write_text(
            "def old_name():\n    pass\n"
        )
        config = _make_config(tmp_path)
        result = _run(
            self.tool, config,
            old_name="old_name", new_name="new_name",
            scope=".", apply=True
        )
        assert not result.is_error
        updated = py_file.read_text()
        assert "new_name" in updated
        assert "old_name" not in updated

    def test_apply_renames_call_sites(self, tmp_path):
        """apply=True should rename both definition and all call sites."""
        py_file = tmp_path / "calls.py"
        py_file.write_text(
            "def orig():\n    pass\n\ndef user():\n    orig()\n    orig()\n"
        )
        config = _make_config(tmp_path)
        result = _run(
            self.tool, config,
            old_name="orig", new_name="renamed",
            scope=".", apply=True
        )
        assert not result.is_error
        updated = py_file.read_text()
        assert "renamed" in updated
        # orig should be gone
        assert "orig" not in updated

    def test_apply_output_mentions_will_rename(self, tmp_path):
        """apply=True output mentions 'Will rename' or 'Applied'."""
        (tmp_path / "r.py").write_text(
            "def alpha():\n    pass\n"
        )
        config = _make_config(tmp_path)
        result = _run(
            self.tool, config,
            old_name="alpha", new_name="beta",
            scope=".", apply=True
        )
        assert not result.is_error
        assert "will rename" in result.output.lower() or "applied" in result.output.lower()

    def test_apply_renames_class(self, tmp_path):
        """apply=True should rename class definitions."""
        py_file = tmp_path / "cls.py"
        py_file.write_text(
            "class OldClass:\n    pass\n\nobj = OldClass()\n"
        )
        config = _make_config(tmp_path)
        result = _run(
            self.tool, config,
            old_name="OldClass", new_name="NewClass",
            scope=".", symbol_type="class", apply=True
        )
        assert not result.is_error
        updated = py_file.read_text()
        assert "NewClass" in updated
        assert "OldClass" not in updated

    def test_apply_renames_method_in_target_class(self, tmp_path):
        """apply=True renames a method in the specified class."""
        py_file = tmp_path / "meth.py"
        py_file.write_text(
            "class MyClass:\n    def old_method(self):\n        pass\n"
        )
        config = _make_config(tmp_path)
        result = _run(
            self.tool, config,
            old_name="old_method", new_name="new_method",
            scope=".", symbol_type="method",
            class_name="MyClass", apply=True
        )
        assert not result.is_error
        updated = py_file.read_text()
        assert "def new_method" in updated
        assert "def old_method" not in updated

    def test_apply_method_only_renames_in_correct_class(self, tmp_path):
        """apply=True with class_name only renames in the target class, not others."""
        py_file = tmp_path / "multi.py"
        py_file.write_text(
            "class MyClass:\n    def old_method(self):\n        pass\n"
            "\n"
            "class Other:\n    def old_method(self):\n        pass\n"
        )
        config = _make_config(tmp_path)
        result = _run(
            self.tool, config,
            old_name="old_method", new_name="new_method",
            scope=".", symbol_type="method",
            class_name="MyClass", apply=True
        )
        assert not result.is_error
        updated = py_file.read_text()
        # MyClass.old_method renamed
        assert "def new_method" in updated
        # Other.old_method unchanged
        assert "old_method" in updated, "Other.old_method should still be present"

    def test_apply_does_not_modify_non_python_files(self, tmp_path):
        """apply=True should only affect .py files."""
        txt_file = tmp_path / "readme.txt"
        txt_file.write_text("old_func appears here\n")
        py_file = tmp_path / "mod.py"
        py_file.write_text("def old_func():\n    pass\n")
        config = _make_config(tmp_path)
        result = _run(
            self.tool, config,
            old_name="old_func", new_name="new_func",
            scope=".", apply=True
        )
        assert not result.is_error
        # .txt file must be untouched
        assert txt_file.read_text() == "old_func appears here\n"
        # .py file must be updated
        assert "new_func" in py_file.read_text()

    def test_apply_multi_file(self, tmp_path):
        """apply=True renames across multiple files."""
        (tmp_path / "a.py").write_text("def shared():\n    pass\n")
        (tmp_path / "b.py").write_text("def caller():\n    shared()\n")
        config = _make_config(tmp_path)
        result = _run(
            self.tool, config,
            old_name="shared", new_name="common",
            scope=".", apply=True
        )
        assert not result.is_error
        assert "common" in (tmp_path / "a.py").read_text()
        assert "common" in (tmp_path / "b.py").read_text()

    def test_apply_reports_files_updated(self, tmp_path):
        """apply=True output includes count of files updated."""
        (tmp_path / "c.py").write_text("def foo():\n    pass\n")
        config = _make_config(tmp_path)
        result = _run(
            self.tool, config,
            old_name="foo", new_name="bar",
            scope=".", apply=True
        )
        assert not result.is_error
        # Should mention '1/1 file(s) updated' or similar
        assert "file" in result.output.lower()
        assert "1" in result.output


# ---------------------------------------------------------------------------
# symbol_type filtering
# ---------------------------------------------------------------------------

class TestAstRenameSymbolTypeFilter:
    def setup_method(self):
        self.tool = AstRenameTool()

    def test_function_type_only_renames_functions(self, tmp_path):
        """symbol_type='function' should rename function defs, not class names."""
        py_file = tmp_path / "mixed.py"
        py_file.write_text(
            "class target:\n    pass\n\ndef target():\n    pass\n"
        )
        config = _make_config(tmp_path)
        result = _run(
            self.tool, config,
            old_name="target", new_name="renamed",
            scope=".", symbol_type="function", apply=True
        )
        assert not result.is_error
        updated = py_file.read_text()
        # The def should be renamed
        assert "def renamed" in updated
        # The class should still be named target
        assert "class target" in updated

    def test_class_type_only_renames_classes(self, tmp_path):
        """symbol_type='class' should rename class defs, not functions."""
        py_file = tmp_path / "mixed2.py"
        py_file.write_text(
            "class target:\n    pass\n\ndef target():\n    pass\n"
        )
        config = _make_config(tmp_path)
        result = _run(
            self.tool, config,
            old_name="target", new_name="renamed",
            scope=".", symbol_type="class", apply=True
        )
        assert not result.is_error
        updated = py_file.read_text()
        # The class should be renamed
        assert "class renamed" in updated
        # The function should still be named target
        assert "def target" in updated

    def test_any_type_renames_class_definition_and_instantiation(self, tmp_path):
        """symbol_type='any' renames class def and usage."""
        py_file = tmp_path / "any_type.py"
        py_file.write_text(
            "class Foo:\n    pass\n\nx = Foo()\n"
        )
        config = _make_config(tmp_path)
        result = _run(
            self.tool, config,
            old_name="Foo", new_name="Bar",
            scope=".", symbol_type="any", apply=True
        )
        assert not result.is_error
        updated = py_file.read_text()
        assert "class Bar" in updated
        assert "x = Bar()" in updated

    def test_any_type_is_default(self, tmp_path):
        """Not specifying symbol_type defaults to 'any'."""
        py_file = tmp_path / "default_type.py"
        py_file.write_text("def foo():\n    pass\nfoo()\n")
        config = _make_config(tmp_path)
        result = _run(
            self.tool, config,
            old_name="foo", new_name="bar",
            scope=".", apply=True
        )
        assert not result.is_error
        updated = py_file.read_text()
        assert "bar" in updated
        assert "foo" not in updated


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestAstRenameEdgeCases:
    def setup_method(self):
        self.tool = AstRenameTool()

    def test_syntax_error_in_file_skipped_gracefully(self, tmp_path):
        """Files with syntax errors should be skipped without crashing."""
        (tmp_path / "broken.py").write_text(
            "def foo(\n    # broken syntax\n"
        )
        (tmp_path / "good.py").write_text(
            "def foo():\n    pass\n"
        )
        config = _make_config(tmp_path)
        result = _run(
            self.tool, config,
            old_name="foo", new_name="bar",
            scope="."
        )
        # Should not crash — should process good.py at minimum
        assert result is not None
        assert not result.is_error

    def test_scope_as_single_file(self, tmp_path):
        """scope can point to a single .py file."""
        py_file = tmp_path / "single.py"
        py_file.write_text("def foo():\n    pass\n")
        config = _make_config(tmp_path)
        result = _run(
            self.tool, config,
            old_name="foo", new_name="bar",
            scope="single.py"
        )
        assert not result.is_error
        assert "single.py" in result.output

    def test_rename_preserves_other_code(self, tmp_path):
        """Renaming should not disturb unrelated code in the file."""
        py_file = tmp_path / "preserve.py"
        py_file.write_text(
            "import os\n\nTHING = 42\n\ndef to_rename():\n    return THING\n"
        )
        config = _make_config(tmp_path)
        result = _run(
            self.tool, config,
            old_name="to_rename", new_name="renamed",
            scope=".", apply=True
        )
        assert not result.is_error
        updated = py_file.read_text()
        assert "import os" in updated
        assert "THING = 42" in updated
        assert "def renamed" in updated

    def test_dunder_names_not_rejected_as_invalid(self, tmp_path):
        """__init__ is a valid Python identifier and should not be rejected."""
        (tmp_path / "dunder.py").write_text(
            "class MyClass:\n    def __init__(self):\n        pass\n"
        )
        config = _make_config(tmp_path)
        result = _run(
            self.tool, config,
            old_name="__init__", new_name="__setup__",
            scope=".", symbol_type="method", class_name="MyClass"
        )
        # Should not fail on identifier validation
        assert result is not None
        if result.is_error:
            assert "identifier" not in result.error.lower(), (
                "dunder names should be valid identifiers"
            )

    def test_result_output_is_not_empty(self, tmp_path):
        """All successful results should have non-empty output."""
        (tmp_path / "f.py").write_text("def my_func():\n    pass\n")
        config = _make_config(tmp_path)
        result = _run(
            self.tool, config,
            old_name="my_func", new_name="better_func",
            scope="."
        )
        assert not result.is_error
        assert result.output
        assert len(result.output) > 0
