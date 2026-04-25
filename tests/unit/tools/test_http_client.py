"""Unit tests for harness.tools.http_client.

All network calls are mocked — these tests run entirely offline.
"""
from __future__ import annotations

import asyncio
import json
import urllib.error
from io import BytesIO
from unittest.mock import MagicMock, Mock, patch


from harness.core.config import HarnessConfig
from harness.tools.http_client import (
    HttpRequestTool,
    _ALLOWED_METHODS,
    _do_request,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config() -> Mock:
    cfg = Mock(spec=HarnessConfig)
    cfg.workspace = "/tmp"
    cfg.allowed_paths = ["/tmp"]
    return cfg


def _make_fake_response(
    status: int = 200,
    reason: str = "OK",
    body: bytes = b"hello world",
    headers: dict[str, str] | None = None,
    url: str = "https://example.com/",
) -> MagicMock:
    """Build a fake urllib response object."""
    resp = MagicMock()
    resp.status = status
    resp.reason = reason
    resp.url = url
    resp.read.return_value = body
    resp_headers = {"Content-Type": "text/plain; charset=utf-8"}
    if headers:
        resp_headers.update(headers)
    resp.headers = resp_headers
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# _do_request unit tests (sync helper)
# ---------------------------------------------------------------------------

class TestDoRequest:
    """Test the synchronous _do_request() helper directly."""

    def test_basic_get_returns_parsed_result(self):
        """Successful GET returns dict with expected keys."""
        fake_resp = _make_fake_response(body=b"response body")
        with patch("urllib.request.urlopen", return_value=fake_resp):
            result = _do_request(
                "GET",
                "https://example.com/",
                headers={},
                body=None,
                timeout=10,
                max_chars=1000,
            )
        assert result["status"] == 200
        assert result["reason"] == "OK"
        assert result["body"] == "response body"
        assert result["truncated"] is False
        assert result["url"] == "https://example.com/"

    def test_response_truncated_when_over_max_chars(self):
        """Response body truncated at max_chars."""
        body = b"x" * 50
        fake_resp = _make_fake_response(body=body)
        with patch("urllib.request.urlopen", return_value=fake_resp):
            result = _do_request(
                "GET",
                "https://example.com/",
                headers={},
                body=None,
                timeout=10,
                max_chars=20,  # smaller than body length
            )
        assert len(result["body"]) == 20
        assert result["truncated"] is True

    def test_response_not_truncated_when_under_max_chars(self):
        """Response body not truncated when under max_chars."""
        body = b"short response"
        fake_resp = _make_fake_response(body=body)
        with patch("urllib.request.urlopen", return_value=fake_resp):
            result = _do_request(
                "GET",
                "https://example.com/",
                headers={},
                body=None,
                timeout=10,
                max_chars=1000,
            )
        assert result["body"] == "short response"
        assert result["truncated"] is False

    def test_custom_charset_decoded(self):
        """Response using non-UTF8 charset is decoded correctly."""
        # Latin-1 encoded body
        body = "café".encode("latin-1")
        fake_resp = _make_fake_response(
            body=body,
            headers={"Content-Type": "text/html; charset=latin-1"},
        )
        with patch("urllib.request.urlopen", return_value=fake_resp):
            result = _do_request(
                "GET",
                "https://example.com/",
                headers={},
                body=None,
                timeout=10,
                max_chars=1000,
            )
        assert "caf" in result["body"]  # café decoded

    def test_post_body_sent_as_bytes(self):
        """POST body string is encoded and sent."""
        fake_resp = _make_fake_response(body=b"created")
        captured_req = []

        def fake_urlopen(req, timeout=None):
            captured_req.append(req)
            return fake_resp

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = _do_request(
                "POST",
                "https://example.com/api",
                headers={},
                body="payload data",
                timeout=10,
                max_chars=1000,
            )

        assert result["status"] == 200
        req = captured_req[0]
        assert req.data == b"payload data"


# ---------------------------------------------------------------------------
# HttpRequestTool.execute() tests (async)
# ---------------------------------------------------------------------------

class TestHttpRequestToolValidation:
    """Test input validation in HttpRequestTool.execute()."""

    def test_invalid_url_scheme_rejected(self):
        """URL without http/https prefix is rejected."""
        tool = HttpRequestTool()
        result = _run(tool.execute(_make_config(), url="ftp://example.com"))
        assert result.is_error
        assert "http://" in result.error or "https://" in result.error

    def test_empty_url_rejected(self):
        """Empty URL is rejected."""
        tool = HttpRequestTool()
        result = _run(tool.execute(_make_config(), url=""))
        assert result.is_error

    def test_invalid_method_rejected(self):
        """Unknown HTTP method is rejected."""
        tool = HttpRequestTool()
        result = _run(tool.execute(_make_config(), url="https://example.com", method="INVALID"))
        assert result.is_error
        assert "INVALID" in result.error

    def test_allowed_methods_constant(self):
        """_ALLOWED_METHODS contains all expected HTTP verbs."""
        assert "GET" in _ALLOWED_METHODS
        assert "POST" in _ALLOWED_METHODS
        assert "PUT" in _ALLOWED_METHODS
        assert "DELETE" in _ALLOWED_METHODS
        assert "PATCH" in _ALLOWED_METHODS
        assert "HEAD" in _ALLOWED_METHODS

    def test_method_normalized_to_uppercase(self):
        """Lowercase method is normalized to uppercase before validation."""
        tool = HttpRequestTool()
        fake_resp = _make_fake_response()
        with patch("urllib.request.urlopen", return_value=fake_resp):
            result = _run(tool.execute(_make_config(), url="https://example.com", method="get"))
        assert not result.is_error


class TestHttpRequestToolSuccess:
    """Test successful HTTP requests."""

    def test_successful_get_returns_json_output(self):
        """Successful GET returns JSON-serialized result."""
        fake_resp = _make_fake_response(status=200, body=b"response body")
        tool = HttpRequestTool()
        with patch("urllib.request.urlopen", return_value=fake_resp):
            result = _run(tool.execute(_make_config(), url="https://example.com"))
        assert not result.is_error
        data = json.loads(result.output)
        assert data["status"] == 200
        assert data["body"] == "response body"
        assert data["truncated"] is False

    def test_json_body_serialized_and_content_type_set(self):
        """Dict body is JSON-serialized and Content-Type set to application/json."""
        tool = HttpRequestTool()
        fake_resp = _make_fake_response(status=201, body=b"created")
        captured_req = []

        def fake_urlopen(req, timeout=None):
            captured_req.append(req)
            return fake_resp

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = _run(tool.execute(
                _make_config(),
                url="https://example.com/api",
                method="POST",
                body={"key": "value", "num": 42},
            ))

        assert not result.is_error
        req = captured_req[0]
        # Body should be JSON
        body_json = json.loads(req.data.decode())
        assert body_json["key"] == "value"
        assert body_json["num"] == 42
        # Content-Type should be application/json
        assert req.headers.get("Content-type") == "application/json"

    def test_custom_headers_forwarded(self):
        """Custom headers are included in the request."""
        tool = HttpRequestTool()
        fake_resp = _make_fake_response()
        captured_req = []

        def fake_urlopen(req, timeout=None):
            captured_req.append(req)
            return fake_resp

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = _run(tool.execute(
                _make_config(),
                url="https://example.com",
                headers={"Authorization": "Bearer token123"},
            ))

        assert not result.is_error
        req = captured_req[0]
        assert req.headers.get("Authorization") == "Bearer token123"

    def test_truncation_applied_to_large_response(self):
        """Large response body is truncated to max_chars."""
        tool = HttpRequestTool()
        fake_resp = _make_fake_response(body=b"A" * 5000)
        with patch("urllib.request.urlopen", return_value=fake_resp):
            result = _run(tool.execute(
                _make_config(),
                url="https://example.com",
                max_chars=100,
            ))
        assert not result.is_error
        data = json.loads(result.output)
        assert len(data["body"]) == 100
        assert data["truncated"] is True

    def test_timeout_clamped_to_valid_range(self):
        """Timeout values outside [1, 120] are clamped silently."""
        tool = HttpRequestTool()
        fake_resp = _make_fake_response()
        # This just ensures no exception is raised; clamping happens internally
        with patch("urllib.request.urlopen", return_value=fake_resp):
            result = _run(tool.execute(
                _make_config(),
                url="https://example.com",
                timeout=999,  # above max, should be clamped to 120
            ))
        assert not result.is_error


class TestHttpRequestToolErrors:
    """Test error handling in HttpRequestTool."""

    def test_http_error_returns_error_result(self):
        """HTTPError (e.g. 404) returns is_error=True with status info."""
        tool = HttpRequestTool()
        exc = urllib.error.HTTPError(
            url="https://example.com/missing",
            code=404,
            msg="Not Found",
            hdrs=MagicMock(),
            fp=BytesIO(b"not found body"),
        )
        exc.headers = {}
        with patch("urllib.request.urlopen", side_effect=exc):
            result = _run(tool.execute(_make_config(), url="https://example.com/missing"))
        assert result.is_error
        assert "404" in result.error

    def test_url_error_returns_error_result(self):
        """URLError (e.g. DNS failure) returns is_error=True."""
        tool = HttpRequestTool()
        exc = urllib.error.URLError(reason="Name or service not known")
        with patch("urllib.request.urlopen", side_effect=exc):
            result = _run(tool.execute(_make_config(), url="https://nonexistent.invalid"))
        assert result.is_error
        assert "Network error" in result.error

    def test_timeout_error_returns_error_result(self):
        """TimeoutError returns is_error=True with descriptive message."""
        tool = HttpRequestTool()
        with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            result = _run(tool.execute(_make_config(), url="https://slow.example.com", timeout=5))
        assert result.is_error
        assert "timed out" in result.error.lower() or "timeout" in result.error.lower()

    def test_os_error_returns_error_result(self):
        """OSError (e.g. connection refused) returns is_error=True."""
        tool = HttpRequestTool()
        with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
            result = _run(tool.execute(_make_config(), url="https://example.com"))
        assert result.is_error
        assert "OS error" in result.error or "connection refused" in result.error

    def test_unexpected_exception_returns_error_result(self):
        """Unexpected exceptions are caught and returned as errors."""
        tool = HttpRequestTool()
        with patch("urllib.request.urlopen", side_effect=RuntimeError("unexpected")):
            result = _run(tool.execute(_make_config(), url="https://example.com"))
        assert result.is_error
        assert "RuntimeError" in result.error or "unexpected" in result.error


class TestHttpRequestToolSchema:
    """Test tool metadata and schema."""

    def test_tool_name(self):
        tool = HttpRequestTool()
        assert tool.name == "http_request"

    def test_required_url_in_schema(self):
        tool = HttpRequestTool()
        schema = tool.input_schema()
        assert "url" in schema["required"]

    def test_schema_has_method_enum(self):
        tool = HttpRequestTool()
        schema = tool.input_schema()
        props = schema["properties"]
        assert "method" in props
        assert set(props["method"]["enum"]) == set(_ALLOWED_METHODS)

    def test_schema_body_accepts_string_object_null(self):
        tool = HttpRequestTool()
        schema = tool.input_schema()
        body_type = schema["properties"]["body"]["type"]
        assert "string" in body_type
        assert "object" in body_type
        assert "null" in body_type
