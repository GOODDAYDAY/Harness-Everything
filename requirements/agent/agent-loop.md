# Agent Loop

User stories for cycle lifecycle, shutdown, resumption, notes, mission control, mode selection, budget, artifacts, and periodic checkpoint.

---

## Cycle Lifecycle

### US-01: As a cycle, I need to execute in ordered phases (execute, verify, stage, evaluate, commit, persist, control), so that each concern is handled at the right time with the right inputs

Each cycle follows a fixed sequence: the agent runs its tool-use dialogue, then the framework verifies the output, stages changes, evaluates quality, commits to version control, persists artifacts, and finally checks whether to continue. This phased approach ensures that verification always precedes committing and that evaluation data is available before persistence.

#### Acceptance Criteria
- Given a cycle begins, when it completes normally, then the execution phase runs before verification, verification runs before staging, staging runs before evaluation, evaluation runs before commit, and commit runs before persistence
- Given the verification phase detects failures, when the cycle proceeds, then the staging and commit phases are skipped but evaluation and persistence still run

### US-02: As a cycle, I need a system prompt assembled from the mission, project parameters, persistent notes, strategic direction, and cycle number, so that the agent has full context for its work

The agent operates without an orchestrator, so all relevant context must be packed into the system prompt at the start of each cycle. This includes the standing mission, any project-specific parameters, notes from previous cycles, strategic direction from the last review, and the current cycle number.

#### Acceptance Criteria
- Given a mission is configured, when the system prompt is built, then the mission text appears in the prompt
- Given project parameters are configured, when the system prompt is built, then each parameter is included
- Given persistent notes exist from previous cycles, when the system prompt is built, then the notes content is included
- Given a strategic direction was produced by the last checkpoint, when the system prompt is built, then the direction text appears before the mission
- Given the agent is on cycle five of twenty, when the system prompt is built, then the cycle number and maximum are indicated

### US-03: As a cycle, I need a differentiated opening instruction for the first cycle versus subsequent cycles, so that the agent knows whether to orient itself or continue from prior work

The first cycle needs an instruction to explore and orient before picking a task. Subsequent cycles should review their notes and pick up where they left off. This prevents the agent from re-exploring on every cycle or skipping orientation on the first.

#### Acceptance Criteria
- Given this is the first cycle of a run, when the user message is built, then it instructs the agent to read and understand the codebase before acting
- Given this is a subsequent cycle, when the user message is built, then it instructs the agent to review its notes and continue from prior work

### US-04: As a cycle, I need post-execution verification hooks to check syntax, static analysis, and import health, so that broken code is never committed

After the agent finishes its tool-use dialogue, the framework runs automated checks against the changed files. These hooks act as quality gates: if any gating hook fails, the cycle's changes are not committed, preventing broken code from entering version control.

#### Acceptance Criteria
- Given hooks are configured and the agent modified files, when verification runs, then each configured check executes against the workspace
- Given a gating hook fails, when the cycle proceeds, then the commit is skipped and the failure reason is recorded
- Given all hooks pass, when the cycle proceeds, then the cycle is eligible for committing
- Given a non-gating hook crashes, when the cycle proceeds, then the commit is not blocked

---

## Shutdown

### US-05: As the agent, I need to finish my current cycle before shutting down when an OS signal is received, so that work in progress is not lost mid-cycle

When the operator sends a termination signal, the agent should not abort immediately. Instead, it finishes the current cycle (including commit and persistence) and then exits cleanly. This ensures no half-completed work is left in an inconsistent state.

#### Acceptance Criteria
- Given the agent is in the middle of a cycle, when a termination signal is received, then the current cycle completes fully before the agent stops
- Given a termination signal was received, when the current cycle finishes, then the run is recorded with a "partial" status
- Given multiple termination signals are received, when the agent processes them, then only the first is acted upon (no duplicate handling)

### US-06: As the agent, I need to write a final summary when the run ends for any reason, so that the run directory is marked as complete and not mistakenly resumed later

When a run ends -- whether by mission completion, blockage, exhaustion, or shutdown -- a final summary must be written to disk. This summary serves as a completion marker: its presence tells the framework that this run directory should not be resumed.

#### Acceptance Criteria
- Given the agent completes a run, when the final summary is written, then it includes the mission status, cycle count, and tool call total
- Given a run directory has a final summary, when the framework looks for resumable runs, then this directory is not selected

---

## Resumption

### US-07: As the agent, I need to detect and resume an incomplete prior run, so that a crash or restart does not lose accumulated context and artifacts

If the agent is started and an incomplete run directory exists (no final summary marker), it should resume that run rather than creating a new one. This makes the agent crash-safe: committed work and notes survive across restarts.

