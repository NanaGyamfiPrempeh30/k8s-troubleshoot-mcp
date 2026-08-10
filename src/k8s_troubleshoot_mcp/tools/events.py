"""Event-related tools for k8s-troubleshoot-mcp.

Implements: get_namespace_events

REQ-055: Uses EventsV1Api (events.k8s.io/v1). The deprecated CoreV1Api events
endpoint is never used.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import MaxRetryError, NewConnectionError

from k8s_troubleshoot_mcp.config import ServerConfig
from k8s_troubleshoot_mcp.k8s_client import K8sClients
from k8s_troubleshoot_mcp.pagination import total_available
from k8s_troubleshoot_mcp.response import (
    success,
    namespace_not_allowed,
    api_exception_error,
    connection_error,
    serialize_log_content,
)

logger = logging.getLogger(__name__)

# REQ-056: hard ceiling on returned events, regardless of requested limit.
MAX_EVENTS = 50


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


def _event_sort_key(event: Any) -> datetime:
    """Most recent timestamp available on an events.k8s.io/v1 Event.

    EventsV1 records time in event_time, falling back to the deprecated
    core/v1-compatible fields for events mirrored from the legacy API.
    """
    if event.event_time:
        return event.event_time
    if event.deprecated_last_timestamp:
        return event.deprecated_last_timestamp
    if event.deprecated_first_timestamp:
        return event.deprecated_first_timestamp
    return datetime.min.replace(tzinfo=timezone.utc)


def _regarding_field(event: Any, attribute: str) -> str | None:
    """Read a field from the event's `regarding` object reference, escaped.

    REQ-057a: escaped. An ObjectReference on an Event is written by whichever
    controller emitted the event and is not verified against a real object, so
    both kind and name are emitter-controlled rather than API-validated.
    """
    regarding = event.regarding
    if regarding is None:
        return None
    raw = getattr(regarding, attribute, None)
    if raw is None:
        return None
    return serialize_log_content(str(raw))


def get_namespace_events(
    clients: K8sClients,
    config: ServerConfig,
    namespace: str,
    limit: int = MAX_EVENTS,
) -> dict[str, Any]:
    """Get the most recent events across all resource types in a namespace.

    REQ-055: Sorted by last timestamp descending, via EventsV1Api.
    REQ-056: limit is capped at 50; `capped` reports whether the cap bound.
    REQ-057: Each event carries involved object kind and name, reason, message,
             count, first and last timestamps, and type.
    REQ-057a: reason, type, message and both involved-object fields are escaped.
    Property 15: at most 50 events regardless of the requested limit.

    Args:
        clients: Kubernetes API clients.
        config: Server configuration with allowed namespaces.
        namespace: Namespace to read events from.
        limit: Requested number of events; silently capped at 50.

    Returns:
        Structured response dict with the event list or error.
    """
    tool_name = "get_namespace_events"

    # Check namespace before any API call
    ns_error = _check_namespace(tool_name, namespace, config)
    if ns_error:
        return ns_error

    # REQ-056: cap before use. A non-positive request yields no events rather
    # than an error — an empty result is a truthful answer to "give me 0 events".
    capped = limit > MAX_EVENTS
    effective_limit = max(0, min(limit, MAX_EVENTS))

    try:
        # REQ-055: EventsV1Api, never CoreV1Api. No field_selector — this tool
        # covers all resource types in the namespace.
        event_list = clients.events_v1.list_namespaced_event(
            namespace=namespace,
            _request_timeout=config.api_timeout_seconds,
        )
    except ApiException as exc:
        return _handle_api_exception(tool_name, exc)
    except (MaxRetryError, NewConnectionError, OSError) as exc:
        return _handle_connection_error(tool_name, exc)

    # REQ-056a: captured before the slice, while the full set is still in hand.
    events_available = total_available(event_list, logger, "event")

    # Sorting happens client-side rather than via the API's `limit` parameter:
    # that parameter paginates in the API server's own ordering, so using it
    # would return an arbitrary slice rather than the most recent events.
    events_sorted = sorted(
        event_list.items or [], key=_event_sort_key, reverse=True
    )[:effective_limit]

    events = []
    for event in events_sorted:
        first_ts = event.deprecated_first_timestamp or event.event_time
        last_ts = event.deprecated_last_timestamp or event.event_time

        # REQ-057a / Property 8: note is the canonical prompt-injection vector,
        # and reason/type carry no fixed-vocabulary guarantee because any
        # controller in the cluster — including third-party operators and CRD
        # controllers — may emit an Event with arbitrary values. This mirrors
        # REQ-030a for get_pod_events.
        events.append({
            "involved_object_kind": _regarding_field(event, "kind"),
            "involved_object_name": _regarding_field(event, "name"),
            "reason": serialize_log_content(event.reason or ""),
            "message": serialize_log_content(event.note or ""),
            "count": event.deprecated_count or 1,
            "first_timestamp": _format_timestamp(first_ts),
            "last_timestamp": _format_timestamp(last_ts),
            "type": serialize_log_content(event.type or "Normal"),
        })

    data = {
        "namespace": namespace,
        # `total` describes this response; `total_available` describes the
        # namespace. They differ exactly when events were left behind.
        "total": len(events),
        "total_available": events_available,
        "capped": capped,
        "events": events,
    }

    return success(tool_name, data)
