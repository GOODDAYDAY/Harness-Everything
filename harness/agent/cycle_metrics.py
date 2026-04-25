"""Cycle-level metrics for the autonomous agent loop.

Computes, persists, and formats per-cycle quality metrics across seven axes:

  1. **Tool Efficiency** — how productive were the tool calls?
  2. **Output Quality** — how many files changed, turns per change.
  3. **Execution Health** — hooks, timing, output volume.
  4. **Redundancy** — repeated reads of the same file.
  5. **Behaviour Signals** — bash reliance, test/lint discipline.
  6. **Context Quality** — did the agent read files it actually edited?
  7. **Memory & Learning** — notes consultation, plan-before-act, test-after-edit.

Design principles:
  * Pure-function collectors: each ``_compute_*`` takes raw data, returns a
    metrics dict.  No side effects, easy to test.
  * Single integration point: ``collect_cycle_metrics()`` is the only function
    the agent loop calls.  It returns a ``CycleMetrics`` dataclass that can
    be serialised to JSON, formatted as a markdown report, or condensed to a
    one-line summary.
  * No import of ``agent_loop`` — avoids circular deps.  The loop passes
    raw data (exec_log, changed_paths, etc.) into the public API.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Read-only / analysis tools — calls that gather context but don't mutate.
# ---------------------------------------------------------------------------
_READ_TOOLS: frozenset[str] = frozenset({
    "batch_read", "read_file", "grep_search", "glob_search",
    "list_directory", "tree", "symbol_extractor", "code_analysis",
    "cross_reference", "feature_search", "project_map", "file_info",
    "call_graph", "data_flow", "dependency_analyzer", "diff_files",
    "context_budget", "tool_discovery", "todo_scan", "git_status",
    "git_diff", "git_log", "git_search",
})

# Tools that mutate files on disk.
_WRITE_TOOLS: frozenset[str] = frozenset({
    "batch_edit", "batch_write", "edit_file", "write_file",
    "file_patch", "find_replace", "delete_file", "move_file",
    "copy_file", "ast_rename",
})


# ---- dataclass -----------------------------------------------------------

@dataclass
class CycleMetrics:
    """All computed metrics for a single agent cycle."""

    cycle: int  # 1-indexed

    # -- Axis 1: Tool efficiency --
    total_tool_calls: int = 0
    error_tool_calls: int = 0
    first_try_success_rate: float = 0.0  # fraction of non-error calls
    read_calls: int = 0
    write_calls: int = 0
    bash_calls: int = 0
    read_write_ratio: float = 0.0
    bash_fraction: float = 0.0
    unique_tools_used: int = 0
    tool_distribution: dict[str, int] = field(default_factory=dict)

    # -- Axis 2: Output quality --
    files_changed: int = 0
    turns_per_change: float = 0.0  # total_tool_calls / files_changed

    # -- Axis 3: Execution health --
    hooks_passed: bool = True
    hook_failure_count: int = 0
    elapsed_s: float = 0.0
    avg_tool_duration_ms: float = 0.0
    total_output_chars: int = 0

    # -- Axis 4: Redundancy --
    redundant_reads: int = 0  # re-reads of the same path
    redundant_read_rate: float = 0.0

    # -- Axis 5: Behaviour signals --
    scratchpad_calls: int = 0
    test_runner_calls: int = 0
    lint_calls: int = 0

    # -- Axis 6: Context quality --
    context_files_read: int = 0   # unique files explicitly read
    context_files_used: int = 0   # read files that were later edited
    context_hit_rate: float = 0.0   # used / read — targeting accuracy
    context_waste_rate: float = 0.0  # 1 - hit_rate — wasted reads

    # -- Axis 7: Memory & learning --
    notes_consulted: bool = False    # agent read agent_notes.md
    plan_before_act: bool = False    # reads/search came before first write
    test_after_edit: bool = False    # test_runner ran after the last edit
    edit_test_cycles: int = 0        # number of edit→test iteration loops


# ---- pure metric collectors -----------------------------------------------

def _compute_tool_efficiency(exec_log: list[dict[str, Any]]) -> dict[str, Any]:
    """Axis 1 + distribution: tool call statistics."""
    total = len(exec_log)
    errors = sum(1 for e in exec_log if e.get("is_error"))
    reads = sum(1 for e in exec_log if e.get("tool") in _READ_TOOLS)
    writes = sum(1 for e in exec_log if e.get("tool") in _WRITE_TOOLS)
    bash = sum(1 for e in exec_log if e.get("tool") == "bash")
    scratchpad = sum(1 for e in exec_log if e.get("tool") == "scratchpad")
    test_runner = sum(1 for e in exec_log if e.get("tool") == "test_runner")
    lint = sum(1 for e in exec_log if e.get("tool") == "lint_check")

    dist: dict[str, int] = {}
    for e in exec_log:
        name = e.get("tool", "?")
        dist[name] = dist.get(name, 0) + 1

    durations = [e.get("duration_ms", 0) for e in exec_log if "duration_ms" in e]

    return {
        "total_tool_calls": total,
        "error_tool_calls": errors,
        "first_try_success_rate": (total - errors) / total if total else 0.0,
        "read_calls": reads,
        "write_calls": writes,
        "bash_calls": bash,
        "read_write_ratio": reads / writes if writes else 0.0,
        "bash_fraction": bash / total if total else 0.0,
        "unique_tools_used": len(dist),
        "tool_distribution": dict(sorted(dist.items(), key=lambda kv: -kv[1])),
        "scratchpad_calls": scratchpad,
        "test_runner_calls": test_runner,
        "lint_calls": lint,
        "avg_tool_duration_ms": sum(durations) / len(durations) if durations else 0.0,
        "total_output_chars": sum(len(e.get("output", "")) for e in exec_log),
    }


def _compute_change_efficiency(
    exec_log: list[dict[str, Any]],
    changed_paths: list[str],
) -> dict[str, Any]:
    """Axis 2: how many tool calls per actual file change."""
    n_files = len(changed_paths)
    total = len(exec_log)
    return {
        "files_changed": n_files,
        "turns_per_change": total / n_files if n_files else 0.0,
    }


def _compute_redundancy(exec_log: list[dict[str, Any]]) -> dict[str, Any]:
    """Axis 4: detect repeated reads of the same path."""
    read_paths: list[str] = []
    for e in exec_log:
        tool = e.get("tool", "")
        inp = e.get("input") or {}
        if tool == "batch_read":
            for fp in inp.get("paths", []):
                if isinstance(fp, str):
                    read_paths.append(fp)
                elif isinstance(fp, dict):
                    read_paths.append(fp.get("path", ""))
        elif tool == "read_file":
            p = inp.get("path", "")
            if p:
                read_paths.append(p)

    seen: set[str] = set()
    redundant = 0
    for p in read_paths:
        if p in seen:
            redundant += 1
        seen.add(p)

    total_reads = len(read_paths)
    return {
        "redundant_reads": redundant,
        "redundant_read_rate": redundant / total_reads if total_reads else 0.0,
    }


def _compute_context_quality(
    exec_log: list[dict[str, Any]],
    changed_paths: list[str],
) -> dict[str, Any]:
    """Axis 6: did the agent read the files it ended up editing?

    ``context_hit_rate`` = (files read AND edited) / (files read).
    A high hit rate means the agent targeted its reading well; a low rate
    means it read many files it never acted on (wasted context window).
    """
    read_set: set[str] = set()
    for e in exec_log:
        tool = e.get("tool", "")
        inp = e.get("input") or {}
        if tool == "batch_read":
            for fp in inp.get("paths", []):
                if isinstance(fp, str):
                    read_set.add(fp)
                elif isinstance(fp, dict):
                    p = fp.get("path", "")
                    if p:
                        read_set.add(p)
        elif tool == "read_file":
            p = inp.get("path", "")
            if p:
                read_set.add(p)

    changed_set = set(changed_paths)
    used = read_set & changed_set
    total_read = len(read_set)
    hit = len(used) / total_read if total_read else 0.0

    return {
        "context_files_read": total_read,
        "context_files_used": len(used),
        "context_hit_rate": hit,
        "context_waste_rate": 1.0 - hit if total_read else 0.0,
    }


def _compute_memory_learning(exec_log: list[dict[str, Any]]) -> dict[str, Any]:
    """Axis 7: workflow discipline and memory usage.

    Checks three behavioural patterns:
    * **notes_consulted** — did the agent read ``agent_notes.md`` (cross-cycle
      memory) at any point?
    * **plan_before_act** — were there only read/search/scratchpad calls before
      the first file-mutating call?  ``True`` means the agent gathered context
      before jumping to edits.
    * **test_after_edit** — did a ``test_runner`` call happen after the last
      file-mutating call?  ``True`` means the agent verified its work.
    * **edit_test_cycles** — how many edit→test iteration pairs occurred?
      More cycles = more iterative refinement.
    """
    notes_consulted = False
    first_write_idx: int | None = None
    last_write_idx: int | None = None
    has_test_after_last_write = False
    edit_test_cycles = 0
    in_edit_phase = False

    for i, e in enumerate(exec_log):
        tool = e.get("tool", "")
        inp = e.get("input") or {}

        # Check notes consultation
        if not notes_consulted:
            if tool == "batch_read":
                for fp in inp.get("paths", []):
                    p = fp if isinstance(fp, str) else fp.get("path", "")
                    if "agent_notes" in p:
                        notes_consulted = True
                        break
            elif tool == "read_file":
                if "agent_notes" in inp.get("path", ""):
                    notes_consulted = True

        # Track write positions
        if tool in _WRITE_TOOLS:
            if first_write_idx is None:
                first_write_idx = i
            last_write_idx = i
            in_edit_phase = True

        # Track edit→test cycles
        if tool == "test_runner" and in_edit_phase:
            edit_test_cycles += 1
            in_edit_phase = False

    # plan_before_act: everything before first write is read-only/search/scratchpad
    plan_before_act = False
    if first_write_idx is not None and first_write_idx > 0:
        plan_before_act = True
        for e in exec_log[:first_write_idx]:
            t = e.get("tool", "")
            if t not in _READ_TOOLS and t != "scratchpad" and t != "context_budget":
                plan_before_act = False
                break
    elif first_write_idx is None:
        # No writes at all — vacuously true (agent only explored)
        plan_before_act = len(exec_log) > 0

    # test_after_edit: test_runner appeared after last write
    if last_write_idx is not None:
        for e in exec_log[last_write_idx + 1:]:
            if e.get("tool") == "test_runner":
                has_test_after_last_write = True
                break

    return {
        "notes_consulted": notes_consulted,
        "plan_before_act": plan_before_act,
        "test_after_edit": has_test_after_last_write,
        "edit_test_cycles": edit_test_cycles,
    }


# ---- public API -----------------------------------------------------------

def collect_cycle_metrics(
    cycle: int,
    exec_log: list[dict[str, Any]],
    changed_paths: list[str],
    hook_failures: list[str],
    elapsed_s: float,
) -> CycleMetrics:
    """Compute all metrics for a single cycle from raw data.

    This is the only function ``AgentLoop.run()`` needs to call.
    All inputs come directly from the agent loop — no file I/O here.
    """
    eff = _compute_tool_efficiency(exec_log)
    chg = _compute_change_efficiency(exec_log, changed_paths)
    red = _compute_redundancy(exec_log)
    ctx = _compute_context_quality(exec_log, changed_paths)
    mem = _compute_memory_learning(exec_log)

    return CycleMetrics(
        cycle=cycle,
        # axis 1
        total_tool_calls=eff["total_tool_calls"],
        error_tool_calls=eff["error_tool_calls"],
        first_try_success_rate=round(eff["first_try_success_rate"], 4),
        read_calls=eff["read_calls"],
        write_calls=eff["write_calls"],
        bash_calls=eff["bash_calls"],
        read_write_ratio=round(eff["read_write_ratio"], 2),
        bash_fraction=round(eff["bash_fraction"], 4),
        unique_tools_used=eff["unique_tools_used"],
        tool_distribution=eff["tool_distribution"],
        scratchpad_calls=eff["scratchpad_calls"],
        test_runner_calls=eff["test_runner_calls"],
        lint_calls=eff["lint_calls"],
        # axis 2
        files_changed=chg["files_changed"],
        turns_per_change=round(chg["turns_per_change"], 2),
        # axis 3
        hooks_passed=len(hook_failures) == 0,
        hook_failure_count=len(hook_failures),
        elapsed_s=round(elapsed_s, 1),
        avg_tool_duration_ms=round(eff["avg_tool_duration_ms"], 0),
        total_output_chars=eff["total_output_chars"],
        # axis 4
        redundant_reads=red["redundant_reads"],
        redundant_read_rate=round(red["redundant_read_rate"], 4),
        # axis 6
        context_files_read=ctx["context_files_read"],
        context_files_used=ctx["context_files_used"],
        context_hit_rate=round(ctx["context_hit_rate"], 4),
        context_waste_rate=round(ctx["context_waste_rate"], 4),
        # axis 7
        notes_consulted=mem["notes_consulted"],
        plan_before_act=mem["plan_before_act"],
        test_after_edit=mem["test_after_edit"],
        edit_test_cycles=mem["edit_test_cycles"],
    )


# ---- serialisation --------------------------------------------------------

def metrics_to_dict(m: CycleMetrics) -> dict[str, Any]:
    """Serialise to a JSON-safe dict."""
    return asdict(m)


# ---- report formatting ----------------------------------------------------

def format_detailed_report(m: CycleMetrics) -> str:
    """Markdown report for ``cycle_N/metrics_report.md``."""
    lines = [
        f"# Cycle {m.cycle} — Metrics Report",
        "",
        "## Tool Efficiency",
        f"- Total tool calls: **{m.total_tool_calls}**",
        f"- Errors: **{m.error_tool_calls}** "
        f"(first-try success: {m.first_try_success_rate:.1%})",
        f"- Read calls: {m.read_calls}  |  Write calls: {m.write_calls}  "
        f"| R/W ratio: {m.read_write_ratio:.1f}",
        f"- Bash calls: {m.bash_calls} ({m.bash_fraction:.1%} of total)",
        f"- Unique tools: {m.unique_tools_used}",
        "",
        "### Tool Distribution",
    ]
    for name, count in m.tool_distribution.items():
        pct = count / m.total_tool_calls * 100 if m.total_tool_calls else 0
        bar = "#" * max(1, round(pct / 2))
        lines.append(f"  {name:<25s} {count:>3d}  {bar} {pct:.0f}%")
    lines += [
        "",
        "## Output Quality",
        f"- Files changed: **{m.files_changed}**",
        f"- Turns per change: **{m.turns_per_change:.1f}**",
        "",
        "## Execution Health",
        f"- Hooks passed: {'yes' if m.hooks_passed else 'NO — ' + str(m.hook_failure_count) + ' failure(s)'}",
        f"- Elapsed: {m.elapsed_s:.1f}s",
        f"- Avg tool duration: {m.avg_tool_duration_ms:.0f}ms",
        f"- Total output chars: {m.total_output_chars:,}",
        "",
        "## Redundancy",
        f"- Redundant reads: {m.redundant_reads} "
        f"(rate: {m.redundant_read_rate:.1%})",
        "",
        "## Behaviour Signals",
        f"- Scratchpad usage: {m.scratchpad_calls}",
        f"- Test runner calls: {m.test_runner_calls}",
        f"- Lint calls: {m.lint_calls}",
        "",
        "## Context Quality",
        f"- Files read: {m.context_files_read}",
        f"- Files read AND edited: {m.context_files_used}",
        f"- Hit rate: **{m.context_hit_rate:.1%}** "
        f"(waste: {m.context_waste_rate:.1%})",
        "",
        "## Memory & Learning",
        f"- Notes consulted: {'yes' if m.notes_consulted else 'no'}",
        f"- Plan before act: {'yes' if m.plan_before_act else 'NO — wrote before reading'}",
        f"- Test after edit: {'yes' if m.test_after_edit else 'NO — no final verification'}",
        f"- Edit-test cycles: {m.edit_test_cycles}",
    ]
    return "\n".join(lines) + "\n"


def format_summary(m: CycleMetrics) -> str:
    """One-line summary for appending to ``agent_notes.md``."""
    status = "PASS" if m.hooks_passed else "FAIL"
    notes = "Y" if m.notes_consulted else "N"
    plan = "Y" if m.plan_before_act else "N"
    test = "Y" if m.test_after_edit else "N"
    return (
        f"[metrics] cycle={m.cycle} tools={m.total_tool_calls} "
        f"err={m.error_tool_calls} success={m.first_try_success_rate:.0%} "
        f"files={m.files_changed} turns/chg={m.turns_per_change:.1f} "
        f"bash={m.bash_fraction:.0%} redundant={m.redundant_reads} "
        f"ctx_hit={m.context_hit_rate:.0%} "
        f"notes={notes} plan={plan} test={test} "
        f"hooks={status} elapsed={m.elapsed_s:.0f}s"
    )


# ---- persistence ----------------------------------------------------------

def persist_cycle_metrics(
    metrics: CycleMetrics,
    artifacts_write: Any,
    cycle_segment: str,
) -> None:
    """Write metrics JSON and markdown report to cycle artifacts.

    ``artifacts_write`` is ``ArtifactStore.write`` — passed as a callable to
    avoid importing ArtifactStore (keeps this module dependency-free).
    """
    artifacts_write(
        json.dumps(metrics_to_dict(metrics), indent=2),
        cycle_segment, "metrics.json",
    )
    artifacts_write(
        format_detailed_report(metrics),
        cycle_segment, "metrics_report.md",
    )
