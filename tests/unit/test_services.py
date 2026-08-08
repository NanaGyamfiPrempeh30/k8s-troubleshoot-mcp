"""Unit tests for tools/services module."""

from __future__ import annotations

import logging
from unittest import mock

import pytest
from kubernetes.client.exceptions import ApiException

from k8s_troubleshoot_mcp.config import ServerConfig
from k8s_troubleshoot_mcp.tools.services import (
    get_service,
    get_endpoints,
    _pod_name_from_target_ref,
    _is_truncated,
    OVER_CAPACITY_ANNOTATION,
)

# REQ-047b warnings are emitted on this module's logger.
SERVICES_LOGGER = "k8s_troubleshoot_mcp.tools.services"


@pytest.fixture
def config():
    """Create a test ServerConfig."""
    return ServerConfig(
        kubeconfig_path="/test/kubeconfig",
        allowed_namespaces=frozenset({"default", "staging", "prod"}),
        log_level="INFO",
        api_timeout_seconds=30,
        max_log_lines=200,
    )


@pytest.fixture
def mock_clients():
    """Create mock K8sClients."""
    clients = mock.MagicMock()
    clients.core_v1 = mock.MagicMock()
    return clients


def make_address(ip, pod_name=None, node_name=None):
    """Build a mock V1EndpointAddress."""
    address = mock.MagicMock()
    address.ip = ip
    address.node_name = node_name
    if pod_name is None:
        address.target_ref = None
    else:
        address.target_ref = mock.MagicMock()
        address.target_ref.name = pod_name
    return address


def make_subset(addresses=None, not_ready_addresses=None, ports=None):
    """Build a mock V1EndpointSubset."""
    subset = mock.MagicMock()
    subset.addresses = addresses
    subset.not_ready_addresses = not_ready_addresses
    subset.ports = ports
    return subset


def make_endpoints(subsets=None, annotations=None):
    """Build a mock V1Endpoints.

    annotations is set explicitly (default None) so no test depends on
    MagicMock's auto-attribute behavior for the over-capacity annotation.
    """
    endpoints = mock.MagicMock()
    endpoints.subsets = subsets
    endpoints.metadata = mock.MagicMock()
    endpoints.metadata.annotations = annotations
    return endpoints


class TestPodNameFromTargetRef:
    """Tests for _pod_name_from_target_ref helper."""

    def test_extracts_pod_name(self):
        """Returns target_ref.name when present."""
        assert _pod_name_from_target_ref(make_address("10.0.0.1", "web-abc")) == "web-abc"

    def test_returns_none_without_target_ref(self):
        """Returns None for addresses not backed by a pod."""
        assert _pod_name_from_target_ref(make_address("10.0.0.1")) is None


class TestIsTruncated:
    """Tests for _is_truncated helper. REQ-047a / REQ-047b / REQ-049a."""

    def test_annotation_absent(self):
        """No annotations dict at all means not truncated."""
        assert _is_truncated(make_endpoints(), "web", "default") is False

    def test_annotation_dict_present_but_key_absent(self):
        """Other annotations present, over-capacity key absent."""
        endpoints = make_endpoints(annotations={"foo": "bar"})
        assert _is_truncated(endpoints, "web", "default") is False

    def test_truncated_value(self):
        """Kubernetes 1.22+ writes 'truncated'."""
        endpoints = make_endpoints(annotations={OVER_CAPACITY_ANNOTATION: "truncated"})
        assert _is_truncated(endpoints, "web", "default") is True

    def test_warning_value(self):
        """Kubernetes 1.21 wrote 'warning' without truncating."""
        endpoints = make_endpoints(annotations={OVER_CAPACITY_ANNOTATION: "warning"})
        assert _is_truncated(endpoints, "web", "default") is True

    def test_unrecognized_value(self):
        """REQ-047b: an unrecognized value is not treated as truncation."""
        endpoints = make_endpoints(annotations={OVER_CAPACITY_ANNOTATION: "something"})
        assert _is_truncated(endpoints, "web", "default") is False

    def test_no_metadata(self):
        """A missing metadata block means not truncated."""
        endpoints = mock.MagicMock()
        endpoints.metadata = None
        assert _is_truncated(endpoints, "web", "default") is False


