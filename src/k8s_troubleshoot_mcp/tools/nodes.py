"""Node-related tools for k8s-troubleshoot-mcp.

Implements: get_node_status, list_nodes
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
    api_exception_error,
    connection_error,
    serialize_log_content,
)


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


def _is_unschedulable(node: Any) -> bool:
    """Whether a node is cordoned, as a real boolean.

    REQ-035/REQ-038 specify a boolean. `spec.unschedulable` is declared bool in
    openapi_types but is **omitted** by the API server when false, so the client
    leaves it None on every schedulable node — which serialized as
    `"unschedulable": null`, off-contract, on a real cluster.

    Absent means schedulable, so None collapses to False. That is the opposite
    of the storage_class_name treatment in storage.py, where None and "" are
    distinct states and must both survive; here there is no third state to lose.
    """
    if node.spec is None:
        return False
    return bool(node.spec.unschedulable)


def _extract_node_roles(labels: dict[str, str] | None) -> list[str]:
    """Extract node roles from labels.

    Node roles are indicated by labels with the prefix
    'node-role.kubernetes.io/' where the role name is the suffix.
    """
    if not labels:
        return []

    roles = []
    role_prefix = "node-role.kubernetes.io/"
    for key in labels:
        if key.startswith(role_prefix):
            role = key[len(role_prefix):]
            if role:
                roles.append(role)

    return sorted(roles)


def _get_ready_status(conditions: list[Any] | None) -> str:
    """Extract Ready condition status from node conditions."""
    if not conditions:
        return "Unknown"

    for cond in conditions:
        if cond.type == "Ready":
            return cond.status

    return "Unknown"


def get_node_status(
    clients: K8sClients,
    config: ServerConfig,
    node_name: str,
) -> dict[str, Any]:
    """Get detailed status of a specific node.

    REQ-035: Returns node conditions, capacity, allocatable, taints,
             unschedulable flag, roles, and kubelet version.
    REQ-036: Cluster-scoped - no namespace argument, no ALLOWED_NAMESPACES check.
    REQ-037: Returns node_not_found error if node doesn't exist.

    Args:
        clients: Kubernetes API clients.
        config: Server configuration (for api_timeout_seconds).
        node_name: Name of the node.

    Returns:
        Structured response dict with node status data or error.
    """
    tool_name = "get_node_status"

    # REQ-036: No namespace check - nodes are cluster-scoped

    try:
        node = clients.core_v1.read_node(
            name=node_name,
            _request_timeout=config.api_timeout_seconds,
        )
    except ApiException as exc:
        if exc.status == 404:
            return error(
                tool_name,
                "node_not_found",
                f"Node '{node_name}' not found.",
            )
        return _handle_api_exception(tool_name, exc)
    except (MaxRetryError, NewConnectionError, OSError) as exc:
        return _handle_connection_error(tool_name, exc)

    # Extract conditions with message escaping (Property 8)
    conditions = []
    if node.status and node.status.conditions:
        for cond in node.status.conditions:
            # Escape message - free-text field, user-influenceable
            raw_message = cond.message or ""
            escaped_message = serialize_log_content(raw_message)

            conditions.append({
                "type": cond.type,
                "status": cond.status,
                "reason": cond.reason,
                "message": escaped_message,
            })

    # Extract capacity
    capacity = {}
    if node.status and node.status.capacity:
        capacity = {
            "cpu": node.status.capacity.get("cpu", "0"),
            "memory": node.status.capacity.get("memory", "0"),
        }

    # Extract allocatable
    allocatable = {}
    if node.status and node.status.allocatable:
        allocatable = {
            "cpu": node.status.allocatable.get("cpu", "0"),
            "memory": node.status.allocatable.get("memory", "0"),
        }

    # Extract taints
    taints = []
    if node.spec and node.spec.taints:
        for taint in node.spec.taints:
            taints.append({
                "key": taint.key,
                "value": taint.value,
                "effect": taint.effect,
            })

    # Extract roles from labels
    labels = node.metadata.labels if node.metadata else None
    roles = _extract_node_roles(labels)

    # Extract kubelet version.
    # REQ-035a: escaped. node_info fields are self-reported by the kubelet on
    # each node, not written by a control-plane controller, so a compromised or
    # non-standard node agent controls this string.
    kubelet_version = None
    if node.status and node.status.node_info:
        raw_version = node.status.node_info.kubelet_version
        kubelet_version = (
            serialize_log_content(raw_version) if raw_version else None
        )

    data = {
        "name": node_name,
        "conditions": conditions,
        "capacity": capacity,
        "allocatable": allocatable,
        "taints": taints,
        "unschedulable": _is_unschedulable(node),
        "roles": roles,
        "kubelet_version": kubelet_version,
    }

    return success(tool_name, data)


def list_nodes(
    clients: K8sClients,
    config: ServerConfig,
) -> dict[str, Any]:
    """List all nodes in the cluster.

    REQ-038: Returns name, Ready condition status, roles, age, kubelet version,
             and unschedulable flag for each node.
    REQ-039: No arguments - cluster-scoped.

    Args:
        clients: Kubernetes API clients.
        config: Server configuration (for api_timeout_seconds).

    Returns:
        Structured response dict with node list or error.
    """
    tool_name = "list_nodes"

    # REQ-039: No namespace check - nodes are cluster-scoped

    try:
        node_list = clients.core_v1.list_node(
            _request_timeout=config.api_timeout_seconds,
        )
    except ApiException as exc:
        return _handle_api_exception(tool_name, exc)
    except (MaxRetryError, NewConnectionError, OSError) as exc:
        return _handle_connection_error(tool_name, exc)

    nodes = []
    for node in node_list.items:
        # Extract Ready condition status
        ready_status = _get_ready_status(
            node.status.conditions if node.status else None
        )

        # Extract roles from labels
        labels = node.metadata.labels if node.metadata else None
        roles = _extract_node_roles(labels)

        # Extract kubelet version - escaped per REQ-035a (kubelet self-reported)
        kubelet_version = None
        if node.status and node.status.node_info:
            raw_version = node.status.node_info.kubelet_version
            kubelet_version = (
                serialize_log_content(raw_version) if raw_version else None
            )

        nodes.append({
            "name": node.metadata.name if node.metadata else "unknown",
            "ready": ready_status,
            "roles": roles,
            "age_seconds": _age_seconds(
                node.metadata.creation_timestamp if node.metadata else None
            ),
            "kubelet_version": kubelet_version,
            "unschedulable": _is_unschedulable(node),
        })

    data = {
        "total": len(nodes),
        "nodes": nodes,
    }

    return success(tool_name, data)
