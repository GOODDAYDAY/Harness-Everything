# Search and Analysis

User stories covering capabilities for finding files, searching file contents, and performing static code analysis: glob search, grep, AST-based analysis, symbol extraction, cross-referencing, and call graphs.

**Actors:**
- "As the agent" -- the agent performing search and analysis to understand the codebase

---

## File Search (Glob)

## US-01: As the agent, I need to find files by name or path pattern so that I can locate relevant source files without knowing their exact paths

The agent provides a glob pattern (e.g., all Python files, all test files under a directory) and receives a list of matching file paths sorted by modification time, most recent first.

### Acceptance Criteria
- Given a glob pattern and a root directory, when the agent searches, then all matching files within the allowed directories are returned as relative paths
- Given a glob pattern that matches no files, when the agent searches, then a message indicates zero matches
- Given results exceeding the requested limit, when the agent searches, then only the most recently modified files up to the limit are returned with a note that results were truncated

## US-02: As the agent, I need file search to have a candidate scan cap so that broad patterns in large repositories do not hang or exhaust memory

When a very broad pattern (e.g., all files) is used in a large repository, the search stops scanning after a configured maximum number of candidates. The result includes a warning advising the agent to narrow the pattern.

### Acceptance Criteria
- Given a broad pattern that produces more candidates than the scan cap, when the search runs, then scanning stops at the cap and the result warns that some matches may be missing
- Given a narrow pattern that produces fewer candidates than the scan cap, when the search runs, then all candidates are evaluated

---

## Content Search (Grep)

## US-03: As the agent, I need to search file contents using a regex pattern so that I can find specific text, imports, or call patterns across the codebase

The agent provides a regex pattern and receives matching lines with file paths and line numbers. An optional file glob filter restricts which files are searched. Context lines before and after each match provide surrounding code for understanding.

### Acceptance Criteria
- Given a regex pattern and a directory, when the agent searches, then all matching lines across all files are returned with relative file paths and line numbers
- Given a context lines setting greater than zero, when matches are found, then surrounding lines are included in the output
- Given a context lines setting of zero, when matches are found, then only the matching lines themselves are returned
- Given a file glob filter, when the agent searches, then only files matching the glob are scanned

## US-04: As the agent, I need case-insensitive content search so that I can find text regardless of capitalization

The agent can enable case-insensitive matching for content searches, useful for finding variable names or strings that may appear in different cases.

### Acceptance Criteria
- Given a case-insensitive search, when the pattern matches text differing only in case, then those matches are included in the results

## US-05: As the agent, I need content search results capped at a specified limit so that broad searches do not flood my context window

The agent specifies the maximum number of matches to return. Once the limit is reached, searching stops and the result indicates it was truncated.

### Acceptance Criteria
- Given a match limit, when the total matches exceed the limit, then only matches up to the limit are returned
- Given a match limit, when fewer matches exist, then all matches are returned

---

## Code Analysis (AST-based)

## US-06: As the agent, I need to analyze a Python file's structure without executing it so that I can understand its classes, functions, imports, and complexity

The agent provides a file or directory path and receives a static analysis report containing: symbol table (classes, functions, constants with line numbers), import map, per-function call graph (outgoing calls), and cyclomatic complexity estimates.

### Acceptance Criteria
- Given a single Python file, when the agent analyzes it, then the report includes all top-level classes with methods, all functions with argument lists, all imports with line numbers, and per-function complexity scores
- Given a directory, when the agent analyzes it, then all matching Python files are analyzed and an aggregate summary across files is appended
- Given a file with syntax errors, when the agent analyzes it, then the error is reported for that file without aborting analysis of other files

## US-07: As the agent, I need to identify high-complexity functions so that I can prioritize refactoring targets

The analysis report flags functions whose complexity score exceeds a threshold, listing them prominently in the summary. This helps the agent focus on the most tangled code.

### Acceptance Criteria
- Given functions with varying complexity, when analysis completes, then functions at or above the complexity threshold are listed in a dedicated high-complexity section
- Given an aggregate analysis of multiple files, when high-complexity functions exist, then they are listed with their file paths for cross-file visibility

## US-08: As the agent, I need analysis output in either human-readable text or structured data format so that I can choose based on my downstream needs

The analysis tool supports both a formatted text output (for reading in context) and a JSON output (for programmatic consumption by other tools or post-processing steps).

### Acceptance Criteria
- Given a text format request, when analysis completes, then the output is formatted with section headers, aligned columns, and summary blocks
- Given a JSON format request, when analysis completes, then the output is valid JSON containing the same data as the text format

---

## Symbol Extraction

## US-09: As the agent, I need to extract the complete source code of a named function, class, or constant without reading the entire file so that I consume minimal context budget

The agent provides a symbol name (e.g., a function name, a class name, or a qualified method name) and receives the exact source text of that definition -- nothing more, nothing less. This is far more token-efficient than reading a whole file when only one function is needed.

