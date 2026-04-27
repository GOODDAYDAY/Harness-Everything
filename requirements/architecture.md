# Technical Architecture

## Philosophy

**Agent-centric, not pipeline.** Harness-Everything gives a single LLM full tool access and lets it decide what to read, edit, and verify. There is no orchestrator, no phase graph, no next-step handoff. The LLM is the only agent; the framework provides the loop, the tools, the safety nets, and the evaluation feedback.

**Config is data, code is generic.** All project-specific content -- missions, prompts, paths, API URLs -- lives in JSON config files (`config/*.json`). Python code contains zero project-specific logic. This means swapping target codebases or LLM providers requires changing one JSON file, not touching source code.

**Separation of construction and execution.** Prompt assembly (what the LLM sees) and LLM invocation (how it runs) are separate concerns. `agent_loop.py` builds the system prompt; `llm.py` handles API calls with retry/pruning/streaming. The two evolve independently.

**Security is a hard boundary.** Every file-accessing tool must call `_check_path()` before touching the filesystem. Path validation covers null bytes, control characters, Unicode homoglyphs, symlink resolution, and TOCTOU protection via atomic `O_NOFOLLOW` opens with inode verification. The workspace sandbox is enforced at the `HarnessConfig` level, not per-tool.

**Async throughout.** All LLM calls and tool executions are async. `asyncio.run()` appears only at the entry point (`main.py`). Read-only tools execute in parallel via `asyncio.gather`; mutating tools run sequentially within each turn.

**Crash safety by design.** Artifacts persist to disk between cycles. Git commits happen after verification hooks pass. A SIGKILL mid-cycle loses at most one uncommitted cycle; everything prior is on disk in `harness_output/` and in git history.

## Architecture Overview

```
main.py
  |
  v
AgentLoop.run()                          # harness/agent/agent_loop.py
  |
  +-- for cycle in range(max_cycles):
  |     |
  |     +-- [1] Build system prompt
  |     |       mission + strategic direction + persistent notes
  |     |       + project parameters + cycle counter
  |     |
  |     +-- [2] LLM.call_with_tools()   # harness/core/llm.py
  |     |       Single tool-use dialogue, up to max_tool_turns calls.
  |     |       Tools dispatched via ToolRegistry.
  |     |       Read-only tools run in parallel; writes run serially.
  |     |       Conversation pruned proactively to stay under context window.
  |     |       File-read cache deduplicates repeated reads within a turn.
  |     |
  |     +-- [3] Post-cycle hooks        # harness/core/hooks.py
  |     |       SyntaxCheckHook   (py_compile, gates commit)
  |     |       StaticCheckHook   (ruff/pyflakes F821/F811/F401, gates commit)
  |     |       ImportSmokeHook   (subprocess import, gates commit)
  |     |
  |     +-- [4] Git: stage + evaluate + commit
  |     |       Stage changed files -> DualEvaluator on staged diff ->
  |     |       commit with metrics/eval/hooks in message body
  |     |
  |     +-- [5] Meta-review (every N cycles)
  |     |       Analyses score trends + git history -> strategic direction
  |     |       injected into subsequent system prompts
  |     |
  |     +-- [6] Persist artifacts + append agent_notes.md
  |     |
  |     +-- [7] Control flow
  |             Check MISSION COMPLETE / MISSION BLOCKED signals,
  |             pause file, shutdown signal.
  |
  +-- Write final_summary.md
  +-- Return AgentResult
```

### Module map

