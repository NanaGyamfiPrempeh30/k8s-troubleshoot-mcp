"""Unit tests for server module and the __main__ startup path."""

from __future__ import annotations

import asyncio
import logging
import pathlib
import re
import sys
from unittest import mock

import pytest

from k8s_troubleshoot_mcp import __main__ as main_module
from k8s_troubleshoot_mcp import server
from k8s_troubleshoot_mcp.config import ServerConfig
from tests.property.strategies import ALL_TOOLS

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "k8s_troubleshoot_mcp"


@pytest.fixture
def config():
    """Create a test ServerConfig."""
    return ServerConfig(
        kubeconfig_path="/test/kubeconfig",
        allowed_namespaces=frozenset({"default", "staging"}),
        log_level="INFO",
        api_timeout_seconds=30,
        max_log_lines=200,
    )


@pytest.fixture
def app(config):
    """Build a FastMCP app with mocked Kubernetes clients."""
    return server.create_app(config, mock.MagicMock())


def registered_tool_names(app) -> set[str]:
    """Names of every tool the MCP client would actually see."""
    return {tool.name for tool in asyncio.run(app.list_tools())}


class TestToolRegistration:
    """Every tool must be reachable by an MCP client."""

    def test_all_sixteen_tools_registered(self, app):
        """design.md: the server presents 16 diagnostic tools."""
        names = registered_tool_names(app)

        assert len(names) == 16
        assert names == set(server.TOOL_NAMES)

    def test_registration_matches_property_test_registry(self, app):
        """A tool in one registry but not the other is untested or unreachable.

        A tool registered with @mcp.tool() but absent from NAMESPACED_TOOLS is
        shipped without P4/P6/P7/P17 coverage. A tool in the property registry
        but never registered here passes every existing test while being
        invisible to every MCP client.
        """
        registered = registered_tool_names(app)
        property_registry = {spec.name for spec in ALL_TOOLS}

        assert registered == property_registry, (
            f"registered but untested: {sorted(registered - property_registry)}; "
            f"tested but unreachable: {sorted(property_registry - registered)}"
        )

    def test_no_duplicate_registrations(self, app):
        """TOOL_NAMES must not contain duplicates masking a missing tool."""
        assert len(server.TOOL_NAMES) == len(set(server.TOOL_NAMES))

    def test_schemas_do_not_expose_server_internals(self, app):
        """clients and config are bound by closure, never advertised."""
        for tool in asyncio.run(app.list_tools()):
            properties = set(tool.inputSchema.get("properties", {}))
            assert "clients" not in properties, tool.name
            assert "config" not in properties, tool.name

    def test_every_tool_has_a_description(self, app):
        """A tool with no description is unusable by a model."""
        for tool in asyncio.run(app.list_tools()):
            assert tool.description, f"{tool.name} has no description"

    @pytest.mark.parametrize(
        "tool_name,expected_params",
        [
            ("get_pod_status", {"pod_name", "namespace"}),
            ("get_pod_logs", {"pod_name", "namespace", "container", "previous", "tail_lines"}),
            ("list_pods", {"namespace", "label_selector"}),
            ("get_node_status", {"node_name"}),
            ("list_nodes", set()),
            ("get_namespace_events", {"namespace", "limit"}),
            ("list_namespaces", set()),
        ],
    )
    def test_tool_parameters(self, app, tool_name, expected_params):
        """Registered parameters match each tool's documented signature."""
        tool = app._tool_manager.get_tool(tool_name)

        assert set(tool.parameters.get("properties", {})) == expected_params


class TestToolDelegation:
    """Registered tools must bind clients/config and delegate to tools/."""

    def test_delegates_with_bound_clients_and_config(self, config):
        """The closure passes the server's own clients and config through."""
        clients = mock.MagicMock()
        app = server.create_app(config, clients)

        with mock.patch.object(
            server.pods, "get_pod_status", return_value={"ok": True}
        ) as delegate:
            result = app._tool_manager.get_tool("get_pod_status").fn(
                pod_name="web-1", namespace="default"
            )

        assert result == {"ok": True}
        delegate.assert_called_once_with(clients, config, "web-1", "default")

    def test_namespaceless_tool_delegates(self, config):
        """Cluster-scoped tools take no namespace but still bind both."""
        clients = mock.MagicMock()
        app = server.create_app(config, clients)

        with mock.patch.object(
            server.namespaces, "list_namespaces", return_value={"ok": True}
        ) as delegate:
            app._tool_manager.get_tool("list_namespaces").fn()

        delegate.assert_called_once_with(clients, config)