class TestUnrecognizedAnnotationWarning:
    """REQ-047b: unrecognized annotation values are surfaced operationally."""

    def test_unrecognized_value_logs_warning(self, caplog):
        """An unknown value must not be silently swallowed."""
        endpoints = make_endpoints(
            annotations={OVER_CAPACITY_ANNOTATION: "over-capacity-v2"}
        )

        with caplog.at_level(logging.WARNING, logger=SERVICES_LOGGER):
            result = _is_truncated(endpoints, "web", "default")

        assert result is False
        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert record.levelno == logging.WARNING
        assert "over-capacity-v2" in record.getMessage()
        assert "default/web" in record.getMessage()

    def test_recognized_value_logs_nothing(self, caplog):
        """A known value is not an operational anomaly."""
        endpoints = make_endpoints(annotations={OVER_CAPACITY_ANNOTATION: "truncated"})

        with caplog.at_level(logging.WARNING, logger=SERVICES_LOGGER):
            _is_truncated(endpoints, "web", "default")

        assert caplog.records == []

    def test_absent_annotation_logs_nothing(self, caplog):
        """The overwhelmingly common case must stay quiet."""
        with caplog.at_level(logging.WARNING, logger=SERVICES_LOGGER):
            _is_truncated(make_endpoints(), "web", "default")

        assert caplog.records == []

    def test_logged_value_is_escaped(self, caplog):
        """REQ-047b: a newline in the value must not forge a second log line."""
        endpoints = make_endpoints(
            annotations={
                OVER_CAPACITY_ANNOTATION: 'x\nWARNING forged line "injected"'
            }
        )

        with caplog.at_level(logging.WARNING, logger=SERVICES_LOGGER):
            _is_truncated(endpoints, "web", "default")

        message = caplog.records[0].getMessage()
        assert "\n" not in message
        assert "\\n" in message
        assert '\\"injected\\"' in message

    def test_warning_reaches_get_endpoints(self, config, mock_clients, caplog):
        """The warning fires through the public tool path, not just the helper."""
        mock_clients.core_v1.read_namespaced_endpoints.return_value = make_endpoints(
            subsets=[make_subset(addresses=[make_address("10.0.0.1")])],
            annotations={OVER_CAPACITY_ANNOTATION: "unexpected"},
        )

        with caplog.at_level(logging.WARNING, logger=SERVICES_LOGGER):
            result = get_endpoints(mock_clients, config, "web", "default")

        assert result["status"] == "success"
        assert result["data"]["truncated"] is False
        assert len(caplog.records) == 1

    def test_warning_reaches_get_service(self, config, mock_clients, caplog):
        """Same path via get_service's secondary endpoints read."""
        mock_clients.core_v1.read_namespaced_service.return_value = make_service()
        mock_clients.core_v1.read_namespaced_endpoints.return_value = make_endpoints(
            subsets=[make_subset(addresses=[make_address("10.0.0.1")])],
            annotations={OVER_CAPACITY_ANNOTATION: "unexpected"},
        )

        with caplog.at_level(logging.WARNING, logger=SERVICES_LOGGER):
            result = get_service(mock_clients, config, "web", "default")

        assert result["status"] == "success"
        assert result["data"]["truncated"] is False
        assert len(caplog.records) == 1


def make_service(
    service_type="ClusterIP",
    cluster_ip="10.96.0.10",
    external_ips=None,
    ports=None,
    selector=None,
):
    """Build a mock V1Service."""
    service = mock.MagicMock()
    service.spec.type = service_type
    service.spec.cluster_ip = cluster_ip
    service.spec.external_ips = external_ips
    service.spec.ports = ports if ports is not None else []
    service.spec.selector = selector if selector is not None else {}
    return service


