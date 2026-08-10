"""Environment variable validation and configuration for k8s-troubleshoot-mcp."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

# System namespaces that are never allowed (REQ-008)
_SYSTEM_NAMESPACES = frozenset({"kube-system", "kube-public"})

# Wildcard tokens that are rejected (REQ-005)
_WILDCARD_TOKENS = frozenset({"*", "all"})

# Hard ceiling for max log lines (REQ-071)
_MAX_LOG_LINES_CEILING = 1000


@dataclass(frozen=True)
class ServerConfig:
    """Immutable server configuration validated at startup."""

    kubeconfig_path: str
    allowed_namespaces: frozenset[str]
    log_level: str
    api_timeout_seconds: int
    max_log_lines: int


def _fatal(message: str) -> None:
    """Write message to stderr and exit with code 1."""
    sys.stderr.write(message + "\n")
    sys.exit(1)


def _validate_kubeconfig() -> str:
    """Validate KUBECONFIG environment variable.

    Returns the validated kubeconfig path.
    Exits with code 1 if validation fails.
    """
    kubeconfig = os.environ.get("KUBECONFIG", "")

    # REQ-001: KUBECONFIG must be set and non-empty
    if not kubeconfig:
        _fatal(
            "KUBECONFIG environment variable is not set. Run "
            "'scripts/generate-kubeconfig.sh <output-path> <namespace> [namespace...]' to "
            "provision the service account and generate its kubeconfig. Do not apply "
            "kubernetes/ manually: role.yaml is namespaced and rolebinding.yaml.template "
            "requires substitution, so a blanket 'kubectl apply -f kubernetes/' reports "
            "success while leaving the server unable to read anything. Then set "
            "KUBECONFIG=<output-path> before starting the server."
        )

    # REQ-002: File must exist and be readable
    if not os.path.isfile(kubeconfig) or not os.access(kubeconfig, os.R_OK):
        _fatal(f"KUBECONFIG path '{kubeconfig}' does not exist or is not readable.")

    return kubeconfig


def _validate_allowed_namespaces() -> frozenset[str]:
    """Validate ALLOWED_NAMESPACES environment variable.

    Returns a frozenset of allowed namespace names.
    Exits with code 1 if validation fails.
    """
    allowed_ns = os.environ.get("ALLOWED_NAMESPACES", "")

    # REQ-004: ALLOWED_NAMESPACES must be set and non-empty
    if not allowed_ns:
        _fatal(
            "ALLOWED_NAMESPACES environment variable is not set. Set it to a "
            "comma-separated list of namespaces this server is permitted to read from "
            "(e.g. ALLOWED_NAMESPACES=staging,production). Wildcard '*' is not accepted."
        )

    # Parse comma-separated list and strip whitespace
    namespaces = {ns.strip() for ns in allowed_ns.split(",") if ns.strip()}

    # REQ-005: Reject wildcard tokens
    wildcards_found = namespaces & _WILDCARD_TOKENS
    if wildcards_found:
        _fatal(
            f"ALLOWED_NAMESPACES contains wildcard token(s): {sorted(wildcards_found)}. "
            "Wildcard namespace access is not permitted."
        )

    # REQ-008: Remove system namespaces and warn
    system_ns_found = namespaces & _SYSTEM_NAMESPACES
    if system_ns_found:
        sys.stderr.write(
            "WARNING: kube-system and kube-public are not permitted in ALLOWED_NAMESPACES "
            "and have been removed from the allowed set.\n"
        )
        namespaces = namespaces - _SYSTEM_NAMESPACES

    # Ensure we still have namespaces after filtering
    if not namespaces:
        _fatal(
            "ALLOWED_NAMESPACES is empty after removing system namespaces. "
            "Provide at least one valid namespace."
        )

    return frozenset(namespaces)


def _validate_log_level() -> str:
    """Validate LOG_LEVEL environment variable.

    Returns the log level string (default INFO).
    """
    valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR"}
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()

    if log_level not in valid_levels:
        # Default to INFO if invalid
        return "INFO"

    return log_level


def _validate_api_timeout() -> int:
    """Validate API_TIMEOUT_SECONDS environment variable.

    Returns the timeout in seconds (default 30).
    Exits with code 1 if validation fails.
    """
    timeout_str = os.environ.get("API_TIMEOUT_SECONDS", "")

    if not timeout_str:
        return 30

    # REQ-070: Must be a positive integer
    try:
        timeout = int(timeout_str)
        if timeout <= 0:
            raise ValueError("non-positive")
    except ValueError:
        _fatal(
            f"API_TIMEOUT_SECONDS must be a positive integer (e.g. API_TIMEOUT_SECONDS=30). "
            f"Got: '{timeout_str}'."
        )

    return timeout


def _validate_max_log_lines() -> int:
    """Validate MAX_LOG_LINES environment variable.

    Returns the max log lines (default 200, hard ceiling 1000).
    Exits with code 1 if validation fails.
    """
    max_lines_str = os.environ.get("MAX_LOG_LINES", "")

    if not max_lines_str:
        return 200

    # REQ-071: Must be a positive integer
    try:
        max_lines = int(max_lines_str)
        if max_lines <= 0:
            raise ValueError("non-positive")
    except ValueError:
        _fatal(
            f"MAX_LOG_LINES must be a positive integer. Got: '{max_lines_str}'."
        )

    # REQ-071: Clamp to hard ceiling with warning
    if max_lines > _MAX_LOG_LINES_CEILING:
        sys.stderr.write(
            f"WARNING: MAX_LOG_LINES value {max_lines} exceeds the hard ceiling of "
            f"{_MAX_LOG_LINES_CEILING} and has been clamped to {_MAX_LOG_LINES_CEILING}.\n"
        )
        max_lines = _MAX_LOG_LINES_CEILING

    return max_lines


def validate_env() -> ServerConfig:
    """Validate all environment variables and return ServerConfig.

    Reads and validates KUBECONFIG, ALLOWED_NAMESPACES, LOG_LEVEL,
    API_TIMEOUT_SECONDS, MAX_LOG_LINES.

    Writes error messages to stderr and calls sys.exit(1) on failure.
    Returns a frozen ServerConfig on success.
    """
    kubeconfig_path = _validate_kubeconfig()
    allowed_namespaces = _validate_allowed_namespaces()
    log_level = _validate_log_level()
    api_timeout_seconds = _validate_api_timeout()
    max_log_lines = _validate_max_log_lines()

    return ServerConfig(
        kubeconfig_path=kubeconfig_path,
        allowed_namespaces=allowed_namespaces,
        log_level=log_level,
        api_timeout_seconds=api_timeout_seconds,
        max_log_lines=max_log_lines,
    )