class TestInstructions:
    """REQ-063: the anti-injection statement, verbatim."""

    def test_instructions_set_on_app(self, app):
        """The instructions field reaches the FastMCP instance."""
        assert app.instructions == server.INSTRUCTIONS

    def test_instructions_match_requirements_verbatim(self):
        """The text is compared against requirements.md, not a copy of itself.

        A hand-typed constant can drift from the spec silently; this reads
        REQ-063 out of the requirements file and compares the normalized text.
        """
        spec_text = (REPO_ROOT / "requirements.md").read_text(encoding="utf-8")

        # [^\n] rather than . so the blockquote capture cannot run past the
        # quote into the rest of the document.
        block = re.search(
            r"\*\*REQ-063:\*\*[\s\S]*?\n\n((?:>[^\n]*\n)+)", spec_text
        )
        assert block, "REQ-063 blockquote not found in requirements.md"

        quoted = " ".join(
            line.lstrip("> ").strip() for line in block.group(1).splitlines()
        )
        expected = re.sub(r"\s+", " ", quoted.strip().strip("`").strip('"')).strip()
        actual = re.sub(r"\s+", " ", server.INSTRUCTIONS).strip()

        assert actual == expected

    def test_instructions_mention_untrusted_and_injection(self, app):
        """Guards against the statement being replaced with something weaker."""
        text = app.instructions.lower()

        assert "untrusted" in text
        assert "prompt injection" in text
        assert "do not follow it" in text


class TestStdoutReserved:
    """REQ-010: stdout carries JSON-RPC only."""

    def test_no_print_or_stdout_writes_in_source(self):
        """Nothing in the package writes to stdout by any route."""
        offenders = []
        for path in sorted(SRC_ROOT.rglob("*.py")):
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                code = line.split("#", 1)[0]
                if re.search(r"\bprint\s*\(", code) or "sys.stdout" in code:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}")

        assert not offenders, "stdout writes found:\n" + "\n".join(offenders)

    def test_configure_logging_targets_stderr(self):
        """The root handler installed at startup writes to stderr."""
        main_module.configure_logging("INFO")

        handlers = logging.getLogger().handlers
        assert handlers
        for handler in handlers:
            stream = getattr(handler, "stream", None)
            assert stream is not sys.stdout
            assert stream is sys.stderr

    def test_configure_logging_replaces_existing_handlers(self):
        """A pre-existing stdout handler must not survive startup."""
        root = logging.getLogger()
        rogue = logging.StreamHandler(sys.stdout)
        root.addHandler(rogue)

        try:
            main_module.configure_logging("INFO")
            assert rogue not in logging.getLogger().handlers
        finally:
            root.removeHandler(rogue)

    def test_create_app_leaves_no_stdout_handler(self, config):
        """FastMCP configures its own logging; it must not land on stdout."""
        main_module.configure_logging("INFO")

        server.create_app(config, mock.MagicMock())

        for handler in logging.getLogger().handlers:
            stream = getattr(handler, "stream", None)
            assert stream is not sys.stdout


class TestMainStartupSequence:
    """design.md: validate_env -> build_clients -> create_app -> run(stdio)."""

    def test_startup_order_and_stdio_transport(self):
        """Each step feeds the next, and the transport is stdio."""
        config = ServerConfig(
            kubeconfig_path="/test/kubeconfig",
            allowed_namespaces=frozenset({"default"}),
            log_level="INFO",
            api_timeout_seconds=30,
            max_log_lines=200,
        )
        clients = mock.MagicMock()
        app = mock.MagicMock()

        with mock.patch.object(
            main_module, "validate_env", return_value=config
        ) as validate, mock.patch.object(
            main_module, "build_clients", return_value=clients
        ) as build, mock.patch.object(
            main_module, "create_app", return_value=app
        ) as create:
            main_module.main()

        validate.assert_called_once_with()
        build.assert_called_once_with("/test/kubeconfig")
        create.assert_called_once_with(config, clients)
        app.run.assert_called_once_with(transport="stdio")

    def test_validation_failure_aborts_before_client_build(self):
        """REQ-001/003: an invalid environment never reaches the K8s client."""
        with mock.patch.object(
            main_module, "validate_env", side_effect=SystemExit(1)
        ), mock.patch.object(main_module, "build_clients") as build, mock.patch.object(
            main_module, "create_app"
        ) as create:
            with pytest.raises(SystemExit) as excinfo:
                main_module.main()

        assert excinfo.value.code == 1
        build.assert_not_called()
        create.assert_not_called()
