# Intelligence Loop — Phase 1 (Evaluator Discrimination)

**Status**: deployed 2026-04-20, ρ baseline = 0.87 (server) / 0.87 (local)
**Commit**: `13106fb feat(intel): evaluator-discrimination probe — first goal-driven loop`
**Plan reference**: `~/.claude/plans/dazzling-crafting-pancake.md`

---

## Why this exists

Analysis of 487 self-improvement commits over 3.5 days showed the harness is
**Goodharting its own reward**:

| Signal | Number | What it means |
|---|---|---|
| Peripheral-file edits vs intelligence-core edits | 583 : 71 | LLM avoids the modules that actually make the harness smart |
| `planner.py` edits in 487 commits | 1 | Planning logic untouched |
| `synthesis` edits | 4 | Synthesis logic untouched |
| `memory.py` edits | 2 | Memory logic untouched |
| Avg score trend over 3.5 days | 12.9 → 14.2 | ≈ noise |
| Tunnel-vision cycles | `cross_reference.py` (76) → `file_read.py` (35) | Same file beaten for days |

**Root cause**: a single proposal cannot reveal whether the evaluator is
better at discriminating quality — that requires a benchmark run over many
samples. With no such signal in context, the LLM rationally drifts toward
changes it *can* see working (security, tests, refactors).

---

## What was built (this commit)

The first of four planned "intelligence sub-loops". Pattern:

```
        ┌─────────┐    ┌──────────┐    ┌──────────┐    ┌────────────┐
        │  Goal   │ →  │  Probe   │ →  │  Driver  │ →  │ Anti-game  │
        └─────────┘    └──────────┘    └──────────┘    └────────────┘
        one number    deterministic    inject into     read-only +
                       benchmark        next prompt    hash pin
```

### 1. Goal — Spearman ρ on a 20-proposal benchmark

20 synthetic proposals, 5 tiers × 4 samples, ranked 1-20:

| Tier | Ranks | What they look like |
|---|---|---|
| Exemplar | 1-4 | Cite real `FILE::function`, complete ANALYSIS, SCORE line, calibration anchors |
| Good | 5-8 | Mostly correct, one section missing |
| Middling | 9-12 | Vague, no specific symbols |
| Poor | 13-16 | Generic advice, hallucinated function names |
| Disaster | 17-20 | Off-topic, violate SELF-IMPROVEMENT LOOP PROTECTION, empty |

Higher ρ = evaluator's ranking matches ground truth = better discrimination.
Target: ρ ≥ 0.85 (Phase 1 ceiling, see "What baseline tells us").

### 2. Probe — `benchmarks/evaluator_calibration/run_probe.py`

Standalone subprocess. Loads the 20 proposals, feeds each through
`DualEvaluator.evaluate()` in parallel (asyncio.gather), parses the SCORE
line, computes Spearman ρ in pure stdlib (~15 lines, tie-aware).

Cost: 40 LLM calls per probe (20 × 2 evaluators), ~22s, ~$0.05.

Output: appends one JSONL line per run to `probe_results.jsonl` with
`{rho, rho_basic, rho_diffusion, n, elapsed_s, timestamp}`.

### 3. Driver — `$intel_metric_block` template variable

After each outer round, `pipeline_loop._run_outer_round` calls
`intel_metrics.run_eval_probe()` and stashes the result on the loop
instance. On the next round, `phase_runner._build_executor_prompt`
substitutes `$intel_metric_block` with:

```
## INTELLIGENCE METRIC — evaluator discrimination (Spearman ρ on 20-proposal benchmark)

Round trajectory: [0.85, 0.86, 0.87]  (higher is better; target ≥ 0.85)
Current: 0.87    Δ vs prev: +0.01    Regressions in last 5: 0

Your framework_improvement proposal SHOULD target evaluator quality —
modifying one of:
  • harness/evaluation/dual_evaluator.py
  • harness/prompts/dual_evaluator.py
  • harness/prompts/evaluator.py
If your proposal does NOT touch these files AND ρ (currently 0.87) is
below 0.85, you lose 3 points on Architecture Fit. The probe runs
automatically at round end — you will see the new ρ next round.
```

