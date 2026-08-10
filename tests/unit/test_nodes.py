"""Unit tests for tools/nodes module."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest import mock

import pytest
from kubernetes.client.exceptions import ApiException

from k8s_troubleshoot_mcp.config import ServerConfig
from k8s_troubleshoot_mcp.tools.nodes import (
    get_node_status,
    list_nodes,
    _extract_node_roles,
    _get_ready_status,
)


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
def mock_clients():
    """Create mock K8sClients."""
    clients = mock.MagicMock()
    clients.core_v1 = mock.MagicMock()
    return clients


class TestExtractNodeRoles:
    """Tests for _extract_node_roles helper."""

    def test_extracts_single_role(self):
        """Extracts single role from labels."""
        labels = {"node-role.kubernetes.io/control-plane": ""}
        assert _extract_node_roles(labels) == ["control-plane"]

    def test_extracts_multiple_roles(self):
        """Extracts multiple roles, sorted alphabetically."""
        labels = {
            "node-role.kubernetes.io/worker": "",
            "node-role.kubernetes.io/control-plane": "",
        }
        assert _extract_node_roles(labels) == ["control-plane", "worker"]

    def test_ignores_non_role_labels(self):
        """Ignores labels without role prefix."""
        labels = {
            "kubernetes.io/hostname": "node-1",
            "node-role.kubernetes.io/worker": "",
        }
        assert _extract_node_roles(labels) == ["worker"]

    def test_empty_labels(self):
        """Returns empty list for empty labels."""
        assert _extract_node_roles({}) == []
        assert _extract_node_roles(None) == []


class TestGetReadyStatus:
    """Tests for _get_ready_status helper."""

    def test_returns_ready_status(self):
        """Returns Ready condition status."""
        conditions = [
            mock.MagicMock(type="Ready", status="True"),
            mock.MagicMock(type="DiskPressure", status="False"),
        ]
        assert _get_ready_status(conditions) == "True"

    def test_returns_unknown_when_no_ready(self):
        """Returns Unknown when no Ready condition."""
        conditions = [
            mock.MagicMock(type="DiskPressure", status="False"),
        ]
        assert _get_ready_status(conditions) == "Unknown"

    def test_returns_unknown_for_empty(self):
        """Returns Unknown for empty conditions."""
        assert _get_ready_status([]) == "Unknown"
        assert _get_ready_status(None) == "Unknown"


class TestGetNodeStatus:
    """Tests for get_node_status function."""

    def test_returns_correct_structure(self, config, mock_clients):
        """REQ-035: Returns required fields."""
        # Create mock node
        mock_node = mock.MagicMock()
        mock_node.metadata.labels = {"node-role.kubernetes.io/control-plane": ""}

        mock_node.status.conditions = [
            mock.MagicMock(
                type="Ready",
                status="True",
                reason="KubeletReady",
                message="kubelet is posting ready status",
            ),
        ]
        mock_node.status.capacity = {"cpu": "4", "memory": "16Gi"}
        mock_node.status.allocatable = {"cpu": "3900m", "memory": "15Gi"}
        mock_node.status.node_info.kubelet_version = "v1.29.0"

        mock_node.spec.taints = [
            mock.MagicMock(key="node-role.kubernetes.io/control-plane", value=None, effect="NoSchedule"),
        ]
        mock_node.spec.unschedulable = None  # schedulable: API omits the field

        mock_clients.core_v1.read_node.return_value = mock_node

        result = get_node_status(mock_clients, config, "node-1")

        assert result["status"] == "success"
        data = result["data"]
        assert data["name"] == "node-1"
        assert len(data["conditions"]) == 1
        assert data["conditions"][0]["type"] == "Ready"
        assert data["conditions"][0]["status"] == "True"
        assert data["capacity"] == {"cpu": "4", "memory": "16Gi"}
        assert data["allocatable"] == {"cpu": "3900m", "memory": "15Gi"}
        assert len(data["taints"]) == 1
        assert data["unschedulable"] is False
        assert data["roles"] == ["control-plane"]
        assert data["kubelet_version"] == "v1.29.0"

    def test_node_not_found(self, config, mock_clients):
        """REQ-037: Returns node_not_found error for 404."""
        mock_clients.core_v1.read_node.side_effect = ApiException(
            status=404, reason="Not Found"
        )

        result = get_node_status(mock_clients, config, "missing-node")

        assert result["status"] == "error"
        assert result["error"] == "node_not_found"
        assert "missing-node" in result["message"]

    def test_no_namespace_check(self, config, mock_clients):
        """REQ-036: No ALLOWED_NAMESPACES check for cluster-scoped resource."""
        mock_node = mock.MagicMock()
        mock_node.metadata.labels = {}
        mock_node.status.conditions = []
        mock_node.status.capacity = {}
        mock_node.status.allocatable = {}
        mock_node.status.node_info.kubelet_version = "v1.29.0"
        mock_node.spec.taints = None
        mock_node.spec.unschedulable = None  # schedulable: API omits the field

        mock_clients.core_v1.read_node.return_value = mock_node

        # Should succeed even though config has restricted namespaces
        result = get_node_status(mock_clients, config, "any-node")

        assert result["status"] == "success"
        mock_clients.core_v1.read_node.assert_called_once()

    def test_condition_message_escaped(self, config, mock_clients):
        """Property 8: Condition message is escaped (free-text field)."""
        mock_node = mock.MagicMock()
        mock_node.metadata.labels = {}
        mock_node.status.conditions = [
            mock.MagicMock(
                type="Ready",
                status="False",
                reason="KubeletNotReady",
                message='<script>alert("injection")</script>',
            ),
        ]
        mock_node.status.capacity = {}
        mock_node.status.allocatable = {}
        mock_node.status.node_info.kubelet_version = "v1.29.0"
        mock_node.spec.taints = None
        mock_node.spec.unschedulable = None  # schedulable: API omits the field

        mock_clients.core_v1.read_node.return_value = mock_node

        result = get_node_status(mock_clients, config, "node-1")

        message = result["data"]["conditions"][0]["message"]
        # Angle brackets must be escaped
        assert "\\u003c" in message
        assert "\\u003e" in message
        assert "<" not in message
        assert ">" not in message

    def test_api_exception_handled(self, config, mock_clients):
        """Property 7: ApiException returns structured error."""
        mock_clients.core_v1.read_node.side_effect = ApiException(
            status=403, reason="Forbidden"
        )

        result = get_node_status(mock_clients, config, "node-1")

        assert result["status"] == "error"
        assert result["error"] == "kubernetes_api_error"
        assert result["http_status"] == 403

    def test_uses_request_timeout(self, config, mock_clients):
        """_request_timeout is passed to API call."""
        mock_node = mock.MagicMock()
        mock_node.metadata.labels = {}
        mock_node.status.conditions = []
        mock_node.status.capacity = {}
        mock_node.status.allocatable = {}
        mock_node.status.node_info.kubelet_version = "v1.29.0"
        mock_node.spec.taints = None
        mock_node.spec.unschedulable = None  # schedulable: API omits the field

        mock_clients.core_v1.read_node.return_value = mock_node

        get_node_status(mock_clients, config, "node-1")

        call_kwargs = mock_clients.core_v1.read_node.call_args.kwargs
        assert call_kwargs["_request_timeout"] == 30


class TestListNodes:
    """Tests for list_nodes function."""

    def test_returns_correct_structure(self, config, mock_clients):
        """REQ-038: Returns required fields."""
        now = datetime.now(timezone.utc)

        mock_node = mock.MagicMock()
        mock_node.metadata.name = "worker-1"
        mock_node.metadata.creation_timestamp = now - timedelta(days=30)
        mock_node.metadata.labels = {"node-role.kubernetes.io/worker": ""}
        mock_node.status.conditions = [
            mock.MagicMock(type="Ready", status="True"),
        ]
        mock_node.status.node_info.kubelet_version = "v1.29.0"
        mock_node.spec.unschedulable = None  # schedulable: API omits the field

        mock_clients.core_v1.list_node.return_value = mock.MagicMock(
            items=[mock_node]
        )

        result = list_nodes(mock_clients, config)

        assert result["status"] == "success"
        data = result["data"]
        assert data["total"] == 1
        node = data["nodes"][0]
        assert node["name"] == "worker-1"
        assert node["ready"] == "True"
        assert node["roles"] == ["worker"]
        assert node["kubelet_version"] == "v1.29.0"
        assert node["unschedulable"] is False
        assert node["age_seconds"] > 0

    def test_no_arguments_required(self, config, mock_clients):
        """REQ-039: No arguments - cluster-scoped."""
        mock_clients.core_v1.list_node.return_value = mock.MagicMock(items=[])

        # Should work without any namespace argument
        result = list_nodes(mock_clients, config)

        assert result["status"] == "success"
        mock_clients.core_v1.list_node.assert_called_once()

    def test_multiple_nodes(self, config, mock_clients):
        """Returns multiple nodes."""
        now = datetime.now(timezone.utc)

        nodes = []
        for i in range(3):
            mock_node = mock.MagicMock()
            mock_node.metadata.name = f"node-{i}"
            mock_node.metadata.creation_timestamp = now - timedelta(hours=i)
            mock_node.metadata.labels = {}
            mock_node.status.conditions = [
                mock.MagicMock(type="Ready", status="True"),
            ]
            mock_node.status.node_info.kubelet_version = "v1.29.0"
            mock_node.spec.unschedulable = None  # schedulable: API omits the field
            nodes.append(mock_node)

        mock_clients.core_v1.list_node.return_value = mock.MagicMock(items=nodes)

        result = list_nodes(mock_clients, config)

        assert result["data"]["total"] == 3
        assert len(result["data"]["nodes"]) == 3

    def test_cordoned_node(self, config, mock_clients):
        """REQ-038: Returns unschedulable (cordoned) status."""
        mock_node = mock.MagicMock()
        mock_node.metadata.name = "cordoned-node"
        mock_node.metadata.creation_timestamp = datetime.now(timezone.utc)
        mock_node.metadata.labels = {}
        mock_node.status.conditions = [
            mock.MagicMock(type="Ready", status="True"),
        ]
        mock_node.status.node_info.kubelet_version = "v1.29.0"
        mock_node.spec.unschedulable = True  # Cordoned

        mock_clients.core_v1.list_node.return_value = mock.MagicMock(
            items=[mock_node]
        )

        result = list_nodes(mock_clients, config)

        assert result["data"]["nodes"][0]["unschedulable"] is True

    def test_uses_request_timeout(self, config, mock_clients):
        """_request_timeout is passed to API call."""
        mock_clients.core_v1.list_node.return_value = mock.MagicMock(items=[])

        list_nodes(mock_clients, config)

        call_kwargs = mock_clients.core_v1.list_node.call_args.kwargs
        assert call_kwargs["_request_timeout"] == 30

    def test_api_exception_handled(self, config, mock_clients):
        """Property 7: ApiException returns structured error."""
        mock_clients.core_v1.list_node.side_effect = ApiException(
            status=403, reason="Forbidden"
        )

        result = list_nodes(mock_clients, config)

        assert result["status"] == "error"
        assert result["error"] == "kubernetes_api_error"
        assert result["http_status"] == 403


class TestUnschedulableIsAlwaysBoolean:
    """REQ-035/REQ-038: `unschedulable` is a boolean, never null.

    Found by real-cluster validation: `list_nodes` returned
    `"unschedulable": null`. The API server omits `spec.unschedulable` when the
    node is schedulable, so the client leaves it None — and every test in this
    file previously set it to False by hand, a shape a schedulable node does not
    actually have. The tests agreed with each other, not with the cluster.
    """

    @staticmethod
    def _node(unschedulable):
        node = mock.MagicMock()
        node.metadata.name = "node-1"
        node.metadata.labels = {}
        node.metadata.creation_timestamp = datetime.now(timezone.utc)
        node.status.conditions = [
            mock.MagicMock(
                type="Ready", status="True", reason="KubeletReady", message="ok"
            )
        ]
        node.status.capacity = {"cpu": "4", "memory": "16Gi"}
        node.status.allocatable = {"cpu": "4", "memory": "16Gi"}
        node.status.node_info.kubelet_version = "v1.29.0"
        node.spec.taints = []
        node.spec.unschedulable = unschedulable
        return node

    def test_get_node_status_none_becomes_false(self, config, mock_clients):
        """The reported bug: absent means schedulable, not unknown."""
        mock_clients.core_v1.read_node.return_value = self._node(None)

        result = get_node_status(mock_clients, config, "node-1")

        assert result["data"]["unschedulable"] is False
        assert result["data"]["unschedulable"] is not None

    def test_list_nodes_none_becomes_false(self, config, mock_clients):
        """The reported bug, on the tool it was observed in."""
        mock_clients.core_v1.list_node.return_value = mock.MagicMock(
            items=[self._node(None)]
        )

        result = list_nodes(mock_clients, config)

        assert result["data"]["nodes"][0]["unschedulable"] is False
        assert result["data"]["nodes"][0]["unschedulable"] is not None

    def test_json_serializes_as_false_not_null(self, config, mock_clients):
        """What the MCP client actually receives on the wire."""
        import json

        mock_clients.core_v1.list_node.return_value = mock.MagicMock(
            items=[self._node(None)]
        )

        payload = json.dumps(list_nodes(mock_clients, config))

        assert '"unschedulable": false' in payload
        assert '"unschedulable": null' not in payload

    def test_true_is_preserved(self, config, mock_clients):
        """Normalizing None must not flatten a genuinely cordoned node."""
        mock_clients.core_v1.read_node.return_value = self._node(True)

        result = get_node_status(mock_clients, config, "node-1")

        assert result["data"]["unschedulable"] is True

    def test_explicit_false_stays_false(self, config, mock_clients):
        """An explicitly-false field is still false."""
        mock_clients.core_v1.read_node.return_value = self._node(False)

        result = get_node_status(mock_clients, config, "node-1")

        assert result["data"]["unschedulable"] is False

    def test_missing_spec_is_false(self, config, mock_clients):
        """A node with no spec at all is reported schedulable, not null."""
        node = self._node(None)
        node.spec = None
        mock_clients.core_v1.read_node.return_value = node

        result = get_node_status(mock_clients, config, "node-1")

        assert result["data"]["unschedulable"] is False

    def test_type_is_bool_in_both_tools(self, config, mock_clients):
        """The contract is the type, not just the value."""
        mock_clients.core_v1.read_node.return_value = self._node(None)
        mock_clients.core_v1.list_node.return_value = mock.MagicMock(
            items=[self._node(None)]
        )

        status = get_node_status(mock_clients, config, "node-1")
        listing = list_nodes(mock_clients, config)

        assert isinstance(status["data"]["unschedulable"], bool)
        assert isinstance(listing["data"]["nodes"][0]["unschedulable"], bool)


class TestUnschedulableIsTheOnlyOptionalBoolInTheModels:
    """Guards against the same gap appearing in a sibling field.

    `V1ContainerStatus.ready` is the only other bool the tools surface, and the
    API marks it required, so it cannot arrive as None. If a future Kubernetes
    client relaxes that, this fails and pods.py needs the same normalization.
    """

    def test_container_status_ready_is_required(self):
        import inspect

        from kubernetes import client as kc

        setter = inspect.getsource(kc.V1ContainerStatus.ready.fset)
        assert "must not be `None`" in setter

    def test_node_spec_unschedulable_is_optional(self):
        """The premise of the fix: this field is genuinely optional."""
        import inspect

        from kubernetes import client as kc

        setter = inspect.getsource(kc.V1NodeSpec.unschedulable.fset)
        assert "must not be `None`" not in setter
        assert kc.V1NodeSpec().unschedulable is None


class TestOptionalFieldsWithNoServerSideDefault:
    """Fields the API genuinely omits, where null is the correct answer.

    Distinct from `unschedulable`: absence here carries no defined default, so
    normalizing would invent information. These tests pin that null passes
    through rather than crashing or being coerced — and record the reasoning,
    since "why is this one not normalized too?" is the obvious next question.

    Verified against a live v1.35.1 API server: `spec.taints[].value` is absent
    for valueless taints (`node-role.kubernetes.io/control-plane:NoSchedule`),
    and `status.conditions[].reason` is populated by the kubelet on every real
    node condition but is optional in the schema.
    """

    @staticmethod
    def _node(*, taints=None, reason="KubeletReady"):
        node = mock.MagicMock()
        node.metadata.name = "node-1"
        node.metadata.labels = {}
        node.metadata.creation_timestamp = datetime.now(timezone.utc)
        node.status.conditions = [
            mock.MagicMock(
                type="Ready", status="True", reason=reason, message="ready"
            )
        ]
        node.status.capacity = {"cpu": "4", "memory": "16Gi"}
        node.status.allocatable = {"cpu": "4", "memory": "16Gi"}
        node.status.node_info.kubelet_version = "v1.29.0"
        node.spec.taints = taints
        node.spec.unschedulable = None
        return node

    def test_valueless_taint_reports_null_not_empty_string(
        self, config, mock_clients
    ):
        """A taint with no value is not a taint with an empty value."""
        taint = mock.MagicMock(
            key="node-role.kubernetes.io/control-plane", value=None, effect="NoSchedule"
        )
        mock_clients.core_v1.read_node.return_value = self._node(taints=[taint])

        result = get_node_status(mock_clients, config, "node-1")

        assert result["data"]["taints"][0]["value"] is None
        assert result["data"]["taints"][0]["key"] == "node-role.kubernetes.io/control-plane"
        assert result["data"]["taints"][0]["effect"] == "NoSchedule"

    def test_condition_without_reason_does_not_crash(self, config, mock_clients):
        """condition.reason is optional in the schema even though kubelet sets it."""
        mock_clients.core_v1.read_node.return_value = self._node(reason=None)

        result = get_node_status(mock_clients, config, "node-1")

        assert result["status"] == "success"
        assert result["data"]["conditions"][0]["reason"] is None
        assert result["data"]["conditions"][0]["type"] == "Ready"

    def test_no_taints_at_all_is_an_empty_list_not_null(self, config, mock_clients):
        """spec.taints omitted entirely — the common case on worker nodes."""
        mock_clients.core_v1.read_node.return_value = self._node(taints=None)

        result = get_node_status(mock_clients, config, "node-1")

        assert result["data"]["taints"] == []
