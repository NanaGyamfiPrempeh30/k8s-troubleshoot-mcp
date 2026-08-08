"""Unit tests for k8s_client module."""

from __future__ import annotations

from unittest import mock

import pytest

from k8s_troubleshoot_mcp.k8s_client import K8sClients, build_clients


class TestBuildClients:
    """Tests for build_clients function."""

    def test_loads_config_from_explicit_path(self):
        """Property 1: Config is loaded from explicit path only."""
        with mock.patch("k8s_troubleshoot_mcp.k8s_client.config") as mock_config:
            with mock.patch("k8s_troubleshoot_mcp.k8s_client.client"):
                build_clients("/path/to/kubeconfig.yaml")

                # Verify load_kube_config was called with explicit config_file
                mock_config.load_kube_config.assert_called_once_with(
                    config_file="/path/to/kubeconfig.yaml"
                )

                # Verify load_incluster_config was never called
                mock_config.load_incluster_config.assert_not_called()

    def test_returns_k8s_clients_dataclass(self):
        """build_clients returns K8sClients with correct attributes."""
        with mock.patch("k8s_troubleshoot_mcp.k8s_client.config"):
            with mock.patch("k8s_troubleshoot_mcp.k8s_client.client") as mock_client:
                # Create mock API instances
                mock_core_v1 = mock.MagicMock()
                mock_apps_v1 = mock.MagicMock()
                mock_autoscaling_v2 = mock.MagicMock()
                mock_events_v1 = mock.MagicMock()

                mock_client.CoreV1Api.return_value = mock_core_v1
                mock_client.AppsV1Api.return_value = mock_apps_v1
                mock_client.AutoscalingV2Api.return_value = mock_autoscaling_v2
                mock_client.EventsV1Api.return_value = mock_events_v1

                clients = build_clients("/path/to/kubeconfig.yaml")

                assert isinstance(clients, K8sClients)
                assert clients.core_v1 is mock_core_v1
                assert clients.apps_v1 is mock_apps_v1
                assert clients.autoscaling_v2 is mock_autoscaling_v2
                assert clients.events_v1 is mock_events_v1

    def test_k8s_clients_is_frozen(self):
        """K8sClients dataclass is immutable."""
        with mock.patch("k8s_troubleshoot_mcp.k8s_client.config"):
            with mock.patch("k8s_troubleshoot_mcp.k8s_client.client"):
                clients = build_clients("/path/to/kubeconfig.yaml")

                with pytest.raises(Exception):
                    clients.core_v1 = mock.MagicMock()

    def test_no_default_path_fallback(self):
        """Property 1: No fallback to ~/.kube/config or other defaults.

        This is ensured by always passing config_file argument explicitly.
        """
        with mock.patch("k8s_troubleshoot_mcp.k8s_client.config") as mock_config:
            with mock.patch("k8s_troubleshoot_mcp.k8s_client.client"):
                build_clients("/explicit/path")

                # Check that load_kube_config was NOT called without arguments
                # or with None/empty config_file
                call_args = mock_config.load_kube_config.call_args
                assert call_args is not None
                assert call_args.kwargs.get("config_file") == "/explicit/path"

    def test_all_api_clients_created(self):
        """All required API client types are instantiated."""
        with mock.patch("k8s_troubleshoot_mcp.k8s_client.config"):
            with mock.patch("k8s_troubleshoot_mcp.k8s_client.client") as mock_client:
                build_clients("/path/to/kubeconfig.yaml")

                # Verify all required API clients were created
                mock_client.CoreV1Api.assert_called_once()
                mock_client.AppsV1Api.assert_called_once()
                mock_client.AutoscalingV2Api.assert_called_once()
                mock_client.EventsV1Api.assert_called_once()
