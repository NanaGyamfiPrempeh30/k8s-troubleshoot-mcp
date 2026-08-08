"""Namespace-related tools for k8s-troubleshoot-mcp.

Implements: list_namespaces
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import MaxRetryError, NewConnectionError

from k8s_troubleshoot_mcp.config import ServerConfig
from k8s_troubleshoot_mcp.k8s_client import K8sClients
from k8s_troubleshoot_mcp.response import (
    success,
    api_exception_error,
    connection_error,
)

# REQ-010: handlers are configured at startup to write to stderr only.
logger = logging.getLogger(__name__)


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


def _warn_if_partial(namespace_list: Any) -> None:
    """Warn if the API server returned a paginated (incomplete) list.

    REQ-058a. The list call passes no `limit`, and the Kubernetes API contract
    says an unbounded list returns all results — a non-empty `continue` token
    should therefore be impossible. If one appears anyway (an intercepting proxy,
    an aggregated API server, a future server behaviour), the response would be
    page one of N while looking exactly like a complete answer, and an allowed
    namespace absent from that page would be silently reported as not existing.
    That is a wrong answer that looks right, so it is surfaced rather than
    swallowed.
    """
    metadata = getattr(namespace_list, "metadata", None)
    if metadata is None:
        return

    # The client maps the API's `continue` field to the `_continue` attribute.
    continue_token = getattr(metadata, "_continue", None)
    if continue_token:
        logger.warning(
            "Kubernetes returned a paginated namespace list despite no limit "
            "being requested; the result may be incomplete and allowed "
            "namespaces could be missing from this response."
        )


def list_namespaces(
    clients: K8sClients,
    config: ServerConfig,
) -> dict[str, Any]:
    """List the namespaces this server is permitted to read.

    REQ-058: Returns name, phase (Active/Terminating) and age for each namespace.
    REQ-059: Takes no arguments. Calls the cluster-scoped list_namespace API, then
             filters the result to allowed_namespaces before returning.
    REQ-072: Namespaces returned by the API that are not in allowed_namespaces are
             silently dropped, so a restricted client never learns the cluster's
             full namespace topology.
    REQ-058a: A paginated response is reported as a warning to stderr.
    Property 16: the response is a subset of config.allowed_namespaces.

    The response is the *intersection* of what the cluster has and what this
    server is allowed to see. Both directions matter: returning the allowlist
    without consulting the cluster would claim non-existent namespaces exist,
    and returning the cluster list unfiltered would leak topology. Neither is
    caught by a subset check against the allowlist alone.

    No field in the REQ-058 set is free text: a namespace name is a DNS-1123
    label and phase is a NamespacePhase enum, so nothing routes through
    serialize_log_content. status.conditions
    (V1NamespaceCondition.message/.reason) is free text but is excluded per
    REQ-058, would need serialize_log_content if added. spec.finalizers is
    likewise excluded per REQ-058.

    Args:
        clients: Kubernetes API clients.
        config: Server configuration with allowed namespaces.

    Returns:
        Structured response dict with the permitted namespace list or error.
    """
    tool_name = "list_namespaces"

    # REQ-059: no namespace argument to gate on; the allowlist is applied to the
    # response instead of to the request.

    try:
        # No `limit` is passed: the API contract returns the complete list when
        # unbounded. Passing a limit would paginate in the API server's own key
        # order, which would silently answer "the first N namespaces" rather
        # than "the namespaces you may see".
        namespace_list = clients.core_v1.list_namespace(
            _request_timeout=config.api_timeout_seconds,
        )
    except ApiException as exc:
        return _handle_api_exception(tool_name, exc)
    except (MaxRetryError, NewConnectionError, OSError) as exc:
        return _handle_connection_error(tool_name, exc)

    _warn_if_partial(namespace_list)

    namespaces = []
    for namespace in namespace_list.items or []:
        metadata = namespace.metadata
        name = metadata.name if metadata else None

        # REQ-072 / Property 16: intersection with the allowlist. A namespace the
        # cluster reports but this server may not read is dropped silently.
        if name is None or name not in config.allowed_namespaces:
            continue

        namespaces.append({
            "name": name,
            "phase": namespace.status.phase if namespace.status else None,
            "age_seconds": _age_seconds(
                metadata.creation_timestamp if metadata else None
            ),
        })

    # Sorted by name so the output is deterministic rather than dependent on the
    # API server's incidental key ordering, which the API contract does not
    # guarantee.
    namespaces.sort(key=lambda entry: entry["name"])

    data = {
        "total": len(namespaces),
        "namespaces": namespaces,
    }

    return success(tool_name, data)
