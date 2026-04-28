# CLAUDE.md

## Project Overview

Harness-Everything is an AI coding harness that uses any Anthropic-compatible LLM (Claude, DeepSeek, etc.) to autonomously improve codebases. It runs in agent mode: a single LLM with full tool access, persistent notes, and multi-cycle execution.

## Tech Stack

- Python 3.11+, async throughout
- Anthropic SDK (`anthropic>=0.40.0`)
- No other runtime dependencies

## Key Commands

```bash
# Run agent mode
python -m harness.cli <config.json>

# Syntax check all harness code
python -m py_compile harness/*.py harness/tools/*.py

# Quick import verification
python -c "from harness.agent import AgentConfig, AgentLoop; print('OK')"
```

## Architecture Rules

- **Tool pattern**: Every tool is a `Tool` subclass in `harness/tools/`. Must implement `name`, `description`, `input_schema()`, `async execute(config, **params) -> ToolResult`. Register in `tools/__init__.py` `_ALL_TOOLS`.
- **Path security**: Any tool that accesses the filesystem must call `self._check_path(config, path)` before reading/writing. Never bypass this.
- **Config is data**: All project-specific content (prompts, paths, API URLs) lives in JSON configs, not in code. Code is generic.
- **Async**: All LLM calls and tool executions are async. Use `asyncio.run()` only at the entry point (`harness/cli.py`).

## File Layout

```
harness/cli.py             # CLI entry point (agent-only)
harness/
  core/
    config.py              # HarnessConfig
    llm.py                 # LLM client (retry, pruning, streaming)
    artifacts.py           # Hierarchical artifact storage
    checkpoint.py          # Resume-safe .done markers
    hooks.py               # Verification hooks (syntax, import smoke, static)
    project_context.py     # Project metadata injection
    security.py            # Path security validation
    signal_util.py         # Shutdown signal handlers
  agent/
    agent_loop.py          # AgentLoop — the main execution engine
    cycle_metrics.py       # Per-cycle metrics collection
  evaluation/
    dual_evaluator.py      # Dual isolated evaluator (DualScore, ScoreItem)
    static_analysis.py     # Deterministic code checks
    metrics.py             # Structured metrics collector
  prompts/
    dual_evaluator.py      # Dual evaluator prompts
    agent_meta_review.py   # Meta-review prompt
  tools/
    base.py                # Tool ABC, ToolResult
    registry.py            # ToolRegistry (dispatch, alias normalization)
    file_read.py           # ReadFileTool
    file_write.py          # WriteFileTool
    file_edit.py           # EditFileTool
    file_ops.py            # Delete, Move, Copy
    file_patch.py          # Structured diff patching
    directory.py           # ListDirectory, CreateDirectory, Tree
    search_glob.py         # GlobSearchTool
    search_grep.py         # GrepSearchTool
    bash.py                # BashTool (denylist)
    git.py                 # GitStatus, GitDiff, GitLog
    code_analysis.py       # AST-based analysis
    symbol_extractor.py    # Python symbol extraction
    cross_reference.py     # Import graph / xref
    _ast_utils.py          # Shared AST helpers
    find_replace.py        # Multi-file find/replace
    diff_files.py          # File differ
    test_runner.py         # Pytest integration
    python_eval.py         # Safe Python eval
    web_search.py          # DuckDuckGo (opt-in)
```

## Common Patterns

**Adding a new tool:**
1. Create `harness/tools/my_tool.py` with `class MyTool(Tool)`
2. Implement `name`, `description`, `input_schema()`, `async execute()`
3. If file-accessing: set `requires_path_check = True` and call `self._check_path()`
4. Add import to `harness/tools/__init__.py` and append to `_ALL_TOOLS`

**LLM provider switch:**
Set `base_url` and `api_key` in the agent config JSON's `harness` section. Env fallbacks: `HARNESS_BASE_URL`, `HARNESS_API_KEY`.

## What NOT to Do

- Don't put project-specific content (prompts, URLs, paths) in Python code — it goes in JSON configs
- Don't bypass `_check_path()` in tools — it's the security boundary
- Don't use `ANTHROPIC_BASE_URL` env var — it conflicts with Claude Code's proxy. Use `HARNESS_BASE_URL` instead

## Sensitive Information — Do Not Commit

The following categories of information must never appear in tracked files or git history:

**Personal / identity**
- Real names, work email addresses, employee IDs
- Personal directory paths (e.g. `/Users/<name>/...`)

**Company / internal**
- Company names, internal domain names (e.g. `*.company-internal.com`)
- Internal API gateway URLs (base_url values pointing to private infrastructure)
- Internal project codenames or product names
- Internal IP addresses (10.x.x.x, 192.168.x.x, etc.)

**Credentials**
- API keys, auth tokens, passwords — even in comments or examples
- Database connection strings

**Project-specific agent configs**
- Non-example `agent_*.json` files may contain workspace paths and API URLs
- Use `agent_example*.json` as sanitized templates for sharing

**If sensitive content is accidentally committed**, rewrite history with:
```bash
# Replace text in file blobs
git-filter-repo --replace-text replacements.txt --force

# Replace in commit messages
git-filter-repo --message-callback 'return message.replace(b"sensitive", b"redacted")' --force

# Replace author identity
git-filter-repo --mailmap mailmap.txt --force
```

## Git Identity

This repo uses a public identity. Verify before committing:
```bash
git config user.name   # should be: GOODDAYDAY
git config user.email  # should be: 865700600@qq.com
```
