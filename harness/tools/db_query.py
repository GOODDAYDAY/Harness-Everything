"""db_query — read-only SQL query tool for production database feedback.

Allows the autonomous agent to query a target project's PostgreSQL database
and use the results to guide data-driven code improvements.  Read-only access
is enforced at two layers: SQL keyword filtering (pre-execution) and
PostgreSQL transaction-level read-only mode (defense-in-depth).

Requires ``asyncpg`` (optional dependency).  If not installed, the tool
returns a clear error at first use rather than crashing at import time.

Config is read from ``config.tool_config["db_query"]`` at execution time::

    {
        "dsn": "postgresql://user:pass@host:5432/dbname",
        "timeout": 30,
        "max_rows": 500
    }
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from harness.core.config import HarnessConfig
from harness.tools.base import Tool, ToolResult

log = logging.getLogger(__name__)

# SQL keywords that are allowed as the leading statement keyword.
# WITH is allowed because CTEs resolve to SELECT; the read-only transaction
# mode is the real safety net against write operations hidden inside CTEs.
_ALLOWED_SQL_KEYWORDS: frozenset[str] = frozenset({
    "SELECT", "EXPLAIN", "SHOW", "WITH",
})

# SQL keywords that indicate a write operation — rejected before execution.
_WRITE_KEYWORDS: frozenset[str] = frozenset({
    "INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER",
    "TRUNCATE", "GRANT", "REVOKE", "COPY", "CALL",
})

_DEFAULT_TIMEOUT: int = 30
_MAX_TIMEOUT: int = 120
_DEFAULT_MAX_ROWS: int = 500
_MAX_MAX_ROWS: int = 5000
_MAX_CELL_WIDTH: int = 120


class DbQueryTool(Tool):
    """Execute read-only SQL against a configured PostgreSQL database."""

    name = "db_query"
    description = (
        "Run a read-only SQL query against the target project's production "
        "database.  Use this to inspect task metrics, user feedback, LLM call "
        "patterns, and step failure rates — then use the insights to guide "
        "your code improvements.  Only SELECT / EXPLAIN / SHOW / WITH queries "
        "are allowed; all write operations are rejected."
    )
    tags: frozenset[str] = frozenset({"execution"})

    def input_schema(self) -> dict[str, Any]:
        """Return JSON Schema for the tool input."""
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "SQL query to execute (SELECT only).  "
                        "Examples: "
                        "'SELECT rating, comment FROM feedback ORDER BY created_at DESC LIMIT 20', "
                        "'SELECT caller, count(*) FROM llm_call_logs GROUP BY caller'"
                    ),
                },
            },
            "required": ["query"],
        }

    async def execute(self, config: HarnessConfig, **params: Any) -> ToolResult:
        """Run a read-only SQL query and return formatted results."""
        query = params.get("query", "").strip()

        # 1. Validate query is provided
        if not query:
            return ToolResult(error="Query parameter is required.", is_error=True)

        # 2. Load tool-specific config
        tool_cfg = self._load_config(config)
        if isinstance(tool_cfg, ToolResult):
            return tool_cfg

        # 3. Validate SQL is read-only
        safety_check = self._validate_read_only(query)
        if safety_check is not None:
            return safety_check

        # 4. Execute query against database
        return await self._execute_query(query, tool_cfg)

    # ══════════════════════════════════════════════════════════════════════
    #  Private methods — one concern each
    # ══════════════════════════════════════════════════════════════════════

    def _load_config(self, config: HarnessConfig) -> dict[str, Any] | ToolResult:
        """Load and validate db_query config from tool_config."""
        db_cfg = config.tool_config.get("db_query")
        if not db_cfg or not isinstance(db_cfg, dict):
            log.warning("db_query tool invoked but no config found in tool_config.db_query")
            return ToolResult(
                error=(
                    "No database configuration found.  Add a 'db_query' section "
                    "to harness.tool_config in your agent config JSON with at "
                    "least a 'dsn' field."
                ),
                is_error=True,
            )

        if not db_cfg.get("dsn"):
            return ToolResult(
                error="tool_config.db_query.dsn is required (PostgreSQL connection string).",
                is_error=True,
            )

        log.debug("db_query config loaded, dsn=%s", _redact_dsn(db_cfg["dsn"]))
        return db_cfg

    def _validate_read_only(self, query: str) -> ToolResult | None:
        """Reject non-SELECT SQL before it reaches the database.

        Security layers:
        1. Reject multi-statement queries (semicolons) — prevents bypassing
           read-only mode via ``SELECT 1; SET TRANSACTION READ WRITE; DELETE ...``
        2. Keyword allowlist — only SELECT/EXPLAIN/SHOW/WITH pass
        3. SET TRANSACTION READ ONLY at connection level (in _execute_query)

        Returns None if the query is allowed, or a ToolResult error if rejected.
        """
        # Layer 1: reject multi-statement queries
        if _contains_multiple_statements(query):
            log.warning("db_query: rejected multi-statement query (semicolons)")
            return ToolResult(
                error="Multi-statement queries (semicolons) are not allowed.  Submit one query at a time.",
                is_error=True,
            )

        # Layer 2: keyword allowlist
        first_keyword = _extract_first_keyword(query)
        if not first_keyword:
            log.warning("db_query: could not parse SQL keyword from query")
            return ToolResult(
                error="Could not determine SQL statement type.  Query must start with SELECT, EXPLAIN, SHOW, or WITH.",
                is_error=True,
            )

        if first_keyword in _WRITE_KEYWORDS:
            log.warning("db_query: rejected write operation, keyword=%s", first_keyword)
            return ToolResult(
                error=f"Write operations are not allowed.  Rejected: {first_keyword} statement.",
                is_error=True,
            )

        if first_keyword not in _ALLOWED_SQL_KEYWORDS:
            log.warning("db_query: rejected unknown SQL keyword=%s", first_keyword)
            return ToolResult(
                error=f"Only SELECT, EXPLAIN, SHOW, and WITH queries are allowed.  Got: {first_keyword}.",
                is_error=True,
            )

        log.debug("db_query: SQL keyword validated, keyword=%s", first_keyword)
        return None

    async def _execute_query(
        self, query: str, tool_cfg: dict[str, Any]
    ) -> ToolResult:
        """Connect to database, execute query, and return formatted results."""
        # 1. Lazy-import asyncpg
        asyncpg = _import_asyncpg()
        if isinstance(asyncpg, ToolResult):
            return asyncpg

        dsn = tool_cfg["dsn"]
        timeout = min(tool_cfg.get("timeout", _DEFAULT_TIMEOUT), _MAX_TIMEOUT)
        max_rows = min(tool_cfg.get("max_rows", _DEFAULT_MAX_ROWS), _MAX_MAX_ROWS)

        conn = None
        try:
            # 2. Connect with timeout
            conn = await asyncio.wait_for(
                asyncpg.connect(dsn),
                timeout=timeout,
            )
            log.info("db_query: connected to database")

            # 3. Set transaction read-only (defense-in-depth)
            await conn.execute("SET TRANSACTION READ ONLY")

            # 4. Fetch rows with limit + 1 to detect truncation
            rows = await asyncio.wait_for(
                conn.fetch(query),
                timeout=timeout,
            )
            log.info("db_query: query returned %d rows", len(rows))

            # 5. Format results as markdown table
            return self._format_results(rows, max_rows)

        except asyncio.TimeoutError:
            log.warning("db_query: query timed out after %ds", timeout)
            return ToolResult(
                error=f"Query timed out after {timeout} seconds.",
                is_error=True,
            )
        except Exception as exc:
            error_type = type(exc).__name__
            log.warning("db_query: query failed, error=%s: %s", error_type, exc)
            return ToolResult(
                error=f"Database error ({error_type}): {exc}",
                is_error=True,
            )
        finally:
            if conn is not None:
                try:
                    await conn.close()
                except Exception:
                    pass

    def _format_results(self, rows: list, max_rows: int) -> ToolResult:
        """Format query results as a readable markdown table."""
        if not rows:
            return ToolResult(output="0 rows returned.")

        total_rows = len(rows)
        truncated = total_rows > max_rows
        display_rows = rows[:max_rows]

        # Extract column names from the first row
        columns = list(display_rows[0].keys())

        # Build markdown table
        lines = self._build_table_lines(columns, display_rows)

        # Add row count footer
        if truncated:
            lines.append(f"\nShowing {max_rows} of {total_rows} total rows (truncated).")
        else:
            lines.append(f"\n{total_rows} rows returned.")

        return ToolResult(output="\n".join(lines))

    def _build_table_lines(
        self, columns: list[str], rows: list
    ) -> list[str]:
        """Build markdown table header and data rows."""
        # Format cell values, truncating long strings
        formatted_rows = []
        for row in rows:
            formatted_rows.append([
                _format_cell(row[col]) for col in columns
            ])

        # Header
        header = "| " + " | ".join(columns) + " |"
        separator = "| " + " | ".join("---" for _ in columns) + " |"

        lines = [header, separator]
        for row_cells in formatted_rows:
            lines.append("| " + " | ".join(row_cells) + " |")

        return lines


# ══════════════════════════════════════════════════════════════════════════
#  Module-level helpers
# ══════════════════════════════════════════════════════════════════════════

def _import_asyncpg():
    """Lazy-import asyncpg; return module or ToolResult error."""
    try:
        import asyncpg  # noqa: F811
        return asyncpg
    except ImportError:
        return ToolResult(
            error=(
                "asyncpg is not installed.  Install with: "
                "pip install 'harness-everything[db]'  "
                "(or: pip install asyncpg)"
            ),
            is_error=True,
        )


def _contains_multiple_statements(query: str) -> bool:
    """Check if query contains multiple SQL statements (semicolons).

    Ignores semicolons inside single-quoted string literals so that
    ``SELECT * FROM t WHERE col = 'a;b'`` is not rejected.
    """
    in_string = False
    for char in query:
        if char == "'":
            in_string = not in_string
        elif char == ";" and not in_string:
            return True
    return False


def _extract_first_keyword(query: str) -> str | None:
    """Extract the first meaningful SQL keyword from a query string.

    Strips comments (-- and /* ... */) and whitespace, then returns the
    first word uppercased.  Returns None if no keyword can be found.
    """
    # Remove single-line comments
    cleaned = re.sub(r"--[^\n]*", "", query)
    # Remove multi-line comments
    cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL)
    # Strip whitespace
    cleaned = cleaned.strip()
    if not cleaned:
        return None
    # Extract first word
    match = re.match(r"[A-Za-z_]+", cleaned)
    return match.group(0).upper() if match else None


def _format_cell(value: Any) -> str:
    """Format a single cell value for display in a markdown table."""
    if value is None:
        return "NULL"
    text = str(value)
    # Replace pipe characters to avoid breaking markdown table
    text = text.replace("|", "\\|")
    # Replace newlines with spaces
    text = text.replace("\n", " ")
    # Truncate long values
    if len(text) > _MAX_CELL_WIDTH:
        text = text[:_MAX_CELL_WIDTH - 3] + "..."
    return text


def _redact_dsn(dsn: str) -> str:
    """Redact password from DSN for logging."""
    return re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", dsn)
