"""web_search — search the web and optionally fetch page content.

Uses the DuckDuckGo HTML search endpoint (no API key required, pure stdlib).

Two sub-operations
------------------
* **search** (default): query DuckDuckGo, return a ranked list of
  title + URL + snippet entries.
* **fetch**: download a single URL and return a cleaned text extract
  (HTML tags stripped, boilerplate collapsed).

Design goals
------------
* Zero extra dependencies — ``urllib``, ``html.parser``, ``re``, stdlib only.
* Async-safe — network I/O runs in a thread-pool executor so the event loop
  is never blocked.
* LLM-friendly output — results are compactly formatted; long pages are
  truncated to ``max_chars`` so they fit inside a context window.
* Fail-safe — network errors and HTTP failures return a ToolResult with
  ``is_error=True`` and a descriptive message; they never raise.

Limitations
-----------
* DuckDuckGo's HTML endpoint does not require authentication but may rate-
  limit aggressive scrapers.  One search per tool call is well within normal
  usage.
* JavaScript-rendered pages cannot be fetched (no headless browser).
  Static HTML, docs pages, GitHub READMEs, and similar work well.
"""

from __future__ import annotations

import asyncio
import html
import html.parser
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from harness.core.config import HarnessConfig
from harness.tools.base import Tool, ToolResult

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DDG_URL = "https://html.duckduckgo.com/html/"
_DEFAULT_TIMEOUT = 15          # seconds per HTTP request
_DEFAULT_MAX_RESULTS = 8       # search results returned
_DEFAULT_MAX_CHARS = 12_000    # max chars for fetched page content
_MAX_SNIPPET_CHARS = 300       # max chars per search result snippet
_USER_AGENT = (
    "Mozilla/5.0 (compatible; HarnessEverything/1.0; +https://github.com/harness-everything)"
)

# ---------------------------------------------------------------------------
# Lightweight HTML → plain-text extractor
# ---------------------------------------------------------------------------

# Tags whose content we discard entirely (scripts, styles, nav boilerplate)
_SKIP_TAGS = frozenset({
    "script", "style", "noscript", "nav", "footer", "header",
    "aside", "form", "button", "input", "select", "textarea",
    "svg", "canvas", "iframe", "object", "embed",
})

# Block-level tags that should produce a newline in the output
_BLOCK_TAGS = frozenset({
    "p", "div", "section", "article", "main", "li", "tr", "td", "th",
    "h1", "h2", "h3", "h4", "h5", "h6", "br", "hr",
    "blockquote", "pre", "code",
})


