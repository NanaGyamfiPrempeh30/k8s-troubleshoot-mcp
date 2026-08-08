"""Storage-related tools for k8s-troubleshoot-mcp.

Implements: get_pvc_status
"""

from __future__ import annotations

import logging
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

# REQ-010: handlers are configured at startup to write to stderr only.
logger = logging.getLogger(__name__)

# REQ-053b: the volume-expansion state machine, spanning Kubernetes versions.
# The *Infeasible states came from the RecoverVolumeExpansionFailure work that
# reached GA in 1.34; the *Failed states predate it. Both are listed because a
# single server may face clusters of either vintage, and treating a legitimate
# state from the other vintage as unknown would produce a warning storm.
KNOWN_RESIZE_STATES = frozenset({
    "ControllerResizeInProgress",
    "ControllerResizeFailed",
    "ControllerResizeInfeasible",
    "NodeResizePending",
    "NodeResizeInProgress",
    "NodeResizeFailed",
    "NodeResizeInfeasible",
})

# allocated_resource_statuses is keyed by resource name; PVC expansion concerns
# the "storage" resource.
STORAGE_RESOURCE_KEY = "storage"


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


def _storage_class_name(spec: Any) -> str | None:
    """Return the PVC's storage class name, preserving the empty-string case.

    An unset storage_class_name (None) means "use the default StorageClass".
    An explicit empty string means "no StorageClass" — bind only to a PV that
    has no class, disabling dynamic provisioning. These are different
    intentions and must not be collapsed into one value.
    """
    if spec is None:
        return None
    return spec.storage_class_name


def _resize_status(status: Any, pvc_name: str, namespace: str) -> str | None:
    """Return the in-progress volume expansion state, or None if none.

    REQ-053a: sourced from status.allocated_resource_statuses["storage"]. This is
    the only field that distinguishes a mid-expansion PVC — where
    requested_storage legitimately exceeds actual_capacity — from a stalled or
    failed one. Always routed through serialize_log_content; a no-op for the
    known states, and containment for anything else.

    REQ-053b: an unrecognized state is still returned, because an unknown state
    is more informative to an operator than None, but it is logged as a WARNING
    first — it means this server's model of the expansion state machine has gone
    stale relative to the cluster.
    """
    if status is None or not status.allocated_resource_statuses:
        return None

    state = status.allocated_resource_statuses.get(STORAGE_RESOURCE_KEY)
    if state is None:
        return None

    state = str(state)
    if state not in KNOWN_RESIZE_STATES:
        logger.warning(
            'Unrecognized volume expansion state "%s" on PersistentVolumeClaim '
            "%s/%s; reporting it verbatim. This server's model of the volume "
            "expansion state machine may be stale relative to this cluster.",
            serialize_log_content(state),
            namespace,
            pvc_name,
        )

    return serialize_log_content(state)


def get_pvc_status(
    clients: K8sClients,
    config: ServerConfig,
    pvc_name: str,
    namespace: str,
) -> dict[str, Any]:
    """Get detailed status of a specific PersistentVolumeClaim.

    REQ-053: Returns phase, storage class name, access modes, requested storage,
             actual capacity (if bound), bound PV name (if bound), volume mode,
             and resize_status.
    REQ-053a: resize_status explains a legitimate disagreement between
             requested_storage and actual_capacity during an expansion.
    REQ-053b: an unrecognized expansion state is returned verbatim and logged as
             a WARNING to stderr.
    REQ-054: Returns pvc_not_found error if the PVC doesn't exist.

    Of the REQ-053 fields only resize_status can carry non-enum content: phase,
    access modes and volume mode are fixed enums, storage class and PV name are
    DNS-1123 object names, and storage sizes are Kubernetes quantity strings.
    resize_status is escaped unconditionally; nothing else needs it.

    status.conditions (V1PersistentVolumeClaimCondition.message/.reason) is
    free text but is excluded per REQ-053, would need serialize_log_content
    if added. Tracked as v0.2.0 Backlog item 2 in design.md.

    status.allocated_resources (the target size being worked toward) remains
    excluded per REQ-053; only the state machine in
    allocated_resource_statuses is surfaced.

    Args:
        clients: Kubernetes API clients.
        config: Server configuration with allowed namespaces.
        pvc_name: Name of the PersistentVolumeClaim.
        namespace: Namespace containing the PVC.

    Returns:
        Structured response dict with PVC status data or error.
    """
    tool_name = "get_pvc_status"

    # Check namespace before any API call
    ns_error = _check_namespace(tool_name, namespace, config)
    if ns_error:
        return ns_error

    try:
        pvc = clients.core_v1.read_namespaced_persistent_volume_claim(
            name=pvc_name,
            namespace=namespace,
            _request_timeout=config.api_timeout_seconds,
        )
    except ApiException as exc:
        if exc.status == 404:
            return error(
                tool_name,
                "pvc_not_found",
                f"PersistentVolumeClaim '{pvc_name}' not found in namespace "
                f"'{namespace}'.",
            )
        return _handle_api_exception(tool_name, exc)
    except (MaxRetryError, NewConnectionError, OSError) as exc:
        return _handle_connection_error(tool_name, exc)

    spec = pvc.spec
    status = pvc.status

    # Requested storage comes from the spec — what the user asked for.
    requested_storage = None
    if spec and spec.resources and spec.resources.requests:
        requested_storage = spec.resources.requests.get("storage")

    # Actual capacity comes from the status — what was really provisioned.
    # Only populated once bound, and may legitimately exceed the request when a
    # provisioner rounds up to its minimum granularity.
    actual_capacity = None
    if status and status.capacity:
        actual_capacity = status.capacity.get("storage")

    # volume_name is set on the spec once the claim binds to a PV.
    bound_pv = spec.volume_name if spec else None
    if not bound_pv:
        bound_pv = None

    data = {
        "name": pvc_name,
        "namespace": namespace,
        "phase": status.phase if status else None,
        "storage_class": _storage_class_name(spec),
        "access_modes": list(spec.access_modes) if spec and spec.access_modes else [],
        "requested_storage": requested_storage,
        "actual_capacity": actual_capacity,
        "bound_pv": bound_pv,
        "volume_mode": spec.volume_mode if spec else None,
        "resize_status": _resize_status(status, pvc_name, namespace),
    }

    return success(tool_name, data)
