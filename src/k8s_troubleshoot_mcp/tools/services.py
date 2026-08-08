"""Service-related tools for k8s-troubleshoot-mcp.

Implements: get_service, get_endpoints

Both tools use CoreV1Api per design.md line 185 — NetworkingV1Api was
deliberately removed from K8sClients and no v1.0 tool requires it.
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


# The legacy core/v1 endpoints controller caps an Endpoints object at 1000
# addresses and annotates the object when it does. Kubernetes 1.22+ writes
# "truncated"; 1.21 wrote "warning" without truncating. Both are treated as
# "this list is not the whole story" — see REQ-047a / REQ-049a.
OVER_CAPACITY_ANNOTATION = "endpoints.kubernetes.io/over-capacity"
_OVER_CAPACITY_VALUES = frozenset({"truncated", "warning"})


def _is_truncated(endpoints: Any, service_name: str, namespace: str) -> bool:
    """Report whether an Endpoints object has been truncated by the controller.

    REQ-047a / REQ-049a: true when the over-capacity annotation is present with
    a recognized value, false when absent.

    REQ-047b: an unrecognized value also returns false — the response contract
    admits only the two known values — but logs a WARNING first. An unknown
    value means this server's assumptions about endpoint-capacity semantics
    have gone stale relative to the cluster, and that must not be swallowed
    just because the response shape can't express it.
    """
    metadata = endpoints.metadata
    if metadata is None or not metadata.annotations:
        return False

    value = metadata.annotations.get(OVER_CAPACITY_ANNOTATION)
    if value is None:
        return False
    if value in _OVER_CAPACITY_VALUES:
        return True

    # Annotation values are cluster-controlled free text. Escape before logging
    # so an embedded newline cannot forge a second log line (REQ-047b).
    logger.warning(
        'Unrecognized "%s" annotation value "%s" on Endpoints %s/%s; treating '
        "as not truncated. This server's Kubernetes endpoint-capacity "
        "assumptions may be stale relative to this cluster.",
        OVER_CAPACITY_ANNOTATION,
        serialize_log_content(str(value)),
        namespace,
        service_name,
    )
    return False


def _pod_name_from_target_ref(address: Any) -> str | None:
    """Extract the backing pod name from an endpoint address.

    V1EndpointAddress.target_ref is a V1ObjectReference whose name is a
    DNS-1123 object name, not free text. May be absent for endpoints that
    are not backed by a pod (e.g. manually managed Endpoints objects).
    """
    target_ref = address.target_ref
    if target_ref is None:
        return None
    return target_ref.name


def _ready_endpoint_summary(
    clients: K8sClients,
    config: ServerConfig,
    service_name: str,
    namespace: str,
) -> tuple[int | None, bool | None]:
    """Count ready endpoint addresses backing a service, with truncation flag.

    REQ-047a: returns (None, None) if the endpoints object cannot be read — an
    unknown must not be reported as a known zero. A service with no endpoints
    object at all is a normal state (no matching pods yet) and returns (0, False)
    rather than turning a successful get_service call into an error.
    """
    try:
        endpoints = clients.core_v1.read_namespaced_endpoints(
            name=service_name,
            namespace=namespace,
            _request_timeout=config.api_timeout_seconds,
        )
    except ApiException as exc:
        if exc.status == 404:
            return 0, False
        return None, None
    except (MaxRetryError, NewConnectionError, OSError):
        return None, None

    truncated = _is_truncated(endpoints, service_name, namespace)

    if not endpoints.subsets:
        return 0, truncated

    total = 0
    for subset in endpoints.subsets:
        if subset.addresses:
            total += len(subset.addresses)
    return total, truncated


def get_service(
    clients: K8sClients,
    config: ServerConfig,
    service_name: str,
    namespace: str,
) -> dict[str, Any]:
    """Get detailed configuration of a specific service.

    REQ-047: Returns service type, ClusterIP, external IPs, ports (port, target
             port, protocol, node port), selector, ready endpoint count, and a
             truncated flag.
    REQ-047a: truncated is true when the Endpoints object carries the
             over-capacity annotation, meaning ready_endpoints is a floor rather
             than the true backend count. Both fields are None when the Endpoints
             object cannot be read.
    REQ-047b: an unrecognized annotation value yields truncated false and logs a
             WARNING to stderr.
    REQ-048: Returns service_not_found error if the service doesn't exist.

    No field in the REQ-047 set is free text: type/protocol are fixed enums,
    ClusterIP/external IPs are validated addresses, ports are ints, and the
    selector holds label keys/values constrained by Kubernetes label syntax.
    Nothing here routes through serialize_log_content.

    Args:
        clients: Kubernetes API clients.
        config: Server configuration with allowed namespaces.
        service_name: Name of the service.
        namespace: Namespace containing the service.

    Returns:
        Structured response dict with service data or error.
    """
    tool_name = "get_service"

    # Check namespace before any API call
    ns_error = _check_namespace(tool_name, namespace, config)
    if ns_error:
        return ns_error

    try:
        service = clients.core_v1.read_namespaced_service(
            name=service_name,
            namespace=namespace,
            _request_timeout=config.api_timeout_seconds,
        )
    except ApiException as exc:
        if exc.status == 404:
            return error(
                tool_name,
                "service_not_found",
                f"Service '{service_name}' not found in namespace '{namespace}'.",
            )
        return _handle_api_exception(tool_name, exc)
    except (MaxRetryError, NewConnectionError, OSError) as exc:
        return _handle_connection_error(tool_name, exc)

    spec = service.spec

    # status.conditions (V1Condition.message/.reason) and
    # status.load_balancer.ingress[].ports[].error are free-text fields that
    # exist on V1Service but are excluded per REQ-047, would need
    # serialize_log_content if added.

    # Extract ports
    ports = []
    if spec and spec.ports:
        for port in spec.ports:
            ports.append({
                "port": port.port,
                "target_port": port.target_port,
                "protocol": port.protocol,
                "node_port": port.node_port,
            })

    ready_endpoints, truncated = _ready_endpoint_summary(
        clients, config, service_name, namespace
    )

    data = {
        "name": service_name,
        "namespace": namespace,
        "type": spec.type if spec else None,
        "cluster_ip": spec.cluster_ip if spec else None,
        "external_ips": list(spec.external_ips) if spec and spec.external_ips else [],
        "ports": ports,
        "selector": dict(spec.selector) if spec and spec.selector else {},
        "ready_endpoints": ready_endpoints,
        "truncated": truncated,
    }

    return success(tool_name, data)


def get_endpoints(
    clients: K8sClients,
    config: ServerConfig,
    service_name: str,
    namespace: str,
) -> dict[str, Any]:
    """Get ready and not-ready endpoint addresses backing a service.

    REQ-049: Returns ready addresses (IP, pod name, node name), not-ready
             addresses (IP, pod name), port information, and a truncated flag.
    REQ-049a: truncated is true when the Endpoints object carries the
             over-capacity annotation, meaning the address lists are capped at
             1000 and incomplete with respect to the Service's real backends.
    REQ-050: Returns a structured error if no endpoints object exists. An
             endpoints object with no ready addresses is a success response
             with an empty ready list and a populated not_ready list.
    Property 14: N ready and M not-ready addresses in the endpoints object
             produce exactly N and M entries respectively. Note this validates
             faithful reflection of the Endpoints object only, not completeness
             against actual backend count — see the documented limitation on
             Property 14 in design.md.

    V1EndpointAddress (hostname, ip, node_name, target_ref) and
    CoreV1EndpointPort (app_protocol, name, port, protocol) contain no
    message or reason fields, so nothing here routes through
    serialize_log_content.

    Args:
        clients: Kubernetes API clients.
        config: Server configuration with allowed namespaces.
        service_name: Name of the service whose endpoints to read.
        namespace: Namespace containing the service.

    Returns:
        Structured response dict with endpoint partitions or error.
    """
    tool_name = "get_endpoints"

    # Check namespace before any API call
    ns_error = _check_namespace(tool_name, namespace, config)
    if ns_error:
        return ns_error

    try:
        endpoints = clients.core_v1.read_namespaced_endpoints(
            name=service_name,
            namespace=namespace,
            _request_timeout=config.api_timeout_seconds,
        )
    except ApiException as exc:
        if exc.status == 404:
            return error(
                tool_name,
                "endpoints_not_found",
                f"No endpoints object exists for service '{service_name}' "
                f"in namespace '{namespace}'.",
            )
        return _handle_api_exception(tool_name, exc)
    except (MaxRetryError, NewConnectionError, OSError) as exc:
        return _handle_connection_error(tool_name, exc)

    ready: list[dict[str, Any]] = []
    not_ready: list[dict[str, Any]] = []
    ports: list[dict[str, Any]] = []

    for subset in endpoints.subsets or []:
        for address in subset.addresses or []:
            ready.append({
                "ip": address.ip,
                "pod_name": _pod_name_from_target_ref(address),
                "node_name": address.node_name,
            })

        # not_ready addresses omit node_name per design.md line 409
        for address in subset.not_ready_addresses or []:
            not_ready.append({
                "ip": address.ip,
                "pod_name": _pod_name_from_target_ref(address),
            })

        for port in subset.ports or []:
            ports.append({
                "port": port.port,
                "protocol": port.protocol,
            })

    data = {
        "service_name": service_name,
        "namespace": namespace,
        "ready": ready,
        "not_ready": not_ready,
        "ports": ports,
        "truncated": _is_truncated(endpoints, service_name, namespace),
    }

    return success(tool_name, data)