class TestGetService:
    """Tests for get_service function."""

    def test_returns_correct_structure(self, config, mock_clients):
        """REQ-047: Returns required fields."""
        mock_port = mock.MagicMock()
        mock_port.port = 80
        mock_port.target_port = 8080
        mock_port.protocol = "TCP"
        mock_port.node_port = None

        mock_clients.core_v1.read_namespaced_service.return_value = make_service(
            ports=[mock_port], selector={"app": "web"}
        )
        mock_clients.core_v1.read_namespaced_endpoints.return_value = make_endpoints(
            subsets=[make_subset(addresses=[make_address("10.0.0.1", "web-1")])]
        )

        result = get_service(mock_clients, config, "web", "default")

        assert result["status"] == "success"
        data = result["data"]
        assert data["name"] == "web"
        assert data["namespace"] == "default"
        assert data["type"] == "ClusterIP"
        assert data["cluster_ip"] == "10.96.0.10"
        assert data["external_ips"] == []
        assert data["selector"] == {"app": "web"}
        assert data["ready_endpoints"] == 1
        assert data["truncated"] is False
        assert data["ports"] == [
            {"port": 80, "target_port": 8080, "protocol": "TCP", "node_port": None}
        ]

    def test_nodeport_service(self, config, mock_clients):
        """REQ-047: node_port is returned when applicable."""
        mock_port = mock.MagicMock()
        mock_port.port = 80
        mock_port.target_port = "http"
        mock_port.protocol = "TCP"
        mock_port.node_port = 31000

        mock_clients.core_v1.read_namespaced_service.return_value = make_service(
            service_type="NodePort",
            cluster_ip="10.96.0.11",
            external_ips=["192.0.2.5"],
            ports=[mock_port],
            selector={"app": "web"},
        )
        mock_clients.core_v1.read_namespaced_endpoints.return_value = make_endpoints()

        result = get_service(mock_clients, config, "web", "default")

        data = result["data"]
        assert data["type"] == "NodePort"
        assert data["external_ips"] == ["192.0.2.5"]
        assert data["ports"][0]["node_port"] == 31000
        assert data["ports"][0]["target_port"] == "http"

    def test_service_not_found(self, config, mock_clients):
        """REQ-048: Returns service_not_found error for 404."""
        mock_clients.core_v1.read_namespaced_service.side_effect = ApiException(
            status=404, reason="Not Found"
        )

        result = get_service(mock_clients, config, "missing", "default")

        assert result["status"] == "error"
        assert result["error"] == "service_not_found"
        assert "missing" in result["message"]

    def test_namespace_not_allowed(self, config, mock_clients):
        """Property 4: Disallowed namespace returns error without API call."""
        result = get_service(mock_clients, config, "web", "forbidden")

        assert result["status"] == "error"
        assert result["error"] == "namespace_not_allowed"
        mock_clients.core_v1.read_namespaced_service.assert_not_called()
        mock_clients.core_v1.read_namespaced_endpoints.assert_not_called()

    def test_truncated_annotation_sets_flag(self, config, mock_clients):
        """REQ-047a: over-capacity annotation sets truncated true."""
        mock_clients.core_v1.read_namespaced_service.return_value = make_service()
        mock_clients.core_v1.read_namespaced_endpoints.return_value = make_endpoints(
            subsets=[make_subset(addresses=[make_address("10.0.0.1")])],
            annotations={OVER_CAPACITY_ANNOTATION: "truncated"},
        )

        result = get_service(mock_clients, config, "web", "default")

        assert result["status"] == "success"
        assert result["data"]["truncated"] is True

    def test_warning_annotation_sets_flag(self, config, mock_clients):
        """REQ-047a: pre-1.22 'warning' value also sets truncated true."""
        mock_clients.core_v1.read_namespaced_service.return_value = make_service()
        mock_clients.core_v1.read_namespaced_endpoints.return_value = make_endpoints(
            subsets=[make_subset(addresses=[make_address("10.0.0.1")])],
            annotations={OVER_CAPACITY_ANNOTATION: "warning"},
        )

        result = get_service(mock_clients, config, "web", "default")

        assert result["data"]["truncated"] is True

    def test_annotation_absent_sets_flag_false(self, config, mock_clients):
        """REQ-047a: absent annotation sets truncated false, not None."""
        mock_clients.core_v1.read_namespaced_service.return_value = make_service()
        mock_clients.core_v1.read_namespaced_endpoints.return_value = make_endpoints(
            subsets=[make_subset(addresses=[make_address("10.0.0.1")])],
            annotations={"other.io/annotation": "value"},
        )

        result = get_service(mock_clients, config, "web", "default")

        assert result["data"]["truncated"] is False

    def test_missing_endpoints_does_not_fail_service(self, config, mock_clients):
        """A service with no endpoints object still returns success."""
        mock_clients.core_v1.read_namespaced_service.return_value = make_service(
            selector={"app": "web"}
        )
        mock_clients.core_v1.read_namespaced_endpoints.side_effect = ApiException(
            status=404, reason="Not Found"
        )

        result = get_service(mock_clients, config, "web", "default")

        assert result["status"] == "success"
        assert result["data"]["ready_endpoints"] == 0
        assert result["data"]["truncated"] is False

    def test_endpoints_error_yields_none_count_and_none_truncated(
        self, config, mock_clients
    ):
        """REQ-047a: a non-404 endpoints failure reports None for both fields."""
        mock_clients.core_v1.read_namespaced_service.return_value = make_service()
        mock_clients.core_v1.read_namespaced_endpoints.side_effect = ApiException(
            status=403, reason="Forbidden"
        )

        result = get_service(mock_clients, config, "web", "default")

        assert result["status"] == "success"
        assert result["data"]["ready_endpoints"] is None
        assert result["data"]["truncated"] is None

    def test_ready_endpoints_sums_across_subsets(self, config, mock_clients):
        """ready_endpoints counts addresses across all subsets."""
        mock_clients.core_v1.read_namespaced_service.return_value = make_service()
        mock_clients.core_v1.read_namespaced_endpoints.return_value = make_endpoints(
            subsets=[
                make_subset(addresses=[make_address("10.0.0.1"), make_address("10.0.0.2")]),
                make_subset(addresses=[make_address("10.0.0.3")]),
            ]
        )

        result = get_service(mock_clients, config, "web", "default")

        assert result["data"]["ready_endpoints"] == 3

    def test_uses_request_timeout(self, config, mock_clients):
        """_request_timeout is passed to both API calls."""
        mock_clients.core_v1.read_namespaced_service.return_value = make_service()
        mock_clients.core_v1.read_namespaced_endpoints.return_value = make_endpoints()

        get_service(mock_clients, config, "web", "default")

        service_kwargs = mock_clients.core_v1.read_namespaced_service.call_args.kwargs
        endpoints_kwargs = mock_clients.core_v1.read_namespaced_endpoints.call_args.kwargs
        assert service_kwargs["_request_timeout"] == 30
        assert endpoints_kwargs["_request_timeout"] == 30

    def test_api_exception_handled(self, config, mock_clients):
        """Property 7: ApiException returns structured error."""
        mock_clients.core_v1.read_namespaced_service.side_effect = ApiException(
            status=403, reason="Forbidden"
        )

        result = get_service(mock_clients, config, "web", "default")

        assert result["status"] == "error"
        assert result["error"] == "kubernetes_api_error"
        assert result["http_status"] == 403