#### Acceptance Criteria
- Given an incomplete run directory exists in the output location, when the agent starts, then it resumes from that directory instead of creating a new one
- Given no incomplete run directory exists, when the agent starts, then a new run directory is created
- Given a prior run has a final summary, when the agent starts, then that run is not considered for resumption

---

## Notes

### US-08: As the agent, I need a persistent notes file that survives across cycles and restarts, so that the agent retains memory of what it has done and learned

Each cycle's summary -- including tool usage statistics, evaluation feedback, and the agent's own conclusions -- is appended to a notes file on disk. The agent reads this file at the start of each cycle to maintain continuity. Because it is on disk rather than in conversation history, it survives context window pruning and process restarts.

#### Acceptance Criteria
- Given a cycle completes, when notes are appended, then the cycle number, timestamp, and summary are written to the notes file
- Given the notes file exists, when a new cycle starts, then the file's contents are included in the system prompt
- Given the notes file does not exist yet, when the first cycle completes, then the file is created with the first entry
- Given notes appending fails due to a filesystem error, when the cycle proceeds, then the failure is logged but the cycle is not interrupted

---

## Mission Control

### US-09: As the agent, I need to detect when the mission is complete and stop cycling, so that the agent does not keep running after its work is done

The agent signals mission completion by including a specific phrase in its final output. When the framework detects this signal, it stops the loop and records the run as successful. This prevents wasted compute on a finished mission.

#### Acceptance Criteria
- Given the agent's output contains a completion signal (case-insensitive), when the control phase checks it, then the run ends with a "complete" status
- Given the agent's output does not contain a completion signal, when the control phase checks it, then the next cycle begins

### US-10: As the agent, I need to signal when it is blocked by something requiring human intervention, so that the run stops and the operator is notified

Some problems cannot be resolved autonomously (missing credentials, external access, architectural decisions). The agent signals this by including a blockage phrase in its output. The framework stops the loop and records the run as blocked.

#### Acceptance Criteria
- Given the agent's output contains a blockage signal, when the control phase checks it, then the run ends with a "blocked" status
- Given the run ends due to blockage, when the final summary is written, then the agent's explanation of what it needs is preserved

### US-11: As the agent, I need a maximum cycle limit, so that the run cannot consume unbounded resources

Even if the agent never signals completion or blockage, the run must eventually stop. The maximum cycle count acts as a hard budget ceiling.

#### Acceptance Criteria
- Given the maximum cycle count is reached, when the control phase checks it, then the run ends with an "exhausted" status
- Given the maximum cycle count is configured as one, when the agent starts, then exactly one cycle executes

### US-12: As the agent, I need to be pausable via a file-based signal, so that the operator can temporarily halt the agent without losing state

The operator can pause the agent by creating a specific file in the workspace. The agent finishes its current cycle and then waits until the file is removed. This allows the operator to inspect the workspace, make manual changes, or simply pause compute usage.

#### Acceptance Criteria
- Given the pause file exists after a cycle completes, when the agent checks for it, then the agent sleeps until the file is removed
- Given the pause file is removed while the agent is waiting, when the agent detects this, then it resumes with the next cycle
- Given a shutdown signal is received while the agent is paused, when the agent processes it, then the agent exits instead of continuing to wait

---

## Mode Selection

### US-13: As the agent, I need a one-shot mode where the mission completion signal stops the run, so that finite missions terminate naturally

In one-shot mode, the agent works toward a defined goal and stops when it declares the mission complete. The completion rules instruct the agent to signal when the mission is done or blocked.

#### Acceptance Criteria
- Given the agent is in one-shot mode, when it outputs a completion signal, then the run ends immediately
- Given the agent is in one-shot mode, when it outputs a blockage signal, then the run ends immediately

### US-14: As the agent, I need a continuous mode where the completion signal is ignored, so that standing maintenance missions run indefinitely

In continuous mode, the agent keeps cycling even after it has addressed the current focus area. Instead of stopping, it explores the codebase for new improvements. The run only ends at the cycle limit, on blockage, or on operator shutdown.

#### Acceptance Criteria
- Given the agent is in continuous mode, when it outputs a completion signal, then the run continues to the next cycle
- Given the agent is in continuous mode, when it outputs a blockage signal, then the run ends
- Given the agent is in continuous mode, when the cycle instructions are built, then they tell the agent to look for new improvements rather than declaring completion

---

## Artifacts

### US-15: As a cycle, I need my full output, tool execution log, and any hook failures persisted to disk, so that every cycle's work is auditable

Each cycle produces three categories of artifacts: the agent's text output, a structured log of every tool invocation, and (if applicable) a record of which verification hooks failed. These are written to the cycle's subdirectory in the run's artifact tree.

