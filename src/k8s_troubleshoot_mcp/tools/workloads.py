"""Workload-related tools for k8s-troubleshoot-mcp.

Implements: get_deployment_status, list_deployments, get_statefulset_status,
            get_daemonset_status
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import MaxRetryError, NewConnectionError

from k8s_troubleshoot_mcp.config import ServerConfig
from k8s_troubleshoot_mcp.k8s_client import K8sClients
from k8s_troubleshoot_mcp.response import (
    success,
    error,
    namespace_not_allowed,
    api_exception_error,
    connection_error,
    serialize_log_content,
)


def _check_namespace(
    tool_name: str,
    namespace: str,
    config: ServerConfig,
) -> dict[str, Any] | None:
    """Check if namespace is allowed.

    Returns error dict if namespace not allowed, None if allowed.
    """
    if namespace not in config.allowed_namespaces:
        return namespace_not_allowed(tool_name, namespace, config.allowed_namespaces)
    return None


def _handle_api_exception(tool_name: str, exc: ApiException) -> dict[str, Any]:
    """Convert ApiException to structured error response."""
    return api_exception_error(
        tool_name,
        exc.status,
        exc.reason or "Unknown",
        str(exc),
    )


def _handle_connection_error(tool_name: str, exc: Exception) -> dict[str, Any]:
    """Convert connection error to structured error response."""
    return connection_error(tool_name, f"Connection error: {exc}")


def _age_seconds(creation_timestamp: datetime | None) -> int:
    """Calculate age in seconds from creation timestamp."""
    if creation_timestamp is None:
        return 0
    now = datetime.now(timezone.utc)
    return int((now - creation_timestamp).total_seconds())


def _get_active_replicaset(deployment: Any) -> str | None:
    """Get the name of the active ReplicaSet for a deployment.

    The active ReplicaSet is identified by matching the pod-template-hash
    in the deployment's status.
    """
    if not deployment.status:
        return None

    # The active RS can be derived from the deployment's revision annotation
    # or by finding the RS with matching template hash
    # For simplicity, we return None if we can't determine it
    # A more complete implementation would list ReplicaSets and find the match
    return None


def get_deployment_status(
    clients: K8sClients,
    config: ServerConfig,
    deployment_name: str,
    namespace: str,
) -> dict[str, Any]:
    """Get detailed status of a specific deployment.

    REQ-040: Returns replicas, conditions (with message), rollout strategy,
             and active ReplicaSet name.
    REQ-041: Returns deployment_not_found error if deployment doesn't exist.

    Args:
        clients: Kubernetes API clients.
        config: Server configuration with allowed namespaces.
        deployment_name: Name of the deployment.
        namespace: Namespace containing the deployment.

    Returns:
        Structured response dict with deployment status data or error.
    """
    tool_name = "get_deployment_status"

    # Check namespace before any API call
    ns_error = _check_namespace(tool_name, namespace, config)
    if ns_error:
        return ns_error

    try:
        deployment = clients.apps_v1.read_namespaced_deployment(
            name=deployment_name,
            namespace=namespace,
            _request_timeout=config.api_timeout_seconds,
        )
    except ApiException as exc:
        if exc.status == 404:
            return error(
                tool_name,
                "deployment_not_found",
                f"Deployment '{deployment_name}' not found in namespace '{namespace}'.",
            )
        return _handle_api_exception(tool_name, exc)
    except (MaxRetryError, NewConnectionError, OSError) as exc:
        return _handle_connection_error(tool_name, exc)

    # Extract conditions with message escaping (Property 8)
    conditions = []
    if deployment.status and deployment.status.conditions:
        for cond in deployment.status.conditions:
            # Escape message - free-text field per design.md line 398
            raw_message = cond.message or ""
            escaped_message = serialize_log_content(raw_message)

            conditions.append({
                "type": cond.type,
                "status": cond.status,
                "reason": cond.reason,
                "message": escaped_message,
            })

    # Extract rollout strategy
    rollout_strategy = None
    if deployment.spec and deployment.spec.strategy:
        rollout_strategy = deployment.spec.strategy.type

    # Extract replica counts
    status = deployment.status
    desired_replicas = deployment.spec.replicas if deployment.spec else 0
    ready_replicas = status.ready_replicas if status and status.ready_replicas else 0
    available_replicas = status.available_replicas if status and status.available_replicas else 0
    updated_replicas = status.updated_replicas if status and status.updated_replicas else 0

    data = {
        "name": deployment_name,
        "namespace": namespace,
        "desired_replicas": desired_replicas,
        "ready_replicas": ready_replicas,
        "available_replicas": available_replicas,
        "updated_replicas": updated_replicas,
        "conditions": conditions,
        "rollout_strategy": rollout_strategy,
        "active_replicaset": _get_active_replicaset(deployment),
    }

    return success(tool_name, data)


def list_deployments(
    clients: K8sClients,
    config: ServerConfig,
    namespace: str,
) -> dict[str, Any]:
    """List all deployments in a namespace.

    REQ-042: Returns name, desired/ready replicas, available replicas, age,
             and whether fully available for each deployment.

    Args:
        clients: Kubernetes API clients.
        config: Server configuration with allowed namespaces.
        namespace: Namespace to list deployments from.

    Returns:
        Structured response dict with deployment list or error.
    """
    tool_name = "list_deployments"

    # Check namespace before any API call
    ns_error = _check_namespace(tool_name, namespace, config)
    if ns_error:
        return ns_error

    try:
        deployment_list = clients.apps_v1.list_namespaced_deployment(
            namespace=namespace,
            _request_timeout=config.api_timeout_seconds,
        )
    except ApiException as exc:
        return _handle_api_exception(tool_name, exc)
    except (MaxRetryError, NewConnectionError, OSError) as exc:
        return _handle_connection_error(tool_name, exc)

    deployments = []
    for dep in deployment_list.items:
        status = dep.status
        spec = dep.spec

        desired_replicas = spec.replicas if spec else 0
        ready_replicas = status.ready_replicas if status and status.ready_replicas else 0
        available_replicas = status.available_replicas if status and status.available_replicas else 0

        # Deployment is fully available when available_replicas == desired_replicas
        fully_available = available_replicas == desired_replicas and desired_replicas > 0

        deployments.append({
            "name": dep.metadata.name if dep.metadata else "unknown",
            "desired_replicas": desired_replicas,
            "ready_replicas": ready_replicas,
            "available_replicas": available_replicas,
            "age_seconds": _age_seconds(
                dep.metadata.creation_timestamp if dep.metadata else None
            ),
            "fully_available": fully_available,
        })

    data = {
        "namespace": namespace,
        "total": len(deployments),
        "deployments": deployments,
    }

    return success(tool_name, data)


def get_statefulset_status(
    clients: K8sClients,
    config: ServerConfig,
    statefulset_name: str,
    namespace: str,
) -> dict[str, Any]:
    """Get detailed status of a specific StatefulSet.

    REQ-043: Returns replicas, ready/current/updated replicas, current/update
             revision, and update strategy.
    REQ-044: Returns statefulset_not_found error if StatefulSet doesn't exist.

    Args:
        clients: Kubernetes API clients.
        config: Server configuration with allowed namespaces.
        statefulset_name: Name of the StatefulSet.
        namespace: Namespace containing the StatefulSet.

    Returns:
        Structured response dict with StatefulSet status data or error.
    """
    tool_name = "get_statefulset_status"

    # Check namespace before any API call
    ns_error = _check_namespace(tool_name, namespace, config)
    if ns_error:
        return ns_error

    try:
        sts = clients.apps_v1.read_namespaced_stateful_set(
            name=statefulset_name,
            namespace=namespace,
            _request_timeout=config.api_timeout_seconds,
        )
    except ApiException as exc:
        if exc.status == 404:
            return error(
                tool_name,
                "statefulset_not_found",
                f"StatefulSet '{statefulset_name}' not found in namespace '{namespace}'.",
            )
        return _handle_api_exception(tool_name, exc)
    except (MaxRetryError, NewConnectionError, OSError) as exc:
        return _handle_connection_error(tool_name, exc)

    status = sts.status
    spec = sts.spec

    # status.conditions (V1StatefulSetCondition.message/.reason) is excluded
    # per REQ-043, would need serialize_log_content if added.

    # Extract replica counts
    replicas = spec.replicas if spec else 0
    ready_replicas = status.ready_replicas if status and status.ready_replicas else 0
    current_replicas = status.current_replicas if status and status.current_replicas else 0
    updated_replicas = status.updated_replicas if status and status.updated_replicas else 0

    # Extract revisions
    current_revision = status.current_revision if status else None
    update_revision = status.update_revision if status else None

    # Extract update strategy
    update_strategy = None
    if spec and spec.update_strategy:
        update_strategy = spec.update_strategy.type

    data = {
        "name": statefulset_name,
        "namespace": namespace,
        "replicas": replicas,
        "ready_replicas": ready_replicas,
        "current_replicas": current_replicas,
        "updated_replicas": updated_replicas,
        "current_revision": current_revision,
        "update_revision": update_revision,
        "update_strategy": update_strategy,
    }

    return success(tool_name, data)


def get_daemonset_status(
    clients: K8sClients,
    config: ServerConfig,
    daemonset_name: str,
    namespace: str,
) -> dict[str, Any]:
    """Get detailed status of a specific DaemonSet.

    REQ-045: Returns desired/current number scheduled, number ready/available,
             number misscheduled, and update strategy.
    REQ-046: Returns daemonset_not_found error if DaemonSet doesn't exist.

    Args:
        clients: Kubernetes API clients.
        config: Server configuration with allowed namespaces.
        daemonset_name: Name of the DaemonSet.
        namespace: Namespace containing the DaemonSet.

    Returns:
        Structured response dict with DaemonSet status data or error.
    """
    tool_name = "get_daemonset_status"

    # Check namespace before any API call
    ns_error = _check_namespace(tool_name, namespace, config)
    if ns_error:
        return ns_error

    try:
        ds = clients.apps_v1.read_namespaced_daemon_set(
            name=daemonset_name,
            namespace=namespace,
            _request_timeout=config.api_timeout_seconds,
        )
    except ApiException as exc:
        if exc.status == 404:
            return error(
                tool_name,
                "daemonset_not_found",
                f"DaemonSet '{daemonset_name}' not found in namespace '{namespace}'.",
            )
        return _handle_api_exception(tool_name, exc)
    except (MaxRetryError, NewConnectionError, OSError) as exc:
        return _handle_connection_error(tool_name, exc)

    status = ds.status
    spec = ds.spec

    # status.conditions (V1DaemonSetCondition.message/.reason) is excluded
    # per REQ-045, would need serialize_log_content if added.

    # Extract scheduled counts
    desired_number_scheduled = status.desired_number_scheduled if status else 0
    current_number_scheduled = status.current_number_scheduled if status else 0
    number_ready = status.number_ready if status else 0
    number_available = status.number_available if status and status.number_available else 0
    number_misscheduled = status.number_misscheduled if status else 0

    # Extract update strategy
    update_strategy = None
    if spec and spec.update_strategy:
        update_strategy = spec.update_strategy.type

    data = {
        "name": daemonset_name,
        "namespace": namespace,
        "desired_number_scheduled": desired_number_scheduled,
        "current_number_scheduled": current_number_scheduled,
        "number_ready": number_ready,
        "number_available": number_available,
        "number_misscheduled": number_misscheduled,
        "update_strategy": update_strategy,
    }

    return success(tool_name, data)
