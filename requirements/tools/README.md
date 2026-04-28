# Tools Domain -- Framework and Dispatch

This domain covers the tool framework itself: how tools are registered, discovered, dispatched, and how errors are communicated back to the caller. These are the infrastructure-level concerns that every individual tool relies on.

**Actors:**
- "As a tool" -- the tool's own behavioral contract within the framework
- "As the agent" -- the agent that invokes tools to accomplish tasks

---

## Tool Registration and Discovery

## US-01: As a tool, I need to be registered in a central catalog so that the agent can discover and invoke me by name

Every tool declares its identity (name and description) and is collected into a single catalog at startup. Without registration, a tool is invisible to the agent and cannot be invoked.

### Acceptance Criteria
- Given a tool with a unique name, when the system starts, then the tool appears in the catalog and can be looked up by name
- Given a tool whose name duplicates an already-registered tool, when registration is attempted, then the later registration replaces the earlier one (last-write-wins)
- Given the catalog, when the agent requests the list of available tools, then all registered tools are returned

## US-02: As the agent, I need each tool to declare its input schema so that I know what parameters to supply

Each tool publishes a JSON-compatible description of its accepted parameters, including which are required and which are optional with defaults. This schema is sent to the LLM as part of the API call so it can generate valid invocations.

### Acceptance Criteria
- Given a registered tool, when the agent requests its schema, then a structured description of all parameters (names, types, defaults, descriptions) is returned
- Given the schema, when it is exported for the LLM API, then it includes the tool name, description, and input schema as a single definition

## US-03: As the agent, I need tools partitioned into a default set and an optional set so that only the tools I need are active

Some tools expand the agent's capabilities in ways that are expensive (e.g., network access) or irrelevant for many tasks. The framework provides a default set that is always available and an optional set that must be explicitly enabled.

### Acceptance Criteria
- Given a fresh registry build with no extra configuration, when the agent lists available tools, then only default tools are present
- Given a registry build with extra tool names specified, when the agent lists available tools, then the requested optional tools are also present
- Given an unknown tool name in the extra-tools list, when the registry is built, then a warning is logged and the unknown name is skipped without aborting

## US-04: As the agent, I need to filter tools by capability category so that I can present a focused toolset for specific tasks

Tools are tagged with capability categories (e.g., reading, writing, searching, analysis). The registry can produce a filtered subset containing only tools matching at least one requested category, reducing schema size and cognitive load for the LLM.

### Acceptance Criteria
- Given a filter request for a specific category, when the filtered registry is built, then it contains only tools tagged with that category plus any tools with no tags (backward-compatible defaults)
- Given a filter request for a category with no matching tools, when the filtered registry is built, then it contains only the untagged tools

## US-05: As the agent, I need tools restricted by an allowlist so that in constrained environments only approved tools can execute

The runtime configuration may specify an allowlist of tool names. When present, only tools on the list may be dispatched, even if they are registered. This provides defense-in-depth for restricted execution environments.

### Acceptance Criteria
- Given a non-empty allowlist and a tool invocation for a tool not on the list, when dispatch is attempted, then a permission error is returned
- Given a non-empty allowlist and a tool invocation for an allowed tool, when dispatch is attempted, then the tool executes normally
- Given an empty or absent allowlist, when any registered tool is invoked, then it executes normally (no restriction)

---

## Dispatch and Parameter Handling

## US-06: As the agent, I need tool dispatch to normalize common parameter name mistakes so that my first invocation attempt succeeds

LLMs frequently use alternative parameter names (e.g., "file_path" instead of "path", "old_string" instead of "old_str"). The dispatch layer automatically maps known aliases to the correct parameter names before invoking the tool, avoiding a wasted round-trip.

### Acceptance Criteria
- Given a tool invocation with a known alias (e.g., "file_path" for "path"), when the tool is dispatched, then the alias is silently renamed to the correct parameter and the tool executes successfully
- Given a tool invocation where both the alias and the correct name are present, when the tool is dispatched, then the correct name takes precedence and the alias is not applied
- Given a tool invocation with an alias whose target is not in the tool's schema, when the tool is dispatched, then the alias is left unchanged

## US-07: As the agent, I need dispatch to reject unknown parameters so that I get a clear error instead of a silent misfire

If the agent sends a parameter that does not exist in the tool's schema (a hallucinated parameter), dispatch rejects the call immediately with a list of known parameters, rather than letting the tool silently ignore it or crash deep inside its implementation.

### Acceptance Criteria
- Given a tool invocation with a parameter not in the tool's schema, when dispatch runs, then a schema error is returned listing the unknown parameter(s) and all known parameters
- Given a tool invocation with only valid parameters, when dispatch runs, then no schema error is raised

---

## Error Handling and Reporting

## US-08: As the agent, I need tool errors categorized by kind so that I know whether to fix my parameters, check permissions, or report a failure

Tool errors fall into three distinct categories: schema errors (wrong or missing parameters), permission errors (path outside allowed scope), and tool errors (I/O failures, unexpected exceptions). Each category requires a different corrective action from the agent.

### Acceptance Criteria
- Given a missing required parameter, when the tool is invoked, then a schema error is returned that names the missing parameter and lists all required parameters
- Given a path outside the allowed directories, when a file tool is invoked, then a permission error is returned that identifies the offending path and lists allowed directories
- Given an unexpected exception during tool execution, when the error is caught, then a tool error is returned with the exception type and message

## US-09: As a tool, I need to return a uniform result structure so that the agent can always parse my output the same way

Every tool invocation produces a result with the same shape: output text (on success), error text (on failure), a success/failure flag, and elapsed time. This uniformity means the agent never needs tool-specific result parsing logic.

### Acceptance Criteria
- Given a successful tool execution, when the result is returned, then it contains output text, no error text, the success flag set, and the elapsed time
- Given a failed tool execution, when the result is returned, then it contains error text, the failure flag set, and the elapsed time
- Given any result, when it is formatted for the LLM API, then it produces a text content block with either the output or the error text

## US-10: As a tool, I need to measure my own execution time so that the agent can detect slow operations and optimize its strategy

Each tool invocation is timed from dispatch to completion. The elapsed time is recorded in the result and logged as a structured trace event, enabling performance monitoring and helping the agent prioritize faster alternatives.

### Acceptance Criteria
- Given any tool invocation, when it completes (success or failure), then the result includes the elapsed time in seconds
- Given any tool invocation, when it completes, then a structured trace log entry is emitted containing the tool name, success status, and duration in milliseconds

---

## Output Safety

## US-11: As a tool, I need to safely serialize large outputs so that oversized results do not corrupt the agent's context window

When a tool produces a large structured result (e.g., a cross-reference map), the output is automatically trimmed by progressively halving the largest list fields until the result fits within a size budget. The trimmed result remains valid and includes a truncation indicator.

### Acceptance Criteria
- Given a tool result that fits within the size budget, when serialization runs, then the output is returned unchanged
- Given a tool result that exceeds the size budget, when serialization runs, then list fields are progressively shortened and a truncation flag is set
- Given a tool result that cannot be reduced below the size budget even after maximum trimming, when serialization runs, then a minimal error envelope is returned instead of invalid output
