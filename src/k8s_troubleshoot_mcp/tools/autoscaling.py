"""Autoscaling-related tools for k8s-troubleshoot-mcp.

Implements: get_hpa_status
"""

from __future__ import annotations

from datetime import datetime
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

# Which attribute on a V2MetricSpec / V2MetricStatus holds the payload, keyed by
# the metric's `type` discriminator.
_METRIC_SLOT_BY_TYPE = {
    "Resource": "resource",
    "ContainerResource": "container_resource",
    "Pods": "pods",
    "Object": "object",
    "External": "external",
}

# Resource and ContainerResource name the metric directly (cpu, memory — a fixed
# ResourceName enum). The other three carry a V2MetricIdentifier whose name is
# operator- or adapter-defined free text.
_DIRECTLY_NAMED_TYPES = frozenset({"Resource", "ContainerResource"})


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


def _format_timestamp(ts: datetime | None) -> str | None:
    """Format datetime as ISO8601 string or None."""
    if ts is None:
        return None
    return ts.isoformat()


def _format_metric_value(value: Any) -> str | None:
    """Render a V2MetricValueStatus or V2MetricTarget as a display string.

    Utilization is a percentage int; average value and value are Kubernetes
    quantity strings. Preference order matches how the HPA controller itself
    reports a metric.
    """
    if value is None:
        return None
    if getattr(value, "average_utilization", None) is not None:
        return f"{value.average_utilization}%"
    if getattr(value, "average_value", None) is not None:
        return str(value.average_value)
    if getattr(value, "value", None) is not None:
        return str(value.value)
    return None


def _metric_slot(entry: Any) -> Any:
    """Return the populated payload object for a metric spec/status entry."""
    slot_name = _METRIC_SLOT_BY_TYPE.get(entry.type)
    if slot_name is None:
        return None
    return getattr(entry, slot_name, None)


def _metric_name(entry: Any) -> str | None:
    """Extract a metric's name, escaped.

    REQ-051a: for Pods/Object/External metrics the name comes from a
    V2MetricIdentifier and is operator- or adapter-defined, not a fixed enum, so
    it routes through serialize_log_content. For Resource/ContainerResource the
    name is a ResourceName enum (cpu, memory); escaping it is a no-op and is
    applied uniformly rather than branching on trust.
    """
    slot = _metric_slot(entry)
    if slot is None:
        return None

    if entry.type in _DIRECTLY_NAMED_TYPES:
        raw = getattr(slot, "name", None)
    else:
        identifier = getattr(slot, "metric", None)
        raw = getattr(identifier, "name", None) if identifier is not None else None

    if raw is None:
        return None
    return serialize_log_content(str(raw))


def _metric_key(entry: Any) -> tuple[str | None, str | None]:
    """Join key pairing a spec metric with its status counterpart."""
    return (entry.type, _metric_name(entry))


def _build_metrics(spec: Any, status: Any) -> list[dict[str, Any]]:
    """Pair current metric readings with their configured targets.

    Paired on (type, name) rather than list position: spec.metrics and
    status.current_metrics are parallel in practice but nothing in the API
    guarantees it, and mispairing would silently report one metric's current
    value against another's target.
    """
    targets: dict[tuple[str | None, str | None], str | None] = {}
    if spec and spec.metrics:
        for spec_entry in spec.metrics:
            slot = _metric_slot(spec_entry)
            target = getattr(slot, "target", None) if slot is not None else None
            targets[_metric_key(spec_entry)] = _format_metric_value(target)

    metrics = []
    if status and status.current_metrics:
        for status_entry in status.current_metrics:
            slot = _metric_slot(status_entry)
            current = getattr(slot, "current", None) if slot is not None else None
            key = _metric_key(status_entry)

            metrics.append({
                "type": status_entry.type,
                "name": key[1],
                "current_value": _format_metric_value(current),
                "target_value": targets.get(key),
            })

    return metrics


def get_hpa_status(
    clients: K8sClients,
    config: ServerConfig,
    hpa_name: str,
    namespace: str,
) -> dict[str, Any]:
    """Get detailed status of a specific HorizontalPodAutoscaler.

    REQ-051: Returns current/desired/min/max replicas, current metrics (type,
             current value, target value), HPA conditions, and last scale time.
    REQ-051a: Condition messages route through serialize_log_content, as do
             operator-defined metric names.
    REQ-052: Returns hpa_not_found error if the HPA doesn't exist.

    Uses AutoscalingV2Api. autoscaling/v1 cannot express multiple metrics or
    external metrics and would silently under-report a v2 HPA's configuration.

    condition.reason is left unescaped, consistent with the accepted tradeoff
    for pod/node/deployment conditions: HPA condition reasons are authored by
    the HPA controller in kube-controller-manager from a fixed vocabulary
    (SucceededRescale, FailedGetResourceMetric, DesiredWithinRange, ...), so
    they carry the same core-controller guarantee. condition.message does not —
    on a metrics failure the controller embeds the metrics adapter's own error
    text, which originates outside the cluster's control plane.

    Args:
        clients: Kubernetes API clients.
        config: Server configuration with allowed namespaces.
        hpa_name: Name of the HorizontalPodAutoscaler.
        namespace: Namespace containing the HPA.

    Returns:
        Structured response dict with HPA status data or error.
    """
    tool_name = "get_hpa_status"

    # Check namespace before any API call
    ns_error = _check_namespace(tool_name, namespace, config)
    if ns_error:
        return ns_error

    try:
        hpa = clients.autoscaling_v2.read_namespaced_horizontal_pod_autoscaler(
            name=hpa_name,
            namespace=namespace,
            _request_timeout=config.api_timeout_seconds,
        )
    except ApiException as exc:
        if exc.status == 404:
            return error(
                tool_name,
                "hpa_not_found",
                f"HorizontalPodAutoscaler '{hpa_name}' not found in namespace "
                f"'{namespace}'.",
            )
        return _handle_api_exception(tool_name, exc)
    except (MaxRetryError, NewConnectionError, OSError) as exc:
        return _handle_connection_error(tool_name, exc)

    spec = hpa.spec
    status = hpa.status

    # REQ-051a / Property 8: condition.message is escaped.
    # spec.scale_target_ref and spec.behavior are excluded per REQ-051; both are
    # enum/object-name shaped and would not need serialize_log_content if added.
    conditions = []
    if status and status.conditions:
        for cond in status.conditions:
            conditions.append({
                "type": cond.type,
                "status": cond.status,
                "reason": cond.reason,
                "message": serialize_log_content(cond.message or ""),
            })

    data = {
        "name": hpa_name,
        "namespace": namespace,
        "current_replicas": status.current_replicas if status else 0,
        "desired_replicas": status.desired_replicas if status else 0,
        "min_replicas": spec.min_replicas if spec else None,
        "max_replicas": spec.max_replicas if spec else None,
        "metrics": _build_metrics(spec, status),
        "conditions": conditions,
        "last_scale_time": _format_timestamp(
            status.last_scale_time if status else None
        ),
    }

    return success(tool_name, data)