| Layer | Module | Responsibility |
|:---|:---|:---|
| Entry | `main.py` | CLI parsing, `asyncio.run()` |
| Agent | `harness/agent/agent_loop.py` | Cycle orchestration, system prompt construction, control flow |
| Agent | `harness/agent/agent_git.py` | All git operations: stage, commit, push, tag, diff, hash |
| Agent | `harness/agent/agent_eval.py` | Evaluation orchestration, score formatting, meta-review |
| Agent | `harness/agent/cycle_metrics.py` | Per-cycle metrics collection and persistence |
| Core | `harness/core/config.py` | `HarnessConfig` dataclass with validation |
| Core | `harness/core/llm.py` | Async Anthropic client, retry, conversation pruning, tool-use loop, file-read cache, scratchpad, parallel tool dispatch |
| Core | `harness/core/artifacts.py` | `ArtifactStore` -- hierarchical run/cycle storage with resume support |
| Core | `harness/core/checkpoint.py` | Resume-safe `.done` markers |
| Core | `harness/core/hooks.py` | `VerificationHook` ABC + `SyntaxCheckHook`, `ImportSmokeHook`, `StaticCheckHook`, `PytestHook`, `GitCommitHook` |
| Core | `harness/core/security.py` | Path security: null bytes, control chars, homoglyphs, TOCTOU-safe reads, scope validation |
| Core | `harness/core/project_context.py` | Project metadata injection |
| Core | `harness/core/signal_util.py` | Graceful shutdown signal handlers |
| Evaluation | `harness/evaluation/dual_evaluator.py` | `DualEvaluator` -- two isolated LLM evaluators (basic + diffusion), `DualScore`, `ScoreItem`, score parsing, output validation, calibration anchors |
| Evaluation | `harness/evaluation/static_analysis.py` | Deterministic code checks |
| Evaluation | `harness/evaluation/metrics.py` | Structured metrics collector |
| Prompts | `harness/prompts/dual_evaluator.py` | System prompts for basic (correctness/completeness/specificity/architecture) and diffusion (caller impact/maintenance debt/emergent behaviour/rollback safety) evaluators, plus reasoning-mode variants |
| Prompts | `harness/prompts/agent_meta_review.py` | Meta-review system/user prompt templates |
| Tools | `harness/tools/base.py` | `Tool` ABC, `ToolResult`, `FileSecurity`, atomic validation helpers |
| Tools | `harness/tools/registry.py` | `ToolRegistry` -- dispatch, parameter alias normalization, unknown-param rejection |
| Tools | `harness/tools/__init__.py` | `build_registry()`, `DEFAULT_TOOLS` (37), `OPTIONAL_TOOLS` (5) |
| Tools | `harness/tools/*.py` | 42 individual tools (see below) |
| Deploy | `deploy/harness.service` | systemd user unit |
| Deploy | `deploy/heartbeat.sh` | Cron: restart after failure (every 30 min) |
| Deploy | `deploy/cleanup_runs.sh` | Cron: delete old `run_*` dirs (daily) |
| Deploy | `.github/workflows/deploy.yml` | Tag-triggered CI: smoke test + deploy + rollback |

### Tool catalogue (42 tools)

**Default tools (37):**
- Batch file ops: `batch_read`, `batch_edit`, `batch_write`
- Single file edit: `edit_file`
- Scratchpad: `scratchpad` (in-memory notes surviving conversation pruning)
- Search: `grep_search`, `glob_search`, `feature_search`, `todo_scan`
- Analysis: `symbol_extractor`, `code_analysis`, `cross_reference`, `data_flow`, `call_graph`, `dependency_analyzer`, `project_map`, `file_info`
- Testing: `test_runner`, `lint_check`, `context_budget`
- File/dir ops: `delete_file`, `move_file`, `copy_file`, `list_directory`, `create_directory`, `tree`, `file_patch`, `find_replace`, `diff_files`
- Git: `git_status`, `git_diff`, `git_log`
- Specialized: `python_eval`, `json_transform`, `ast_rename`, `tool_discovery`
- Shell: `bash` (with command denylist)

**Optional tools (5, opt-in via `extra_tools`):**
- `web_search` -- DuckDuckGo search + page fetch (network required)
- `http_request` -- generic HTTP client (network required)
- `git_search` -- git history/blame/grep (high schema cost)
- `read_file` -- single-file variant (superseded by `batch_read`)
- `write_file` -- single-file variant (superseded by `batch_write`)

## Key Decisions

