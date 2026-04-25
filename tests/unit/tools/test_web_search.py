"""Unit tests for harness.tools.web_search.

All network calls are mocked — these tests run entirely offline.
"""
from __future__ import annotations

import asyncio
import urllib.error
from io import BytesIO
from unittest.mock import MagicMock, Mock, patch


from harness.core.config import HarnessConfig
from harness.tools.web_search import (
    WebSearchTool,
    _DDGParser,
    _DDGResult,
    _HTMLTextExtractor,
    _extract_ddg_url,
    _html_to_text,
    _BLOCK_TAGS,
    _SKIP_TAGS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config() -> Mock:
    cfg = Mock(spec=HarnessConfig)
    cfg.workspace = "/tmp"
    cfg.allowed_paths = ["/tmp"]
    return cfg


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# _HTMLTextExtractor tests
# ---------------------------------------------------------------------------

class TestHTMLTextExtractor:
    """Test the HTML → plain text converter."""

    def test_plain_text_passthrough(self):
        """Text nodes without HTML are returned as-is."""
        extractor = _HTMLTextExtractor()
        extractor.feed("Hello world")
        assert extractor.get_text() == "Hello world"

    def test_tags_stripped(self):
        """HTML tags are stripped from output."""
        extractor = _HTMLTextExtractor()
        extractor.feed("<b>bold</b> and <i>italic</i>")
        text = extractor.get_text()
        assert "<b>" not in text
        assert "bold" in text
        assert "italic" in text

    def test_script_content_skipped(self):
        """Content inside <script> tags is excluded."""
        extractor = _HTMLTextExtractor()
        extractor.feed("<p>visible</p><script>var x = 1;</script><p>also visible</p>")
        text = extractor.get_text()
        assert "visible" in text
        assert "var x" not in text

    def test_style_content_skipped(self):
        """Content inside <style> tags is excluded."""
        extractor = _HTMLTextExtractor()
        extractor.feed("<style>body { color: red; }</style><p>text</p>")
        text = extractor.get_text()
        assert "color" not in text
        assert "text" in text

    def test_block_tags_add_newlines(self):
        """Block-level elements produce line breaks in output."""
        extractor = _HTMLTextExtractor()
        extractor.feed("<p>First paragraph</p><p>Second paragraph</p>")
        text = extractor.get_text()
        assert "First paragraph" in text
        assert "Second paragraph" in text
        # Should have a newline between paragraphs
        assert "\n" in text

    def test_entity_decoding(self):
        """HTML entities like &amp; and &lt; are decoded."""
        extractor = _HTMLTextExtractor()
        extractor.feed("<p>AT&amp;T and &lt;example&gt;</p>")
        text = extractor.get_text()
        assert "AT&T" in text
        assert "<example>" in text

    def test_whitespace_collapsed(self):
        """Multiple spaces within a line are collapsed to one."""
        extractor = _HTMLTextExtractor()
        extractor.feed("<p>too   many    spaces</p>")
        text = extractor.get_text()
        assert "too many spaces" in text

    def test_nested_skip_tags(self):
        """Nested skip tags are properly tracked via depth counter."""
        extractor = _HTMLTextExtractor()
        extractor.feed("before<script><script>nested</script></script>after")
        text = extractor.get_text()
        assert "before" in text
        assert "after" in text
        assert "nested" not in text

    def test_empty_html_returns_empty_string(self):
        """Empty HTML returns empty string."""
        extractor = _HTMLTextExtractor()
        extractor.feed("")
        assert extractor.get_text() == ""

    def test_skip_tags_set_contains_expected_elements(self):
        """_SKIP_TAGS contains key boilerplate elements."""
        assert "script" in _SKIP_TAGS
        assert "style" in _SKIP_TAGS
        assert "nav" in _SKIP_TAGS
        assert "footer" in _SKIP_TAGS

    def test_block_tags_set_contains_expected_elements(self):
        """_BLOCK_TAGS contains key block elements."""
        assert "p" in _BLOCK_TAGS
        assert "div" in _BLOCK_TAGS
        assert "h1" in _BLOCK_TAGS
        assert "br" in _BLOCK_TAGS


class TestHtmlToText:
    """Test the _html_to_text() convenience function."""

    def test_simple_html_to_text(self):
        """Basic HTML is converted to readable text."""
        result = _html_to_text("<h1>Title</h1><p>Body text here.</p>")
        assert "Title" in result
        assert "Body text here" in result

    def test_empty_string_returns_empty(self):
        result = _html_to_text("")
        assert result == ""

    def test_malformed_html_handled_gracefully(self):
        """Malformed HTML does not raise exceptions."""
        result = _html_to_text("<p>unclosed tag <b>bold")
        # Should return something, not raise
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# _extract_ddg_url tests
# ---------------------------------------------------------------------------

class TestExtractDdgUrl:
    """Test DuckDuckGo URL extraction."""

    def test_direct_url_returned_as_is(self):
        """Direct URLs (not DDG redirects) are returned unchanged."""
        url = "https://example.com/page"
        assert _extract_ddg_url(url) == url

    def test_empty_string_returns_empty(self):
        assert _extract_ddg_url("") == ""

    def test_ddg_redirect_url_decoded(self):
        """DDG /l/?uddg=... redirect URLs are decoded to the real URL."""
        real_url = "https://example.com/real-page"
        import urllib.parse
        encoded = urllib.parse.quote(real_url)
        redirect = f"/l/?uddg={encoded}&rut=abc"
        result = _extract_ddg_url(redirect)
        assert result == real_url

    def test_ddg_double_slash_redirect_decoded(self):
        """DDG //duckduckgo.com/l/?uddg=... is also decoded."""
        real_url = "https://python.org/docs"
        import urllib.parse
        encoded = urllib.parse.quote(real_url)
        redirect = f"//duckduckgo.com/l/?uddg={encoded}"
        result = _extract_ddg_url(redirect)
        assert result == real_url

    def test_malformed_redirect_returns_original(self):
        """Malformed DDG redirect gracefully returns original href."""
        href = "/l/?malformed=xyz"
        result = _extract_ddg_url(href)
        # No crash; returns something (the original href or empty)
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# _DDGParser tests
# ---------------------------------------------------------------------------

class TestDDGParser:
    """Test the DuckDuckGo HTML result parser."""

    DDG_SAMPLE_HTML = """
    <div class="result__body">
      <h2 class="result__title">
        <a class="result__a" href="https://example.com/page1">Example Title One</a>
      </h2>
      <a class="result__snippet">This is the snippet for result one.</a>
    </div>
    <div class="result__body">
      <h2 class="result__title">
        <a class="result__a" href="https://example.org/page2">Another Page Title</a>
      </h2>
      <a class="result__snippet">Second snippet text here.</a>
    </div>
    """

    def test_parses_two_results(self):
        """Parser extracts two result entries from sample HTML."""
        parser = _DDGParser()
        parser.feed(self.DDG_SAMPLE_HTML)
        assert len(parser.results) == 2

    def test_first_result_title(self):
        """First result has correct title."""
        parser = _DDGParser()
        parser.feed(self.DDG_SAMPLE_HTML)
        assert parser.results[0].title == "Example Title One"

    def test_first_result_url(self):
        """First result has correct URL."""
        parser = _DDGParser()
        parser.feed(self.DDG_SAMPLE_HTML)
        assert parser.results[0].url == "https://example.com/page1"

    def test_first_result_snippet(self):
        """First result has correct snippet."""
        parser = _DDGParser()
        parser.feed(self.DDG_SAMPLE_HTML)
        assert "snippet for result one" in parser.results[0].snippet

    def test_empty_html_returns_no_results(self):
        """Parsing empty HTML returns no results."""
        parser = _DDGParser()
        parser.feed("")
        assert parser.results == []

    def test_html_without_results_returns_empty(self):
        """HTML with no result__a class returns no results."""
        parser = _DDGParser()
        parser.feed("<html><body><p>Nothing here</p></body></html>")
        assert parser.results == []

    def test_snippet_truncated_to_max_chars(self):
        """Very long snippets are truncated to _MAX_SNIPPET_CHARS."""
        from harness.tools.web_search import _MAX_SNIPPET_CHARS
        long_snippet = "x" * (_MAX_SNIPPET_CHARS + 100)
        html = f"""
        <h2 class="result__title">
          <a class="result__a" href="https://example.com">Title</a>
        </h2>
        <a class="result__snippet">{long_snippet}</a>
        """
        parser = _DDGParser()
        parser.feed(html)
        assert len(parser.results) > 0
        assert len(parser.results[0].snippet) <= _MAX_SNIPPET_CHARS


# ---------------------------------------------------------------------------
# WebSearchTool.execute() tests (async, mocked network)
# ---------------------------------------------------------------------------

class TestWebSearchToolValidation:
    """Test input validation in WebSearchTool.execute()."""

    def test_empty_query_returns_error(self):
        """Empty query string returns is_error=True."""
        tool = WebSearchTool()
        result = _run(tool.execute(_make_config(), query="   "))
        assert result.is_error
        assert "empty" in result.error.lower()

    def test_unknown_action_returns_error(self):
        """Unknown action returns is_error=True."""
        tool = WebSearchTool()
        result = _run(tool.execute(_make_config(), query="test", action="invalid_action"))
        assert result.is_error
        assert "invalid_action" in result.error

    def test_fetch_invalid_url_returns_error(self):
        """fetch action with non-http URL returns is_error=True."""
        tool = WebSearchTool()
        result = _run(tool.execute(_make_config(), query="ftp://example.com", action="fetch"))
        assert result.is_error
        assert "http://" in result.error or "https://" in result.error


class TestWebSearchToolSearch:
    """Test the search action with mocked DuckDuckGo."""

    def _make_fake_results(self, n: int = 3) -> list[_DDGResult]:
        return [
            _DDGResult(
                title=f"Result {i}",
                url=f"https://example.com/{i}",
                snippet=f"Snippet for result {i}",
            )
            for i in range(1, n + 1)
        ]

    def test_search_returns_formatted_results(self):
        """Successful search returns formatted result list."""
        tool = WebSearchTool()
        fake_results = self._make_fake_results(3)
        with patch("harness.tools.web_search._ddg_search_sync", return_value=fake_results):
            result = _run(tool.execute(_make_config(), query="python testing"))
        assert not result.is_error
        assert "Result 1" in result.output
        assert "https://example.com/1" in result.output
        assert "Snippet for result 1" in result.output

    def test_search_output_contains_query(self):
        """Search output includes the original query."""
        tool = WebSearchTool()
        fake_results = self._make_fake_results(2)
        with patch("harness.tools.web_search._ddg_search_sync", return_value=fake_results):
            result = _run(tool.execute(_make_config(), query="my test query"))
        assert "my test query" in result.output

    def test_search_empty_results_returns_no_results_message(self):
        """Empty result list returns helpful message instead of error."""
        tool = WebSearchTool()
        with patch("harness.tools.web_search._ddg_search_sync", return_value=[]):
            result = _run(tool.execute(_make_config(), query="xyzzy impossible query"))
        assert not result.is_error
        assert "No results" in result.output or "empty" in result.output.lower()

    def test_search_result_count_in_output(self):
        """Output includes count of results returned."""
        tool = WebSearchTool()
        fake_results = self._make_fake_results(5)
        with patch("harness.tools.web_search._ddg_search_sync", return_value=fake_results):
            result = _run(tool.execute(_make_config(), query="test"))
        assert "5" in result.output  # 5 result(s) mentioned

    def test_search_http_error_returns_error_result(self):
        """HTTPError from DuckDuckGo is caught and returned as error."""
        tool = WebSearchTool()
        exc = urllib.error.HTTPError(
            url=None, code=429, msg="Too Many Requests",
            hdrs=MagicMock(), fp=BytesIO(b"")
        )
        with patch("harness.tools.web_search._ddg_search_sync", side_effect=exc):
            result = _run(tool.execute(_make_config(), query="test"))
        assert result.is_error
        assert "429" in result.error

    def test_search_url_error_returns_error_result(self):
        """URLError is caught and returned as error."""
        tool = WebSearchTool()
        exc = urllib.error.URLError(reason="Network unreachable")
        with patch("harness.tools.web_search._ddg_search_sync", side_effect=exc):
            result = _run(tool.execute(_make_config(), query="test"))
        assert result.is_error
        assert "Network error" in result.error

    def test_search_timeout_returns_error_result(self):
        """TimeoutError is caught and returned as error."""
        tool = WebSearchTool()
        with patch("harness.tools.web_search._ddg_search_sync", side_effect=TimeoutError("timed out")):
            result = _run(tool.execute(_make_config(), query="test", timeout=5))
        assert result.is_error
        assert "timed out" in result.error.lower() or "timeout" in result.error.lower()


class TestWebSearchToolFetch:
    """Test the fetch action with mocked network."""

    def test_fetch_returns_page_content(self):
        """Successful fetch returns extracted page text with URL header."""
        tool = WebSearchTool()
        with patch("harness.tools.web_search._fetch_page_sync", return_value="Page content here"):
            result = _run(tool.execute(
                _make_config(),
                query="https://example.com",
                action="fetch",
            ))
        assert not result.is_error
        assert "Page content here" in result.output
        assert "https://example.com" in result.output

    def test_fetch_output_has_url_header(self):
        """Fetched page output includes the URL as header."""
        tool = WebSearchTool()
        with patch("harness.tools.web_search._fetch_page_sync", return_value="content"):
            result = _run(tool.execute(
                _make_config(),
                query="https://example.com/docs",
                action="fetch",
            ))
        assert "Fetched: https://example.com/docs" in result.output

    def test_fetch_empty_page_returns_message(self):
        """Empty page returns informative message (not error)."""
        tool = WebSearchTool()
        with patch("harness.tools.web_search._fetch_page_sync", return_value="   "):
            result = _run(tool.execute(
                _make_config(),
                query="https://example.com",
                action="fetch",
            ))
        assert not result.is_error
        assert "empty" in result.output.lower() or "content could not" in result.output.lower()

    def test_fetch_http_error_returns_error_result(self):
        """HTTPError from fetch is caught and returned as error."""
        tool = WebSearchTool()
        exc = urllib.error.HTTPError(
            url=None, code=403, msg="Forbidden",
            hdrs=MagicMock(), fp=BytesIO(b"")
        )
        with patch("harness.tools.web_search._fetch_page_sync", side_effect=exc):
            result = _run(tool.execute(
                _make_config(),
                query="https://example.com",
                action="fetch",
            ))
        assert result.is_error
        assert "403" in result.error

    def test_fetch_url_error_returns_error_result(self):
        """URLError from fetch is caught and returned as error."""
        tool = WebSearchTool()
        exc = urllib.error.URLError(reason="Connection refused")
        with patch("harness.tools.web_search._fetch_page_sync", side_effect=exc):
            result = _run(tool.execute(
                _make_config(),
                query="https://example.com",
                action="fetch",
            ))
        assert result.is_error
        assert "Network error" in result.error

    def test_fetch_timeout_returns_error_result(self):
        """TimeoutError from fetch is caught and returned as error."""
        tool = WebSearchTool()
        with patch("harness.tools.web_search._fetch_page_sync", side_effect=TimeoutError()):
            result = _run(tool.execute(
                _make_config(),
                query="https://example.com",
                action="fetch",
                timeout=3,
            ))
        assert result.is_error
        assert "timed out" in result.error.lower() or "timeout" in result.error.lower()


class TestWebSearchToolSchema:
    """Test tool metadata and schema."""

    def test_tool_name(self):
        tool = WebSearchTool()
        assert tool.name == "web_search"

    def test_required_query_in_schema(self):
        tool = WebSearchTool()
        schema = tool.input_schema()
        assert "query" in schema["required"]

    def test_schema_action_enum(self):
        tool = WebSearchTool()
        schema = tool.input_schema()
        action_prop = schema["properties"]["action"]
        assert set(action_prop["enum"]) == {"search", "fetch"}

    def test_no_network_tags(self):
        """WebSearchTool is tagged as network tool."""
        tool = WebSearchTool()
        assert "network" in tool.tags

    def test_requires_path_check_is_false(self):
        """WebSearchTool doesn't need filesystem path checks."""
        tool = WebSearchTool()
        assert tool.requires_path_check is False


class TestDDGResult:
    """Test _DDGResult data class."""

    def test_ddg_result_slots(self):
        """_DDGResult stores title, url, snippet correctly."""
        r = _DDGResult(title="My Title", url="https://example.com", snippet="A snippet")
        assert r.title == "My Title"
        assert r.url == "https://example.com"
        assert r.snippet == "A snippet"
