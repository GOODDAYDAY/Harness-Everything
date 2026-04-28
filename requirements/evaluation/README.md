# Evaluation Domain

The evaluation domain provides objective, fact-based quality signals that ground scoring in verifiable evidence rather than subjective opinion. It has three sub-areas:

1. **Static Analysis** -- deterministic, non-LLM code checks that surface objective defects (syntax errors, broken imports, missing symbols, structural regressions).
2. **Metrics** -- quantitative measures of scoring quality itself, ensuring evaluators differentiate meaningfully between submissions.
3. **Dual Evaluator** -- two independent scoring perspectives that combine into a single quality signal (covered in `dual-evaluator.md`).

---

## Static Analysis

### US-01: As the static analyzer, I need to verify that every changed file is syntactically valid, so that objectively broken code is caught before any subjective review begins

A file that cannot be parsed by the language runtime is broken by definition. This check must run before all other analysis steps because downstream checks (imports, symbols) rely on a parseable file.

#### Acceptance Criteria
- Given a set of changed files, when any file contains a syntax error, then an error-level finding is reported with the file path and line number
- Given a set of changed files, when all files are syntactically valid, then no syntax-related findings are produced
- Given a file that fails to compile for an unexpected reason, then a warning-level finding is reported instead of crashing the analysis

### US-02: As the static analyzer, I need to detect imports that reference modules not available in the environment or workspace, so that missing dependencies are surfaced as warnings

An import of a module that does not exist in the standard library, installed packages, or the project workspace is likely a mistake. However, it may also be an intentional new dependency, so this is a warning rather than a hard error.

#### Acceptance Criteria
- Given a changed file that imports a module not found in stdlib, installed packages, or workspace, then a warning-level finding is reported
- Given a changed file that imports a module available in the standard library, then no finding is reported for that import
- Given a changed file that imports a sibling module within the workspace, then no finding is reported for that import
- Given a file with a syntax error, then import checks are skipped for that file to avoid duplicate reporting

### US-03: As the static analyzer, I need to verify that each named symbol imported from a workspace module actually exists in that module, so that typos and stale references are caught as errors

When code imports a specific symbol from a known module and that symbol does not exist, it will fail at runtime. This is a stronger signal than a missing module and warrants an error-level finding.

#### Acceptance Criteria
- Given a file that imports a named symbol from a workspace module, when that symbol exists as a top-level definition, assignment, or re-export, then no finding is reported
- Given a file that imports a named symbol from a workspace module, when that symbol does not exist in that module, then an error-level finding is reported listing the symbol and the available exports
- Given a file that imports a name that corresponds to a sub-module or sub-package of the target, then no finding is reported
- Given a star import, then the symbol check is skipped for that import

### US-04: As the static analyzer, I need to detect when top-level definitions disappear from a file after execution, so that accidental deletions of classes or functions are flagged as potential regressions

If a top-level class or function that existed before execution is no longer present afterward, callers that depend on it will break. This is surfaced as a warning because the removal may be intentional.

#### Acceptance Criteria
- Given a file that existed before execution and has a pre-execution snapshot, when a top-level definition is removed, then a warning-level finding is reported naming the disappeared symbol
- Given a newly created file with no pre-execution snapshot, then no structural regression check is performed
- Given a file where all previous top-level definitions are still present, then no structural regression finding is reported

### US-05: As the static analyzer, I need to produce a structured report that the evaluator can inject into its prompt, so that LLM-based scoring is grounded in objective facts

The report must be machine-readable and human-readable, summarizing all findings in a format the evaluator can consume directly. Error-level findings must be called out as automatic failure conditions.

#### Acceptance Criteria
- Given a completed analysis run, when there are findings, then the report includes a summary line with error count, warning count, and clean file count
- Given a completed analysis run, when there are findings, then the report renders a table with level, file, line, and description for each finding
- Given a completed analysis run with error-level findings, then the report includes an explicit instruction that the reviewer must fail the evaluation
- Given no changed files to analyze, then the report produces an empty output block
- Given non-Python files in the changed set, then those files are counted as skipped and excluded from checks

---

## Metrics

### US-06: As the evaluator, I need a measure of how well scores in the critical middle range are differentiated from each other, so that I can detect when scoring collapses into a narrow band and fails to distinguish between meaningfully different submissions

The middle range of the scoring scale is where most submissions land and where discrimination matters most. If all scores cluster tightly, the evaluation is not providing useful signal. The spread of scores in this range quantifies differentiation quality.

#### Acceptance Criteria
- Given a set of evaluations with two or more scores in the middle range, then a spread metric is calculated and returned as a non-negative number
- Given a set of evaluations with fewer than two scores in the middle range, then the metric returns zero to indicate insufficient data
- Given a set of evaluations where the input is not a list, then a type error is raised
- Given evaluation entries with missing or non-numeric scores, then those entries are silently skipped without affecting the calculation
- Given a set of evaluations using sample-based statistics, then the metric accounts for small sample sizes by using an unbiased estimator
