# Code Analysis Tools

AST-based static analysis tools: `code_analysis`, `symbol_extractor`, `cross_reference`, `call_graph`, `dependency_analyzer`, `data_flow`, `ast_rename`, `project_map`.

All tools in this domain use Python's `ast` module for analysis. No code is executed. No external dependencies.

---

## code_analysis

**Source**: `harness/tools/code_analysis.py`

| Field | Value |
|-------|-------|
| name | `"code_analysis"` |
| requires_path_check | `True` |
| tags | `frozenset({"analysis"})` |

### Description

AST-based static analysis of Python source files. Reports: symbol table, import map, outgoing call graph per function, and cyclomatic-complexity proxy.

### Input Schema

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `path` | `string` | Yes | -- | File or directory to analyze |
| `file_glob` | `string` | No | `"**/*.py"` | Glob pattern when path is a directory |
| `format` | `string` (enum) | No | `"text"` | `text` or `json` |
| `limit` | `integer` | No | `50` | Max files to analyze |

Required: `["path"]`

### Analysis Output (`_analyse_source`)

For each file:
- **imports**: `[{type: "import"|"from", module, name?, alias?, line}]`
- **symbols**: Top-level classes, functions, constants
  - Functions: `{kind: "function"|"async_function", name, line, args, complexity}`
  - Classes: `{kind: "class", name, line, bases, methods, method_count}`
  - Constants: `{kind: "constant", name, line}` (ALL_CAPS names only)
- **functions**: All callables including class methods. `{name, line, is_async, args, complexity, calls}`. Method names formatted as `"ClassName.method_name"`.
- **summary**: `{classes, functions, imports, avg_complexity, high_complexity_functions}`. High-complexity threshold: complexity >= 10.

### Cyclomatic Complexity

Branch-count heuristic (`_complexity` function). Base count = 1. Each of these AST node types adds 1:
- `If`, `For`, `AsyncFor`, `While`, `ExceptHandler`, `With`, `AsyncWith`, `Assert`, `comprehension`
- `BoolOp`: adds `len(values) - 1` (each extra `and`/`or` operand).

### Text Format

```
=== filename ===
Lines: N  Classes: N  Functions: N  Imports: N  Avg complexity: N.N
High-complexity (>=10): func1, func2

Imports:
  L  42  import os
  L  43  from pathlib import Path

Symbols:
  L  10  class MyClass(Base)  [3 method(s): __init__, run, stop]
  L  50  def my_func(self, x)  complexity=4

Call graph (outgoing calls per function):
  my_func -> helper_a, helper_b
```

Multi-file: aggregate summary appended (`=== AGGREGATE SUMMARY ===`).

---

## symbol_extractor

**Source**: `harness/tools/symbol_extractor.py`

| Field | Value |
|-------|-------|
| name | `"symbol_extractor"` |
| requires_path_check | `True` |
| tags | `frozenset({"analysis"})` |

### Description

Extract the complete source of named Python symbols (functions, classes, methods, constants) via AST. More token-efficient than `read_file` when you only need specific definitions.

### Input Schema

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `path` | `string` | Yes | -- | File or directory to search |
| `symbols` | `string` or `array[string]` | Yes | -- | Symbol name(s) or glob patterns |
| `file_glob` | `string` | No | `"**/*.py"` | File glob for directory search |
| `context_lines` | `integer` | No | `0` | Lines of context before the definition |
| `format` | `string` (enum) | No | `"text"` | `text` or `json` |
| `limit` | `integer` | No | `20` | Max symbols to return |
| `find_cross_references` | `boolean` | No | `false` | Also find cross-references (callers, callees, test files) |

Required: `["path", "symbols"]`

### Symbol Matching Rules

- If symbol string is a plain string, split by comma.
- **Dotted patterns** (e.g. `"MyClass.method"`): matched against the qualified name.
- **Undotted patterns** (e.g. `"input_schema"`, `"_check_*"`): matched against **both** the bare name and the method-name portion. So `"input_schema"` matches `ReadFileTool.input_schema`.
- **Glob patterns**: `fnmatch` syntax supported. `"_check_*"` matches all helpers starting with `_check_`.

### Symbol Types

`_SymbolMatch` records: `qualname`, `file`, `lineno`, `end_lineno`, `source_text`, `kind`.
Kinds: `"function"`, `"async_function"`, `"class"`, `"method"`, `"async_method"`, `"constant"`.

### Source Extraction

- `ast.get_source_segment(source, node)` with fallback to line-range slice.
- `textwrap.dedent` applied to extracted text.
- Context lines prepended via `_add_context_before` (shows decorators, preceding comments).

### Cross-References Integration

When `find_cross_references=True` and `format="json"`:
- Initializes `CrossReferenceTool()` and calls it for each extracted symbol.
- For single symbol: unwraps to flat `{callers, callees, test_files}`.
- For multiple symbols: per-symbol nesting.
- Errors in cross-reference analysis are returned immediately.

### Text Format

