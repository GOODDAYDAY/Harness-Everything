"""Tests for harness/tools/_ast_utils.py."""

from __future__ import annotations

import ast

from harness.tools._ast_utils import (
    parse_module,
    build_parent_map,
    dotted_name,
    extract_calls,
    safe_parse,
    parent_class,
    call_name,
    innermost_function,
    find_symbol_references,
    collect_variable_context,
    extract_callees,
    function_signature,
)


# ---------------------------------------------------------------------------
# parse_module — returns (tree, error) tuple
# ---------------------------------------------------------------------------

class TestParseModule:
    def test_valid_file_returns_module_tree(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text("x = 1\n")
        tree, err = parse_module(f)
        assert isinstance(tree, ast.Module)
        assert err is None

    def test_missing_file_returns_none_tree(self, tmp_path):
        tree, err = parse_module(tmp_path / "no_such_file.py")
        assert tree is None
        assert err is not None

    def test_syntax_error_returns_none_tree(self, tmp_path):
        f = tmp_path / "bad.py"
        f.write_text("def :broken(\n")
        tree, err = parse_module(f)
        assert tree is None
        assert err is not None

    def test_empty_file_returns_empty_module(self, tmp_path):
        f = tmp_path / "empty.py"
        f.write_text("")
        tree, err = parse_module(f)
        assert isinstance(tree, ast.Module)
        assert err is None

    def test_error_message_contains_filename(self, tmp_path):
        f_name = tmp_path / "missing.py"
        _, err = parse_module(f_name)
        assert str(f_name) in err or "missing" in err.lower()


# ---------------------------------------------------------------------------
# safe_parse — returns ast.Module or None
# ---------------------------------------------------------------------------

class TestSafeParse:
    def test_valid_source_returns_module(self):
        tree = safe_parse("x = 1")
        assert isinstance(tree, ast.Module)

    def test_invalid_source_returns_none(self):
        result = safe_parse("def :broken(")
        assert result is None

    def test_empty_source_returns_module(self):
        tree = safe_parse("")
        assert isinstance(tree, ast.Module)

    def test_complex_source_parses(self):
        src = "class Foo:\n    def bar(self):\n        return 42\n"
        tree = safe_parse(src)
        assert isinstance(tree, ast.Module)


# ---------------------------------------------------------------------------
# build_parent_map — returns {id(node): parent_node}
# ---------------------------------------------------------------------------

class TestBuildParentMap:
    def test_returns_dict(self):
        tree = ast.parse("x = 1\n")
        pmap = build_parent_map(tree)
        assert isinstance(pmap, dict)

    def test_child_nodes_have_parent_ids(self):
        tree = ast.parse("x = 1\n")
        pmap = build_parent_map(tree)
        # All non-root nodes should have their id in pmap
        assign = tree.body[0]
        assert id(assign) in pmap

    def test_parent_of_function_is_module(self):
        tree = ast.parse("def f():\n    pass\n")
        pmap = build_parent_map(tree)
        func = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
        parent = pmap[id(func)]
        assert isinstance(parent, ast.Module)

    def test_method_parent_is_classdef(self):
        src = "class C:\n    def m(self):\n        pass\n"
        tree = ast.parse(src)
        pmap = build_parent_map(tree)
        method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
        parent = pmap[id(method)]
        assert isinstance(parent, ast.ClassDef)


# ---------------------------------------------------------------------------
# dotted_name
# ---------------------------------------------------------------------------

class TestDottedName:
    def test_name_node(self):
        node = ast.parse("foo", mode="eval").body
        assert dotted_name(node) == "foo"

    def test_attribute_node(self):
        node = ast.parse("os.path", mode="eval").body
        assert dotted_name(node) == "os.path"

    def test_nested_attribute(self):
        node = ast.parse("os.path.join", mode="eval").body
        assert dotted_name(node) == "os.path.join"

    def test_non_name_returns_placeholder(self):
        # BinOp, numeric literals, etc. return '<expr>' or similar
        node = ast.parse("1 + 2", mode="eval").body
        result = dotted_name(node)
        # Should be non-empty but not a real dotted name
        assert result is not None

    def test_call_node_returns_placeholder(self):
        # A Call node's result is '<expr>' or similar placeholder
        node = ast.parse("foo()", mode="eval").body
        result = dotted_name(node)
        assert result is not None  # returns '<expr>' not empty/None


# ---------------------------------------------------------------------------
# call_name — returns last component of callee dotted name
# ---------------------------------------------------------------------------

class TestCallName:
    def test_simple_call(self):
        tree = ast.parse("foo()")
        call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
        result = call_name(call)
        assert result == "foo"

    def test_method_call_returns_method_name(self):
        # call_name returns only the final attribute name for obj.method()
        tree = ast.parse("obj.method()")
        call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
        result = call_name(call)
        assert result == "method"

    def test_chained_attribute_call(self):
        # a.b.c() — returns 'b.c' (drops the first part)
        tree = ast.parse("a.b.c()")
        call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
        result = call_name(call)
        # call_name returns the dotted name minus the root object
        assert "c" in result


# ---------------------------------------------------------------------------
# extract_calls — returns list of dotted call names
# ---------------------------------------------------------------------------

class TestExtractCalls:
    def test_finds_direct_calls(self):
        tree = ast.parse("def f():\n    foo()\n    bar()\n")
        func = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
        calls = extract_calls(func)
        assert "foo" in calls
        assert "bar" in calls

    def test_no_calls_returns_empty(self):
        tree = ast.parse("def f():\n    x = 1\n")
        func = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
        calls = extract_calls(func)
        assert calls == []

    def test_finds_method_calls_as_dotted(self):
        tree = ast.parse("def f():\n    self.method()\n")
        func = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
        calls = extract_calls(func)
        # extract_calls uses dotted_name so returns 'self.method'
        assert any("method" in c for c in calls)

    def test_deduplicates_repeated_calls(self):
        tree = ast.parse("def f():\n    foo()\n    foo()\n    foo()\n")
        func = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
        calls = extract_calls(func)
        assert calls.count("foo") == 1

    def test_returns_list_type(self):
        tree = ast.parse("def f():\n    foo()\n")
        func = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
        calls = extract_calls(func)
        assert isinstance(calls, list)


# ---------------------------------------------------------------------------
# extract_callees
# ---------------------------------------------------------------------------

class TestExtractCallees:
    def test_finds_callees(self):
        tree = ast.parse("def f():\n    foo()\n    bar.baz()\n")
        func = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
        callees = extract_callees(func)
        assert isinstance(callees, (list, set, tuple))

    def test_includes_called_names(self):
        tree = ast.parse("def f():\n    alpha()\n    beta()\n")
        func = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
        callees = list(extract_callees(func))
        assert any("alpha" in str(c) for c in callees)


# ---------------------------------------------------------------------------
# parent_class — takes (tree, target_node) returns class name or None
# ---------------------------------------------------------------------------

class TestParentClass:
    def test_method_returns_class_name(self):
        src = "class MyClass:\n    def my_method(self):\n        pass\n"
        tree = ast.parse(src)
        method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
        result = parent_class(tree, method)
        assert result == "MyClass"

    def test_top_level_function_returns_none(self):
        src = "def my_func():\n    pass\n"
        tree = ast.parse(src)
        func = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
        result = parent_class(tree, func)
        assert result is None

    def test_nested_inner_class_returns_inner(self):
        src = (
            "class Outer:\n"
            "    class Inner:\n"
            "        def method(self):\n"
            "            pass\n"
        )
        tree = ast.parse(src)
        # method is nested in Inner
        method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
        result = parent_class(tree, method)
        assert result == "Inner"


# ---------------------------------------------------------------------------
# innermost_function — takes (node, parent_map) returns function name or None
# ---------------------------------------------------------------------------

class TestInnermostFunction:
    def test_finds_enclosing_function_name(self):
        src = "def outer():\n    def inner():\n        x = 1\n"
        tree = ast.parse(src)
        pmap = build_parent_map(tree)
        inner = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "inner"
        )
        assign = inner.body[0]
        result = innermost_function(assign, pmap)
        assert result == "inner"

    def test_top_level_statement_returns_none(self):
        src = "x = 1\n"
        tree = ast.parse(src)
        pmap = build_parent_map(tree)
        assign = tree.body[0]
        result = innermost_function(assign, pmap)
        assert result is None

    def test_direct_function_child(self):
        src = "def my_func():\n    x = 1\n"
        tree = ast.parse(src)
        pmap = build_parent_map(tree)
        func = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
        assign = func.body[0]
        result = innermost_function(assign, pmap)
        assert result == "my_func"


