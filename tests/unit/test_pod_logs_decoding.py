"""Regression tests for get_pod_logs response decoding.

Found by real-cluster validation, not by the suite: `content` came back as a
Python bytes repr ("b'line one\\nline two\\n'") and `lines_returned` was 1 for a
17-line log. Every existing test mocked read_namespaced_pod_log with a `str`,
so the whole suite agreed on a response shape the real client never produces.

These tests pin the real shape: with `_preload_content=False` the kubernetes
client returns the raw urllib3 response and the body is **bytes**.
"""

from __future__ import annotations

from unittest import mock

import pytest

from k8s_troubleshoot_mcp.config import ServerConfig
from k8s_troubleshoot_mcp.k8s_client import K8sClients
from k8s_troubleshoot_mcp.tools.pods import get_pod_logs
from tests.property.strategies import PodLogResponse, pod_log_response


@pytest.fixture
def config():
    return ServerConfig(
        kubeconfig_path="/test/kubeconfig",
        allowed_namespaces=frozenset({"default"}),
        log_level="INFO",
        api_timeout_seconds=30,
        max_log_lines=200,
    )


@pytest.fixture
def mock_clients():
    return K8sClients(
        core_v1=mock.MagicMock(),
        apps_v1=mock.MagicMock(),
        events_v1=mock.MagicMock(),
        autoscaling_v2=mock.MagicMock(),
    )


class RawBytesResponse:
    """Minimal stand-in carrying only `.data`, as urllib3 does."""

    def __init__(self, data: bytes) -> None:
        self.data = data


class TestBytesBodyIsDecoded:
    def test_bytes_body_becomes_text_not_a_repr(self, config, mock_clients):
        """The exact reported symptom: content must not be a bytes repr."""
        mock_clients.core_v1.read_namespaced_pod_log.return_value = RawBytesResponse(
            b"line one\nline two\n"
        )

        result = get_pod_logs(mock_clients, config, "pod-1", "default")

        content = result["data"]["content"]
        assert not content.startswith("b'"), f"bytes repr leaked: {content!r}"
        assert not content.startswith('b"'), f"bytes repr leaked: {content!r}"
        assert "line one" in content
        assert "line two" in content

    def test_line_count_matches_a_realistic_multiline_log(self, config, mock_clients):
        """lines_returned reported 1 for a 17-line log before the fix."""
        raw = "".join(f"2026-08-09T00:00:0{i % 10}Z log line {i}\n" for i in range(17))
        mock_clients.core_v1.read_namespaced_pod_log.return_value = RawBytesResponse(
            raw.encode("utf-8")
        )

        result = get_pod_logs(mock_clients, config, "pod-1", "default")

        assert result["data"]["lines_returned"] == 17

    def test_newlines_survive_as_escaped_newlines_not_literal_backslash_n(
        self, config, mock_clients
    ):
        """str(bytes) turns real newlines into the two characters \\ and n.

        serialize_log_content escapes a real newline to the same two characters,
        so content alone cannot distinguish the bug from correct output — the
        line count is what separates them, and it is asserted alongside.
        """
        mock_clients.core_v1.read_namespaced_pod_log.return_value = pod_log_response(
            "alpha\nbeta\ngamma\n"
        )

        result = get_pod_logs(mock_clients, config, "pod-1", "default")

        assert result["data"]["lines_returned"] == 3
        assert "\\n" in result["data"]["content"]

    def test_multibyte_utf8_round_trips(self, config, mock_clients):
        """A non-ASCII log must decode, not surface as \\xc3\\xa9 escapes."""
        mock_clients.core_v1.read_namespaced_pod_log.return_value = RawBytesResponse(
            "café → naïve\n".encode("utf-8")
        )

        result = get_pod_logs(mock_clients, config, "pod-1", "default")

        content = result["data"]["content"]
        assert "café" in content
        assert "naïve" in content
        assert "\\x" not in content

    def test_bytearray_and_memoryview_bodies_decode(self, config, mock_clients):
        """Body type is urllib3's business; accept the bytes-like family."""
        for body in (bytearray(b"a\nb\n"), memoryview(b"a\nb\n")):
            mock_clients.core_v1.read_namespaced_pod_log.return_value = (
                RawBytesResponse(body)
            )

            result = get_pod_logs(mock_clients, config, "pod-1", "default")

            assert result["status"] == "success"
            assert result["data"]["lines_returned"] == 2


