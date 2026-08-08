"""Unit tests for tools/storage module."""

from __future__ import annotations

import logging
from unittest import mock

import pytest
from kubernetes.client.exceptions import ApiException

from k8s_troubleshoot_mcp.config import ServerConfig
from k8s_troubleshoot_mcp.tools.storage import (
    get_pvc_status,
    _storage_class_name,
    _resize_status,
    KNOWN_RESIZE_STATES,
)

# REQ-053b warnings are emitted on this module's logger.
STORAGE_LOGGER = "k8s_troubleshoot_mcp.tools.storage"


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


def make_pvc(
    phase="Bound",
    storage_class="standard",
    access_modes=None,
    requested="10Gi",
    capacity="10Gi",
    volume_name="pv-abc123",
    volume_mode="Filesystem",
    resize_statuses=None,
):
    """Build a mock V1PersistentVolumeClaim.

    resize_statuses is set explicitly (default None) so no test depends on
    MagicMock auto-attribute behavior for allocated_resource_statuses.
    """
    pvc = mock.MagicMock()
    pvc.spec.storage_class_name = storage_class
    pvc.spec.access_modes = (
        access_modes if access_modes is not None else ["ReadWriteOnce"]
    )
    pvc.spec.volume_name = volume_name
    pvc.spec.volume_mode = volume_mode
    pvc.spec.resources.requests = {"storage": requested} if requested else None
    pvc.status.phase = phase
    pvc.status.capacity = {"storage": capacity} if capacity else None
    pvc.status.allocated_resource_statuses = resize_statuses
    return pvc


class TestStorageClassName:
    """Tests for _storage_class_name helper."""

    def test_named_class(self):
        """A named class is returned as-is."""
        spec = mock.MagicMock()
        spec.storage_class_name = "fast-ssd"
        assert _storage_class_name(spec) == "fast-ssd"

    def test_empty_string_preserved(self):
        """Empty string means 'no StorageClass' and must not become None."""
        spec = mock.MagicMock()
        spec.storage_class_name = ""
        assert _storage_class_name(spec) == ""

    def test_none_preserved(self):
        """None means 'use the default StorageClass' and must not become ''."""
        spec = mock.MagicMock()
        spec.storage_class_name = None
        assert _storage_class_name(spec) is None

    def test_no_spec(self):
        """A missing spec yields None."""
        assert _storage_class_name(None) is None


class TestResizeStatus:
    """Tests for _resize_status helper. REQ-053a / REQ-053b."""

    def test_no_status_object(self):
        """A missing status yields None."""
        assert _resize_status(None, "vol", "default") is None

    def test_field_absent(self):
        """No allocated_resource_statuses means no resize in progress."""
        status = mock.MagicMock()
        status.allocated_resource_statuses = None
        assert _resize_status(status, "vol", "default") is None

    def test_no_storage_key(self):
        """A statuses dict without a storage entry yields None."""
        status = mock.MagicMock()
        status.allocated_resource_statuses = {"example.com/foo": "SomeState"}
        assert _resize_status(status, "vol", "default") is None

    @pytest.mark.parametrize("state", sorted(KNOWN_RESIZE_STATES))
    def test_every_known_state_returned(self, state):
        """REQ-053b: each documented state round-trips unchanged."""
        status = mock.MagicMock()
        status.allocated_resource_statuses = {"storage": state}
        assert _resize_status(status, "vol", "default") == state

    def test_known_state_list_matches_spec(self):
        """REQ-053b: guard the exact documented set, both vintages."""
        assert KNOWN_RESIZE_STATES == {
            "ControllerResizeInProgress",
            "ControllerResizeFailed",
            "ControllerResizeInfeasible",
            "NodeResizePending",
            "NodeResizeInProgress",
            "NodeResizeFailed",
            "NodeResizeInfeasible",
        }