| Date | Decision | Rationale | How to Extend |
|:---|:---|:---|:---|
| -- | Agent-centric model (no pipeline/phases) | A single LLM with full tool access is simpler, more flexible, and avoids inter-phase coordination overhead. The LLM decides what to work on each cycle. | `AgentConfig.mission` steers focus; `AgentConfig.continuous=true` enables standing maintenance without endpoint. |
| -- | All config in JSON, code is generic | Swapping target projects or LLM providers should be a config change, not a code change. JSON configs support `//`-prefix comment keys for documentation. | Add new fields to `HarnessConfig` dataclass; `from_dict()` rejects unknown keys so typos are caught immediately. |
| -- | Verification hooks gate commits | Broken code must never be committed. SyntaxCheck (py_compile), ImportSmoke (subprocess import), and StaticCheck (ruff F821/F811/F401) run after each cycle and block commit on failure. | Subclass `VerificationHook`, set `gates_commit=True`, add to `AgentConfig.cycle_hooks`. See `PytestHook` as a template. |
| -- | `_check_path()` as universal security boundary | Every file-accessing tool validates paths against `allowed_paths` with symlink resolution, null-byte detection, homoglyph detection, and TOCTOU protection. | Set `requires_path_check=True` on new tools and call `self._check_path()` in `execute()`. The `FileSecurity` class provides atomic validate+read/write/delete helpers. |
| -- | Dual evaluator with isolation | Two evaluators (basic: correctness/completeness; diffusion: caller impact/rollback safety) run in parallel, never see each other's output, and scores are combined with 60/40 weighting. Prevents groupthink. | Override `basic_system`/`diffusion_system` params in `DualEvaluator.evaluate()`, or add evaluation modes to `_MODE_HEADERS`. |
| -- | Batch tools as defaults, single-file as optional | `batch_read`/`batch_edit`/`batch_write` replace `read_file`/`edit_file`/`write_file` as defaults. Multi-file changes cost one LLM round-trip instead of N. | Single-file variants available via `extra_tools=["read_file","write_file"]` in config. |
| -- | Conversation pruning (proactive + reactive) | Long tool-use loops can exceed the context window. Proactive compaction replaces old tool results with signal-preserving stubs after 6 turns. Reactive pruning triggers at 300K chars. Tool-specific signal tiers preserve test output longer than search results. | Adjust `_HIGH_SIGNAL_TOOLS`, `_MEDIUM_SIGNAL_TOOLS`, `_LOW_SIGNAL_PRUNE_TOOLS` sets in `llm.py`. |
| -- | File-read cache per tool-use loop | Within a single `call_with_tools()` invocation, repeated reads of the same file at the same offset/limit return cached results. Writes invalidate the cache entry. | Cache is transparent; no tool changes needed. Write-tool names listed in `_WRITE_TOOLS` set in `llm.py`. |
| -- | Read-only tools run in parallel | Tools classified as `_READ_ONLY_TOOL_NAMES` in `llm.py` are dispatched concurrently via `asyncio.gather`. Mutating tools run sequentially. | Add tool names to `_READ_ONLY_TOOL_NAMES` for new read-only tools. |
| -- | Scratchpad survives conversation pruning | The `scratchpad` tool saves notes into a list that is re-injected into the system prompt every turn, surviving pruning. Capped at 30 entries. | The scratchpad is handled inline in `call_with_tools()`, not via the tool registry. |
| -- | Self-improvement via push-tag-deploy-restart | Python imports are loaded once at startup. When the harness modifies its own code, a process restart is needed. The cycle: commit -> push -> tag -> GitHub Actions deploys -> systemd restarts. | Configure `auto_push`, `auto_tag_interval`, `auto_tag_prefix` in `AgentConfig`. CI workflow at `.github/workflows/deploy.yml`. |
| -- | Periodic meta-review | Every N committed cycles (default 5), a separate LLM call analyses score trends + git history and produces strategic direction guidance injected into subsequent system prompts. | Set `AgentConfig.meta_review_interval`. Templates in `harness/prompts/agent_meta_review.py`. |
| -- | `HARNESS_BASE_URL` / `HARNESS_API_KEY` env vars | Avoids conflicting with Claude Code's `ANTHROPIC_BASE_URL` proxy (127.0.0.1:9099). Config fields take priority over env vars. | Set in config JSON or as env vars. DeepSeek uses `/anthropic` endpoint (not `/v1`). |
| -- | Pause file for graceful pause/resume | Touch `.harness.pause` in workspace to pause after current cycle; remove to resume. Polls every 30 seconds, honours shutdown signals while paused. | `AgentConfig.pause_file` and `pause_poll_interval`. |
| -- | Exit code 2 for zero-work catastrophe | When agent completes cycles but makes zero tool calls, exit code 2 signals systemd to treat this as a failure (triggers heartbeat restart). | Check `AgentResult.total_tool_calls` in `main.py`. |