This block appears **only in the framework_improvement phase prompt**
(per phase-name check). Other phases see no extra signal.

The result is also surfaced in `summary.json["intel_metrics"]` for
external monitoring tools.

### 4. Anti-game

Three layers:

| Mechanism | Where | What it stops |
|---|---|---|
| `allowed_edit_globs` exclusion | All 4 phases — `benchmarks/**` not listed | `EditFileTool`/`WriteFileTool`/`FilePatchTool` reject writes via `Tool._check_phase_scope` |
| SHA-256 pin | `benchmarks/evaluator_calibration/.sha256` | `intel_metrics.verify_hash` skips probe + logs WARNING if `proposals.jsonl` or `run_probe.py` mutates |
| Glob-pattern exclusion | `glob_patterns` in config: `harness/**/*.py`, `tests/**/*.py`, `main.py` | LLM never sees `proposals.jsonl` in `$file_context`, so it can't memorise the benchmark |

Verified locally:
- `update_hash()` writes baseline pin → `verify_hash()` returns `(True, "hash matches")`
- After tampering proposals.jsonl with `\n` → `verify_hash()` returns `(False, "hash mismatch: on-disk=2e76… pinned=191a…")`
- After restore → `(True, "hash matches")`
- Edit attempts to `benchmarks/...` from EditFileTool with proper `phase_edit_globs` are rejected with `PHASE SCOPE ERROR` (verified earlier in same session, see `2313083` precedent)

---

## What baseline tells us (the punchline)

```
Local  baseline: ρ_combined=0.8733  ρ_basic=0.7928  ρ_diffusion=0.7492  n=20  22s
Server baseline: ρ_combined=0.8556  ρ_basic=0.8599  ρ_diffusion=0.7756  n=20  21s
```

**ρ ≈ 0.87 already meets the Phase 1 target of 0.85.** This is itself the
most important finding:

1. **The evaluator's tier-level discrimination is already strong.** It
   reliably distinguishes Exemplar from Disaster, Good from Middling. The
   "you can't tell good from bad code" failure mode does not apply to the
   current evaluator — at least not at coarse granularity.
2. **The remaining headroom is intra-tier** — telling apart two Exemplar
   proposals where one is slightly more specific than the other. Synthetic
   proposals can't measure this; you'd need 20 real proposals with hand-
   curated 1-20 ordering.
3. **So Phase 1 will saturate fast.** Expect ρ to drift to 0.92-0.95 as
   the LLM polishes calibration anchor checks, then plateau because the
   benchmark can't reward finer distinctions.

This was anticipated by the plan ("Phase 1 may reveal the evaluator is not
the bottleneck"). Phase 1 still ships because:
- It proves the loop machinery (probe, driver, anti-game) works
- It provides a per-round trajectory the LLM can react to
- It guards against *regression* — if ρ drops by 0.05+, the LLM sees it
  next round
- The next sub-loop (planner specificity, synthesis novelty, or memory
  utility) can copy this scaffolding wholesale

---

## Files added/changed

| File | Change |
|---|---|
| `benchmarks/evaluator_calibration/proposals.jsonl` | NEW — 20 proposals × 5 tiers |
| `benchmarks/evaluator_calibration/run_probe.py` | NEW — standalone probe with hand-rolled Spearman |
| `benchmarks/evaluator_calibration/.sha256` | NEW — anti-tamper hash pin |
| `harness/pipeline/intel_metrics.py` | NEW — probe orchestrator + format_trajectory |
| `harness/pipeline/pipeline_loop.py` | call probe after each round; surface in summary.json |
| `harness/pipeline/phase_runner.py` | inject `$intel_metric_block` for framework_improvement only |
| `config/pipeline_example_self_improve_server.json` | framework_improvement prompt reads `$intel_metric_block`, benchmarks/ added to LOOP PROTECTION list |
| `main.py` | thread pipeline_config_path through to probe |
| `.gitignore` | exclude probe_results.jsonl runtime artifact |

---

## What to watch for next

After 3-5 rounds run on the server, check:

```bash
ssh ubuntu@43.128.100.101 'cat /home/ubuntu/harness-everything/benchmarks/evaluator_calibration/probe_results.jsonl'
```

You should see one line per round, ρ ideally drifting upward. If ρ
oscillates or drops, the LLM is making evaluator changes that look
plausible but actually hurt discrimination — that itself is a signal.

Then check what framework_improvement is editing:

```bash
ssh ubuntu@43.128.100.101 'cd /home/ubuntu/harness-everything && \
  git log --pretty=format:"%h %s" | grep "framework_improvement" | head -10'
```

If the harness now consistently touches `harness/evaluation/dual_evaluator.py`
or `harness/prompts/dual_evaluator.py` rather than `file_read.py`, the
driver is working — the prompt block has redirected attention.

If after 5 rounds it's *still* editing `file_read.py`, the driver is too
weak; we'd raise the penalty in the prompt block (-3 → -5 on Architecture
Fit) or add a hard "no commit unless one of these files touched" rule.