class TestUnrecognizedResizeStateWarning:
    """REQ-053b: unknown expansion states are surfaced operationally."""

    def test_unknown_state_logs_warning_and_is_returned(self, caplog):
        """An unknown state is reported verbatim, not swallowed to None."""
        status = mock.MagicMock()
        status.allocated_resource_statuses = {"storage": "ControllerResizeParked"}

        with caplog.at_level(logging.WARNING, logger=STORAGE_LOGGER):
            result = _resize_status(status, "data-vol", "default")

        assert result == "ControllerResizeParked"
        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert record.levelno == logging.WARNING
        assert "ControllerResizeParked" in record.getMessage()
        assert "default/data-vol" in record.getMessage()

    @pytest.mark.parametrize("state", sorted(KNOWN_RESIZE_STATES))
    def test_known_states_log_nothing(self, state, caplog):
        """No known state may produce a warning — that would be a log storm."""
        status = mock.MagicMock()
        status.allocated_resource_statuses = {"storage": state}

        with caplog.at_level(logging.WARNING, logger=STORAGE_LOGGER):
            _resize_status(status, "data-vol", "default")

        assert caplog.records == []

    def test_absent_field_logs_nothing(self, caplog):
        """The overwhelmingly common case must stay quiet."""
        status = mock.MagicMock()
        status.allocated_resource_statuses = None

        with caplog.at_level(logging.WARNING, logger=STORAGE_LOGGER):
            _resize_status(status, "data-vol", "default")

        assert caplog.records == []

    def test_unknown_state_escaped_in_log_and_response(self, caplog):
        """REQ-053a/b: escaped in both channels, no forged log line."""
        status = mock.MagicMock()
        status.allocated_resource_statuses = {
            "storage": 'x\nWARNING forged "line"'
        }

        with caplog.at_level(logging.WARNING, logger=STORAGE_LOGGER):
            result = _resize_status(status, "data-vol", "default")

        assert "\n" not in result
        assert "\\n" in result
        assert '\\"line\\"' in result
        assert "\n" not in caplog.records[0].getMessage()

    def test_warning_reaches_get_pvc_status(self, config, mock_clients, caplog):
        """The warning fires through the public tool path, not just the helper."""
        mock_clients.core_v1.read_namespaced_persistent_volume_claim.return_value = (
            make_pvc(resize_statuses={"storage": "TotallyNewState"})
        )

        with caplog.at_level(logging.WARNING, logger=STORAGE_LOGGER):
            result = get_pvc_status(mock_clients, config, "data-vol", "default")

        assert result["status"] == "success"
        assert result["data"]["resize_status"] == "TotallyNewState"
        assert len(caplog.records) == 1


