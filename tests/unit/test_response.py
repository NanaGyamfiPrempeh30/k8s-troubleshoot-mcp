"""Unit tests for response module."""

from __future__ import annotations

import json

import pytest
from hypothesis import given, strategies as st

from k8s_troubleshoot_mcp.response import (
    success,
    error,
    namespace_not_allowed,
    api_exception_error,
    connection_error,
    serialize_log_content,
)


class TestSuccess:
    """Tests for success() function."""

    def test_returns_correct_structure(self):
        """REQ-016: Success response has tool, status, and data fields."""
        result = success("get_pod_status", {"phase": "Running"})

        assert result["tool"] == "get_pod_status"
        assert result["status"] == "success"
        assert result["data"] == {"phase": "Running"}

    def test_is_json_serializable(self):
        """REQ-015: Response must be JSON-serializable."""
        result = success("list_pods", {"pods": [{"name": "pod-1"}]})

        # Should not raise
        serialized = json.dumps(result)
        assert json.loads(serialized) == result


class TestError:
    """Tests for error() function."""

    def test_returns_correct_structure(self):
        """REQ-016: Error response has tool, status, error, and message fields."""
        result = error("get_pod_status", "pod_not_found", "Pod not found")

        assert result["tool"] == "get_pod_status"
        assert result["status"] == "error"
        assert result["error"] == "pod_not_found"
        assert result["message"] == "Pod not found"

    def test_includes_extra_fields(self):
        """Extra fields are included in response."""
        result = error(
            "get_pod_status",
            "pod_not_found",
            "Pod not found",
            namespace="default",
            pod_name="my-pod",
        )

        assert result["namespace"] == "default"
        assert result["pod_name"] == "my-pod"

    def test_is_json_serializable(self):
        """REQ-015: Response must be JSON-serializable."""
        result = error("get_pod_status", "pod_not_found", "Pod not found")

        serialized = json.dumps(result)
        assert json.loads(serialized) == result


class TestNamespaceNotAllowed:
    """Tests for namespace_not_allowed() function."""

    def test_returns_correct_structure(self):
        """REQ-007: Returns structured namespace_not_allowed error."""
        result = namespace_not_allowed(
            "get_pod_status",
            "forbidden-ns",
            frozenset({"staging", "prod"}),
        )

        assert result["tool"] == "get_pod_status"
        assert result["status"] == "error"
        assert result["error"] == "namespace_not_allowed"
        assert "forbidden-ns" in result["message"]
        assert result["allowed_namespaces"] == ["prod", "staging"]  # sorted

    def test_allowed_namespaces_sorted(self):
        """allowed_namespaces list is sorted alphabetically."""
        result = namespace_not_allowed(
            "list_pods",
            "test",
            frozenset({"z-ns", "a-ns", "m-ns"}),
        )

        assert result["allowed_namespaces"] == ["a-ns", "m-ns", "z-ns"]


class TestApiExceptionError:
    """Tests for api_exception_error() function."""

    def test_returns_correct_structure(self):
        """REQ-017: ApiException error includes http_status and reason."""
        result = api_exception_error(
            "get_pod_status",
            404,
            "Not Found",
            "Pod 'my-pod' not found in namespace 'default'",
        )

        assert result["tool"] == "get_pod_status"
        assert result["status"] == "error"
        assert result["error"] == "kubernetes_api_error"
        assert result["http_status"] == 404
        assert result["reason"] == "Not Found"
        assert "my-pod" in result["message"]


class TestConnectionError:
    """Tests for connection_error() function."""

    def test_returns_correct_structure(self):
        """REQ-018: Connection error returns structured error."""
        result = connection_error(
            "get_pod_status",
            "Failed to connect to Kubernetes API server",
        )

        assert result["tool"] == "get_pod_status"
        assert result["status"] == "error"
        assert result["error"] == "connection_error"
        assert "Failed to connect" in result["message"]


class TestSerializeLogContent:
    """Tests for serialize_log_content() function."""

    def test_escapes_double_quotes(self):
        """Property 8: Double quotes are escaped."""
        result = serialize_log_content('Hello "world"')
        assert '\\"' in result
        assert result == 'Hello \\"world\\"'

    def test_escapes_backslash(self):
        """Property 8: Backslashes are escaped."""
        result = serialize_log_content("path\\to\\file")
        assert "\\\\" in result
        assert result == "path\\\\to\\\\file"

    def test_escapes_control_characters(self):
        """Property 8: Unicode control characters U+0000-U+001F are escaped."""
        # Tab, newline, carriage return
        result = serialize_log_content("line1\nline2\ttab\rreturn")
        assert "\\n" in result
        assert "\\t" in result
        assert "\\r" in result

    def test_escapes_null_byte(self):
        """Property 8: Null byte U+0000 is escaped."""
        result = serialize_log_content("before\x00after")
        assert "\\u0000" in result

    def test_escapes_angle_brackets(self):
        """Property 8: Angle brackets are escaped as Unicode (structural mitigation)."""
        result = serialize_log_content("<script>alert('xss')</script>")
        # < and > must be escaped as \u003c and \u003e
        assert "\\u003c" in result
        assert "\\u003e" in result
        assert "<" not in result
        assert ">" not in result
        assert "alert" in result

    def test_round_trip_integrity(self):
        """Content can be round-tripped through JSON parsing."""
        original = 'Test "quotes" and \\backslash\n and <tags>'
        serialized = serialize_log_content(original)

        # Wrap in quotes and parse as JSON string
        parsed = json.loads(f'"{serialized}"')
        assert parsed == original

    def test_empty_string(self):
        """Empty string returns empty string."""
        result = serialize_log_content("")
        assert result == ""

    @given(st.text())
    def test_property_8_arbitrary_strings(self, text: str):
        """Property 8: Arbitrary strings are properly escaped for JSON embedding.

        For any string containing special characters, the result can be
        embedded in a JSON object without altering surrounding JSON semantics.
        """
        serialized = serialize_log_content(text)

        # The serialized content should be embeddable in JSON
        json_obj = f'{{"content": "{serialized}"}}'
        try:
            parsed = json.loads(json_obj)
            # The parsed content should equal the original
            assert parsed["content"] == text
        except json.JSONDecodeError:
            pytest.fail(
                f"Serialized content broke JSON parsing. Original: {repr(text)}, "
                f"Serialized: {repr(serialized)}"
            )
