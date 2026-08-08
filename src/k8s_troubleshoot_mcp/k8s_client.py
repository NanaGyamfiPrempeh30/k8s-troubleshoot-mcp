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

    return K8sClients(
        core_v1=client.CoreV1Api(),
        apps_v1=client.AppsV1Api(),
        autoscaling_v2=client.AutoscalingV2Api(),
        events_v1=client.EventsV1Api(),
    )