## Extension Guide

### Adding a new tool

1. Create `harness/tools/my_tool.py`:

```python
from harness.tools.base import Tool, ToolResult
from harness.core.config import HarnessConfig
from typing import Any

class MyTool(Tool):
    name = "my_tool"
    description = "Brief description for the LLM schema"
    requires_path_check = True  # if accessing filesystem
    tags = frozenset({"analysis"})  # optional categorization

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "..."},
            },
            "required": ["path"],
        }

    async def execute(self, config: HarnessConfig, **params: Any) -> ToolResult:
        path = params["path"]
        if self.requires_path_check:
            resolved = self._check_path(config, path, require_exists=True)
            if isinstance(resolved, ToolResult):
                return resolved  # security error
        # ... tool logic ...
        return ToolResult(output="result text")
```

2. Register in `harness/tools/__init__.py`:
   - Import: `from harness.tools.my_tool import MyTool`
   - Add `MyTool()` to `DEFAULT_TOOLS` (always available) or `OPTIONAL_TOOLS` (opt-in via `extra_tools`)

3. If the tool is read-only (no side effects), add its name to `_READ_ONLY_TOOL_NAMES` in `harness/core/llm.py` for parallel execution.

4. If the tool writes files, add its name to `_WRITE_TOOLS` in `harness/core/llm.py` for cache invalidation.

### Adding a new verification hook

1. Subclass `VerificationHook` in `harness/core/hooks.py`:

```python
class MyHook(VerificationHook):
    name = "my_hook"
    gates_commit = True  # True = failure blocks commit

    async def run(self, config: HarnessConfig, context: dict[str, Any]) -> HookResult:
        changed_files = context.get("files_changed", [])
        # ... verification logic ...
        return HookResult(passed=True, output="OK")
```

2. Add the hook name to `AgentLoop._build_hooks()` in `agent_loop.py`.

3. Enable in config: `"cycle_hooks": ["syntax", "static", "import_smoke", "my_hook"]`.

### Adding a new evaluation mode

1. Define mode header in `_MODE_HEADERS` dict in `harness/evaluation/dual_evaluator.py`.

2. Optionally add dedicated system prompts in `harness/prompts/dual_evaluator.py` (e.g. `REASONING_BASIC_SYSTEM`).

3. Pass the mode string to `DualEvaluator.evaluate(mode="my_mode")`.

### Switching LLM providers

Set `base_url` and `api_key` in the config JSON's `harness` section:

```json
{
  "harness": {
    "model": "deepseek-chat",
    "base_url": "https://api.deepseek.com/anthropic",
    "api_key": "your-key"
  }
}
```

Env var fallbacks: `HARNESS_BASE_URL`, `HARNESS_API_KEY`. Any provider supporting the Anthropic API message format works.

### Deploying as a service

1. Copy `deploy/harness.service` to `~/.config/systemd/user/`.
2. Set API key in `~/.config/harness/env`.
3. Enable with `systemctl --user enable harness.service`.
4. Install crons for `deploy/heartbeat.sh` (every 30 min) and `deploy/cleanup_runs.sh` (daily at 04:00).
5. For self-improvement loop: enable `auto_push`, `auto_tag_interval`, and the `.github/workflows/deploy.yml` workflow.
