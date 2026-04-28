# Harness Runbook — Investigating the Live Pipeline

This document is the standing operating procedure for any session that needs to
diagnose, repair, or extend the production self-improvement loop. Read it
top-to-bottom on a cold start; you should be able to assess "is the system
healthy and what should I do next" within five minutes.

---

## 0. The 30-second status check

Run this block first. Every diagnosis flows from these answers.

```bash
ssh ubuntu@43.128.100.101 "
  echo '=== service ==='
  systemctl --user is-active harness
  ps -p \$(systemctl --user show -p MainPID --value harness) -o pid,etime,cmd 2>/dev/null

  echo '=== HEAD vs origin ==='
  cd /home/ubuntu/harness-everything
  git log --oneline -3
  git rev-list --left-right --count HEAD...origin/main 2>/dev/null

  echo '=== latest run dir + memory tail ==='
  ls -t harness_output/run_* 2>/dev/null | head -1
  tail -3 \$(ls -t harness_output/run_*/memory.jsonl 2>/dev/null | head -1)

  echo '=== last 12 log lines ==='
  tail -12 \$(ls -t logs/*.log 2>/dev/null | head -1)
"
```

**Decode:**

- `is-active` → expected `active`. `inactive` = stopped on purpose. `failed` =
  crashed and given up. `activating` = systemd is mid-restart loop (something
  is crashing repeatedly).
- `etime` shorter than time-since-last-tag = service was just restarted by a
  deploy.
- `git rev-list` left/right counts: `0\t0` means in sync. `0\tN` means remote
  ahead (auto-pushes since you last pulled).
- `memory.jsonl` last line tells you what phase just completed.

---

## 1. Architecture — only what you need to debug

```
GitHub main ──tag harness-r* push──▶ .github/workflows/deploy.yml
                                      │ smoke (py_compile + config load)
                                      │ if pass: advance harness-last-good
                                      │ if fail: rollback to harness-last-good
                                      ▼
ubuntu@43.128.100.101:/home/ubuntu/harness-everything
  ├── harness/                       ← source (gets reset --hard to tag)
  ├── pipeline_server.json           ← gitignored runtime config
  │                                    overwritten on every deploy from
  │                                    config/pipeline_example_self_improve_server.json
  ├── harness_output/run_TIMESTAMP/  ← every run's artifacts
  │     ├── memory.jsonl             ← cross-round learnings (the gold)
  │     ├── round_N/phase_K_NAME/inner_M/
  │     │     ├── basic_eval.txt     ← evaluator A output
  │     │     ├── diffusion_eval.txt ← evaluator B output
  │     │     ├── implement_output.txt ← LLM tool-use trace
  │     │     ├── post_impl_snapshot.txt
  │     │     ├── tool_metrics.json
  │     │     └── .done              ← checkpoint marker
  │     └── round_N/phase_K_NAME/.done
  └── logs/harness.log               ← one giant rolling log

User-level systemd: ~/.config/systemd/user/harness.service
  → managed with `systemctl --user`, NEVER `sudo systemctl`
  → service config: Restart=on-failure with StartLimitBurst

Cron (crontab -l):
  */30 * * * * deploy/heartbeat.sh    ← restarts only when state == failed
  0 4 * * *    deploy/cleanup_runs.sh ← deletes run_* > 7 days old
```

**Critical mental model:** the running service auto-pushes commits to
`origin/main` after each round, and (when `auto_tag_at_end=true` and score
clears the threshold) auto-pushes a `harness-r-{rounds}-{shortsha}` tag, which
re-triggers the deploy workflow, which restarts the service. **Tag push is a
self-restart channel.** Don't be surprised when `etime` resets every ~30 min on
its own.

---

## 2. Reading the prompt stack — what the LLM actually sees

This is the most-misunderstood part of the system. The LLM's behaviour is
determined not by "the prompt" but by a STACK of prompts and injected
context that varies per phase. Diagnosing "why is the LLM doing X" requires
reading every layer, not just the system prompt.

### 2.1 Where prompts live (canonical sources)