class _HTMLTextExtractor(html.parser.HTMLParser):
    """Minimal HTML → plain-text converter using stdlib html.parser.

    Strips all tags, decodes entities, collapses whitespace, and inserts
    line-breaks at block-level elements so the result is readable prose.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth: int = 0   # > 0 means we are inside a skipped tag
        self._pending_newline: bool = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag in _BLOCK_TAGS:
            self._pending_newline = True

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag in _BLOCK_TAGS:
            self._pending_newline = True

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        text = data
        if not text.strip():
            return
        if self._pending_newline:
            self._parts.append("\n")
            self._pending_newline = False
        self._parts.append(text)

    def get_text(self) -> str:
        raw = "".join(self._parts)
        # Collapse runs of whitespace within lines; preserve meaningful newlines
        lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in raw.splitlines()]
        # Collapse more than two consecutive blank lines
        result_lines: list[str] = []
        blank_run = 0
        for ln in lines:
            if ln == "":
                blank_run += 1
                if blank_run <= 2:
                    result_lines.append(ln)
            else:
                blank_run = 0
                result_lines.append(ln)
        return "\n".join(result_lines).strip()


def _html_to_text(markup: str) -> str:
    """Convert an HTML string to readable plain text."""
    extractor = _HTMLTextExtractor()
    try:
        extractor.feed(markup)
    except Exception:
        # Malformed HTML is common — best-effort
        pass
    return extractor.get_text()


# ---------------------------------------------------------------------------
# DuckDuckGo result parser
# ---------------------------------------------------------------------------

class _DDGResult:
    """One search result entry."""
    __slots__ = ("title", "url", "snippet")

    def __init__(self, title: str, url: str, snippet: str) -> None:
        self.title = title
        self.url = url
        self.snippet = snippet


class _DDGParser(html.parser.HTMLParser):
    """Parse DuckDuckGo HTML search results page.

    DuckDuckGo's HTML interface wraps results in a predictable structure:

    ``<div class="result__body">``
    ``  <h2 class="result__title"><a class="result__a" href="...">TITLE</a></h2>``
    ``  <a class="result__snippet">SNIPPET</a>``
    ``</div>``

    This parser extracts title, URL, and snippet for each result.
    Class names have been stable for several years; we fall back gracefully
    when they change.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[_DDGResult] = []
        self._in_title: bool = False
        self._in_snippet: bool = False
        self._current_url: str = ""
        self._current_title: str = ""
        self._current_snippet: str = ""

    def handle_starttag(self, tag: str, attrs: list) -> None:
        attr_dict = dict(attrs)
        classes = attr_dict.get("class", "")

        if tag == "a" and "result__a" in classes:
            raw_href = attr_dict.get("href", "")
            # DDG sometimes encodes the real URL in a redirect wrapper;
            # try to extract the ``uddg`` query param which holds the real URL.
            self._current_url = _extract_ddg_url(raw_href)
            self._in_title = True
            self._current_title = ""

        elif tag in ("a", "div") and "result__snippet" in classes:
            self._in_snippet = True
            self._current_snippet = ""

    def handle_endtag(self, tag: str) -> None:
        if self._in_title and tag == "a":
            self._in_title = False

        if self._in_snippet and tag in ("a", "div"):
            self._in_snippet = False
            # Commit completed result when we have all three parts
            if self._current_url and self._current_title:
                self.results.append(
                    _DDGResult(
                        title=self._current_title.strip(),
                        url=self._current_url,
                        snippet=self._current_snippet.strip()[:_MAX_SNIPPET_CHARS],
                    )
                )
                self._current_url = ""
                self._current_title = ""
                self._current_snippet = ""

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._current_title += data
        elif self._in_snippet:
            self._current_snippet += data


def _extract_ddg_url(href: str) -> str:
    """Unwrap a DuckDuckGo redirect URL to the real destination.

    DDG sometimes uses ``/l/?uddg=<encoded-url>&...`` redirect links.
    We decode the ``uddg`` parameter when present; otherwise return the
    href as-is (which is usually a direct URL for the HTML endpoint).
    """
    if not href:
        return ""
    if href.startswith("/l/?") or href.startswith("//duckduckgo.com/l/?"):
        try:
            # Make it parseable by adding a scheme if needed
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                href = "https://duckduckgo.com" + href
            parsed = urllib.parse.urlparse(href)
            params = urllib.parse.parse_qs(parsed.query)
            uddg = params.get("uddg", [""])[0]
            if uddg:
                return urllib.parse.unquote(uddg)
        except Exception:
            pass
    return href


# ---------------------------------------------------------------------------
# Network helpers (run in executor to avoid blocking the event loop)
# ---------------------------------------------------------------------------