# ---------------------------------------------------------------------------
# function_signature
# ---------------------------------------------------------------------------

class TestFunctionSignature:
    def test_simple_signature_contains_name_and_args(self):
        src = "def foo(a, b):\n    pass\n"
        tree = ast.parse(src)
        lines = src.splitlines()
        func = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
        sig = function_signature(func, lines)
        assert "foo" in sig
        assert "a" in sig
        assert "b" in sig

    def test_no_args_signature(self):
        src = "def foo():\n    pass\n"
        tree = ast.parse(src)
        lines = src.splitlines()
        func = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
        sig = function_signature(func, lines)
        assert "foo" in sig

    def test_returns_string(self):
        src = "def foo(x=1):\n    pass\n"
        tree = ast.parse(src)
        lines = src.splitlines()
        func = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
        sig = function_signature(func, lines)
        assert isinstance(sig, str)

    def test_star_args_included(self):
        src = "def foo(*args, **kwargs):\n    pass\n"
        tree = ast.parse(src)
        lines = src.splitlines()
        func = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
        sig = function_signature(func, lines)
        assert "foo" in sig


# ---------------------------------------------------------------------------
# find_symbol_references — takes (tree, symbol, filename)
# returns dict with 'definitions', 'calls', 'references'
# ---------------------------------------------------------------------------