| File | What it controls | Tracked? |
|---|---|---|
| `pipeline_server.json` (server runtime) | Per-phase config — actually live | NO (gitignored) |
| `config/pipeline_example_self_improve_server.json` | TRACKED template; `cp`'d over `pipeline_server.json` on every deploy | YES |
| `harness/prompts/dual_evaluator.py` | `BASIC_SYSTEM` and `DIFFUSION_SYSTEM` — the two evaluators | YES |
| `harness/prompts/synthesis.py` | `SYNTHESIS_SYSTEM` — synthesises N inner-round proposals into one | YES |
| `harness/prompts/planner.py` | Three-way planner (conservative + aggressive + merge) | YES |
| `harness/prompts/meta_review.py` | Meta-review prompt for periodic cross-round analysis | YES |
| `harness/pipeline/executor.py:EXECUTOR_SYSTEM` | The tool-use loop's system prompt | YES |
| `harness/pipeline/phase_runner.py:_DEBATE_SYSTEM_DEFAULT` | Default debate-mode system prompt when phase has no explicit one | YES |

**Critical:** `pipeline_server.json` is what's actually live. The TEMPLATE is
what `git reset --hard <tag>` deploys. If they have drifted (someone
hand-edited the runtime config), the next deploy silently overwrites the
runtime back to the template. **Always read the template if you want to
know what will be running after the next deploy.**

### 2.2 The actual stack one phase iteration sees

When phase N runs in `implement` mode, the executor LLM receives this
concatenation:

```
[executor.py: WORKSPACE preamble + EXECUTOR_SYSTEM]   ← code, tracked
                                                         "WORKSPACE: <path>; READ before EDIT;
                                                         SCOPE DISCIPLINE; etc."

+ [phase config: system_prompt with template variables expanded:]
       $file_context  ← keyword-ranked source files
                        (phase_runner.py:_read_source_files;
                         keywords come from phase.name + falsifiable_criterion)
       $prior_best    ← best-scoring synthesis from earlier phases this round
                        (PREPENDED with memory_ctx — see §4)
       $syntax_errors ← errors carried over from previous inner round
       $falsifiable_criterion  ← phase config field, also injected separately

+ [user msg: "## Plan to Execute" + the planner's output]
```

Three other LLM call sites have their own stacks:

- **Dual evaluator** sees `BASIC_SYSTEM` / `DIFFUSION_SYSTEM` + the
  proposal/synthesis being evaluated + (when present) the prior best for
  delta scoring.
- **Synthesis** sees `SYNTHESIS_SYSTEM` + `SYNTHESIS_USER_TEMPLATE` filled
  with `phase_name`, `falsifiable_criterion`, `file_context`, and
  `round_data` (the inner round outputs).
- **Planner** runs three-way: conservative + aggressive + merge prompts,
  each seeing the same task + context.

### 2.3 The single most useful artifact: `executor_prompt.txt`

Every inner round writes the EXACT prompt the executor LLM received.
Ground truth for "what did the LLM see":

```bash
ssh ubuntu@43.128.100.101 "
  ls -t /home/ubuntu/harness-everything/harness_output/run_*/round_*/phase_*/inner_*/executor_prompt.txt | head -1 | xargs head -250
"
```

Use this whenever:
- LLM keeps doing X that the prompt didn't ask for → check what
  `$prior_best` and the memory context block injected.
