"""Kubernetes client factory for k8s-troubleshoot-mcp.

This module builds Kubernetes API clients from an explicit kubeconfig path.
It never falls back to in-cluster config or ~/.kube/config.
"""

from __future__ import annotations

from dataclasses import dataclass

from kubernetes import client, config


@dataclass(frozen=True)
class K8sClients:
    """Container for Kubernetes API client instances.

    All clients share the same configuration loaded from the kubeconfig file.
    """

    core_v1: client.CoreV1Api
    apps_v1: client.AppsV1Api
    autoscaling_v2: client.AutoscalingV2Api
    events_v1: client.EventsV1Api


def build_clients(kubeconfig_path: str) -> K8sClients:
    """Build Kubernetes API clients from the specified kubeconfig path.

    Loads kubeconfig from the explicit path only.
    Never calls config.load_incluster_config() or config.load_kube_config()
    without an explicit config_file argument.

    Args:
        kubeconfig_path: Path to the kubeconfig file (must exist and be readable).

    Returns:
        K8sClients containing all required API client instances.

    Raises:
        kubernetes.config.ConfigException: If the kubeconfig cannot be loaded.
    """
    # Load config from explicit path only - REQ-003 enforcement
    config.load_kube_config(config_file=kubeconfig_path)

    # REQ-003a: the generated client validates *responses* against the OpenAPI
    # schema and raises ValueError from a model setter when a field the schema
    # marks required is absent. A live v1.35.1 API server returns
    # `eventTime: null` on every event mirrored from the legacy core/v1 path,
    # while the schema marks eventTime required — so both event tools raised
    # inside kubernetes.client during deserialization, before their own
    # ApiException handling could run, and the error reached the MCP layer
    # unstructured.
    #
    # This server never writes, so there is no outgoing payload for validation
    # to protect. For incoming data the API server is the authority: a schema
    # disagreement must not turn a readable object into an exception. Disabling
    # this turns "raise" into "the field is None", which every tool already
    # handles.
    api_config = client.Configuration.get_default_copy()
    api_config.client_side_validation = False
    api_client = client.ApiClient(configuration=api_config)

    return K8sClients(
        core_v1=client.CoreV1Api(api_client),
        apps_v1=client.AppsV1Api(api_client),
        autoscaling_v2=client.AutoscalingV2Api(api_client),
        events_v1=client.EventsV1Api(api_client),
    )