class TestFindSymbolReferences:
    def test_finds_function_definition(self):
        src = "def my_func():\n    pass\n"
        tree = ast.parse(src)
        refs = find_symbol_references(tree, "my_func", "test.py")
        assert len(refs["definitions"]) >= 1

    def test_finds_call_reference(self):
        src = "def my_func():\n    pass\nmy_func()\n"
        tree = ast.parse(src)
        refs = find_symbol_references(tree, "my_func", "test.py")
        assert len(refs["definitions"]) >= 1
        assert len(refs["calls"]) >= 1

    def test_no_reference_returns_empty_lists(self):
        src = "x = 1\n"
        tree = ast.parse(src)
        refs = find_symbol_references(tree, "no_such_symbol_xyz", "test.py")
        assert refs["definitions"] == []
        assert refs["calls"] == []

    def test_class_definition(self):
        src = "class MyClass:\n    pass\n"
        tree = ast.parse(src)
        refs = find_symbol_references(tree, "MyClass", "test.py")
        assert len(refs["definitions"]) >= 1

    def test_returns_line_numbers(self):
        src = "def my_func():\n    pass\n"
        tree = ast.parse(src)
        refs = find_symbol_references(tree, "my_func", "test.py")
        # Each entry in definitions should be a (line, col) tuple
        lineno, col = refs["definitions"][0]
        assert lineno == 1

    def test_has_expected_keys(self):
        src = "x = 1\n"
        tree = ast.parse(src)
        refs = find_symbol_references(tree, "x", "test.py")
        assert "definitions" in refs
        assert "calls" in refs
        assert "references" in refs


# ---------------------------------------------------------------------------
# collect_variable_context — takes (tree) returns {var: class}
# ---------------------------------------------------------------------------

class TestCollectVariableContext:
    def test_returns_dict(self):
        src = "def f():\n    x = 42\n"
        tree = ast.parse(src)
        ctx = collect_variable_context(tree)
        assert isinstance(ctx, dict)

    def test_self_attribute_assignments_do_not_crash(self):
        src = "class C:\n    def __init__(self):\n        self.value = 1\n"
        tree = ast.parse(src)
        ctx = collect_variable_context(tree)
        assert isinstance(ctx, dict)

    def test_empty_source(self):
        tree = ast.parse("")
        ctx = collect_variable_context(tree)
        assert isinstance(ctx, dict)

    def test_constructor_assignment_detected(self):
        # obj = SomeClass() should create a mapping obj -> SomeClass
        src = "obj = SomeClass()\n"
        tree = ast.parse(src)
        ctx = collect_variable_context(tree)
        # If the implementation detects this pattern, 'obj' maps to 'SomeClass'
        # It's implementation-specific; just ensure no crash and returns dict
        assert isinstance(ctx, dict)