```
=== path/to/file.py :: ClassName.method_name  [method]  (line 42-58) ===
def method_name(self, arg):
    ...
```

---

## cross_reference

**Source**: `harness/tools/cross_reference.py`

| Field | Value |
|-------|-------|
| name | `"cross_reference"` |
| requires_path_check | `True` |
| tags | `frozenset({"analysis"})` |
| `_MAX_OUTPUT_BYTES` | `8_192` |

### Description

Find where a Python symbol is defined and all its call sites across the codebase. Returns definition location, callers list (max 50), callees list (max 30), and test files (max 20).

### Symbol Validation

- `_VALID_SYMBOL_PATTERN`: ASCII-only, `[a-zA-Z_][a-zA-Z0-9_]*` optionally dot-qualified, max 10 identifiers.
- `_MAX_SYMBOL_IDENTIFIERS`: `10`.
- Rejects: empty/whitespace-only, exceeds depth limit, non-ASCII, consecutive dots, leading/trailing dots.

### Input Schema

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `symbol` | `string` | Yes | -- | Symbol to look up. `func_name` or `ClassName.method_name`. |
| `root` | `string` | No | `""` | Directory to search (default: workspace) |
| `include_tests` | `boolean` | No | `true` | Include test files in results |

Required: `["symbol"]`

### Execution

1. Validate symbol format.
2. Split symbol: if dot-qualified, `class_name = parts[0]`, `func_name = parts[1]`.
3. Scan all `.py` files via `_rglob_safe`.
4. For each file, read atomically via `read_file_atomically`.
5. **Definition**: First `FunctionDef`/`AsyncFunctionDef` matching `func_name` (and optionally `class_name` via `parent_class`). Or `ClassDef` matching bare name.
6. **Callers**: `ast.Call` nodes where `call_name(node, context)` matches. Instance method calls detected via `_is_instance_method_call`. Max 50.
7. **Callees**: `extract_callees(node)` from the definition. Max 30.
8. **Test files**: Files matching `test_*`, `*_test.py`, `*_spec.py` whose source contains the function name (word-boundary regex). Max 20.

### Instance Method Call Detection (`_is_instance_method_call`)

Checks if a `Call` node represents an instance method call:
- Direct call on typed variable: `obj.method()` where context maps `obj` to the class.
- Direct `self.method()` inside the class.
- For chained calls (depth > 0) or unknown types: optimistic matching (return True to maximize recall).

### Output

JSON via `_safe_json(result, max_bytes=8192)`:

```json
{
  "symbol": "my_func",
  "definition": {"file": "mod.py", "line": 42, "signature": "def my_func(x, y):"},
  "callers": [{"file": "caller.py", "line": 10, "snippet": "my_func(1, 2)"}],
  "callees": ["helper_a", "helper_b"],
  "test_files": ["tests/test_mod.py"],
  "files_scanned": 150,
  "truncated": false
}
```

Snippets truncated to `MAX_SNIPPET_LENGTH = 200`.

---

## call_graph

**Source**: `harness/tools/call_graph.py`

| Field | Value |
|-------|-------|
| name | `"call_graph"` |
| requires_path_check | `False` |
| tags | `frozenset({"analysis"})` |
| `_MAX_OUTPUT_BYTES` | `24_000` |
| `_MAX_NODES` | `200` |
| `_MAX_DEPTH` | `5` |

### Description

Build a call graph rooted at a given function using AST analysis. Traces outgoing calls recursively up to specified depth.

### Input Schema

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `function_name` | `string` | Yes | -- | Root function (`my_func` or `MyClass.my_method`) |
| `root` | `string` | No | `""` | Directory to search (default: workspace) |
| `depth` | `integer` | Yes | -- | Recursion depth (min 1, max 5) |
| `include_builtins` | `boolean` | No | `false` | Include Python builtins/stdlib in graph |

Required: `["function_name", "depth"]`

### Index Building (`_build_index`)

Single pass over all Python files. Returns two indexes:
- `defs_by_qualname`: `{qualname: {file, line, calls}}` -- first encountered wins.
- `defs_by_bare`: `{bare_name: [{file, line, calls}]}` -- all definitions for a bare name.

### Graph Construction

BFS expansion from seed function:
- Visited set prevents infinite loops (cycle guard).
- Node cap: 200 unique nodes.
- Depth cap: hard-capped at 5.

### Output

JSON via `_safe_json`. Each node: `{file, line, calls, depth}`.

---

## dependency_analyzer

**Source**: `harness/tools/dependency_analyzer.py`

| Field | Value |
|-------|-------|
| name | `"dependency_analyzer"` |
| requires_path_check | `False` |
| tags | `frozenset({"analysis"})` |
| `_MAX_OUTPUT_BYTES` | `24_000` |

### Description

Analyze Python import dependencies. Builds module-level dependency graph, detects circular imports.

### Input Schema

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `root` | `string` | No | `""` | Directory to analyze (default: workspace) |
| `mode` | `string` (enum) | No | `"graph"` | `graph`, `cycles`, `imports` |
| `include_stdlib` | `boolean` | No | `false` | Include stdlib/third-party imports |
| `module_filter` | `string` | No | `""` | Dotted module prefix to restrict output |

