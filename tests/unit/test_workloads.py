"""Unit tests for tools/workloads module."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest import mock

import pytest
from kubernetes.client.exceptions import ApiException

from k8s_troubleshoot_mcp.config import ServerConfig
from k8s_troubleshoot_mcp.tools.workloads import (
    get_deployment_status,
    list_deployments,
    get_statefulset_status,
    get_daemonset_status,
)


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
    """Create mock K8sClients."""
    clients = mock.MagicMock()
    clients.apps_v1 = mock.MagicMock()
    return clients


class TestGetDeploymentStatus:
    """Tests for get_deployment_status function."""

    def test_returns_correct_structure(self, config, mock_clients):
        """REQ-040: Returns required fields."""
        mock_deployment = mock.MagicMock()
        mock_deployment.spec.replicas = 3
        mock_deployment.spec.strategy.type = "RollingUpdate"
        mock_deployment.status.ready_replicas = 3
        mock_deployment.status.available_replicas = 3
        mock_deployment.status.updated_replicas = 3
        mock_deployment.status.conditions = [
            mock.MagicMock(
                type="Available",
                status="True",
                reason="MinimumReplicasAvailable",
                message="Deployment has minimum availability.",
            ),
        ]

        mock_clients.apps_v1.read_namespaced_deployment.return_value = mock_deployment

        result = get_deployment_status(mock_clients, config, "my-deploy", "default")

        assert result["status"] == "success"
        data = result["data"]
        assert data["name"] == "my-deploy"
        assert data["namespace"] == "default"
        assert data["desired_replicas"] == 3
        assert data["ready_replicas"] == 3
        assert data["available_replicas"] == 3
        assert data["updated_replicas"] == 3
        assert data["rollout_strategy"] == "RollingUpdate"
        assert len(data["conditions"]) == 1
        assert data["conditions"][0]["type"] == "Available"

    def test_deployment_not_found(self, config, mock_clients):
        """REQ-041: Returns deployment_not_found error for 404."""
        mock_clients.apps_v1.read_namespaced_deployment.side_effect = ApiException(
            status=404, reason="Not Found"
        )

        result = get_deployment_status(mock_clients, config, "missing", "default")

        assert result["status"] == "error"
        assert result["error"] == "deployment_not_found"
        assert "missing" in result["message"]

    def test_namespace_not_allowed(self, config, mock_clients):
        """Property 4: Disallowed namespace returns error without API call."""
        result = get_deployment_status(mock_clients, config, "deploy", "forbidden")

        assert result["status"] == "error"
        assert result["error"] == "namespace_not_allowed"
        mock_clients.apps_v1.read_namespaced_deployment.assert_not_called()

    def test_condition_message_escaped(self, config, mock_clients):
        """Property 8: Condition message is escaped (free-text field)."""
        mock_deployment = mock.MagicMock()
        mock_deployment.spec.replicas = 1
        mock_deployment.spec.strategy.type = "RollingUpdate"
        mock_deployment.status.ready_replicas = 0
        mock_deployment.status.available_replicas = 0
        mock_deployment.status.updated_replicas = 0
        mock_deployment.status.conditions = [
            mock.MagicMock(
                type="Available",
                status="False",
                reason="MinimumReplicasUnavailable",
                message='<script>alert("injection")</script>',
            ),
        ]

        mock_clients.apps_v1.read_namespaced_deployment.return_value = mock_deployment

        result = get_deployment_status(mock_clients, config, "deploy", "default")

        message = result["data"]["conditions"][0]["message"]
        assert "\\u003c" in message
        assert "\\u003e" in message
        assert "<" not in message
        assert ">" not in message

    def test_uses_request_timeout(self, config, mock_clients):
        """_request_timeout is passed to API call."""
        mock_deployment = mock.MagicMock()
        mock_deployment.spec.replicas = 1
        mock_deployment.spec.strategy.type = "RollingUpdate"
        mock_deployment.status.ready_replicas = 1
        mock_deployment.status.available_replicas = 1
        mock_deployment.status.updated_replicas = 1
        mock_deployment.status.conditions = []

        mock_clients.apps_v1.read_namespaced_deployment.return_value = mock_deployment

        get_deployment_status(mock_clients, config, "deploy", "default")

        call_kwargs = mock_clients.apps_v1.read_namespaced_deployment.call_args.kwargs
        assert call_kwargs["_request_timeout"] == 30

    def test_api_exception_handled(self, config, mock_clients):
        """Property 7: ApiException returns structured error."""
        mock_clients.apps_v1.read_namespaced_deployment.side_effect = ApiException(
            status=403, reason="Forbidden"
        )

        result = get_deployment_status(mock_clients, config, "deploy", "default")

        assert result["status"] == "error"
        assert result["error"] == "kubernetes_api_error"
        assert result["http_status"] == 403


class TestListDeployments:
    """Tests for list_deployments function."""

    def test_returns_correct_structure(self, config, mock_clients):
        """REQ-042: Returns required fields."""
        now = datetime.now(timezone.utc)

        mock_deployment = mock.MagicMock()
        mock_deployment.metadata.name = "web-app"
        mock_deployment.metadata.creation_timestamp = now - timedelta(days=7)
        mock_deployment.spec.replicas = 3
        mock_deployment.status.ready_replicas = 3
        mock_deployment.status.available_replicas = 3

        mock_clients.apps_v1.list_namespaced_deployment.return_value = mock.MagicMock(
            items=[mock_deployment]
        )

        result = list_deployments(mock_clients, config, "default")

        assert result["status"] == "success"
        data = result["data"]
        assert data["namespace"] == "default"
        assert data["total"] == 1
        dep = data["deployments"][0]
        assert dep["name"] == "web-app"
        assert dep["desired_replicas"] == 3
        assert dep["ready_replicas"] == 3
        assert dep["available_replicas"] == 3
        assert dep["fully_available"] is True
        assert dep["age_seconds"] > 0

    def test_fully_available_false_when_not_ready(self, config, mock_clients):
        """fully_available is False when available != desired."""
        mock_deployment = mock.MagicMock()
        mock_deployment.metadata.name = "failing-app"
        mock_deployment.metadata.creation_timestamp = datetime.now(timezone.utc)
        mock_deployment.spec.replicas = 3
        mock_deployment.status.ready_replicas = 1
        mock_deployment.status.available_replicas = 1

        mock_clients.apps_v1.list_namespaced_deployment.return_value = mock.MagicMock(
            items=[mock_deployment]
        )

        result = list_deployments(mock_clients, config, "default")

        assert result["data"]["deployments"][0]["fully_available"] is False

    def test_namespace_not_allowed(self, config, mock_clients):
        """Property 4: Disallowed namespace returns error without API call."""
        result = list_deployments(mock_clients, config, "forbidden")

        assert result["status"] == "error"
        assert result["error"] == "namespace_not_allowed"
        mock_clients.apps_v1.list_namespaced_deployment.assert_not_called()

    def test_empty_list(self, config, mock_clients):
        """Returns empty list for namespace with no deployments."""
        mock_clients.apps_v1.list_namespaced_deployment.return_value = mock.MagicMock(
            items=[]
        )

        result = list_deployments(mock_clients, config, "default")

        assert result["status"] == "success"
        assert result["data"]["total"] == 0
        assert result["data"]["deployments"] == []


class TestGetStatefulsetStatus:
    """Tests for get_statefulset_status function."""

    def test_returns_correct_structure(self, config, mock_clients):
        """REQ-043: Returns required fields."""
        mock_sts = mock.MagicMock()
        mock_sts.spec.replicas = 3
        mock_sts.spec.update_strategy.type = "RollingUpdate"
        mock_sts.status.ready_replicas = 3
        mock_sts.status.current_replicas = 3
        mock_sts.status.updated_replicas = 3
        mock_sts.status.current_revision = "myapp-5d8c4d7b9"
        mock_sts.status.update_revision = "myapp-5d8c4d7b9"

        mock_clients.apps_v1.read_namespaced_stateful_set.return_value = mock_sts

        result = get_statefulset_status(mock_clients, config, "myapp", "default")

        assert result["status"] == "success"
        data = result["data"]
        assert data["name"] == "myapp"
        assert data["namespace"] == "default"
        assert data["replicas"] == 3
        assert data["ready_replicas"] == 3
        assert data["current_replicas"] == 3
        assert data["updated_replicas"] == 3
        assert data["current_revision"] == "myapp-5d8c4d7b9"
        assert data["update_revision"] == "myapp-5d8c4d7b9"
        assert data["update_strategy"] == "RollingUpdate"

    def test_statefulset_not_found(self, config, mock_clients):
        """REQ-044: Returns statefulset_not_found error for 404."""
        mock_clients.apps_v1.read_namespaced_stateful_set.side_effect = ApiException(
            status=404, reason="Not Found"
        )

        result = get_statefulset_status(mock_clients, config, "missing", "default")

        assert result["status"] == "error"
        assert result["error"] == "statefulset_not_found"
        assert "missing" in result["message"]

    def test_namespace_not_allowed(self, config, mock_clients):
        """Property 4: Disallowed namespace returns error without API call."""
        result = get_statefulset_status(mock_clients, config, "sts", "forbidden")

        assert result["status"] == "error"
        assert result["error"] == "namespace_not_allowed"
        mock_clients.apps_v1.read_namespaced_stateful_set.assert_not_called()

    def test_uses_request_timeout(self, config, mock_clients):
        """_request_timeout is passed to API call."""
        mock_sts = mock.MagicMock()
        mock_sts.spec.replicas = 1
        mock_sts.spec.update_strategy.type = "RollingUpdate"
        mock_sts.status.ready_replicas = 1
        mock_sts.status.current_replicas = 1
        mock_sts.status.updated_replicas = 1
        mock_sts.status.current_revision = "rev-1"
        mock_sts.status.update_revision = "rev-1"

        mock_clients.apps_v1.read_namespaced_stateful_set.return_value = mock_sts

        get_statefulset_status(mock_clients, config, "sts", "default")

        call_kwargs = mock_clients.apps_v1.read_namespaced_stateful_set.call_args.kwargs
        assert call_kwargs["_request_timeout"] == 30


class TestGetDaemonsetStatus:
    """Tests for get_daemonset_status function."""

    def test_returns_correct_structure(self, config, mock_clients):
        """REQ-045: Returns required fields."""
        mock_ds = mock.MagicMock()
        mock_ds.spec.update_strategy.type = "RollingUpdate"
        mock_ds.status.desired_number_scheduled = 5
        mock_ds.status.current_number_scheduled = 5
        mock_ds.status.number_ready = 5
        mock_ds.status.number_available = 5
        mock_ds.status.number_misscheduled = 0

        mock_clients.apps_v1.read_namespaced_daemon_set.return_value = mock_ds

        result = get_daemonset_status(mock_clients, config, "logging", "default")

        assert result["status"] == "success"
        data = result["data"]
        assert data["name"] == "logging"
        assert data["namespace"] == "default"
        assert data["desired_number_scheduled"] == 5
        assert data["current_number_scheduled"] == 5
        assert data["number_ready"] == 5
        assert data["number_available"] == 5
        assert data["number_misscheduled"] == 0
        assert data["update_strategy"] == "RollingUpdate"

    def test_daemonset_not_found(self, config, mock_clients):
        """REQ-046: Returns daemonset_not_found error for 404."""
        mock_clients.apps_v1.read_namespaced_daemon_set.side_effect = ApiException(
            status=404, reason="Not Found"
        )

        result = get_daemonset_status(mock_clients, config, "missing", "default")

        assert result["status"] == "error"
        assert result["error"] == "daemonset_not_found"
        assert "missing" in result["message"]

    def test_namespace_not_allowed(self, config, mock_clients):
        """Property 4: Disallowed namespace returns error without API call."""
        result = get_daemonset_status(mock_clients, config, "ds", "forbidden")

        assert result["status"] == "error"
        assert result["error"] == "namespace_not_allowed"
        mock_clients.apps_v1.read_namespaced_daemon_set.assert_not_called()

    def test_misscheduled_pods(self, config, mock_clients):
        """Returns number_misscheduled count."""
        mock_ds = mock.MagicMock()
        mock_ds.spec.update_strategy.type = "RollingUpdate"
        mock_ds.status.desired_number_scheduled = 3
        mock_ds.status.current_number_scheduled = 4
        mock_ds.status.number_ready = 4
        mock_ds.status.number_available = 4
        mock_ds.status.number_misscheduled = 1

        mock_clients.apps_v1.read_namespaced_daemon_set.return_value = mock_ds

        result = get_daemonset_status(mock_clients, config, "ds", "default")

        assert result["data"]["number_misscheduled"] == 1

    def test_uses_request_timeout(self, config, mock_clients):
        """_request_timeout is passed to API call."""
        mock_ds = mock.MagicMock()
        mock_ds.spec.update_strategy.type = "RollingUpdate"
        mock_ds.status.desired_number_scheduled = 1
        mock_ds.status.current_number_scheduled = 1
        mock_ds.status.number_ready = 1
        mock_ds.status.number_available = 1
        mock_ds.status.number_misscheduled = 0

        mock_clients.apps_v1.read_namespaced_daemon_set.return_value = mock_ds

        get_daemonset_status(mock_clients, config, "ds", "default")

        call_kwargs = mock_clients.apps_v1.read_namespaced_daemon_set.call_args.kwargs
        assert call_kwargs["_request_timeout"] == 30

    def test_api_exception_handled(self, config, mock_clients):
        """Property 7: ApiException returns structured error."""
        mock_clients.apps_v1.read_namespaced_daemon_set.side_effect = ApiException(
            status=403, reason="Forbidden"
        )

        result = get_daemonset_status(mock_clients, config, "ds", "default")

        assert result["status"] == "error"
        assert result["error"] == "kubernetes_api_error"
        assert result["http_status"] == 403
