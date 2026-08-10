"""Entry point for k8s-troubleshoot-mcp.

Startup sequence per design.md:
    validate_env() -> build_clients() -> create_app() -> mcp.run(transport="stdio")
"""

from __future__ import annotations

import logging
import sys

from kubernetes.config.config_exception import ConfigException
from yaml import YAMLError

from k8s_troubleshoot_mcp.config import validate_env
from k8s_troubleshoot_mcp.k8s_client import build_clients
from k8s_troubleshoot_mcp.server import create_app


def configure_logging(log_level: str) -> None:
    """Route all logging to stderr.

    REQ-010: stdout is reserved exclusively for MCP JSON-RPC messages, so a
    single log line on stdout corrupts the protocol stream. logging.basicConfig
    defaults to stderr, but that default is easy to lose to a later
    basicConfig call or a library that installs its own root handler — so the
    handler is constructed explicitly and any pre-existing root handlers are
    replaced rather than appended to.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(getattr(logging, log_level, logging.INFO))


def main() -> None:
    """Entry point registered as a console script in pyproject.toml."""
    # REQ-001 through REQ-010, REQ-069 through REQ-071. Writes any failure to
    # stderr and exits 1; never returns a partially-valid config.
    config = validate_env()

    configure_logging(config.log_level)
    logger = logging.getLogger(__name__)

    # REQ-003: explicit kubeconfig path only, no fallback chain.
    # REQ-002a: a file that exists and is readable can still be unparseable or
    # structurally not a kubeconfig. Both surface from the client library as
    # exceptions that would otherwise reach the operator as a traceback naming
    # internal library paths instead of a diagnosis.
    try:
        clients = build_clients(config.kubeconfig_path)
    except (ConfigException, YAMLError) as exc:
        # The reason is included, but never the file's contents: a kubeconfig
        # holds a bearer token for the cluster, and quoting the offending line
        # would write it to the terminal and to anything collecting stderr.
        # PyYAML's message carries path, line and column only — pinned by
        # TestReq002aMalformedKubeconfig.test_message_never_quotes_the_file.
        # Collapsed to one line: PyYAML embeds a newline before its
        # "in <file>, line N, column M" clause, and REQ-002a specifies a
        # single-line error so the diagnosis stays greppable in an operator's
        # log rather than arriving as a fragment.
        reason = " ".join(f"{type(exc).__name__}: {exc}".split())
        sys.stderr.write(
            f"KUBECONFIG file '{config.kubeconfig_path}' is not a valid "
            f"kubeconfig: {reason}\n"
        )
        sys.exit(1)

    app = create_app(config, clients)

    logger.info(
        "k8s-troubleshoot-mcp starting on stdio transport; allowed namespaces: %s",
        ", ".join(sorted(config.allowed_namespaces)),
    )

    # stdio transport: the MCP library owns stdout from here on.
    app.run(transport="stdio")


if __name__ == "__main__":
    main()
