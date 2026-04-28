# Agent Git Operations

User stories for auto-commit, multi-repo, commit messages, staging, push, tagging, and squash.

---

## Staging

### US-01: As a cycle, I need only the files I actually changed to be staged for commit, so that unrelated workspace modifications are not accidentally committed

After the agent's tool-use dialogue completes and verification passes, the framework stages only the specific files that the agent's tools modified. This prevents stale or unrelated changes from leaking into the cycle's commit.

#### Acceptance Criteria
- Given the agent modified three specific files during its cycle, when staging runs, then only those three files are added to the index
- Given staging fails in any repository, when the failure is detected, then the commit is skipped and a warning is logged
- Given the agent made no file changes, when staging is considered, then the staging step is skipped gracefully

### US-02: As a cycle, I need staging skipped when verification hooks fail, so that broken code is never committed

When any gating verification hook fails, the cycle's changes should not be staged or committed. The hook failure reasons are recorded for the agent to learn from in the next cycle.

#### Acceptance Criteria
- Given a gating hook failed, when the staging decision is made, then staging is skipped
- Given a gating hook failed, when the cycle proceeds, then the hook failure reasons are logged and included in the cycle notes

---

## Commit Messages

### US-03: As a cycle, I need a structured commit message that includes the cycle number, a content summary, metrics, evaluation scores, and hook status, so that the version control history is self-documenting

Each commit message follows a consistent format: a title line with the cycle number and a brief description of what changed, followed by a body with metrics, evaluation scores, and hook outcomes. This makes the git log a standalone record of the agent's progress and quality.

#### Acceptance Criteria
- Given a cycle completed successfully, when the commit message is built, then the title contains the cycle number and a summary derived from the agent's output
- Given metrics, evaluation scores, and hook results are available, when the commit message is built, then each appears on its own line in the message body
- Given the agent's output was truncated by the tool loop, when the commit message is built, then a diff-based summary is used instead of the truncated text

---

## Auto-Commit

### US-04: As a cycle, I need changes automatically committed after staging, so that each cycle's work is atomically captured in version control

When auto-commit is enabled, the framework commits the staged changes with the structured commit message. Each cycle becomes one atomic commit, making it easy to review, revert, or squash individual cycles.

#### Acceptance Criteria
- Given auto-commit is enabled and staging succeeded, when the commit runs, then a single commit is created containing all staged changes
- Given auto-commit is disabled, when the cycle completes, then no commit is created regardless of staging status
- Given the commit operation fails, when the failure is detected, then a warning is logged and the cycle proceeds

---

## Multi-Repo

### US-05: As a cycle, I need staging and committing to operate across multiple configured repositories, so that projects with submodules or companion repos are supported

The agent may work across multiple git repositories simultaneously (for example, a main project and a submodule). Staging and committing are performed in each configured repository, ensuring all repositories stay in sync.

#### Acceptance Criteria
- Given two repositories are configured, when staging runs, then changes are staged in both repositories
- Given two repositories are configured, when committing runs, then the same commit message is used in both
- Given a configured repository path does not exist, when repository paths are resolved, then a warning is logged and that path is skipped

---

## Push

### US-06: As the agent, I need to optionally push commits to a remote after each cycle, so that remote repositories stay current during long-running missions

When auto-push is enabled, each successful commit is immediately pushed to the configured remote and branch. This keeps remote mirrors up to date and enables other systems to observe the agent's progress in real time.

#### Acceptance Criteria
- Given auto-push is enabled and a commit succeeded, when the push runs, then the current branch is pushed to the configured remote
- Given auto-push is disabled, when a commit succeeds, then no push is attempted
- Given the push fails, when the failure is detected, then a warning is logged but the cycle proceeds
- Given multiple repositories are configured, when push runs, then each repository is pushed independently

---

## Tagging

### US-07: As the agent, I need to create a version tag at each periodic checkpoint, so that checkpoint states are easy to find and reference later

At each periodic checkpoint, if tagging is enabled, the framework creates a tag at the current commit. The tag name encodes the checkpoint identifier and a short commit hash for uniqueness. Tags can optionally be pushed to the remote.

#### Acceptance Criteria
- Given auto-tag is enabled, when a periodic checkpoint runs, then a tag is created at the current commit
- Given auto-tag is enabled and tag pushing is enabled, when a tag is created, then it is pushed to the configured remote
- Given auto-tag is disabled, when a periodic checkpoint runs, then no tag is created
- Given this is the startup checkpoint, when tagging is considered, then no tag is created (tags only apply to periodic checkpoints)

---

## Squash

### US-08: As the agent, I need recent commits grouped and squashed by semantic meaning at checkpoint time, so that the version history stays clean and reviewable

Over many cycles, the agent creates many small commits. At each periodic checkpoint, if squashing is enabled, the framework analyses the recent commits and groups them by logical theme. Each group is squashed into a single commit with a descriptive message, keeping the history clean without losing the semantic boundaries between different areas of work.

#### Acceptance Criteria
- Given auto-squash is enabled and recent commits exist, when a checkpoint runs, then the commits are analysed and grouped by semantic similarity
- Given the analysis produces multi-commit groups, when squashing executes, then each group becomes a single commit with a combined message
- Given the analysis determines all commits are independent, when the result is evaluated, then no squash is performed
- Given the squash operation encounters a conflict, when the error is detected, then the operation is cleanly aborted and the original history is preserved
- Given auto-push is enabled, when squashing is considered, then squashing is disabled to avoid rewriting pushed history

### US-09: As the agent, I need squash failures to be cleanly recoverable, so that a failed squash never leaves the repository in a broken state

Squashing rewrites commit history, which is inherently risky. If the rebase fails for any reason (conflicts, unexpected state), the framework must abort the rebase and leave the repository exactly as it was before the attempt.

#### Acceptance Criteria
- Given the squash rebase fails, when the failure is detected, then the rebase is aborted and the repository returns to its pre-squash state
- Given an unexpected error occurs during squashing, when the error is caught, then an abort is attempted and the failure is logged
- Given the squash succeeds, when the operation completes, then the new commit history is consistent and the temporary files are cleaned up

---

## Diff Queries

### US-10: As a cycle, I need the staged diff available before committing, so that the evaluator can assess the actual code changes

The evaluator needs to see the concrete code changes the agent made, not just the agent's description of what it did. By reading the staged diff before committing, the evaluator receives the actual change content.

#### Acceptance Criteria
- Given changes are staged, when the diff is retrieved, then the full staged diff content is returned
- Given the diff is very large, when it is retrieved, then it is truncated to a manageable size with a truncation notice
- Given no changes are staged, when the diff is retrieved, then an empty result is returned
