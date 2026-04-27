# Technical Architecture

## Philosophy

Harness-Everything is an autonomous coding agent that improves codebases through iterative cycles. The design prioritizes:

- **Single-agent simplicity**: One LLM with full tool access, not a multi-agent pipeline
- **Crash safety**: Every cycle is independently resumable; no work is lost on interruption
- **Self-improvement safety**: The agent can modify its own source code, so verification hooks gate every commit
- **Provider agnosticism**: Any Anthropic-compatible API works (Claude, DeepSeek, etc.)

## Architecture Overview

The system runs in a loop: execute a cycle, verify the output, evaluate quality, commit if safe, then decide whether to continue. Each cycle gets a fresh system prompt with accumulated context (notes, scores, project state). The evaluation system uses two independent LLM evaluators that never see each other's output, producing a weighted composite score.

Tools provide the agent's interface to the filesystem, shell, and codebase analysis. All file operations pass through a security layer that confines access to the configured workspace. Deployment is handled by systemd + GitHub Actions, with automatic rollback on failed smoke tests.

## Key Decisions

| Date | Decision | Rationale | How to Extend |
|:---|:---|:---|:---|
| 2025 | Single agent loop over multi-agent pipeline | Simpler to debug, cheaper to run, easier to resume | Add phases within the cycle, not additional agents |
| 2025 | Dual isolated evaluation | Prevents groupthink; basic measures quality, diffusion measures safety | Add a third evaluator by extending the scoring combination |
| 2025 | Selective file staging over `git add -A` | Avoids committing unintended files (temp files, logs) | Changed paths are tracked per-cycle and staged explicitly |
| 2025 | Batch tools as primary, single-file as secondary | Reduces LLM round-trips; one call reads 50 files | New tools should prefer batch interfaces |
| 2025 | Security via path resolution + O_NOFOLLOW + post-open validation | Defense in depth against symlink/traversal attacks | Add checks to the validation pipeline, don't bypass it |
| 2025 | Tool output compaction with signal-aware tiers | Preserves diagnostic value while fitting context window | Assign new tools to the appropriate signal tier |

## Extension Guide

- **Adding a tool**: Subclass the tool base, implement the schema and execute method, register in the tool list. If it touches files, enable path checking.
- **Adding an evaluation dimension**: Update the evaluator prompt template; the scoring/parsing infrastructure handles arbitrary dimensions.
- **Adding a verification hook**: Subclass the hook base, implement the run method, add to the hook list. Set the gating flag if failure should block commits.
- **Changing LLM provider**: Set the base URL and API key in the config JSON. No code changes needed.