class TestGetPvcStatus:
    """Tests for get_pvc_status function."""

    def test_returns_correct_structure(self, config, mock_clients):
        """REQ-053: Returns required fields."""
        mock_clients.core_v1.read_namespaced_persistent_volume_claim.return_value = (
            make_pvc()
        )

        result = get_pvc_status(mock_clients, config, "data-vol", "default")

        assert result["status"] == "success"
        data = result["data"]
        assert data["name"] == "data-vol"
        assert data["namespace"] == "default"
        assert data["phase"] == "Bound"
        assert data["storage_class"] == "standard"
        assert data["access_modes"] == ["ReadWriteOnce"]
        assert data["requested_storage"] == "10Gi"
        assert data["actual_capacity"] == "10Gi"
        assert data["bound_pv"] == "pv-abc123"
        assert data["volume_mode"] == "Filesystem"
        assert data["resize_status"] is None

    def test_pending_pvc_has_no_capacity_or_pv(self, config, mock_clients):
        """REQ-053: actual_capacity and bound_pv are null when unbound."""
        mock_clients.core_v1.read_namespaced_persistent_volume_claim.return_value = (
            make_pvc(phase="Pending", capacity=None, volume_name=None)
        )

        result = get_pvc_status(mock_clients, config, "pending-vol", "default")

        data = result["data"]
        assert data["phase"] == "Pending"
        assert data["actual_capacity"] is None
        assert data["bound_pv"] is None
        assert data["requested_storage"] == "10Gi"

    def test_empty_volume_name_normalized_to_none(self, config, mock_clients):
        """An empty volume_name means unbound, not a PV named ''."""
        mock_clients.core_v1.read_namespaced_persistent_volume_claim.return_value = (
            make_pvc(phase="Pending", capacity=None, volume_name="")
        )

        result = get_pvc_status(mock_clients, config, "pending-vol", "default")

        assert result["data"]["bound_pv"] is None

    def test_lost_phase(self, config, mock_clients):
        """REQ-053: Lost is a reportable phase."""
        mock_clients.core_v1.read_namespaced_persistent_volume_claim.return_value = (
            make_pvc(phase="Lost")
        )

        result = get_pvc_status(mock_clients, config, "lost-vol", "default")

        assert result["data"]["phase"] == "Lost"

    def test_storage_class_empty_string_preserved(self, config, mock_clients):
        """'' (no StorageClass) is distinct from null (default StorageClass)."""
        mock_clients.core_v1.read_namespaced_persistent_volume_claim.return_value = (
            make_pvc(storage_class="")
        )

        result = get_pvc_status(mock_clients, config, "data-vol", "default")

        assert result["data"]["storage_class"] == ""

    def test_storage_class_none_preserved(self, config, mock_clients):
        """Unset storage class stays null rather than becoming ''."""
        mock_clients.core_v1.read_namespaced_persistent_volume_claim.return_value = (
            make_pvc(storage_class=None)
        )

        result = get_pvc_status(mock_clients, config, "data-vol", "default")

        assert result["data"]["storage_class"] is None

    def test_capacity_may_exceed_request(self, config, mock_clients):
        """A provisioner rounding up is reported faithfully, not normalized."""
        mock_clients.core_v1.read_namespaced_persistent_volume_claim.return_value = (
            make_pvc(requested="1Gi", capacity="10Gi")
        )

        result = get_pvc_status(mock_clients, config, "data-vol", "default")

        assert result["data"]["requested_storage"] == "1Gi"
        assert result["data"]["actual_capacity"] == "10Gi"

    def test_expansion_in_progress_capacity_lags_request(self, config, mock_clients):
        """REQ-053a: resize_status explains the size disagreement."""
        mock_clients.core_v1.read_namespaced_persistent_volume_claim.return_value = (
            make_pvc(
                requested="20Gi",
                capacity="10Gi",
                resize_statuses={"storage": "NodeResizePending"},
            )
        )

        result = get_pvc_status(mock_clients, config, "data-vol", "default")

        assert result["data"]["requested_storage"] == "20Gi"
        assert result["data"]["actual_capacity"] == "10Gi"
        assert result["data"]["resize_status"] == "NodeResizePending"

    def test_stalled_expansion_reports_failure_state(self, config, mock_clients):
        """REQ-053a: a failed expansion is distinguishable from an in-flight one."""
        mock_clients.core_v1.read_namespaced_persistent_volume_claim.return_value = (
            make_pvc(
                requested="20Gi",
                capacity="10Gi",
                resize_statuses={"storage": "ControllerResizeInfeasible"},
            )
        )

        result = get_pvc_status(mock_clients, config, "data-vol", "default")

        assert result["data"]["resize_status"] == "ControllerResizeInfeasible"

    def test_rounding_up_carries_no_resize_status(self, config, mock_clients):
        """A provisioner rounding up is not an expansion (design.md note)."""
        mock_clients.core_v1.read_namespaced_persistent_volume_claim.return_value = (
            make_pvc(requested="1Gi", capacity="10Gi")
        )

        result = get_pvc_status(mock_clients, config, "data-vol", "default")

        assert result["data"]["actual_capacity"] == "10Gi"
        assert result["data"]["resize_status"] is None

    def test_block_volume_mode(self, config, mock_clients):
        """REQ-053: volume_mode Block is reported."""
        mock_clients.core_v1.read_namespaced_persistent_volume_claim.return_value = (
            make_pvc(volume_mode="Block")
        )

        result = get_pvc_status(mock_clients, config, "block-vol", "default")

        assert result["data"]["volume_mode"] == "Block"

    def test_multiple_access_modes(self, config, mock_clients):
        """REQ-053: all access modes are returned."""
        mock_clients.core_v1.read_namespaced_persistent_volume_claim.return_value = (
            make_pvc(access_modes=["ReadWriteOnce", "ReadOnlyMany"])
        )

        result = get_pvc_status(mock_clients, config, "data-vol", "default")

        assert result["data"]["access_modes"] == ["ReadWriteOnce", "ReadOnlyMany"]

    def test_missing_requests_yields_none(self, config, mock_clients):
        """A PVC with no storage request reports null, not a crash."""
        mock_clients.core_v1.read_namespaced_persistent_volume_claim.return_value = (
            make_pvc(requested=None)
        )

        result = get_pvc_status(mock_clients, config, "data-vol", "default")

        assert result["status"] == "success"
        assert result["data"]["requested_storage"] is None

    def test_pvc_not_found(self, config, mock_clients):
        """REQ-054: Returns pvc_not_found error for 404."""
        mock_clients.core_v1.read_namespaced_persistent_volume_claim.side_effect = (
            ApiException(status=404, reason="Not Found")
        )

        result = get_pvc_status(mock_clients, config, "missing", "default")

        assert result["status"] == "error"
        assert result["error"] == "pvc_not_found"
        assert "missing" in result["message"]

    def test_namespace_not_allowed(self, config, mock_clients):
        """Property 4: Disallowed namespace returns error without API call."""
        result = get_pvc_status(mock_clients, config, "data-vol", "forbidden")

        assert result["status"] == "error"
        assert result["error"] == "namespace_not_allowed"
        mock_clients.core_v1.read_namespaced_persistent_volume_claim.assert_not_called()

    def test_uses_request_timeout(self, config, mock_clients):
        """_request_timeout is passed to API call."""
        mock_clients.core_v1.read_namespaced_persistent_volume_claim.return_value = (
            make_pvc()
        )

        get_pvc_status(mock_clients, config, "data-vol", "default")

        call_kwargs = (
            mock_clients.core_v1.read_namespaced_persistent_volume_claim.call_args.kwargs
        )
        assert call_kwargs["_request_timeout"] == 30

    def test_api_exception_handled(self, config, mock_clients):
        """Property 7: ApiException returns structured error."""
        mock_clients.core_v1.read_namespaced_persistent_volume_claim.side_effect = (
            ApiException(status=403, reason="Forbidden")
        )

        result = get_pvc_status(mock_clients, config, "data-vol", "default")

        assert result["status"] == "error"
        assert result["error"] == "kubernetes_api_error"
        assert result["http_status"] == 403

    def test_connection_error_handled(self, config, mock_clients):
        """Property 7: connection failures return structured error."""
        mock_clients.core_v1.read_namespaced_persistent_volume_claim.side_effect = (
            OSError("connection refused")
        )

        result = get_pvc_status(mock_clients, config, "data-vol", "default")

        assert result["status"] == "error"
        assert result["error"] == "connection_error"
