# CLAUDE.md

## Project Overview

Harness-Everything is an AI coding harness that uses any Anthropic-compatible LLM (Claude, DeepSeek, etc.) to autonomously improve codebases. Two modes: simple (single task) and pipeline (multi-phase iterative improvement).

## Tech Stack

- Python 3.11+, async throughout
- Anthropic SDK (`anthropic>=0.40.0`)
- No other runtime dependencies

## Key Commands

```bash
# Run simple mode
python main.py "task description" [config.json]

# Run pipeline mode
python main.py --pipeline pipeline_config.json

# Syntax check all harness code
python -m py_compile harness/*.py harness/tools/*.py

# Quick import verification
python -c "from harness.config import PipelineConfig, HarnessConfig; print('OK')"
```

## Architecture Rules

- **Tool pattern**: Every tool is a `Tool` subclass in `harness/tools/`. Must implement `name`, `description`, `input_schema()`, `async execute(config, **params) -> ToolResult`. Register in `tools/__init__.py` `_ALL_TOOLS`.
- **Path security**: Any tool that accesses the filesystem must call `self._check_path(config, path)` before reading/writing. Never bypass this.
- **Config is data**: All project-specific content (prompts, paths, API URLs) lives in pipeline JSON configs, not in code. Code is generic.
- **Async**: All LLM calls and tool executions are async. Use `asyncio.run()` only at the entry point (`main.py`).
- **Dataclasses for data**: `PhaseConfig`, `HarnessConfig`, `PipelineConfig`, `ToolResult`, `InnerResult`, `PhaseResult` are all dataclasses. No behavior in data classes (except `from_dict()` and simple properties).

## File Layout

```
main.py                    # CLI entry point
harness/
  config.py                # HarnessConfig, PipelineConfig, DualEvaluatorConfig
  llm.py                   # LLM client (retry, pruning, streaming)
  loop.py                  # Simple mode: HarnessLoop
  pipeline.py              # Pipeline mode: PipelineLoop
  phase.py                 # PhaseConfig, InnerResult, PhaseResult (data only)
  phase_runner.py          # PhaseRunner (execute one phase)
  planner.py               # Three-way planner
  executor.py              # Tool-use executor
  evaluator.py             # Three-way evaluator
  dual_evaluator.py        # Dual isolated evaluator
  three_way.py             # ThreeWayResolver pattern
  memory.py                # Cross-round learning (JSONL)
  artifacts.py             # Hierarchical artifact storage
  checkpoint.py            # Resume-safe .done markers
  metrics.py               # Structured metrics collector
  hooks.py                 # Verification hooks (syntax, pytest)
  static_analysis.py       # Deterministic code checks
  project_context.py       # Project metadata injection
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
    semantic_search.py     # Keyword-based semantic search
    find_replace.py        # Multi-file find/replace
    diff_files.py          # File differ
    test_runner.py         # Pytest integration
    python_eval.py         # Safe Python eval
    web_search.py          # DuckDuckGo (opt-in)
  prompts/
    planner.py             # Three-way planner prompts
    evaluator.py           # Three-way evaluator prompts
    dual_evaluator.py      # Dual evaluator prompts
    synthesis.py           # Synthesis prompts
```

## Common Patterns

**Adding a new tool:**
1. Create `harness/tools/my_tool.py` with `class MyTool(Tool)`
2. Implement `name`, `description`, `input_schema()`, `async execute()`
3. If file-accessing: set `requires_path_check = True` and call `self._check_path()`
4. Add import to `harness/tools/__init__.py` and append to `_ALL_TOOLS`

**Pipeline config template variables:**
- `$file_context` — source code injected by glob patterns
- `$prior_best` — best proposal from previous rounds
- `$syntax_errors` — errors from syntax check hook
- `$falsifiable_criterion` — evaluation criterion from phase config

**LLM provider switch:**
Set `base_url` and `api_key` in the pipeline config's `harness` section. Env fallbacks: `HARNESS_BASE_URL`, `HARNESS_API_KEY`.

## What NOT to Do

- Don't put project-specific content (prompts, URLs, paths) in Python code — it goes in pipeline JSON configs
- Don't bypass `_check_path()` in tools — it's the security boundary
- Don't use `ANTHROPIC_BASE_URL` env var — it conflicts with Claude Code's proxy. Use `HARNESS_BASE_URL` instead
- Don't define `metrics` or other shared state as local variables in `PipelineLoop.run()` — use `self.` for anything accessed by other methods
- Pipeline JSON configs (`pipeline_*.json`) are gitignored (except `pipeline_example_*.json`) because they contain sensitive paths and API URLs

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

**Project-specific pipeline configs**
- `pipeline_*.json` files contain workspace paths, API URLs, and detailed internal architecture descriptions
- They are gitignored by design — keep it that way
- Use `pipeline_example_*.json` as sanitized templates for sharing

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