def _http_get(url: str, *, timeout: int, extra_headers: dict[str, str] | None = None) -> bytes:
    """Synchronous HTTP GET; raises ``urllib.error.URLError`` on failure."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    if extra_headers:
        for k, v in extra_headers.items():
            req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read()


def _ddg_search_sync(query: str, max_results: int, timeout: int) -> list[_DDGResult]:
    """Perform a DuckDuckGo HTML search synchronously.

    Sends a POST to the HTML endpoint (same as the browser form) and parses
    the returned HTML for result entries.
    """
    params = urllib.parse.urlencode({"q": query, "b": "", "kl": "us-en"})
    data = params.encode("utf-8")

    req = urllib.request.Request(
        _DDG_URL,
        data=data,
        headers={
            "User-Agent": _USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        raw = resp.read()

    # DDG returns UTF-8; fall back to latin-1 for malformed bytes
    try:
        markup = raw.decode("utf-8")
    except UnicodeDecodeError:
        markup = raw.decode("latin-1", errors="replace")

    parser = _DDGParser()
    try:
        parser.feed(markup)
    except Exception as exc:
        log.warning("web_search: DDG HTML parse warning: %s", exc)

    return parser.results[:max_results]


def _fetch_page_sync(url: str, timeout: int, max_chars: int) -> str:
    """Fetch a URL and return extracted plain text (synchronous)."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,text/plain",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        # Respect content-type charset when available
        content_type = resp.headers.get_content_type() or ""
        charset = resp.headers.get_content_charset("utf-8") or "utf-8"
        raw = resp.read()

    if "text/html" in content_type or not content_type:
        try:
            markup = raw.decode(charset, errors="replace")
        except LookupError:
            markup = raw.decode("utf-8", errors="replace")
        text = _html_to_text(markup)
    else:
        # Plain text, JSON, etc.
        try:
            text = raw.decode(charset, errors="replace")
        except LookupError:
            text = raw.decode("utf-8", errors="replace")

    if len(text) > max_chars:
        # Keep a head + tail excerpt so both page intro and relevant content
        # near the bottom are visible.
        head = text[: max_chars // 2]
        tail = text[-(max_chars // 2):]
        omitted = len(text) - max_chars
        text = (
            head
            + f"\n\n... [{omitted} chars omitted — page truncated to fit context] ...\n\n"
            + tail
        )

    return text


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class WebSearchTool(Tool):
    """Search the web via DuckDuckGo and optionally fetch page content.

    Two operations (selected via ``action`` parameter):

    **search** (default)
        Query DuckDuckGo and return a ranked list of results with title,
        URL, and snippet.  No API key required.  Uses the public HTML
        endpoint — same data the browser sees.

    **fetch**
        Download a single URL and return extracted plain text (HTML stripped,
        boilerplate collapsed).  Useful for reading documentation, GitHub
        READMEs, error pages, etc.

    Both operations run in a thread-pool executor so the asyncio event loop
    is never blocked by network I/O.  A configurable ``timeout`` (default
    15 s) caps each request.
    """

    name = "web_search"
    description = (
        "Search the web via DuckDuckGo or fetch the text content of a URL. "
        "action='search' (default): returns ranked results with title, URL, snippet. "
        "action='fetch': downloads a URL and returns cleaned plain text. "
        "No API key required. Uses stdlib urllib — no extra dependencies."
    )
    # No path check needed — this tool accesses the network, not the filesystem.
    requires_path_check = False

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Search query (action='search') or URL to fetch "
                        "(action='fetch')."
                    ),
                },
                "action": {
                    "type": "string",
                    "enum": ["search", "fetch"],
                    "description": (
                        "'search' to query DuckDuckGo (default), "
                        "'fetch' to download and extract text from a URL."
                    ),
                    "default": "search",
                },
                "max_results": {
                    "type": "integer",
                    "description": (
                        "Maximum number of search results to return "
                        "(action='search' only, default: 8)."
                    ),
                    "default": _DEFAULT_MAX_RESULTS,
                },
                "max_chars": {
                    "type": "integer",
                    "description": (
                        "Maximum characters of page text to return "
                        "(action='fetch' only, default: 12000)."
                    ),
                    "default": _DEFAULT_MAX_CHARS,
                },
                "timeout": {
                    "type": "integer",
                    "description": "HTTP request timeout in seconds (default: 15).",
                    "default": _DEFAULT_TIMEOUT,
                },
            },
            "required": ["query"],
        }

    async def execute(
        self,
        config: HarnessConfig,
        *,
        query: str,
        action: str = "search",
        max_results: int = _DEFAULT_MAX_RESULTS,
        max_chars: int = _DEFAULT_MAX_CHARS,
        timeout: int = _DEFAULT_TIMEOUT,
    ) -> ToolResult:
        """Execute a web search or page fetch asynchronously."""
        if not query.strip():
            return ToolResult(error="query must not be empty", is_error=True)

        # Use get_running_loop() (not the deprecated get_event_loop()) to
        # obtain the loop that is currently executing this coroutine.  In
        # Python >= 3.10, get_event_loop() emits DeprecationWarnings inside
        # a running loop and may return a different loop object entirely.
        loop = asyncio.get_running_loop()

        if action == "fetch":
            return await self._fetch(loop, query, max_chars, timeout)

        if action == "search":
            return await self._search(loop, query, max_results, timeout)

        return ToolResult(
            error=f"Unknown action {action!r}. Use 'search' or 'fetch'.",
            is_error=True,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _search(
        self,
        loop: asyncio.AbstractEventLoop,
        query: str,
        max_results: int,
        timeout: int,
    ) -> ToolResult:
        log.info("web_search: query=%r max_results=%d", query, max_results)
        try:
            results: list[_DDGResult] = await loop.run_in_executor(
                None,
                lambda: _ddg_search_sync(query, max_results, timeout),
            )
        except urllib.error.HTTPError as exc:
            return ToolResult(
                error=f"HTTP {exc.code} from DuckDuckGo: {exc.reason}",
                is_error=True,
            )
        except urllib.error.URLError as exc:
            return ToolResult(
                error=f"Network error reaching DuckDuckGo: {exc.reason}",
                is_error=True,
            )
        except TimeoutError:
            return ToolResult(
                error=f"DuckDuckGo search timed out after {timeout}s",
                is_error=True,
            )
        except Exception as exc:
            return ToolResult(
                error=f"web_search failed: {type(exc).__name__}: {exc}",
                is_error=True,
            )

        if not results:
            return ToolResult(
                output=f"No results found for query: {query!r}\n"
                       "(DuckDuckGo returned an empty result page — "
                       "try rephrasing the query.)"
            )

        lines: list[str] = [
            f"DuckDuckGo results for: {query!r}  ({len(results)} result(s))\n"
        ]
        for i, r in enumerate(results, 1):
            lines.append(f"[{i}] {r.title}")
            lines.append(f"    URL: {r.url}")
            if r.snippet:
                lines.append(f"    {r.snippet}")
            lines.append("")

        log.info("web_search: got %d results for %r", len(results), query)
        return ToolResult(output="\n".join(lines).rstrip())

    async def _fetch(
        self,
        loop: asyncio.AbstractEventLoop,
        url: str,
        max_chars: int,
        timeout: int,
    ) -> ToolResult:
        log.info("web_search: fetch url=%r", url)
        # Basic URL validation — must start with http:// or https://
        if not (url.startswith("http://") or url.startswith("https://")):
            return ToolResult(
                error=f"Invalid URL {url!r}: must start with http:// or https://",
                is_error=True,
            )
        try:
            text: str = await loop.run_in_executor(
                None,
                lambda: _fetch_page_sync(url, timeout, max_chars),
            )
        except urllib.error.HTTPError as exc:
            return ToolResult(
                error=f"HTTP {exc.code} fetching {url}: {exc.reason}",
                is_error=True,
            )
        except urllib.error.URLError as exc:
            return ToolResult(
                error=f"Network error fetching {url}: {exc.reason}",
                is_error=True,
            )
        except TimeoutError:
            return ToolResult(
                error=f"Fetch timed out after {timeout}s for {url}",
                is_error=True,
            )
        except Exception as exc:
            return ToolResult(
                error=f"web_search fetch failed: {type(exc).__name__}: {exc}",
                is_error=True,
            )

        if not text.strip():
            return ToolResult(
                output=f"[Fetched {url} — page appears empty or content could not be extracted]"
            )

        header = f"[Fetched: {url}]\n{'─' * 60}\n"
        log.info("web_search: fetched %d chars from %r", len(text), url)
        return ToolResult(output=header + text)