#### Acceptance Criteria
- Given a cycle completes, when artifacts are persisted, then the agent's text output is written to a file in the cycle's directory
- Given a cycle completes, when artifacts are persisted, then the structured tool execution log is written to a file in the cycle's directory
- Given a cycle had hook failures, when artifacts are persisted, then the failure descriptions are written to a separate file
- Given artifact persistence fails, when the error is caught, then it is logged but the cycle is not interrupted

### US-16: As a cycle, I need a compact summary extracted from my output, so that the persistent notes stay concise

The notes file would grow unboundedly if the full agent output were appended each cycle. Instead, a compact summary is built from the tail of the agent's output, tool usage statistics, and hook results. This keeps the notes file manageable while preserving the essential information.

#### Acceptance Criteria
- Given the agent produced a long output, when the summary is extracted, then only the trailing portion of the output is used
- Given the agent used various tools, when the summary is extracted, then the top tools and their call counts are listed
- Given hooks failed during the cycle, when the summary is extracted, then the failures are mentioned in the summary

---

## Startup Checkpoint

### US-17: As the agent, I need a strategic direction analysis at startup, so that the first cycle has informed priorities instead of exploring blindly

When the agent starts and prior notes or score history exist, the framework runs a strategic review before the first cycle. This gives the agent direction from cycle one, based on the accumulated history from previous sessions. Unlike periodic checkpoints, the startup checkpoint does not perform maintenance actions.

#### Acceptance Criteria
- Given prior notes exist when the agent starts, when the startup checkpoint runs, then a strategic direction is produced and injected into the system prompt for cycle one
- Given no prior notes or scores exist, when the startup checkpoint would run, then the first cycle proceeds with orientation instructions only
- Given the startup checkpoint runs, when it completes, then no maintenance actions (squash, tag) are performed

---

## Periodic Checkpoint

### US-18: As the agent, I need periodic strategic reviews at a configurable interval, so that long-running missions receive course corrections based on quality trends

Every N cycles, the framework runs a checkpoint that analyses evaluation score trends, recent version control history, and persistent notes to produce updated strategic direction. This direction is injected into the system prompt for subsequent cycles, steering the agent's priorities.

#### Acceptance Criteria
- Given the checkpoint interval is five and cycle five completes, when the checkpoint runs, then a strategic direction review is produced
- Given the checkpoint interval is zero, when cycles complete, then no periodic checkpoints run (startup checkpoint still runs)
- Given the strategic direction changes at a checkpoint, when the next cycle starts, then the updated direction appears in the system prompt

### US-19: As the agent, I need old notes compressed at checkpoint time, so that the notes file does not grow without bound across many cycles

When the notes file accumulates many cycle entries, older entries are compressed into a concise summary by the framework, while recent entries are preserved verbatim. This keeps the notes file within a manageable size without losing historical context.

#### Acceptance Criteria
- Given the notes file has many old entries beyond the retention threshold, when a checkpoint runs, then the old entries are replaced with a compressed summary
- Given the notes file has fewer old entries than the compression threshold, when a checkpoint runs, then no compression occurs
- Given compression runs, when the notes file is rewritten, then the most recent entries are preserved unchanged

### US-20: As the agent, I need checkpoint maintenance actions (squash and tag) to run at the same interval as the strategic review, so that version control hygiene is maintained on a predictable schedule

At each periodic checkpoint, the framework may also squash recent commits into logical groups and tag the current state. These are optional, independently togglable features. Squash groups commits by semantic meaning; tagging marks the checkpoint for later reference.

#### Acceptance Criteria
- Given auto-squash is enabled, when a periodic checkpoint runs, then the framework analyses recent commits and squashes them if appropriate
- Given auto-tag is enabled, when a periodic checkpoint runs, then the current state is tagged with a checkpoint identifier
- Given auto-squash is disabled, when a periodic checkpoint runs, then no squash is attempted
- Given the squash analysis determines all commits are independent, when the result is evaluated, then no squash is performed

---

## Error Handling

### US-21: As the agent, I need the run to survive a cycle crash without losing prior progress, so that transient errors do not invalidate an entire multi-cycle run

If the tool-use dialogue crashes during a cycle (network error, provider outage, etc.), the run should record the failure and stop cleanly rather than losing all prior committed work. Previously committed changes and persisted artifacts are unaffected.

#### Acceptance Criteria
- Given the execution phase of a cycle throws an unexpected error, when the error is caught, then the run ends with a "blocked" status and the crash details are recorded
- Given previous cycles completed successfully before the crash, when the run ends, then their commits and artifacts remain intact