---

## What this plan deliberately does NOT do

- **No planner / synthesis / memory probes yet.** Each is a separate Phase
  (2, 3, 4). The infrastructure (`benchmarks/`, `intel_metrics.py`, the
  `$intel_metric_block` substitution) is reusable for all.
- **No commit-blocking ρ gate.** Phase 1 is observe-only. Hard gates risk
  freezing the loop on a single bad round.
- **No fix for non-intelligence-phase tunnel vision.** That is a separate
  concern (file-cooldown, edit-glob narrowing). This plan is strictly
  "did ρ rise".
- **No BASIC_SYSTEM evaluator-side enforcement.** Adding "if proposal is
  for framework_improvement and doesn't touch evaluator code, deduct
  points" requires per-phase eval prompts — bigger refactor. The
  implementer-side pressure (its own prompt + the metric block) is
  enough for Phase 1; revisit if behavior doesn't shift.

---

## Cost / risk ledger

| Cost | Estimate | Mitigation |
|---|---|---|
| Probe LLM calls | 40/round × ~5s = 200s wall + ~$0.05/round | Subprocess timeout 360s; treats failure as "skip this round's metric" not "crash pipeline" |
| Probe latency added per round | +20-25s | Acceptable; rounds already take 1000-1700s |
| Benchmark drift over time | Real proposals would change as harness changes | Plan v2: rotate to memory-curated proposals once Phase 1 saturates |
| Evaluator overfitting benchmark | LLM could write rules that score these 20 proposals high but generalize poorly | Anti-game (read-only + glob exclusion) means LLM cannot see proposals.jsonl content via `$file_context` — only tier-level patterns it can infer from its OWN rubric |
| Service downtime during deploy | Minimal — service was already inactive (natural early-stop), restarted with new code | Done; service active as of 2026-04-20 08:18 CST |

---

## Decision log (for future-me)

- **Synthetic over real proposals** — chose 20 synthetic to avoid 30 min
  of hand-labelling. Trade-off: harder to measure intra-tier; will need
  v2 with real proposals once ρ saturates.
- **Stdlib Spearman over scipy** — keeps zero runtime deps. ~15 lines.
- **Observe-only first, gate later** — explicit non-goal in plan.
- **Inject only into framework_improvement** — other phases would just be
  noise; framework_improvement's PRIORITY ORDER #1 is exactly evaluator
  improvements.
- **Skip per-phase BASIC_SYSTEM mod for Phase 1** — implementer-side
  pressure should be enough; revisit if data shows otherwise.
- **Hash-pin both proposals AND probe code** — pinning only data lets the
  LLM tamper with `run_probe.py` to inflate ρ. Pinning both closes that.
- **Treat probe failure as skip, not crash** — LLM output is non-deterministic;
  one timeout shouldn't tank the round.
