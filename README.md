# Harness-Everything

Configurable AI coding harness with three-way planner-executor-evaluator architecture. Runs autonomous code improvement loops with any LLM that supports the Anthropic API format (Claude, DeepSeek, Gemini via gateway, etc.).

## Two Modes

**Simple mode** — single task, iterative plan-execute-evaluate until pass:

```bash
python main.py "Fix the login bug in auth.py" config.json
```

**Pipeline mode** — multi-phase, multi-round self-improvement loops:

```bash
python main.py --pipeline pipeline_config.json
```

## Quick Start

```bash
# Python 3.11+
pip install -e .

# Set your API key (or configure base_url + api_key in pipeline JSON)
export HARNESS_API_KEY=your-api-key

# Simple mode: one-shot task
python main.py "Add input validation to the signup endpoint"

# Pipeline mode: iterative self-improvement
python main.py --pipeline pipeline_example_self_improve.json
```

## Architecture

```
Simple Mode:
  Planner (three-way) → Executor (tool-use loop) → Evaluator (three-way)
       ↑                                                    │
       └──────────── feedback on FAIL ──────────────────────┘

Pipeline Mode:
  Outer Round 1..N
    └─ Phase 1..M (debate or implement)
         └─ Inner Round 1..K
              ├─ Executor: generate proposal / edit files
              └─ DualEvaluator: Basic (defects) + Diffusion (ripple effects)
         └─ Synthesis: merge best proposals
         └─ Hooks: syntax check, pytest
    └─ Memory: persist cross-round learnings
    └─ Early stop: patience-based
```

### Core Components

| Module | Role |
|---|---|
| `llm.py` | Async Anthropic client with retry, streaming, conversation pruning |
| `three_way.py` | Conservative/aggressive/merge resolution pattern |
| `planner.py` | Three-way plan generation |
| `executor.py` | Tool-use agentic loop (reads/writes/edits files) |
| `evaluator.py` | Three-way verdict (PASS/FAIL + feedback) |
| `dual_evaluator.py` | Two isolated parallel evaluators (basic + diffusion) |
| `phase_runner.py` | Orchestrates one phase: inner rounds + synthesis + hooks |
| `pipeline.py` | Orchestrates outer rounds across phases |
| `memory.py` | Cross-round learning persistence (JSONL) |
| `artifacts.py` | Hierarchical output: run/round/phase/inner |
| `checkpoint.py` | `.done` markers for resume-safe execution |
| `metrics.py` | Per-phase structured metrics (JSON) |
| `hooks.py` | Verification: syntax check, pytest |
| `static_analysis.py` | Deterministic code quality checks (no LLM) |

### Tool System (24 built-in)

**File ops**: `read_file`, `write_file`, `edit_file`, `delete_file`, `move_file`, `copy_file`, `file_patch`
**Directory**: `list_directory`, `create_directory`, `tree`
**Search**: `glob_search`, `grep_search`, `cross_reference`
**Git**: `git_status`, `git_diff`, `git_log`
**Execution**: `bash`, `python_eval`, `test_runner`
**Analysis**: `code_analysis`, `symbol_extractor`, `find_replace`, `diff_files`
**Optional**: `web_search` (opt-in via `extra_tools`)

All tools follow the `Tool` ABC pattern. Path-accessing tools enforce workspace boundaries via `_check_path()`.

## Configuration

### Simple Mode

```json
{
  "model": "claude-sonnet-4-6",
  "max_tokens": 8096,
  "base_url": "https://api.anthropic.com",
  "workspace": "/path/to/project",
  "allowed_paths": ["/path/to/project"],
  "max_iterations": 5,
  "max_tool_turns": 30
}
```

### Pipeline Mode

See [`pipeline_example_self_improve.json`](pipeline_example_self_improve.json) for a single-repo setup and [`pipeline_example_multi_repo.json`](pipeline_example_multi_repo.json) for multi-repo.

Key pipeline fields:

| Field | Default | Description |
|---|---|---|
| `outer_rounds` | 5 | Total improvement iterations |
| `inner_rounds` | 3 | Proposals per phase per outer round |
| `patience` | 3 | Stop after N rounds without score improvement |
| `evaluation_mode` | `dual_isolated` | `three_way` or `dual_isolated` |
| `max_file_context_chars` | 60000 | Budget for source code injection per phase |

### Phase Configuration

Each phase has:

| Field | Description |
|---|---|
| `name` | Phase identifier |
| `mode` | `debate` (text proposals) or `implement` (tool-use, edits files) |
| `system_prompt` | Executor prompt with `$file_context`, `$prior_best`, `$syntax_errors`, `$falsifiable_criterion` |
| `glob_patterns` | Files to inject as context |
| `skip_after_round` | Skip in outer rounds > N |
| `skip_cycle` | Run every N-th outer round (e.g., 3 = rounds 0, 3, 6...) |
| `commit_on_success` | Auto-commit after passing hooks |
| `syntax_check_patterns` | Glob patterns for syntax validation |
| `run_tests` | Run pytest after implementation |

### LLM Provider

Works with any LLM that supports the Anthropic API format. Configure via JSON or env vars:

```json
{
  "model": "claude-sonnet-4-6",
  "base_url": "https://api.anthropic.com",
  "api_key": "sk-..."
}
```

Switch to DeepSeek, a custom gateway, or any other provider by changing `base_url` and `model`:

```json
{
  "model": "deepseek-chat",
  "base_url": "https://api.deepseek.com/v1",
  "api_key": "your-deepseek-key"
}
```

Env var fallback: `HARNESS_BASE_URL`, `HARNESS_API_KEY`, `ANTHROPIC_API_KEY`.

## Output

Pipeline runs produce:

```
output_dir/
├── run_20260414T120000/
│   ├── memory.jsonl              # Cross-round learnings
│   ├── round_1/
│   │   ├── phase_1_security/
│   │   │   ├── inner_1/
│   │   │   │   ├── proposal.txt
│   │   │   │   ├── basic_eval.txt
│   │   │   │   └── diffusion_eval.txt
│   │   │   └── synthesis.txt
│   │   └── summary.md
│   └── ...
│   └── final_summary.md
└── .harness_metrics.json         # Structured execution metrics
```

## Security

- **Path boundaries**: All file access checked against `allowed_paths`
- **Bash denylist**: Configurable command blocklist
- **Tool allowlist**: Only `allowed_tools` are available to the executor
- **Conversation pruning**: Auto-truncate at 600K chars to prevent context explosion
- **Tool budget**: `max_tool_turns` caps runaway loops

## Requirements

- Python 3.11+
- `anthropic>=0.40.0`

## License

MIT
