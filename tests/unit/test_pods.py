"""Unit tests for tools/pods module."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest import mock

import pytest
from kubernetes.client.exceptions import ApiException

from k8s_troubleshoot_mcp.config import ServerConfig
from k8s_troubleshoot_mcp.k8s_client import K8sClients
from k8s_troubleshoot_mcp.tools.pods import (
    get_pod_status,
    get_pod_logs,
    get_pod_events,
    list_pods,
)
from tests.property.strategies import pod_log_response


@pytest.fixture
def config():
    """Create a test ServerConfig."""
    return ServerConfig(
        kubeconfig_path="/test/kubeconfig",
        allowed_namespaces=frozenset({"default", "staging", "prod"}),
        log_level="INFO",
        api_timeout_seconds=30,
        max_log_lines=200,
    )


@pytest.fixture
def mock_clients():
    """Create mock K8sClients with properly mocked API clients."""
    clients = mock.MagicMock()
    clients.core_v1 = mock.MagicMock()
    clients.apps_v1 = mock.MagicMock()
    clients.autoscaling_v2 = mock.MagicMock()
    clients.events_v1 = mock.MagicMock()
    return clients


class TestNamespaceValidation:
    """Tests for namespace validation (Property 4)."""

    def test_disallowed_namespace_returns_error_no_api_call(self, config, mock_clients):
        """Property 4: Disallowed namespace returns error without API call."""
        result = get_pod_status(mock_clients, config, "pod-1", "forbidden")

        assert result["status"] == "error"
        assert result["error"] == "namespace_not_allowed"
        assert "forbidden" in result["message"]
        assert sorted(result["allowed_namespaces"]) == ["default", "prod", "staging"]

        # Verify no API calls were made
        mock_clients.core_v1.read_namespaced_pod.assert_not_called()

    def test_allowed_namespace_proceeds_with_api_call(self, config, mock_clients):
        """Allowed namespace proceeds with API call."""
        mock_pod = mock.MagicMock()
        mock_pod.status.phase = "Running"
        mock_pod.status.conditions = []
        mock_pod.status.container_statuses = []
        mock_pod.status.qos_class = "BestEffort"
        mock_pod.spec.node_name = "node-1"

        mock_clients.core_v1.read_namespaced_pod.return_value = mock_pod

        result = get_pod_status(mock_clients, config, "pod-1", "default")

        assert result["status"] == "success"
        mock_clients.core_v1.read_namespaced_pod.assert_called_once()


class TestGetPodStatus:
    """Tests for get_pod_status function."""

    def test_returns_correct_structure(self, config, mock_clients):
        """REQ-021: Returns required fields."""
        # Create mock condition
        mock_condition = mock.MagicMock()
        mock_condition.type = "Ready"
        mock_condition.status = "True"
        mock_condition.reason = "PodReady"

        # Create mock container status
        mock_container_status = mock.MagicMock()
        mock_container_status.name = "main"
        mock_container_status.ready = True
        mock_container_status.restart_count = 2
        mock_container_status.last_state.terminated = None

        # Create mock pod
        mock_pod = mock.MagicMock()
        mock_pod.status.phase = "Running"
        mock_pod.status.conditions = [mock_condition]
        mock_pod.status.container_statuses = [mock_container_status]
        mock_pod.status.qos_class = "Guaranteed"
        mock_pod.spec.node_name = "worker-1"

        mock_clients.core_v1.read_namespaced_pod.return_value = mock_pod

        result = get_pod_status(mock_clients, config, "my-pod", "default")

        assert result["status"] == "success"
        data = result["data"]
        assert data["pod_name"] == "my-pod"
        assert data["namespace"] == "default"
        assert data["phase"] == "Running"
        assert data["qos_class"] == "Guaranteed"
        assert data["node_name"] == "worker-1"
        assert len(data["conditions"]) == 1
        assert data["conditions"][0]["type"] == "Ready"
        assert len(data["container_statuses"]) == 1
        assert data["container_statuses"][0]["name"] == "main"
        assert data["container_statuses"][0]["restart_count"] == 2

    def test_pod_not_found(self, config, mock_clients):
        """REQ-022: Returns pod_not_found error for 404."""
        mock_clients.core_v1.read_namespaced_pod.side_effect = ApiException(
            status=404, reason="Not Found"
        )

        result = get_pod_status(mock_clients, config, "missing-pod", "default")

        assert result["status"] == "error"
        assert result["error"] == "pod_not_found"
        assert "missing-pod" in result["message"]

    def test_excludes_credential_fields(self, config, mock_clients):
        """Property 9: Response excludes env, volumes, volume_mounts."""
        mock_pod = mock.MagicMock()
        mock_pod.status.phase = "Running"
        mock_pod.status.conditions = []
        mock_pod.status.container_statuses = []
        mock_pod.status.qos_class = "BestEffort"
        mock_pod.spec.node_name = "node-1"
        # These would be on the pod but should not appear in response
        mock_pod.spec.containers = [
            mock.MagicMock(
                env=[{"name": "SECRET", "value": "password123"}],
                volume_mounts=[{"name": "secrets", "mountPath": "/secrets"}],
            )
        ]
        mock_pod.spec.volumes = [{"name": "secrets", "secret": {"secretName": "my-secret"}}]

        mock_clients.core_v1.read_namespaced_pod.return_value = mock_pod

        result = get_pod_status(mock_clients, config, "pod-1", "default")

        data = result["data"]
        assert "env" not in data
        assert "env_vars" not in data
        assert "environment" not in data
        assert "volumes" not in data
        assert "volume_mounts" not in data

    def test_api_exception_handled(self, config, mock_clients):
        """Property 7: ApiException returns structured error."""
        mock_clients.core_v1.read_namespaced_pod.side_effect = ApiException(
            status=403, reason="Forbidden"
        )

        result = get_pod_status(mock_clients, config, "pod-1", "default")

        assert result["status"] == "error"
        assert result["error"] == "kubernetes_api_error"
        assert result["http_status"] == 403
        assert result["reason"] == "Forbidden"


class TestGetPodLogs:
    """Tests for get_pod_logs function."""

    def test_returns_correct_structure(self, config, mock_clients):
        """REQ-024: Returns required fields."""
        mock_clients.core_v1.read_namespaced_pod_log.return_value = pod_log_response(
            "line1\nline2\nline3"
        )

        result = get_pod_logs(mock_clients, config, "my-pod", "default")

        assert result["status"] == "success"
        data = result["data"]
        assert data["pod_name"] == "my-pod"
        assert data["namespace"] == "default"
        assert data["lines_returned"] == 3
        assert data["truncated"] is False
        assert data["previous"] is False
        assert "line1" in data["content"]

    def test_tail_lines_capped_at_max(self, config, mock_clients):
        """Property 10: tail_lines capped at config.max_log_lines."""
        mock_clients.core_v1.read_namespaced_pod_log.return_value = pod_log_response("log content")

        result = get_pod_logs(
            mock_clients, config, "pod-1", "default", tail_lines=500
        )

        assert result["data"]["truncated"] is True

        # Verify API was called with capped value
        call_kwargs = mock_clients.core_v1.read_namespaced_pod_log.call_args.kwargs
        assert call_kwargs["tail_lines"] == 200  # config.max_log_lines

    def test_tail_lines_under_max_not_truncated(self, config, mock_clients):
        """tail_lines under max is not marked truncated."""
        mock_clients.core_v1.read_namespaced_pod_log.return_value = pod_log_response("log")

        result = get_pod_logs(
            mock_clients, config, "pod-1", "default", tail_lines=50
        )

        assert result["data"]["truncated"] is False

    def test_previous_logs(self, config, mock_clients):
        """REQ-026: previous=True requests previous container logs."""
        mock_clients.core_v1.read_namespaced_pod_log.return_value = pod_log_response("previous logs")

        result = get_pod_logs(
            mock_clients, config, "pod-1", "default", previous=True
        )

        assert result["data"]["previous"] is True
        call_kwargs = mock_clients.core_v1.read_namespaced_pod_log.call_args.kwargs
        assert call_kwargs["previous"] is True

    def test_empty_logs_success(self, config, mock_clients):
        """REQ-028: Empty logs return success with empty content."""
        mock_clients.core_v1.read_namespaced_pod_log.return_value = pod_log_response("")

        result = get_pod_logs(mock_clients, config, "pod-1", "default")

        assert result["status"] == "success"
        assert result["data"]["content"] == ""
        assert result["data"]["lines_returned"] == 0

    def test_content_serialized(self, config, mock_clients):
        """Property 8: Log content is serialized for safety."""
        mock_clients.core_v1.read_namespaced_pod_log.return_value = pod_log_response(
            'Log with "quotes" and <tags>'
        )

        result = get_pod_logs(mock_clients, config, "pod-1", "default")

        # Quotes should be escaped
        assert '\\"' in result["data"]["content"]

    def test_container_specified(self, config, mock_clients):
        """Container name is passed to API."""
        mock_clients.core_v1.read_namespaced_pod_log.return_value = pod_log_response("logs")

        result = get_pod_logs(
            mock_clients, config, "pod-1", "default", container="sidecar"
        )

        assert result["data"]["container"] == "sidecar"
        call_kwargs = mock_clients.core_v1.read_namespaced_pod_log.call_args.kwargs
        assert call_kwargs["container"] == "sidecar"


class TestGetPodEvents:
    """Tests for get_pod_events function."""

    def test_uses_events_v1_api(self, config, mock_clients):
        """REQ-029: Uses EventsV1Api, not CoreV1Api."""
        mock_clients.events_v1.list_namespaced_event.return_value = mock.MagicMock(
            items=[]
        )

        get_pod_events(mock_clients, config, "pod-1", "default")

        # Verify EventsV1Api was called
        mock_clients.events_v1.list_namespaced_event.assert_called_once()
        # Verify CoreV1Api was not called
        mock_clients.core_v1.list_namespaced_event.assert_not_called()

    def test_filters_by_pod_name_and_kind(self, config, mock_clients):
        """REQ-029: Filters by regarding.name and regarding.kind."""
        mock_clients.events_v1.list_namespaced_event.return_value = mock.MagicMock(
            items=[]
        )

        get_pod_events(mock_clients, config, "my-pod", "default")

        call_kwargs = mock_clients.events_v1.list_namespaced_event.call_args.kwargs
        assert "regarding.name=my-pod" in call_kwargs["field_selector"]
        assert "regarding.kind=Pod" in call_kwargs["field_selector"]

    def test_empty_events_returns_message(self, config, mock_clients):
        """REQ-031: Empty events return success with message."""
        mock_clients.events_v1.list_namespaced_event.return_value = mock.MagicMock(
            items=[]
        )

        result = get_pod_events(mock_clients, config, "pod-1", "default")

        assert result["status"] == "success"
        assert result["data"]["events"] == []
        assert "No events found" in result["data"]["message"]

    def test_events_sorted_descending(self, config, mock_clients):
        """Property 11: Events sorted by last_timestamp descending."""
        now = datetime.now(timezone.utc)
        old_event = mock.MagicMock(
            reason="Pulled",
            note="Pulled image",
            deprecated_count=1,
            event_time=now - timedelta(hours=2),
            deprecated_first_timestamp=now - timedelta(hours=2),
            deprecated_last_timestamp=now - timedelta(hours=2),
            type="Normal",
        )
        new_event = mock.MagicMock(
            reason="Started",
            note="Started container",
            deprecated_count=1,
            event_time=now - timedelta(minutes=5),
            deprecated_first_timestamp=now - timedelta(minutes=5),
            deprecated_last_timestamp=now - timedelta(minutes=5),
            type="Normal",
        )

        mock_clients.events_v1.list_namespaced_event.return_value = mock.MagicMock(
            items=[old_event, new_event]  # Old first in API response
        )

        result = get_pod_events(mock_clients, config, "pod-1", "default")

        events = result["data"]["events"]
        assert len(events) == 2
        # Newest should be first
        assert events[0]["reason"] == "Started"
        assert events[1]["reason"] == "Pulled"

    def test_events_capped_at_50(self, config, mock_clients):
        """Property 11: Max 50 events returned."""
        now = datetime.now(timezone.utc)
        events = []
        for i in range(100):
            events.append(mock.MagicMock(
                reason=f"Event{i}",
                note=f"Message {i}",
                deprecated_count=1,
                event_time=now - timedelta(minutes=i),
                deprecated_first_timestamp=now - timedelta(minutes=i),
                deprecated_last_timestamp=now - timedelta(minutes=i),
                type="Normal",
            ))

        mock_clients.events_v1.list_namespaced_event.return_value = mock.MagicMock(
            items=events
        )

        result = get_pod_events(mock_clients, config, "pod-1", "default")

        assert len(result["data"]["events"]) == 50

    def test_event_message_escaped(self, config, mock_clients):
        """Property 8: Event messages are escaped (user-influenceable content)."""
        now = datetime.now(timezone.utc)
        malicious_event = mock.MagicMock(
            reason="Failed",
            note='<script>alert("injection")</script>',
            deprecated_count=1,
            event_time=now,
            deprecated_first_timestamp=now,
            deprecated_last_timestamp=now,
            type="Warning",
        )

        mock_clients.events_v1.list_namespaced_event.return_value = mock.MagicMock(
            items=[malicious_event]
        )

        result = get_pod_events(mock_clients, config, "pod-1", "default")

        message = result["data"]["events"][0]["message"]
        # Angle brackets must be escaped
        assert "\\u003c" in message
        assert "\\u003e" in message
        assert "<" not in message
        assert ">" not in message


class TestListPods:
    """Tests for list_pods function."""

    def test_returns_correct_structure(self, config, mock_clients):
        """REQ-032, REQ-034: Returns required fields and total."""
        now = datetime.now(timezone.utc)
        mock_pod = mock.MagicMock()
        mock_pod.metadata.name = "pod-1"
        mock_pod.metadata.creation_timestamp = now - timedelta(hours=1)
        mock_pod.status.phase = "Running"
        mock_pod.status.container_statuses = [
            mock.MagicMock(restart_count=2, ready=True),
            mock.MagicMock(restart_count=3, ready=True),
        ]
        mock_pod.spec.node_name = "node-1"
        mock_pod.spec.containers = []

        mock_clients.core_v1.list_namespaced_pod.return_value = mock.MagicMock(
            items=[mock_pod]
        )

        result = list_pods(mock_clients, config, "default")

        assert result["status"] == "success"
        data = result["data"]
        assert data["namespace"] == "default"
        assert data["total"] == 1
        pod = data["pods"][0]
        assert pod["name"] == "pod-1"
        assert pod["phase"] == "Running"
        assert pod["node_name"] == "node-1"
        assert pod["ready_containers"] == 2
        assert pod["total_containers"] == 2

    def test_restart_count_is_sum(self, config, mock_clients):
        """Property 12: restart_count is sum of all container restart counts."""
        mock_pod = mock.MagicMock()
        mock_pod.metadata.name = "pod-1"
        mock_pod.metadata.creation_timestamp = datetime.now(timezone.utc)
        mock_pod.status.phase = "Running"
        mock_pod.status.container_statuses = [
            mock.MagicMock(restart_count=5, ready=True),
            mock.MagicMock(restart_count=3, ready=True),
            mock.MagicMock(restart_count=2, ready=False),
        ]
        mock_pod.spec.node_name = "node-1"

        mock_clients.core_v1.list_namespaced_pod.return_value = mock.MagicMock(
            items=[mock_pod]
        )

        result = list_pods(mock_clients, config, "default")

        pod = result["data"]["pods"][0]
        assert pod["restart_count"] == 10  # 5 + 3 + 2

    def test_label_selector_passed_unchanged(self, config, mock_clients):
        """Property 13: label_selector passed to K8s API unchanged."""
        mock_clients.core_v1.list_namespaced_pod.return_value = mock.MagicMock(
            items=[]
        )

        selector = "app=nginx,version=1.0"
        list_pods(mock_clients, config, "default", label_selector=selector)

        call_kwargs = mock_clients.core_v1.list_namespaced_pod.call_args.kwargs
        assert call_kwargs["label_selector"] == selector

    def test_no_label_selector_passes_none(self, config, mock_clients):
        """No label_selector passes None to API (equivalent to omitting)."""
        mock_clients.core_v1.list_namespaced_pod.return_value = mock.MagicMock(
            items=[]
        )

        list_pods(mock_clients, config, "default")

        call_kwargs = mock_clients.core_v1.list_namespaced_pod.call_args.kwargs
        assert call_kwargs["label_selector"] is None

    def test_empty_pod_list(self, config, mock_clients):
        """Empty namespace returns empty pods list."""
        mock_clients.core_v1.list_namespaced_pod.return_value = mock.MagicMock(
            items=[]
        )

        result = list_pods(mock_clients, config, "default")

        assert result["status"] == "success"
        assert result["data"]["total"] == 0
        assert result["data"]["pods"] == []