- LLM keeps editing the same file → confirm `$file_context` is putting it
  first (it'll be ranked at the top of the source listing).
- A criterion isn't being addressed → confirm `$falsifiable_criterion`
  interpolated correctly (it's easy for a misplaced `$` to leave the
  literal placeholder in the prompt).
- A wrapper section is dwarfing the system_prompt section — count line
  lengths or `wc -l` between section headers.

### 2.4 Per-phase config field reference

| Field | Effect | Common pitfalls |
|---|---|---|
| `name` + `index` | Phase identity; index controls execution order | Two phases with same index will silently misorder. |
| `mode` | `debate` (text only, parallel inner rounds) or `implement` (tool-use, sequential, can commit) | Only `implement` mode commits or runs hooks. Debate-mode phases are pure analysis. |
| `system_prompt` | Phase's instructions to the executor | Template variables (`$file_context`, `$prior_best`, `$syntax_errors`, `$falsifiable_criterion`) MUST appear or the phase won't see source/prior work. CLAUDE.md warns about this — losing one is silent. |
| `falsifiable_criterion` | Free-text scoring criterion the evaluator sees | Vague criterion → evaluator scores anything plausible. Should reference specific files / measurable outcomes / point penalties. |
| `glob_patterns` | Which source files get injected as `$file_context` | Too narrow → LLM has no context. Too broad → relevance ranking has too much to choose from and tends to pick recently-edited files (tunnel vision). |
| `tool_tags` | Which tools the executor can call | `[]` = all tools. Misconfiguring can silently disable test_runner / bash / web_search. |
| `inner_rounds` | How many parallel proposals to generate before synthesis | Default 2. More = better best-of, more LLM cost. |
| `skip_after_round` / `skip_cycle` | Periodic skip logic | `skip_cycle: 2` runs every other round. Commonly used for analysis-only phases (framework_analysis). |
| `min_proposal_chars` | Skip evaluation for short proposals | Avoid burning evaluator calls on no-op outputs. |
| `syntax_check_patterns` | Globs for SyntaxCheckHook (gates_commit) | If unset, no syntax gate. |
| `import_smoke_modules` | Modules for ImportSmokeHook (gates_commit) | Empty list = hook disabled. Critical for self-improvement pipelines. |
| `commit_on_success` + `commit_repos` | GitCommitHook config | If `commit_on_success: true` but `commit_repos: []`, the hook does nothing silently. |
| `run_tests` + `test_path` | PytestHook config (NOT gating) | Failure logged but does not block commit (by design — see §6 entry on PytestHook). |

### 2.5 Common prompt-side pathology

Each of these is real and seen in this project; learn to recognise them.

- **Vague falsifiable criterion** (e.g. "improvement must produce
  measurable benefit") gives the evaluator zero anchor. Symptom: scores
  drift ±5 with no apparent reason; LLM keeps doing whatever the previous
  round was doing.
- **System prompt's PRIORITY ORDER ignored** because `$prior_best` and
  memory inject more concrete signal pointing elsewhere. Symptom: phase
  whose prompt says "improve X" keeps changing Y. Confirm by reading
  `executor_prompt.txt` and counting mentions of X vs Y. **This is the
  most common failure mode of self-improvement pipelines.**
- **`glob_patterns` includes the test directory** in self-improvement
  pipelines, so the evaluator sees test files counted as "source" and
  rewards changes to tests instead of the code under test.
- **Two phases competing for the same file** — e.g. `framework_improvement`
  and `consolidation_and_testing` both glob `harness/**/*.py`. Whichever
  runs second sees the first's edits as `$prior_best` and tends to extend
  or partially undo them. Check phase ordering and consider tighter
  glob_patterns.
- **Code-side prompt drifts from intent** — `EXECUTOR_SYSTEM`,
  `dual_evaluator` prompts, and `synthesis` prompt are tracked code, so
  they only change when someone (a Claude session OR the harness's own
  self-improvement) edited them. `git log -- harness/prompts/
  harness/pipeline/executor.py` shows when. If a phase's behaviour changed
  unexplainedly, check whether one of these was just rewritten by a
  recent harness commit.
- **Template variable typo** — `$file_context` vs `$filecontext` will
  silently leave the literal text in the prompt. The phase will think it
  has no source code. Always grep your `system_prompt` strings for `$`
  and confirm each placeholder matches the names in
  `harness/pipeline/phase_runner.py`.
- **Evaluator and synthesis use static prompts** — they don't consume the
  per-phase config. So if a phase's intent is "be more lenient" or "weight
  Architecture higher", the phase prompt can SAY so but the evaluator
  ignores it. Editing evaluator behaviour means editing
  `harness/prompts/dual_evaluator.py`, which then affects all phases.

### 2.6 Recipe — "is the prompt or the wrapper winning?"

When you suspect prompt-vs-wrapper conflict (e.g. phase says "improve
evaluator" but every commit touches `cross_reference.py`):

```bash
# 1. Pull the latest executor prompt for the phase in question
ssh ubuntu@43.128.100.101 "
  cat \$(ls -t /home/ubuntu/harness-everything/harness_output/run_*/round_*/phase_K_*/inner_2/executor_prompt.txt | head -1)
" > /tmp/last_prompt.txt

# 2. Count topic mentions across the whole prompt
grep -ic 'evaluator\\|synthesis\\|planner' /tmp/last_prompt.txt   # what the prompt SAYS to improve
grep -ic 'cross_reference' /tmp/last_prompt.txt                   # what the wrapper KEEPS surfacing

# 3. Find which section dominates (split by '## ' headings)
awk '/^## / {h=$0; next} {print h "\\t" $0}' /tmp/last_prompt.txt \
  | grep -ic 'cross_reference'
```

If the wrapper sections (`## Source Context`, `## Memory: Prior Round
Learnings`, `## Prior Best`) mention topic A 30+ times and `system_prompt`
mentions topic B 5 times, the LLM follows A. Fixes are structural:

- Rebalance `memory.format_context` (memory.py:300-303 — currently 6:2
  same-phase:cross-phase).
- Narrow `glob_patterns` so the keyword ranker has fewer candidates.
- Add a `STOP DOING X` clause in the system_prompt — but this rarely
  works because it's a single sentence vs many wrapper signals.
- Change `falsifiable_criterion` to reference DIFFERENT files than the
  current focus, anchoring the evaluator's scoring elsewhere.

### 2.7 Where to put your edits

If you need to change pipeline behaviour:

- Per-phase tweaks (criterion, glob, prompt) → edit
  `config/pipeline_example_self_improve_server.json`. Will deploy on next
  tag; does NOT require a code commit.
- Cross-phase evaluator/synthesis behaviour → edit `harness/prompts/*.py`.
  Will deploy on next tag.
- Wrapper logic (memory injection, file context ranking, prior_best
  semantics) → edit `harness/pipeline/{memory,phase_runner,pipeline_loop}.py`.
  Higher risk; verify locally with `py_compile` before tagging.

The runtime `pipeline_server.json` should NEVER be hand-edited — the next
deploy will overwrite it.

---

## 3. Diagnostic recipes — by symptom

### 3.1 "Service is not `active`"

```bash
ssh ubuntu@43.128.100.101 "
  systemctl --user status harness --no-pager | head -25
  echo '---last error stack---'
  grep -E 'Traceback|Error:|NameError|ImportError|UnboundLocal' \
    \$(ls -t /home/ubuntu/harness-everything/logs/*.log | head -1) | tail -10
"
```

- `failed` + crash trace → fix the code, push, tag.
- `activating (auto-restart)` → systemd is retrying. Same fix path. Heartbeat
  cron will eventually escalate from `failed` back to running once the bad
  state clears, so don't wait — fix and tag.
- `inactive` (clean stop) → someone (maybe a previous Claude session) ran
  `systemctl --user stop harness`. Look for a stop marker:
  `ls ~/.config/harness/STOP_AFTER_CHUNK` — if present, deploy was instructed
  to NOT restart. Remove it and `systemctl --user start harness` (or push a
  tag).

**Crash-loop trap:** if your fix lands as a tag, the deploy `git reset --hard`
syncs the server to the new code AND restarts. The `failed` state from the
crash loop is cleared by the deploy's `systemctl --user restart`. You do not
need to manually `reset-failed`.

### 3.2 "Service is `active` but no new harness commits"

The harness is running but `commit_on_success` is being suppressed. Check the
hook outcomes:

```bash
ssh ubuntu@43.128.100.101 "
  grep -E 'Hook (syntax_check|import_smoke|pytest|git_commit):|gating hooks failed' \
    /home/ubuntu/harness-everything/logs/harness.log | tail -20
"
```

Read the sequence per phase (timestamps clustered within a few seconds):
- All `passed=True` then `git_commit: passed=True` → commits are flowing; the
  problem is elsewhere (maybe `auto_push_min_score` filter).
- `import_smoke: passed=False` → grep for the actual error:
  ```
  grep 'Hook import_smoke errors' logs/harness.log | tail -3
  ```
  Real cause is usually one of: (a) harness module Genuinely broken — fix the
  code; (b) hook misconfigured — check `import_smoke_modules` in config
  template; (c) `python` vs `sys.executable` — check `harness/pipeline/hooks.py`
  for hardcoded `"python"`.
- `syntax_check: passed=False` → the LLM produced unparseable code; safe to
  let the pipeline self-correct over a few rounds.
- `git_commit: skipped (gating hooks failed: X)` → working as designed; X is
  what you should fix.

### 3.3 "All commits churning the same file"

Symptom of the wrapper-overpowers-prompt failure mode (memory + file_context +
prior_best all reinforce one focus). Quantify it:

```bash
ssh ubuntu@43.128.100.101 "cd /home/ubuntu/harness-everything &&
  git log --since='6 hours ago' --pretty=format:'%h' | \
    while read c; do git show --stat \$c | tail -2; done | \
    grep -E '\\.py' | awk '{print \$1}' | sort | uniq -c | sort -rn | head -10
"
```

If one file is >50% of commits, the system is in tunnel vision. The fix is
structural (memory.format_context rebalancing, anti-staleness in
file_context, `Diff Novelty` evaluator dimension) — not "tell the LLM to stop".

### 3.4 "memory.jsonl fields are empty"

```bash
ssh ubuntu@43.128.100.101 "
  python3 -c \"
import json
path = '\$(ls -t /home/ubuntu/harness-everything/harness_output/run_*/memory.jsonl | head -1)'
entries = [json.loads(l) for l in open(path) if l.strip()]
top = sum(1 for e in entries if e['evaluator_top_defect'])
risk = sum(1 for e in entries if e['evaluator_key_risk'])
print(f'{len(entries)} entries; top_defect filled: {top}; key_risk filled: {risk}')
\"
"
```

- `0/N` filled → the regex in `harness/pipeline/memory.py:_extract_top_defect`
  / `_extract_key_risk` doesn't match what the evaluator LLM is producing.
  Pull a sample eval and compare:
  ```bash
  ssh ubuntu@43.128.100.101 "
    grep -E '^##|^\\*\\*' \
      /home/ubuntu/harness-everything/harness_output/run_*/round_N/phase_K_*/inner_2/basic_eval.txt
  "
  ```
  Expand the fallback regex to cover the new heading style.
- Partially filled → some phases use a different evaluator template
  (`framework_analysis` is the usual offender — its prompt asks for numbered
  instructions which the LLM echoes as `## 1. Find the…`). Acceptable as long
  as the implementing phases (2-5) are populated.

### 3.5 "Score is going down"

Pull the delta history per phase:

```bash
ssh ubuntu@43.128.100.101 "
python3 -c \"
import json
path = '\$(ls -t /home/ubuntu/harness-everything/harness_output/run_*/memory.jsonl | head -1)'
by_round = {}
for line in open(path):
    e = json.loads(line)
    by_round.setdefault(e['round'], []).append((e['phase'], e['score'], e['score_delta']))
for r in sorted(by_round):
    print(f'R{r}:')
    for phase, score, delta in by_round[r]:
        sign = '+' if delta >= 0 else ''
        print(f'  {phase[:30]:30s} score={score:5.1f}  delta={sign}{delta:+.1f}')
\"
"
```

Two distinct patterns to distinguish:

- **All phases within ONE round dropping in sequence** (e.g. R2: phase2 score
  15 → phase3 score 7 → phase4 score 9): regression poisoning, the
  `prior_best` propagation bug. Fixed in commit 41a6cd4. If you see this
  pattern again, the fix has been undone or bypassed — check
  `pipeline_loop.py:_run_outer_round` for the best-score gate.
- **Each phase scoring lower than its own prior best across rounds** (e.g. R3
  cons=17, R4 cons=14.5, R5 cons=13): natural convergence. The codebase has
  reached a local maximum; the LLM keeps trying but the evaluator (correctly)
  thinks each iteration is incrementally worse. Watch for `no_improve=N/5` in
  the log; early-stop kicks in at 5.

### 3.6 "Auto-push or auto-tag is broken"

```bash
ssh ubuntu@43.128.100.101 "
  grep -E 'auto_push|auto_tag' \
    /home/ubuntu/harness-everything/logs/harness.log | tail -15
"
```

Common: `auto_push: pull --rebase failed (rc=128) ... unstaged changes`. Means
the workspace has untracked or modified files that survived the previous
restart. Check `git status` on the server. Most likely a scratch file the LLM
left behind.

---

## 4. Reading memory.jsonl — the canonical "what's the system learning" view

One JSON object per line. Schema:

```
ts, round, phase                  ← when + which
score                             ← combined dual-evaluator (0-20)
score_delta                       ← vs best score ever for THIS phase
                                    (NOT vs the immediately prior phase)
insight                           ← first ~400 chars of synthesis
evaluator_top_defect              ← from basic_eval CRITICAL section
evaluator_key_risk                ← from diffusion_eval SECOND-ORDER section
```

**`score_delta` math is per-phase, not per-round.** A negative delta on R5
phase 3 means R5's phase 3 scored lower than the best historic phase 3. It
does NOT mean phase 3 dragged down the round.

**Empty `top_defect`/`key_risk`** is fine when the evaluator's score is high
(prompt explicitly says "or 'none' if score ≥ 9"). It is a problem when the
score is mediocre and the field is still empty — that means the regex missed
the structured output.

**Score patterns to recognise:**

| Pattern | Meaning |
|---|---|
| All deltas positive across rounds | Healthy convergence |
| All deltas negative across rounds | Past local maximum — early-stop should fire |
| One phase has deltas oscillating ±5 each round | Non-converging task; phase prompt may be too vague |
| Same phase keeps citing same file in `insight` | Tunnel vision, see §3.3 |

---

## 5. Action playbook — when to push, when to wait

```
SYMPTOM                             ACTION                  WHY
─────────────────────────────────── ─────────────────────── ─────────────────────
Service crashed (NameError, etc.)   Push fix tag NOW        Loop is dead, every
                                                            minute = lost work
ImportSmokeHook miscfg → 0 commits  Push fix tag NOW        Pipeline is wasting
                                                            LLM cost producing
                                                            commits that get
                                                            silently dropped
Score regression within a round     Investigate, push fix   Real bug; data will
                                                            confirm in 1 round
Score regression across rounds      WAIT for early-stop     Natural convergence;
                                                            interrupting loses
                                                            the dataset
Tunnel vision on one file           WAIT 1-2 rounds         May break naturally;
                                    then bundle fix into    if not, structural
                                    next deploy             fix is non-trivial
Memory regex missing one format     Note it; bundle into    Cosmetic until you
                                    next planned deploy     know it's not a
                                                            one-off
Scratch file in repo root           Tag fix when next       Hygiene issue, not
                                    structural change is    operational
                                    ready
```

**Deploy mechanics — what a tag push actually does:**

1. You: `git tag harness-r-something && git push origin harness-r-something`
2. CI: SSH to server, `git fetch`, `git checkout main`, `git reset --hard <tag>`
3. CI: `cp config/pipeline_example_self_improve_server.json pipeline_server.json`
4. CI: smoke = `py_compile harness/**/*.py main.py` + `PipelineConfig.from_dict`
5. CI: if smoke passed, advance `harness-last-good` tag
6. CI: if smoke failed, `git reset --hard harness-last-good` (rollback)
7. CI: `systemctl --user restart harness.service` (unless STOP marker)
8. New process starts; `Resuming run: ...` if a prior run dir has incomplete
   phases, else `New run: ...`

Implications:
- **Untracked files survive the reset** because `git reset --hard` only
  affects tracked files. If a previous LLM run wrote a scratch file, it
  persists across deploys until you `git clean -fd` or stage+commit+remove.
- **The current in-flight phase is interrupted by SIGTERM** during
  `systemctl restart`. The phase's `inner_*/` artifacts may be partial.
  `.done` markers are atomic — anything without `.done` will re-run on
  resume.
- **A failed smoke triggers rollback**, so a broken commit on `main` doesn't
  brick the server, but it DOES leave you with `main` ahead of
  `harness-last-good`. The next good tag promotes `harness-last-good` again.

---

## 6. Real gotchas (each cost real time at least once)

1. **`sudo systemctl stop harness` says "not loaded"** — the unit lives under
   `~/.config/systemd/user/`, accessible only to user-level systemctl. Always
   `systemctl --user`, never `sudo`.

2. **`systemctl --user is-active` returns `failed` after `stop`** because
   Python exits non-zero when SIGTERM'd. Heartbeat cron will restart on
   `failed`. Run `systemctl --user reset-failed harness` after any deliberate
   stop to mark it `inactive` so the heartbeat leaves it alone.

3. **`subprocess_exec("python", ...)` fails on the server** because the venv
   binary is `/home/ubuntu/harness-venv/bin/python`, not on PATH. Use
   `sys.executable` in any new subprocess hook.

4. **`git add -A` in GitCommitHook sweeps untracked files into commits.**
   This is how `count_lines.py` and `debug_test.py` ended up in
   `harness-everything`'s root. Either change to add specific files, or add
   a pre-commit untracked-file purge.

5. **`--allow-empty` in GitCommitHook produces 0-line commits when a phase
   does nothing.** This is a *feature*, not a bug — empty commits carry the
   harness's structured metadata (score, tool usage, evaluator summaries) and
   are valid telemetry that the system ran the phase.

6. **The auto-tag chain reaction.** When the harness completes a chunk it
   pushes a tag, which triggers a deploy, which restarts the service. If the
   restart starts a new run that immediately auto-tags again (e.g. due to a
   resumed completed run), you get a restart loop that takes a few minutes to
   settle. Tolerate it; don't intervene unless `etime` keeps resetting for
   >10 min.

7. **`prior_best` was "latest wins" before commit 41a6cd4.** If you see a
   round where every phase regressed in sequence (e.g. 15→7→9→14), the fix
   has been removed or bypassed. The fix lives in
   `pipeline_loop.py:_run_outer_round` — look for the `best_synth_score`
   gate.

8. **R4 traceability commit `fc3d99c` introduced two undefined-name bugs
   (`_flatline_streak`, `_FLATLINE_WARN_STREAK`).** Both crashed at round 2
   only — round 1 doesn't enter the trend-detection block. This is the canonical
   example of why ImportSmokeHook isn't enough; static checks catch
   import-time problems but not "branch reached only by certain control flow".

9. **The R3 peak-then-decline pattern is normal.** When the harness reaches a
   local maximum on the file it's been improving, subsequent rounds keep
   trying but score lower. `no_improve=N/5` in the log is the early-stop
   counter; let it fire. Don't push more tags hoping the next round will
   spike.

10. **Pipeline configs are gitignored except for example templates.** The
    server's `pipeline_server.json` is overwritten from
    `config/pipeline_example_self_improve_server.json` on every deploy. To
    change runtime behaviour, edit the template, not the runtime config.

---

## 7. Post-session — refine the harness AND this runbook

Before closing the session, spend 5-10 minutes on this checklist. Skipping it
is how the runbook goes stale and how the harness keeps re-stepping on the
same rakes. Past sessions learned things that future sessions are about to
forget; this is where you stop the bleed.

### 7.1 Capture into this runbook

For each item, ask "would the next cold-start session benefit from knowing
this?" — if yes, edit the relevant section now, in the same session, before
you forget. Open `docs/HARNESS_RUNBOOK.md` and:

- **New symptom you debugged** → add a recipe to §3 with a paste-ready
  command and a decode of the expected output.
- **A gotcha that cost you time** → add it to §6 with a one-line "what it
  looks like" and a one-line "what to do". The bar for inclusion is *did
  this surprise me*, not *is this a major bug*.
- **A recipe in §3 that didn't actually help** → fix it. Either the command
  was wrong, the output decode was missing a case, or the symptom-to-recipe
  mapping was off. Don't leave a stale recipe — it actively misleads.
- **A claim in §4 (memory) or §5 (action playbook) that turned out to be
  wrong** → correct it AND note the correction inline so future readers see
  "this used to say X, but X is wrong because Y".
- **A new prompt field, evaluator dimension, or template variable** → add
  to §2 (prompt stack), since §2 is the canonical reference for "what the
  LLM actually sees".
- **A new code lever you discovered** (config field, env var, hook
  attribute) → add to §1 architecture or to the relevant recipe.

If you didn't have to investigate anything new, the runbook needs no
changes — say so explicitly to yourself rather than pretending. Forced
edits dilute the doc.

### 7.2 Look for harness-side optimisations the session surfaced

Most fixes you push solve the immediate symptom but expose a deeper
structural gap. Don't lose those observations. For each, decide:

| Disposition | Criteria |
|---|---|
| **Fix now, this session** | Operational risk if left unfixed (crash, silent commit suppression, security) |
| **Bundle into next planned deploy** | Improvement, low risk if delayed; benefits from being grouped with related changes |
| **Note in `docs/HARNESS_BACKLOG.md`** | Speculative; needs more evidence before committing to a direction |

Concrete checklist of "is there a deeper issue here" prompts:

- Did a fix involve a one-line constant or initialisation? Search for sibling
  uses (`grep` for adjacent identifiers) — usually the same author missed
  more than one. The R4 traceability commit shipped two undefined names; the
  first fix only caught one.
- Did the wrapper (memory / file_context / prior_best) override a phase
  prompt's stated priorities? That's a structural lever, not an LLM bug.
  Note the specific reinforcement loop you observed.
- Did an evaluator give a high score to work that was hollow (empty diff,
  re-proposing prior round, scratch files)? That's an evaluator dimension
  gap. Note what dimension would have caught it.
- Did a hook fire correctly but get ignored? Check whether `gates_commit` or
  the short-circuit logic in `phase_runner.py` covers that hook.
- Did you find yourself wishing for a piece of telemetry (per-file edit
  count, per-phase score variance, unused tool detection)? That's a metrics
  gap — note what would have answered the question in 5 seconds.

### 7.3 Update memory if it's about behaviour, not findings

Project facts (current state of the harness, who's running, what's deployed)
are ephemeral and belong here in the runbook or in `docs/HARNESS_BACKLOG.md`,
not in cross-conversation memory. Use memory for things like:

- "User prefers bundling P1/P2 fixes into one deploy rather than tag-spam"
- "User considers empty commits valid telemetry; do not propose removing
  `--allow-empty` without checking first"
- "User runs Windows locally; bash tools work via git-bash, but `find`/
  ripgrep paths differ"

These shape *how to collaborate* in the next session, which is exactly what
memory is for.

### 7.4 The honest one-line journal entry

End the session by appending one line to the bottom of this file under the
"Session log" heading below. Format:

```
YYYY-MM-DD — <one sentence: what you fixed or learned, why it mattered>
```

This is not for status reporting; it is so the next session can `tail`
five lines and instantly see the recent history of what's been touched and
why. If the entry feels boring or repetitive, that itself is signal —
either the harness is healthy and stable, or you're not noticing the
patterns.

### Session log

```
2026-04-17 — diagnosed phase regression poisoning (prior_best latest-wins
             instead of best-ever); fix in pipeline_loop.py:_run_outer_round.
2026-04-17 — memory.jsonl evaluator fields were 0/18 because regex only
             matched the canonical `TOP DEFECT:` form; added section-style
             fallback for `## CRITICAL DEFECTS` / `## SECOND-ORDER EFFECTS`.
2026-04-17 — added ImportSmokeHook + gates_commit short-circuit; PytestHook
             stays non-gating to preserve test-writing-phase semantics.
2026-04-18 — two crashes from R4 traceability commit (_flatline_streak +
             _FLATLINE_WARN_STREAK undefined); ImportSmokeHook hardcoded
             "python" instead of sys.executable, silently blocking 8 commits.
2026-04-18 — wrote this runbook; first version covers the recipes accumulated
             over the past two days of incident response.
```

---

## 8. When in doubt — three questions to ask

1. **Is it the LLM or is it the harness?** If the symptom appears regardless
   of which model is configured (we've used Claude and DeepSeek), it's the
   harness. If it appears only with certain prompts or models, it's the LLM
   doing what was asked.

2. **Is the wrapper or the prompt winning?** Read the phase's prompt in the
   config, then read what `prior_best` + memory + file_context inject around
   it. If those three reinforce one focus area, the prompt's stated priorities
   will be ignored. The LLM optimises for highest-frequency context signal.

3. **Did this start happening at a specific commit?** Use `git log --oneline
   -S "<symbol>" -- <file>` to find when a function/constant first appeared.
   Many production crashes came from "the harness committed code that ran
   fine at commit-time because the bad branch wasn't entered, then crashed on
   restart".