### Acceptance Criteria
- Given a function name and a file path, when the agent extracts it, then only the source text of that function is returned with a header showing the file, symbol name, kind, and line range
- Given a class name, when the agent extracts it, then the entire class body is returned
- Given a qualified method name (class and method), when the agent extracts it, then only the method body is returned, not the entire class
- Given a module-level constant name, when the agent extracts it, then the assignment statement is returned

## US-10: As the agent, I need to extract symbols using glob patterns so that I can find all definitions matching a naming convention

The agent can provide a wildcard pattern (e.g., all private helpers starting with a prefix, or all methods named a certain way across classes). All matching definitions are returned.

### Acceptance Criteria
- Given a glob pattern for symbol names, when the agent extracts, then all functions, methods, and constants matching the pattern are returned
- Given a pattern with no matches, when the agent extracts, then a message indicates no symbols were found

## US-11: As the agent, I need to extract multiple symbols in one call so that I can read several related definitions without multiple round-trips

The agent provides a list of symbol names or patterns and receives all matching definitions from the target file or directory in a single response.

### Acceptance Criteria
- Given a list of symbol names, when the agent extracts, then all matching symbols are returned in a single result
- Given a limit on the number of results, when matches exceed the limit, then the result is truncated with a notice

## US-12: As the agent, I need to search an entire directory for a symbol so that I can find where a function or class is defined when I do not know which file it is in

When the target path is a directory, the extraction tool searches all Python files under it for the named symbol. An optional file glob narrows the search scope.

### Acceptance Criteria
- Given a symbol name and a directory, when the agent extracts, then all files under the directory are searched and matching definitions from any file are returned
- Given a file glob filter, when extracting across a directory, then only files matching the glob are searched

## US-13: As the agent, I need optional context lines before a symbol definition so that I can see decorators, comments, or preceding code

The agent can request additional lines of source code before the start of each extracted symbol. This is useful for seeing decorators, docstrings, or comments that give semantic context to the definition.

### Acceptance Criteria
- Given a context-lines parameter greater than zero, when a symbol is extracted, then the requested number of lines preceding the definition are prepended to the output
- Given a context-lines parameter of zero, when a symbol is extracted, then only the definition itself is returned

---

## Cross-referencing

## US-14: As the agent, I need to find where a Python symbol is defined and every place it is called so that I can understand its usage across the codebase

The agent provides a symbol name and receives: its definition location (file and line), a list of all call sites (callers), a list of all functions it calls (callees), and which test files exercise it. All analysis is AST-based, avoiding false positives from comments or strings.

### Acceptance Criteria
- Given a function name, when the agent cross-references it, then the definition file and line, all callers with file/line/snippet, and all callees are returned
- Given a qualified method name (class and method), when the agent cross-references it, then callers include both direct class calls and instance method calls
- Given a symbol with test files that reference it, when cross-referencing with test inclusion enabled, then matching test files are listed
- Given a symbol that does not exist in the codebase, when the agent cross-references it, then the definition is reported as not found and callers/callees are empty

## US-15: As the agent, I need cross-reference results to be bounded so that large codebases do not produce overwhelming output

The cross-reference tool enforces caps on the number of callers, callees, and test files returned. When the cap is reached, the result includes a truncation indicator.

### Acceptance Criteria
- Given a heavily-used symbol exceeding the caller cap, when cross-referencing completes, then only callers up to the cap are returned and the result indicates truncation
- Given long code snippets in caller entries, when cross-referencing completes, then snippets are truncated to a readable length

## US-16: As the agent, I need symbol names validated for security so that maliciously crafted inputs cannot cause unexpected behavior

Cross-reference symbol inputs are validated against a strict format: ASCII letters, digits, underscores, and dots only, with a maximum qualification depth. This prevents injection of path traversal sequences or excessively deep lookups.

### Acceptance Criteria
- Given a valid symbol name (e.g., "my_function" or "MyClass.method"), when validation runs, then it passes
- Given a symbol name containing non-ASCII characters or path separators, when validation runs, then it is rejected with a format error
- Given a symbol name with excessive dot-qualification depth, when validation runs, then it is rejected with a depth error

---

## Combined Extraction and Cross-referencing

## US-17: As the agent, I need to extract a symbol's source and find its cross-references in a single operation so that I get the full picture of a symbol's definition and usage without multiple calls

The symbol extraction tool can optionally include cross-reference data (callers, callees, test files) alongside the extracted source code, combining two lookups into one.

### Acceptance Criteria
- Given an extraction request with cross-references enabled, when the symbol is found, then both the source code and the cross-reference data are returned in a single result (note: cross-reference data in combined results is currently only available in JSON output format; text format shows placeholder text)
- Given an extraction request with cross-references disabled (default), when the symbol is found, then only the source code is returned