class TestGetEndpoints:
    """Tests for get_endpoints function."""

    def test_returns_correct_structure(self, config, mock_clients):
        """REQ-049: Returns ready, not_ready, ports, and truncated."""
        mock_port = mock.MagicMock()
        mock_port.port = 8080
        mock_port.protocol = "TCP"

        mock_clients.core_v1.read_namespaced_endpoints.return_value = make_endpoints(
            subsets=[
                make_subset(
                    addresses=[make_address("10.0.0.1", "web-1", "node-a")],
                    not_ready_addresses=[make_address("10.0.0.2", "web-2", "node-b")],
                    ports=[mock_port],
                )
            ]
        )

        result = get_endpoints(mock_clients, config, "web", "default")

        assert result["status"] == "success"
        data = result["data"]
        assert data["service_name"] == "web"
        assert data["namespace"] == "default"
        assert data["ready"] == [
            {"ip": "10.0.0.1", "pod_name": "web-1", "node_name": "node-a"}
        ]
        assert data["not_ready"] == [{"ip": "10.0.0.2", "pod_name": "web-2"}]
        assert data["ports"] == [{"port": 8080, "protocol": "TCP"}]
        assert data["truncated"] is False

    def test_truncated_annotation_sets_flag(self, config, mock_clients):
        """REQ-049a: over-capacity annotation sets truncated true."""
        mock_clients.core_v1.read_namespaced_endpoints.return_value = make_endpoints(
            subsets=[make_subset(addresses=[make_address("10.0.0.1", "web-1")])],
            annotations={OVER_CAPACITY_ANNOTATION: "truncated"},
        )

        result = get_endpoints(mock_clients, config, "web", "default")

        assert result["status"] == "success"
        assert result["data"]["truncated"] is True
        # Property 14 still holds against the object as read
        assert len(result["data"]["ready"]) == 1

    def test_warning_annotation_sets_flag(self, config, mock_clients):
        """REQ-049a: pre-1.22 'warning' value also sets truncated true."""
        mock_clients.core_v1.read_namespaced_endpoints.return_value = make_endpoints(
            subsets=[make_subset(addresses=[make_address("10.0.0.1")])],
            annotations={OVER_CAPACITY_ANNOTATION: "warning"},
        )

        result = get_endpoints(mock_clients, config, "web", "default")

        assert result["data"]["truncated"] is True

    def test_annotation_absent_sets_flag_false(self, config, mock_clients):
        """REQ-049a: absent annotation sets truncated false."""
        mock_clients.core_v1.read_namespaced_endpoints.return_value = make_endpoints(
            subsets=[make_subset(addresses=[make_address("10.0.0.1")])],
            annotations={"other.io/annotation": "value"},
        )

        result = get_endpoints(mock_clients, config, "web", "default")

        assert result["data"]["truncated"] is False

    def test_not_ready_omits_node_name(self, config, mock_clients):
        """design.md get_endpoints model: not_ready carries ip and pod_name only."""
        mock_clients.core_v1.read_namespaced_endpoints.return_value = make_endpoints(
            subsets=[
                make_subset(
                    not_ready_addresses=[make_address("10.0.0.2", "web-2", "node-b")]
                )
            ]
        )

        result = get_endpoints(mock_clients, config, "web", "default")

        assert set(result["data"]["not_ready"][0].keys()) == {"ip", "pod_name"}

    def test_endpoints_not_found(self, config, mock_clients):
        """REQ-050: Returns structured error when no endpoints object exists."""
        mock_clients.core_v1.read_namespaced_endpoints.side_effect = ApiException(
            status=404, reason="Not Found"
        )

        result = get_endpoints(mock_clients, config, "missing", "default")

        assert result["status"] == "error"
        assert result["error"] == "endpoints_not_found"
        assert "missing" in result["message"]

    def test_no_ready_addresses_is_success(self, config, mock_clients):
        """REQ-050: Zero ready addresses is success, not an error."""
        mock_clients.core_v1.read_namespaced_endpoints.return_value = make_endpoints(
            subsets=[
                make_subset(
                    addresses=None,
                    not_ready_addresses=[
                        make_address("10.0.0.2", "web-2"),
                        make_address("10.0.0.3", "web-3"),
                    ],
                )
            ]
        )

        result = get_endpoints(mock_clients, config, "web", "default")

        assert result["status"] == "success"
        assert result["data"]["ready"] == []
        assert len(result["data"]["not_ready"]) == 2

    def test_partitions_across_multiple_subsets(self, config, mock_clients):
        """Property 14: N ready and M not-ready produce exactly N and M entries."""
        mock_clients.core_v1.read_namespaced_endpoints.return_value = make_endpoints(
            subsets=[
                make_subset(
                    addresses=[make_address("10.0.0.1"), make_address("10.0.0.2")],
                    not_ready_addresses=[make_address("10.0.0.3")],
                ),
                make_subset(
                    addresses=[make_address("10.0.0.4")],
                    not_ready_addresses=[
                        make_address("10.0.0.5"),
                        make_address("10.0.0.6"),
                    ],
                ),
            ]
        )

        result = get_endpoints(mock_clients, config, "web", "default")

        assert len(result["data"]["ready"]) == 3
        assert len(result["data"]["not_ready"]) == 3

    def test_empty_subsets(self, config, mock_clients):
        """An endpoints object with no subsets yields empty partitions."""
        mock_clients.core_v1.read_namespaced_endpoints.return_value = make_endpoints()

        result = get_endpoints(mock_clients, config, "web", "default")

        assert result["status"] == "success"
        assert result["data"]["ready"] == []
        assert result["data"]["not_ready"] == []
        assert result["data"]["ports"] == []
        assert result["data"]["truncated"] is False

    def test_address_without_target_ref(self, config, mock_clients):
        """Manually managed endpoints have no backing pod."""
        mock_clients.core_v1.read_namespaced_endpoints.return_value = make_endpoints(
            subsets=[make_subset(addresses=[make_address("192.0.2.9")])]
        )

        result = get_endpoints(mock_clients, config, "external", "default")

        assert result["data"]["ready"][0]["pod_name"] is None

    def test_namespace_not_allowed(self, config, mock_clients):
        """Property 4: Disallowed namespace returns error without API call."""
        result = get_endpoints(mock_clients, config, "web", "forbidden")

        assert result["status"] == "error"
        assert result["error"] == "namespace_not_allowed"
        mock_clients.core_v1.read_namespaced_endpoints.assert_not_called()

    def test_uses_request_timeout(self, config, mock_clients):
        """_request_timeout is passed to API call."""
        mock_clients.core_v1.read_namespaced_endpoints.return_value = make_endpoints()

        get_endpoints(mock_clients, config, "web", "default")

        call_kwargs = mock_clients.core_v1.read_namespaced_endpoints.call_args.kwargs
        assert call_kwargs["_request_timeout"] == 30

    def test_api_exception_handled(self, config, mock_clients):
        """Property 7: ApiException returns structured error."""
        mock_clients.core_v1.read_namespaced_endpoints.side_effect = ApiException(
            status=403, reason="Forbidden"
        )

        result = get_endpoints(mock_clients, config, "web", "default")

        assert result["status"] == "error"
        assert result["error"] == "kubernetes_api_error"
        assert result["http_status"] == 403
