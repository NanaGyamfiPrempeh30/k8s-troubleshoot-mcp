"""Response construction helpers for k8s-troubleshoot-mcp.

All tools use these functions to ensure uniform response structure
per REQ-015 and REQ-016.
"""

from __future__ import annotations

import json
from typing import Any


def success(tool_name: str, data: dict[str, Any]) -> dict[str, Any]:
    """Construct a success response envelope.

    Args:
        tool_name: Name of the tool returning the response.
        data: Tool-specific response data.

    Returns:
        Structured response dict with tool, status="success", and data fields.
    """
    return {
        "tool": tool_name,
        "status": "success",
        "data": data,
    }


def error(
    tool_name: str,
    error_code: str,
    message: str,
    **extra: Any,
) -> dict[str, Any]:
    """Construct an error response envelope.

    Args:
        tool_name: Name of the tool returning the response.
        error_code: Machine-readable error code (e.g., "pod_not_found").
        message: Human-readable error description.
        **extra: Additional fields to include in the response.

    Returns:
        Structured response dict with tool, status="error", error, message,
        and any extra fields.
    """
    response = {
        "tool": tool_name,
        "status": "error",
        "error": error_code,
        "message": message,
    }
    response.update(extra)
    return response


def namespace_not_allowed(
    tool_name: str,
    namespace: str,
    allowed: frozenset[str],
) -> dict[str, Any]:
    """Construct a namespace-not-allowed error response.

    Per REQ-007, this error is returned when a tool is called with a namespace
    not in the allowed set.

    Args:
        tool_name: Name of the tool returning the response.
        namespace: The disallowed namespace that was requested.
        allowed: The set of allowed namespaces.

    Returns:
        Structured error response with namespace_not_allowed error code
        and the list of allowed namespaces.
    """
    return error(
        tool_name,
        "namespace_not_allowed",
        f"Namespace '{namespace}' is not in the allowed list for this deployment.",
        allowed_namespaces=sorted(allowed),
    )


def api_exception_error(
    tool_name: str,
    status: int,
    reason: str,
    message: str,
) -> dict[str, Any]:
    """Construct an error response for Kubernetes API exceptions.

    Per REQ-017, this captures HTTP status code and reason from ApiException.

    Args:
        tool_name: Name of the tool returning the response.
        status: HTTP status code from the API response.
        reason: HTTP reason phrase (e.g., "Not Found").
        message: Human-readable error description.

    Returns:
        Structured error response with kubernetes_api_error code,
        http_status, and reason fields.
    """
    return error(
        tool_name,
        "kubernetes_api_error",
        message,
        http_status=status,
        reason=reason,
    )


def connection_error(tool_name: str, message: str) -> dict[str, Any]:
    """Construct an error response for connection failures.

    Per REQ-018, connection errors and timeouts return structured errors.

    Args:
        tool_name: Name of the tool returning the response.
        message: Human-readable error description.

    Returns:
        Structured error response with connection_error code.
    """
    return error(tool_name, "connection_error", message)


def serialize_log_content(raw: str) -> str:
    """JSON-encode raw log content for safe inclusion in responses.

    Per REQ-020 and REQ-027, log content must be serialized so all special
    characters (<, >, ", \\, Unicode control characters) are properly escaped.

    This is a structural, deterministic mitigation against prompt injection
    via log content. REQ-063 (MCP instructions) provides a second, independent
    advisory layer — defense in depth requires both.

    Args:
        raw: Raw log string to serialize.

    Returns:
        JSON-encoded string with all special characters escaped.
        The outer quotes from json.dumps are stripped.
    """
    # json.dumps escapes:
    # - " \ (required by JSON spec)
    # - Unicode control characters U+0000-U+001F (required by JSON spec)
    encoded = json.dumps(raw, ensure_ascii=False)

    # Strip the surrounding quotes added by json.dumps
    # json.dumps("test") returns '"test"', we want 'test' with escapes preserved
    result = encoded[1:-1]

    # Escape < and > as Unicode escapes (structural prompt injection mitigation)
    # json.dumps doesn't do this, so we do it manually
    result = result.replace("<", "\\u003c")
    result = result.replace(">", "\\u003e")

    return result
