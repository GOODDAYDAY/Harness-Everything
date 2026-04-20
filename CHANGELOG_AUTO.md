# CHANGELOG_AUTO.md — Auto-generated harness change log

> **Condensed 2026-04-16**: 13 self-improvement rounds summarised below.
> Original verbose entries replaced with compact summaries.

---

## 2026-04-16: Removed parent directory creation from EditFileTool
- Removed parent directory creation logic from `EditFileTool.execute()` since editing existing files shouldn't create parent directories
- Changed parent directory validation from `require_exists=False` to `require_exists=True` for consistency
- Removed corresponding test `test_editfile_creates_parent_directories` which tested now-unneeded behavior

## 2026-04-16: Added _atomic_read_text method to WriteFileTool
- Added missing `_atomic_read_text` method to `WriteFileTool` class for consistency with other file operation tools
- Method provides atomic file reading with TOCTOU protection by calling parent class implementation
- Added test `test_writefile_atomic_read_text` to verify the method works correctly

## 2026-04-16: O_NOFOLLOW preservation in ReadFileTool
- Fixed critical TOCTOU vulnerability in `ReadFileTool.execute()` by using binary mode `os.fdopen(fd, 'rb')` to preserve O_NOFOLLOW symlink protection
- Maintains same Unicode error handling with `decode('utf-8', errors='replace')` for backward compatibility
- Added comprehensive test `test_readfile_preserves_onofollow_through_fdopen` verifying binary mode usage

## 2026-04-16: Critical TOCTOU security fix in read_file_atomically
- Fixed critical TOCTOU vulnerability in `read_file_atomically` by adding proper device/inode validation
- Now correctly detects symlink swaps by comparing opened directory descriptor with original path stats
- Security test `test_read_file_atomically_toctou_dir_fd_validation` now passes with correct behavior

## 2026-04-16: Cross-device move fallback in MoveFileTool
- Fixed cross-device move bug in `MoveFileTool.execute()` by implementing fallback using `shutil.copy2` + `os.unlink`
- Previously failed with "cross-device move not supported" error when moving files across different filesystems
- Now handles cross-device moves transparently with copy+delete fallback
- Added tests `test_movefile_cross_device_fallback` and `test_movefile_cross_device_fallback_failure` to verify behavior

---

## Summary

| Round | Theme | Key Changes | Net Lines |
|-------|-------|-------------|-----------|
| 1 | Registry restructure | GitSearchTool → OPTIONAL_TOOLS, removed import-time assertions, added `tests/` | +14 |
| 2 | Security + AST tools | New `cross_reference`, `semantic_search`, `metrics.py`; explicit allowed_paths enforcement | +440 |
| 3 | Feature search | New `feature_search` tool (keyword-based codebase discovery, 4 categories) | +213 |
| 4 | Call graph | New `call_graph` tool (AST-based, BFS traversal, depth-limited) | +283 |
| 5 | Dependency analyzer | New `dependency_analyzer` tool (import graph, cycle detection, DFS) | +320 |
| 6 | HTTP + JSON tools | New `http_client`, `json_transform` tools; subpackage reorganisation (core/, evaluation/, pipeline/) | +770 |
| 7 | Tool auto-discovery | New `discovery.py` (dynamic tool loading); `ToolRegistry.discover()` | +350 |
| 8 | Architecture fix | HttpRequestTool → OPTIONAL_TOOLS (reduce default schema cost) | −13 |
| 9 | Git search | New `git_search` tool (log/blame/grep/show/log_file modes) | +446 |
| 14 | Security validation test | Added test for consolidated _validate_root_path method verifying correct security validation order (null bytes → control chars → homoglyphs) | +60 |
| 10 | TODO scanner | New `todo_scan` tool (6 tag types, context lines, sort modes) | +300 |
| R3-fix | MetricsCollector fix | Fixed dataclass field ordering (mutable default after non-default) | +2 |
| R3-trace | Registry hardening | Manifest generator, import-time assertions (later removed in Round 1) | +79 |
| R4-security | Symlink safety | `os.path.realpath` consistency in discovery.py; deleted dead `generate_manifest.py` | −67 |

**Cumulative**: 23 → 34 tools added across rounds, then consolidated to 33 (semantic_search merged into feature_search). 6 shim files created in Round 6, eliminated post-rounds.

### Security Consolidation Round
- **Enhanced**: Unicode homoglyph security guard in `base.py::_validate_path` now has explicit test verification
- **Cleaned**: Removed dead `tempfile` import from `tests/test_path_security.py`
- **Improved**: `test_unicode_normalization_attack` now has concrete assertions instead of passive print statements
- **Verified**: All path security tests pass with stricter validation checks