Required: `[]` (no required parameters)

### Modes

**graph**: Full dependency graph: `{module_name: [imported_modules]}`.

**cycles**: Circular import chains only (empty list if none). Uses iterative DFS with WHITE/GRAY/BLACK coloring. Max 20 cycles.

**imports**: Per-file import list.

### Module Name Resolution

- File-to-module: `root/harness/tools/foo.py` -> `harness.tools.foo`. `__init__.py` -> parent package.
- Relative imports: `from . import utils` resolved via `_resolve_relative`. Level 1 = current package, level 2 = parent.
- Workspace-local filtering: only imports whose dotted prefix matches a known workspace module are included (unless `include_stdlib=True`).

---

## data_flow

**Source**: `harness/tools/data_flow.py`

| Field | Value |
|-------|-------|
| name | `"data_flow"` |
| requires_path_check | `False` |
| tags | `frozenset({"analysis"})` |
| `_MAX_OUTPUT_BYTES` | `24_000` |

### Description

Trace how a symbol is used across the workspace. Three modes: `callers`, `reads`, `call_chain`.

### Input Schema

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `symbol` | `string` | Yes | -- | Symbol to trace. `func_name` for callers, `obj.attr` for reads. |
| `mode` | `string` (enum) | No | `"callers"` | `reads`, `callers`, `call_chain` |
| `root` | `string` | No | `""` | Directory to search (default: workspace) |
| `depth` | `integer` | Yes | -- | For `call_chain`: 1 = direct callers, 2 = also callers-of-callers. Capped at 2. For other modes, required but unused. |

Required: `["symbol", "depth"]`

### Modes

**reads**: Find `ast.Attribute` nodes where `node.attr == attr_name` (and optionally `node.value.id == obj_name`). Returns `[{file, line, context}]`.

**callers**: Find `ast.Call` nodes matching the symbol. Returns `[{file, line, enclosing_function}]`. Uses `build_parent_map` and `innermost_function` from `_ast_utils`.

**call_chain**: Depth-1 = `_find_callers(symbol)`. Depth-2 = for each unique `enclosing_function` from depth-1, call `_find_callers` again. Returns `{l1_callers, l2_callers?}`.

### Output

JSON via `_safe_json({symbol, mode, results}, max_bytes=24_000)`.

---

## ast_rename

**Source**: `harness/tools/ast_rename.py`

| Field | Value |
|-------|-------|
| name | `"ast_rename"` |
| requires_path_check | `True` |
| tags | `frozenset({"file_write"})` |
| `MAX_FILES` | `500` |

### Description

Rename a Python symbol across the codebase using AST analysis. Only renames actual code identifiers -- won't touch strings, comments, or unrelated scopes. Safer than `find_replace` for refactoring.

### Input Schema

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `old_name` | `string` | Yes | -- | Current symbol name |
| `new_name` | `string` | Yes | -- | New symbol name |
| `scope` | `string` | Yes | -- | Directory or file to scan |
| `symbol_type` | `string` (enum) | Yes | -- | `function`, `class`, `variable`, `method`, `any` |
| `class_name` | `string` | No | `""` | Required when `symbol_type="method"` |
| `apply` | `boolean` | No | `false` | If true, write changes. If false, preview only. |

Required: `["old_name", "new_name", "scope", "symbol_type"]`

### Validation

- Both `old_name` and `new_name` must match `^[a-zA-Z_][a-zA-Z0-9_]*$`.
- `old_name` must not equal `new_name`.
- `symbol_type="method"` requires `class_name`.

### Supported Renames

- Top-level functions
- Top-level classes
- Module-level variables / constants
- Method names within a specific class
- Import references (`from mod import old_name` -> `from mod import new_name`)

### NOT Renamed

- Local variables inside functions (too ambiguous without type inference)
- Dynamic attribute access (`getattr(obj, "name")`)
- String literals containing the name

---

## project_map

**Source**: `harness/tools/project_map.py`

| Field | Value |
|-------|-------|
| name | `"project_map"` |
| requires_path_check | `True` |
| tags | `frozenset({"analysis"})` |
| `MAX_FILES` | `500` |
| `_MAX_OUTPUT_CHARS` | `30_000` |

### Description

Generate a high-level project overview: modules with line/class/function counts, entry points, and inter-module import graph. One call replaces `tree` + many reads for project orientation.

### Input Schema

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `path` | `string` | Yes | -- | Root directory to scan |
| `max_depth` | `integer` | Yes | -- | Max directory depth (must be >= 1) |
| `include_tests` | `boolean` | No | `false` | Include test files (`test_*.py`, `*_test.py`) |

Required: `["path", "max_depth"]`

### Output

Scans Python files and produces:
- Module list with line counts, class counts, function counts.
- Entry points: files with `if __name__ == "__main__"`.
- Inter-module import graph (who imports whom).
- Summary stats: total files, total lines, total classes, total functions.
