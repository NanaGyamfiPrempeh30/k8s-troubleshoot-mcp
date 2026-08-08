"""Unit tests for tools/namespaces module."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest
from kubernetes.client.exceptions import ApiException

from k8s_troubleshoot_mcp.config import ServerConfig
from k8s_troubleshoot_mcp.tools.namespaces import list_namespaces

# REQ-058a warnings are emitted on this module's logger.
NAMESPACES_LOGGER = "k8s_troubleshoot_mcp.tools.namespaces"


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
    clients.core_v1 = mock.MagicMock()
    return clients


def make_namespace(name, phase="Active", age_days=7):
    """Build a mock V1Namespace."""
    namespace = mock.MagicMock()
    namespace.metadata = mock.MagicMock()
    namespace.metadata.name = name
    namespace.metadata.creation_timestamp = datetime.now(timezone.utc) - timedelta(
        days=age_days
    )
    namespace.status = mock.MagicMock()
    namespace.status.phase = phase
    return namespace


def set_namespaces(mock_clients, names_or_objects, continue_token=None):
    """Point the mocked list_namespace at a set of namespaces."""
    items = [
        n if not isinstance(n, str) else make_namespace(n) for n in names_or_objects
    ]
    result = mock.MagicMock()
    result.items = items
    result.metadata = mock.MagicMock()
    result.metadata._continue = continue_token
    mock_clients.core_v1.list_namespace.return_value = result
    return result


class TestListNamespaces:
    """Tests for list_namespaces function."""

    def test_returns_correct_structure(self, config, mock_clients):
        """REQ-058: Returns name, phase and age for each namespace."""
        set_namespaces(mock_clients, ["default"])

        result = list_namespaces(mock_clients, config)

        assert result["status"] == "success"
        data = result["data"]
        assert data["total"] == 1
        entry = data["namespaces"][0]
        assert entry["name"] == "default"
        assert entry["phase"] == "Active"
        assert entry["age_seconds"] > 0

    def test_takes_no_arguments(self, config, mock_clients):
        """REQ-059: cluster-scoped call with no namespace argument."""
        set_namespaces(mock_clients, [])

        result = list_namespaces(mock_clients, config)

        assert result["status"] == "success"
        mock_clients.core_v1.list_namespace.assert_called_once()

    def test_filters_out_disallowed_namespaces(self, config, mock_clients):
        """REQ-072: namespaces outside the allowlist are silently dropped."""
        set_namespaces(
            mock_clients, ["default", "secret-project", "prod", "other-tenant"]
        )

        result = list_namespaces(mock_clients, config)

        names = {e["name"] for e in result["data"]["namespaces"]}
        assert names == {"default", "prod"}
        assert "secret-project" not in names
        assert "other-tenant" not in names

    def test_allowed_but_absent_namespace_is_not_reported(self, config, mock_clients):
        """The response is the intersection, not the allowlist echoed back.

        config allows default/staging/prod but the cluster only has default, so
        reporting staging or prod would claim non-existent namespaces exist.
        """
        set_namespaces(mock_clients, ["default"])

        result = list_namespaces(mock_clients, config)

        names = {e["name"] for e in result["data"]["namespaces"]}
        assert names == {"default"}
        assert "staging" not in names
        assert "prod" not in names

    def test_does_not_leak_cluster_topology(self, config, mock_clients):
        """REQ-072: a restricted client learns nothing about other namespaces."""
        set_namespaces(
            mock_clients,
            ["default", "finance", "hr-private", "kube-system", "kube-public"],
        )

        result = list_namespaces(mock_clients, config)

        names = {e["name"] for e in result["data"]["namespaces"]}
        assert names == {"default"}

    def test_terminating_phase_reported(self, config, mock_clients):
        """REQ-058: Terminating is a reportable phase."""
        set_namespaces(mock_clients, [make_namespace("default", phase="Terminating")])

        result = list_namespaces(mock_clients, config)

        assert result["data"]["namespaces"][0]["phase"] == "Terminating"

    def test_output_sorted_by_name(self, config, mock_clients):
        """Output is deterministic rather than dependent on API key order."""
        set_namespaces(mock_clients, ["staging", "default", "prod"])

        result = list_namespaces(mock_clients, config)

        names = [e["name"] for e in result["data"]["namespaces"]]
        assert names == sorted(names)
        assert names == ["default", "prod", "staging"]

    def test_no_limit_parameter_passed(self, config, mock_clients):
        """REQ-058a: passing a limit would paginate in API key order."""
        set_namespaces(mock_clients, ["default"])

        list_namespaces(mock_clients, config)

        call_kwargs = mock_clients.core_v1.list_namespace.call_args.kwargs
        assert "limit" not in call_kwargs
        assert "_continue" not in call_kwargs

    def test_paginated_response_logs_warning(self, config, mock_clients, caplog):
        """REQ-058a: an unexpected continue token means the list may be partial."""
        set_namespaces(mock_clients, ["default"], continue_token="eyJ2IjoibWV0")

        with caplog.at_level(logging.WARNING, logger=NAMESPACES_LOGGER):
            result = list_namespaces(mock_clients, config)

        assert result["status"] == "success"
        assert len(caplog.records) == 1
        assert caplog.records[0].levelno == logging.WARNING
        assert "incomplete" in caplog.records[0].getMessage()

    def test_unpaginated_response_logs_nothing(self, config, mock_clients, caplog):
        """The normal case must stay quiet."""
        set_namespaces(mock_clients, ["default"], continue_token=None)

        with caplog.at_level(logging.WARNING, logger=NAMESPACES_LOGGER):
            list_namespaces(mock_clients, config)

        assert caplog.records == []

    def test_empty_continue_token_logs_nothing(self, config, mock_clients, caplog):
        """An empty-string token is absence, not pagination."""
        set_namespaces(mock_clients, ["default"], continue_token="")

        with caplog.at_level(logging.WARNING, logger=NAMESPACES_LOGGER):
            list_namespaces(mock_clients, config)

        assert caplog.records == []

    def test_empty_cluster_returns_empty_list(self, config, mock_clients):
        """No namespaces at all is a success with an empty list."""
        set_namespaces(mock_clients, [])

        result = list_namespaces(mock_clients, config)

        assert result["status"] == "success"
        assert result["data"]["total"] == 0
        assert result["data"]["namespaces"] == []

    def test_namespace_without_metadata_skipped(self, config, mock_clients):
        """A malformed entry with no name is dropped, not crashed on."""
        broken = mock.MagicMock()
        broken.metadata = None
        broken.status = mock.MagicMock()
        broken.status.phase = "Active"
        set_namespaces(mock_clients, [broken, make_namespace("default")])

        result = list_namespaces(mock_clients, config)

        assert {e["name"] for e in result["data"]["namespaces"]} == {"default"}

    def test_uses_request_timeout(self, config, mock_clients):
        """_request_timeout is passed to API call."""
        set_namespaces(mock_clients, [])

        list_namespaces(mock_clients, config)

        call_kwargs = mock_clients.core_v1.list_namespace.call_args.kwargs
        assert call_kwargs["_request_timeout"] == 30

    def test_api_exception_handled(self, config, mock_clients):
        """Property 7: ApiException returns structured error."""
        mock_clients.core_v1.list_namespace.side_effect = ApiException(
            status=403, reason="Forbidden"
        )

        result = list_namespaces(mock_clients, config)

        assert result["status"] == "error"
        assert result["error"] == "kubernetes_api_error"
        assert result["http_status"] == 403

    def test_connection_error_handled(self, config, mock_clients):
        """Property 7: connection failures return structured error."""
        mock_clients.core_v1.list_namespace.side_effect = OSError("refused")

        result = list_namespaces(mock_clients, config)

        assert result["status"] == "error"
        assert result["error"] == "connection_error"
