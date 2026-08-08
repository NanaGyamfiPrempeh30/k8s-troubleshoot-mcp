"""Pod-related tools for k8s-troubleshoot-mcp.

Implements: get_pod_status, get_pod_logs, get_pod_events, list_pods
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


def _format_timestamp(ts: datetime | None) -> str | None:
    """Format datetime as ISO8601 string or None."""
    if ts is None:
        return None
    return ts.isoformat()


def get_pod_status(
    clients: K8sClients,
    config: ServerConfig,
    pod_name: str,
    namespace: str,
) -> dict[str, Any]:
    """Get detailed status of a specific pod.

    REQ-021: Returns pod phase, conditions, container statuses, QoS class, node name.
    REQ-022: Returns pod_not_found error if pod doesn't exist.
    REQ-023: Excludes environment variables, volume mounts, and credential fields.

    Args:
        clients: Kubernetes API clients.
        config: Server configuration with allowed namespaces.
        pod_name: Name of the pod.
        namespace: Namespace containing the pod.

    Returns:
        Structured response dict with pod status data or error.
    """
    tool_name = "get_pod_status"

    # Property 4: Check namespace before any API call
    ns_error = _check_namespace(tool_name, namespace, config)
    if ns_error:
        return ns_error

    try:
        pod = clients.core_v1.read_namespaced_pod(
            name=pod_name,
            namespace=namespace,
            _request_timeout=config.api_timeout_seconds,
        )
    except ApiException as exc:
        if exc.status == 404:
            return error(
                tool_name,
                "pod_not_found",
                f"Pod '{pod_name}' not found in namespace '{namespace}'.",
            )
        return _handle_api_exception(tool_name, exc)
    except (MaxRetryError, NewConnectionError, OSError) as exc:
        return _handle_connection_error(tool_name, exc)

    # Extract conditions
    # cond.message exists in V1PodCondition but is excluded per REQ-021,
    # would need serialize_log_content if added.
    conditions = []
    if pod.status and pod.status.conditions:
        for cond in pod.status.conditions:
            conditions.append({
                "type": cond.type,
                "status": cond.status,
                "reason": cond.reason,
            })

    # Extract container statuses - REQ-023: excludes env, volumes, mounts
    container_statuses = []
    if pod.status and pod.status.container_statuses:
        for cs in pod.status.container_statuses:
            status_info = {
                "name": cs.name,
                "ready": cs.ready,
                "restart_count": cs.restart_count,
                "last_exit_code": None,
                "last_exit_reason": None,
                "last_finished_time": None,
            }

            # Extract last termination state if present.
            # term.message is excluded per REQ-021, would need
            # serialize_log_content if added.
            if cs.last_state and cs.last_state.terminated:
                term = cs.last_state.terminated
                status_info["last_exit_code"] = term.exit_code
                # REQ-021a: escaped. Unlike condition.reason, a termination
                # reason is authored by the container runtime over CRI, not by
                # a control-plane controller, so it does not carry the
                # fixed-vocabulary guarantee the reason tradeoff rests on.
                status_info["last_exit_reason"] = (
                    serialize_log_content(term.reason) if term.reason else None
                )
                status_info["last_finished_time"] = _format_timestamp(term.finished_at)

            container_statuses.append(status_info)

    data = {
        "pod_name": pod_name,
        "namespace": namespace,
        "phase": pod.status.phase if pod.status else "Unknown",
        "conditions": conditions,
        "container_statuses": container_statuses,
        "qos_class": pod.status.qos_class if pod.status else None,
        "node_name": pod.spec.node_name if pod.spec else None,
    }

    return success(tool_name, data)


def get_pod_logs(
    clients: K8sClients,
    config: ServerConfig,
    pod_name: str,
    namespace: str,
    container: str | None = None,
    previous: bool = False,
    tail_lines: int = 100,
) -> dict[str, Any]:
    """Get logs from a pod container.

    REQ-024: Returns pod_name, namespace, container, lines_returned, truncated, content.
    REQ-025: tail_lines capped at config.max_log_lines.
    REQ-026: previous=True returns previous container's logs.
    REQ-027: Content is JSON-serialized for prompt injection mitigation.
    REQ-028: Empty logs return success with content="" and lines_returned=0.

    Args:
        clients: Kubernetes API clients.
        config: Server configuration with allowed namespaces and max_log_lines.
        pod_name: Name of the pod.
        namespace: Namespace containing the pod.
        container: Container name (optional, uses default if not specified).
        previous: Whether to get previous container's logs.
        tail_lines: Number of lines to return (capped at config.max_log_lines).

    Returns:
        Structured response dict with log content or error.
    """
    tool_name = "get_pod_logs"

    # Property 4: Check namespace before any API call
    ns_error = _check_namespace(tool_name, namespace, config)
    if ns_error:
        return ns_error

    # REQ-025/Property 10: Cap tail_lines at config.max_log_lines
    truncated = tail_lines > config.max_log_lines
    effective_tail_lines = min(tail_lines, config.max_log_lines)

    try:
        logs = clients.core_v1.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            container=container if container else None,
            tail_lines=effective_tail_lines,
            previous=previous,
            _request_timeout=config.api_timeout_seconds,
        )

    except ApiException as exc:
        if exc.status == 404:
            return error(
                tool_name,
                "pod_not_found",
                f"Pod '{pod_name}' not found in namespace '{namespace}'.",
            )
        return _handle_api_exception(tool_name, exc)
    except (MaxRetryError, NewConnectionError, OSError) as exc:
        return _handle_connection_error(tool_name, exc)

    # REQ-028: Handle empty logs
    if not logs:
        logs = ""

    # Count actual lines returned
    lines = logs.split("\n") if logs else []
    # Remove trailing empty line from split
    if lines and lines[-1] == "":
        lines = lines[:-1]
    lines_returned = len(lines)

    # REQ-027/Property 8: Serialize log content for safety
    serialized_content = serialize_log_content(logs)

    data = {
        "pod_name": pod_name,
        "namespace": namespace,
        "container": container,
        "lines_returned": lines_returned,
        "truncated": truncated,
        "previous": previous,
        "content": serialized_content,
    }

    return success(tool_name, data)


def get_pod_events(
    clients: K8sClients,
    config: ServerConfig,
    pod_name: str,
    namespace: str,
) -> dict[str, Any]:
    """Get events related to a specific pod.

    REQ-029: Uses EventsV1Api.list_namespaced_event() filtered by regarding.name/kind.
             Never uses CoreV1Api events (deprecated).
    REQ-030: Each event includes reason, message, count, timestamps, type.
    REQ-031: Empty events return success with empty list and message.

    Args:
        clients: Kubernetes API clients.
        config: Server configuration with allowed namespaces.
        pod_name: Name of the pod.
        namespace: Namespace containing the pod.

    Returns:
        Structured response dict with events or error.
    """
    tool_name = "get_pod_events"

    # Property 4: Check namespace before any API call
    ns_error = _check_namespace(tool_name, namespace, config)
    if ns_error:
        return ns_error

    try:
        # Use EventsV1Api (events.k8s.io/v1), NOT CoreV1Api
        # Filter by regarding.name and regarding.kind
        field_selector = f"regarding.name={pod_name},regarding.kind=Pod"

        event_list = clients.events_v1.list_namespaced_event(
            namespace=namespace,
            field_selector=field_selector,
            _request_timeout=config.api_timeout_seconds,
        )
    except ApiException as exc:
        return _handle_api_exception(tool_name, exc)
    except (MaxRetryError, NewConnectionError, OSError) as exc:
        return _handle_connection_error(tool_name, exc)

    # Property 11: Sort by last timestamp descending, cap at 50
    events = event_list.items

    def get_event_time(event: Any) -> datetime:
        """Get the most recent timestamp from an event."""
        # EventsV1 uses event_time, deprecated_last_timestamp, or series.last_observed_time
        if event.event_time:
            return event.event_time
        if event.deprecated_last_timestamp:
            return event.deprecated_last_timestamp
        if event.deprecated_first_timestamp:
            return event.deprecated_first_timestamp
        return datetime.min.replace(tzinfo=timezone.utc)

    events_sorted = sorted(events, key=get_event_time, reverse=True)[:50]

    # REQ-031: Handle empty events
    if not events_sorted:
        data = {
            "pod_name": pod_name,
            "namespace": namespace,
            "events": [],
            "message": f"No events found for pod '{pod_name}' in namespace '{namespace}'.",
        }
        return success(tool_name, data)

    # Format events
    formatted_events = []
    for event in events_sorted:
        # Get timestamps (EventsV1 uses different field names)
        first_ts = event.deprecated_first_timestamp or event.event_time
        last_ts = event.deprecated_last_timestamp or event.event_time

        # Escape message content - user-influenceable via container output,
        # image names, probe messages, operator messages (Property 8)
        raw_message = event.note or ""
        escaped_message = serialize_log_content(raw_message)

        # REQ-030a: reason and type are escaped. An Event may be emitted by any
        # controller in the cluster, including third-party operators and CRD
        # controllers, so neither field is drawn from a control-plane-authored
        # fixed vocabulary the way a core controller's condition.reason is.
        formatted_events.append({
            "reason": serialize_log_content(event.reason or ""),
            "message": escaped_message,
            "count": event.deprecated_count or 1,
            "first_timestamp": _format_timestamp(first_ts),
            "last_timestamp": _format_timestamp(last_ts),
            "type": serialize_log_content(event.type or "Normal"),
        })

    data = {
        "pod_name": pod_name,
        "namespace": namespace,
        "events": formatted_events,
    }

    return success(tool_name, data)


def list_pods(
    clients: K8sClients,
    config: ServerConfig,
    namespace: str,
    label_selector: str | None = None,
) -> dict[str, Any]:
    """List all pods in a namespace.

    REQ-032: Returns name, phase, node_name, restart_count (sum), age, ready/total containers.
    REQ-033: label_selector passed to K8s API unchanged (Property 13).
    REQ-034: Response includes total count.

    Args:
        clients: Kubernetes API clients.
        config: Server configuration with allowed namespaces.
        namespace: Namespace to list pods from.
        label_selector: Optional label selector to filter pods.

    Returns:
        Structured response dict with pod list or error.
    """
    tool_name = "list_pods"

    # Property 4: Check namespace before any API call
    ns_error = _check_namespace(tool_name, namespace, config)
    if ns_error:
        return ns_error

    try:
        # Property 13: Pass label_selector unchanged (None is acceptable to API)
        pod_list = clients.core_v1.list_namespaced_pod(
            namespace=namespace,
            label_selector=label_selector if label_selector else None,
            _request_timeout=config.api_timeout_seconds,
        )

    except ApiException as exc:
        return _handle_api_exception(tool_name, exc)
    except (MaxRetryError, NewConnectionError, OSError) as exc:
        return _handle_connection_error(tool_name, exc)

    pods = []
    for pod in pod_list.items:
        # Property 12: restart_count is sum of all container restart counts
        restart_count = 0
        ready_containers = 0
        total_containers = 0

        if pod.status and pod.status.container_statuses:
            for cs in pod.status.container_statuses:
                restart_count += cs.restart_count
                if cs.ready:
                    ready_containers += 1
                total_containers += 1
        elif pod.spec and pod.spec.containers:
            # No status yet, count from spec
            total_containers = len(pod.spec.containers)

        pods.append({
            "name": pod.metadata.name,
            "phase": pod.status.phase if pod.status else "Unknown",
            "node_name": pod.spec.node_name if pod.spec else None,
            "restart_count": restart_count,
            "age_seconds": _age_seconds(
                pod.metadata.creation_timestamp if pod.metadata else None
            ),
            "ready_containers": ready_containers,
            "total_containers": total_containers,
        })

    data = {
        "namespace": namespace,
        "total": len(pods),
        "pods": pods,
    }

    return success(tool_name, data)
