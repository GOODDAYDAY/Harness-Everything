"""http_client — generic HTTP request tool (GET/POST/PUT/DELETE/PATCH).

Sends HTTP requests using the stdlib ``urllib`` module only — zero extra
dependencies.  Runs the synchronous network call in a thread-pool executor
so the asyncio event loop is never blocked.

Design goals
------------
* **Method support**: GET, POST, PUT, DELETE, PATCH, HEAD.
* **Headers**: arbitrary key/value request headers.
* **Body**: string or JSON-serializable dict body; ``Content-Type`` is
  automatically set to ``application/json`` when a dict body is provided.
* **Response truncation**: long responses are truncated to ``max_chars`` with
  a note, so the LLM context window is not saturated by huge responses.
* **Fail-safe**: network errors, HTTP errors, and timeouts all return a
  ``ToolResult`` with ``is_error=True`` and a descriptive message; they
  never raise out of ``execute``.

Security note
-------------
This tool performs **outbound network requests**.  It is registered in
``DEFAULT_TOOLS`` but callers should ensure the harness is not run in
air-gapped or restricted network environments when they enable this tool.
No path-traversal or workspace-access concern applies (no filesystem access).
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from harness.core.config import HarnessConfig
from harness.tools.base import Tool, ToolResult

log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30           # seconds
_DEFAULT_MAX_CHARS = 16_000     # characters returned from the response body
_ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"})

_USER_AGENT = "HarnessHTTPClient/1.0 (stdlib urllib)"


# ---------------------------------------------------------------------------
# Synchronous HTTP implementation (runs in executor)
# ---------------------------------------------------------------------------


def _do_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    body: str | None,
    timeout: int,
    max_chars: int,
) -> dict[str, Any]:
    """Perform the HTTP request synchronously and return a result dict.

    Returns a dict with keys:
    - ``status``: HTTP status code (int)
    - ``reason``: HTTP reason phrase (str)
    - ``headers``: response headers as ``{name: value}`` dict (str→str)
    - ``body``: response body text, truncated to *max_chars* if necessary
    - ``truncated``: bool — True when the body was truncated
    - ``url``: final URL after any redirects
    """
    req_headers = {"User-Agent": _USER_AGENT}
    req_headers.update(headers)

    data: bytes | None = None
    if body is not None:
        data = body.encode("utf-8") if isinstance(body, str) else body
        if "Content-Type" not in req_headers:
            req_headers["Content-Type"] = "application/octet-stream"

    req = urllib.request.Request(
        url,
        data=data,
        headers=req_headers,
        method=method,
    )

    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        status: int = resp.status
        reason: str = resp.reason
        final_url: str = resp.url
        resp_headers: dict[str, str] = dict(resp.headers)
        raw_bytes: bytes = resp.read()

    # Decode response body
    content_type = resp_headers.get("Content-Type", resp_headers.get("content-type", ""))
    charset = "utf-8"
    if "charset=" in content_type:
        try:
            charset = content_type.split("charset=")[-1].split(";")[0].strip()
        except Exception:
            charset = "utf-8"

    try:
        body_text = raw_bytes.decode(charset, errors="replace")
    except LookupError:
        body_text = raw_bytes.decode("utf-8", errors="replace")

    truncated = False
    if len(body_text) > max_chars:
        body_text = body_text[:max_chars]
        truncated = True

    return {
        "status": status,
        "reason": reason,
        "url": final_url,
        "headers": resp_headers,
        "body": body_text,
        "truncated": truncated,
    }


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class HttpRequestTool(Tool):
    """Make HTTP requests (GET/POST/PUT/DELETE/PATCH/HEAD) using stdlib urllib.

    Supports custom headers, request body (string or JSON dict), and
    configurable timeout.  Response body is returned as text, truncated to
    ``max_chars`` if necessary.  Runs network I/O in a thread-pool executor
    so the event loop is never blocked.
    """

    name = "http_request"
    description = (
        "Make an HTTP request (GET, POST, PUT, DELETE, PATCH, HEAD). "
        "Supports custom headers and a request body (string or JSON dict). "
        "Returns status code, response headers, and body text. "
        "Body is truncated to max_chars if too large. "
        "Uses stdlib urllib — no extra dependencies."
    )
    requires_path_check = False  # no filesystem access
    tags = frozenset({"network"})

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Full URL to request (must start with http:// or https://).",
                },
                "method": {
                    "type": "string",
                    "enum": ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"],
                    "description": "HTTP method (default: GET).",
                    "default": "GET",
                },
                "headers": {
                    "type": "object",
                    "description": (
                        "Additional request headers as a JSON object "
                        "(e.g. {\"Authorization\": \"Bearer token\"}). "
                        "Default: empty."
                    ),
                    "default": {},
                },
                "body": {
                    "type": ["string", "object", "null"],
                    "description": (
                        "Request body. A string is sent as-is; a JSON object is "
                        "serialized to JSON and Content-Type is set to "
                        "application/json automatically. "
                        "Ignored for GET and HEAD. Default: null (no body)."
                    ),
                    "default": None,
                },
                "timeout": {
                    "type": "integer",
                    "description": "Request timeout in seconds (default: 30).",
                    "default": _DEFAULT_TIMEOUT,
                    "minimum": 1,
                    "maximum": 120,
                },
                "max_chars": {
                    "type": "integer",
                    "description": (
                        "Maximum characters of response body to return "
                        "(default: 16000). Responses longer than this are truncated."
                    ),
                    "default": _DEFAULT_MAX_CHARS,
                    "minimum": 1,
                    "maximum": 100_000,
                },
            },
            "required": ["url"],
        }

    async def execute(
        self,
        config: HarnessConfig,
        *,
        url: str,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: str | dict | None = None,
        timeout: int = _DEFAULT_TIMEOUT,
        max_chars: int = _DEFAULT_MAX_CHARS,
    ) -> ToolResult:
        # Validate URL scheme
        if not url or not (url.startswith("http://") or url.startswith("https://")):
            return ToolResult(
                error=f"Invalid URL {url!r}: must start with http:// or https://",
                is_error=True,
            )

        # Normalize method
        method = method.upper()
        if method not in _ALLOWED_METHODS:
            return ToolResult(
                error=(
                    f"Unknown HTTP method {method!r}. "
                    f"Allowed: {sorted(_ALLOWED_METHODS)}"
                ),
                is_error=True,
            )

        # Clamp timeout and max_chars
        timeout = max(1, min(120, timeout))
        max_chars = max(1, min(100_000, max_chars))

        # Normalize headers
        req_headers: dict[str, str] = {}
        if headers:
            req_headers = {str(k): str(v) for k, v in headers.items()}

        # Prepare body
        body_str: str | None = None
        if body is not None:
            if isinstance(body, dict):
                body_str = _json.dumps(body)
                req_headers.setdefault("Content-Type", "application/json")
            else:
                body_str = str(body)

        log.info("http_request: %s %s (timeout=%ds)", method, url, timeout)

        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(
                None,
                lambda: _do_request(
                    method,
                    url,
                    headers=req_headers,
                    body=body_str,
                    timeout=timeout,
                    max_chars=max_chars,
                ),
            )
        except urllib.error.HTTPError as exc:
            # HTTPError is also a valid response — capture status and body
            try:
                raw_error_body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                raw_error_body = ""
            if len(raw_error_body) > max_chars:
                raw_error_body = raw_error_body[:max_chars]
            err_result = {
                "status": exc.code,
                "reason": exc.reason,
                "url": url,
                "headers": dict(exc.headers) if exc.headers else {},
                "body": raw_error_body,
                "truncated": len(raw_error_body) >= max_chars,
                "error": f"HTTP {exc.code} {exc.reason}",
            }
            # Return as an error result with the HTTP response embedded
            return ToolResult(
                output=_json.dumps(err_result),
                error=f"HTTP {exc.code} {exc.reason}",
                is_error=True,
            )
        except urllib.error.URLError as exc:
            return ToolResult(
                error=f"Network error for {url}: {exc.reason}",
                is_error=True,
            )
        except TimeoutError:
            return ToolResult(
                error=f"Request timed out after {timeout}s: {url}",
                is_error=True,
            )
        except OSError as exc:
            return ToolResult(
                error=f"OS error during request: {exc}",
                is_error=True,
            )
        except Exception as exc:
            return ToolResult(
                error=f"http_request failed: {type(exc).__name__}: {exc}",
                is_error=True,
            )

        log.info(
            "http_request: %s %s → %d (%s) body=%d chars truncated=%s",
            method,
            url,
            result["status"],
            result["reason"],
            len(result["body"]),
            result["truncated"],
        )

        # Serialize the full result as JSON
        output = _json.dumps(result)
        return ToolResult(output=output)
