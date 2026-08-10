"""Unit tests for k8s_client module."""

from __future__ import annotations

import json
import textwrap
from unittest import mock

import pytest

from k8s_troubleshoot_mcp.k8s_client import K8sClients, build_clients


def _write_minimal_kubeconfig(tmp_path):
    """A kubeconfig valid enough for load_kube_config, pointing nowhere.

    No request is ever made against it: these tests inspect client
    configuration and deserialization only.
    """
    path = tmp_path / "kubeconfig.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            apiVersion: v1
            kind: Config
            clusters:
              - name: test
                cluster:
                  server: https://127.0.0.1:1
            users:
              - name: test
                user:
                  token: not-a-real-token
            contexts:
              - name: test
                context:
                  cluster: test
                  user: test
            current-context: test
            """
        )
    )
    return path


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


class TestClientSideValidationDisabled:
    """REQ-003a: response validation must not turn a readable object into a raise.

    Found by running the container against a live v1.35.1 cluster, not by the
    suite: every event test builds MagicMock events, which never exercise the
    generated model's setters, so the validation path was invisible. Both event
    tools failed on real data with

        Error executing tool get_pod_events:
        Invalid value for `event_time`, must not be `None`

    raised inside kubernetes.client during deserialization — before the tool's
    own ApiException handling could run, so it reached the MCP layer as an
    unstructured error.
    """

    def test_all_four_clients_have_validation_disabled(self, tmp_path):
        kubeconfig = _write_minimal_kubeconfig(tmp_path)

        clients = build_clients(str(kubeconfig))

        for name in ("core_v1", "apps_v1", "autoscaling_v2", "events_v1"):
            api = getattr(clients, name)
            assert api.api_client.configuration.client_side_validation is False, (
                f"{name} would raise on a schema-noncompliant server response"
            )

    def test_clients_share_one_api_client(self, tmp_path):
        """A second ApiClient could silently carry the default configuration."""
        kubeconfig = _write_minimal_kubeconfig(tmp_path)

        clients = build_clients(str(kubeconfig))

        shared = {
            id(getattr(clients, n).api_client)
            for n in ("core_v1", "apps_v1", "autoscaling_v2", "events_v1")
        }
        assert len(shared) == 1

    def test_event_without_event_time_deserializes(self, tmp_path):
        """The exact shape the live cluster returns.

        events.k8s.io/v1 marks eventTime required, but the API server returns it
        null for every event mirrored from the legacy core/v1 path — which is
        most of what a kubelet emits.
        """
        kubeconfig = _write_minimal_kubeconfig(tmp_path)
        clients = build_clients(str(kubeconfig))

        payload = {
            "apiVersion": "events.k8s.io/v1",
            "kind": "Event",
            "metadata": {"name": "probe.1", "namespace": "default"},
            "eventTime": None,
            "reason": "Scheduled",
            "type": "Normal",
            "note": "Successfully assigned default/probe to minikube",
            "deprecatedCount": 1,
            "deprecatedLastTimestamp": "2026-08-10T06:54:39Z",
        }
        response = mock.Mock(status=200, data=json.dumps(payload).encode())
        response.getheader.return_value = "application/json"

        event = clients.events_v1.api_client.deserialize(response, "EventsV1Event")

        assert event.event_time is None
        assert event.reason == "Scheduled"
        assert event.deprecated_last_timestamp is not None

    def test_validation_would_have_raised_without_the_fix(self):
        """Pins the reason REQ-003a exists, so it cannot be dropped as cargo cult.

        If a future client release stops marking eventTime required, this fails
        and the workaround can be reconsidered deliberately.
        """
        from kubernetes import client as kc

        with pytest.raises(ValueError, match="event_time"):
            kc.EventsV1Event(
                event_time=None,
                metadata=kc.V1ObjectMeta(name="probe"),
            )