class TestDecodingIsFailSafe:
    def test_invalid_utf8_does_not_raise(self, config, mock_clients):
        """Container output carries no encoding guarantee (REQ: no exceptions)."""
        mock_clients.core_v1.read_namespaced_pod_log.return_value = RawBytesResponse(
            b"good line\n\xff\xfe bad bytes\n"
        )

        result = get_pod_logs(mock_clients, config, "pod-1", "default")

        assert result["status"] == "success"
        assert result["data"]["lines_returned"] == 2
        assert "good line" in result["data"]["content"]

    def test_empty_bytes_body_is_empty_content(self, config, mock_clients):
        """REQ-028 holds for the real response shape, not just for ''."""
        mock_clients.core_v1.read_namespaced_pod_log.return_value = RawBytesResponse(
            b""
        )

        result = get_pod_logs(mock_clients, config, "pod-1", "default")

        assert result["status"] == "success"
        assert result["data"]["content"] == ""
        assert result["data"]["lines_returned"] == 0

    def test_none_body_is_empty_content(self, config, mock_clients):
        mock_clients.core_v1.read_namespaced_pod_log.return_value = RawBytesResponse(
            None
        )

        result = get_pod_logs(mock_clients, config, "pod-1", "default")

        assert result["status"] == "success"
        assert result["data"]["content"] == ""

    def test_str_body_passes_through_unchanged(self, config, mock_clients):
        """Defensive: a client version that decoded correctly must not be re-handled."""
        mock_clients.core_v1.read_namespaced_pod_log.return_value = "x\ny\n"

        result = get_pod_logs(mock_clients, config, "pod-1", "default")

        assert result["data"]["lines_returned"] == 2
        assert "x" in result["data"]["content"]


class TestCallShape:
    def test_preload_content_is_disabled(self, config, mock_clients):
        """The fix lives in the call kwargs; assert it directly.

        Without _preload_content=False the client re-enters the broken
        str(bytes) deserialization path regardless of what _decode_log_body does.
        """
        mock_clients.core_v1.read_namespaced_pod_log.return_value = pod_log_response(
            "logs\n"
        )

        get_pod_logs(mock_clients, config, "pod-1", "default")

        call_kwargs = mock_clients.core_v1.read_namespaced_pod_log.call_args.kwargs
        assert call_kwargs["_preload_content"] is False

    def test_request_timeout_still_passed(self, config, mock_clients):
        """_preload_content=False must not drop the timeout."""
        mock_clients.core_v1.read_namespaced_pod_log.return_value = pod_log_response("")

        get_pod_logs(mock_clients, config, "pod-1", "default")

        call_kwargs = mock_clients.core_v1.read_namespaced_pod_log.call_args.kwargs
        assert call_kwargs["_request_timeout"] == config.api_timeout_seconds

    def test_connection_is_released(self, config, mock_clients):
        """Unread urllib3 bodies hold a pooled connection; the server is long-lived."""
        response = PodLogResponse("logs\n")
        mock_clients.core_v1.read_namespaced_pod_log.return_value = response

        get_pod_logs(mock_clients, config, "pod-1", "default")

        assert response.released is True


class TestUpstreamAssumptionStillHolds:
    """Guards the reason the workaround exists, so it can be retired safely."""

    def test_client_str_deserialization_still_produces_a_bytes_repr(self):
        """If this ever fails, the client was fixed and _preload_content=False
        (and _decode_log_body) can be reconsidered — deliberately, not by accident.

        Cause: api_client.py skips its decode step when response_type is "str"
        (`response_type not in ["file", "bytes", "str"]`), so deserialize() gets
        bytes and __deserialize_primitive falls through to str(bytes).
        """
        from kubernetes.client.api_client import ApiClient
        from kubernetes.client.rest import RESTResponse

        class _Urllib3Stub:
            status = 200
            reason = "OK"

            def __init__(self, data: bytes) -> None:
                self.data = data

            def getheaders(self):
                return {}

            def getheader(self, name, default=None):
                return default

        deserialized = ApiClient().deserialize(
            RESTResponse(_Urllib3Stub(b"line one\nline two\n")), "str"
        )

        assert deserialized == "b'line one\\nline two\\n'"
        assert len(deserialized.split("\n")) == 1
