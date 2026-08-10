"""Feature: k8s-troubleshoot-mcp, Property 6: response envelope.

For any tool and any input, the return value must be json.dumps-serializable and
carry the envelope fields: a non-empty "tool", a "status" of success or error,
"data" on success, and "error"+"message" on error.

Validates: REQ-015, REQ-016.
"""

from __future__ import annotations

import json
from unittest import mock

from hypothesis import given, settings, strategies as st

from k8s_troubleshoot_mcp.tools.pods import get_pod_logs, list_pods
from k8s_troubleshoot_mcp.tools.services import get_endpoints
from k8s_troubleshoot_mcp.tools.storage import get_pvc_status
from tests.property.strategies import (
    ALL_TOOLS,
    NAMESPACED_TOOLS,
    FakeClients,
    api_exception,
    arbitrary_text,
    assert_envelope,
    forbidden_clients,
    http_status_codes,
    pod_log_response,
    make_config,
    namespace_names,
    namespace_sets,
    raising_clients,
)

TOOL_INDEX = st.integers(min_value=0, max_value=len(ALL_TOOLS) - 1)


@given(
    tool_index=TOOL_INDEX,
    status=http_status_codes(),
    reason=arbitrary_text(),
)
@settings(max_examples=100, deadline=None)
def test_property_6_error_path_envelope(tool_index, status, reason):
    """Every tool's API-error path produces a serializable error envelope."""
    spec = ALL_TOOLS[tool_index]
    tool_name, invoke = spec.name, spec.invoke
    clients = raising_clients(api_exception(status, reason))
    config = make_config(allowed={"default"})

    response = invoke(clients, config, "default")

    assert_envelope(response, tool_name)
    assert response["status"] == "error"
    json.dumps(response)


@given(
    allowed=namespace_sets(min_size=1, max_size=4),
    outsider=namespace_names(),
    tool_index=st.integers(min_value=0, max_value=len(NAMESPACED_TOOLS) - 1),
)
@settings(max_examples=100, deadline=None)
def test_property_6_gate_path_envelope(allowed, outsider, tool_index):
    """The namespace-gate path also produces a valid, serializable envelope."""
    if outsider in allowed:
        outsider = outsider + "-outside"

    spec = NAMESPACED_TOOLS[tool_index]
    tool_name, invoke = spec.name, spec.invoke
    clients, _ = forbidden_clients()

    response = invoke(clients, make_config(allowed=allowed), outsider)

    assert_envelope(response, tool_name)
    json.dumps(response)


@given(
    tool_index=TOOL_INDEX,
    message=arbitrary_text(),
)
@settings(max_examples=100, deadline=None)
def test_property_6_connection_error_envelope(tool_index, message):
    """Connection failures also yield a well-formed envelope, never a raise."""
    spec = ALL_TOOLS[tool_index]
    tool_name, invoke = spec.name, spec.invoke
    clients = raising_clients(OSError(message))

    response = invoke(clients, make_config(allowed={"default"}), "default")

    assert_envelope(response, tool_name)
    assert response["status"] == "error"
    json.dumps(response)


@given(content=arbitrary_text())
@settings(max_examples=100, deadline=None)
def test_property_6_get_pod_logs_success_envelope(content):
    """Arbitrary log payloads still serialize inside a success envelope."""
    core = mock.MagicMock()
    core.read_namespaced_pod_log.return_value = pod_log_response(content)
    clients = FakeClients(core, mock.MagicMock(), mock.MagicMock(), mock.MagicMock())

    response = get_pod_logs(clients, make_config(), "pod", "default")

    assert_envelope(response, "get_pod_logs")
    assert response["status"] == "success"
    json.dumps(response)


@given(namespace=namespace_names())
@settings(max_examples=100, deadline=None)
def test_property_6_list_pods_success_envelope(namespace):
    """An empty pod list is still a valid success envelope."""
    core = mock.MagicMock()
    core.list_namespaced_pod.return_value = mock.MagicMock(items=[])
    clients = FakeClients(core, mock.MagicMock(), mock.MagicMock(), mock.MagicMock())

    response = list_pods(clients, make_config(allowed={namespace}), namespace)

    assert_envelope(response, "list_pods")
    assert response["status"] == "success"
    json.dumps(response)


@given(
    phase=st.sampled_from(["Pending", "Bound", "Lost"]),
    storage_class=st.one_of(st.none(), st.just(""), st.text(max_size=20)),
    requested=st.text(alphabet="0123456789GMik", min_size=1, max_size=8),
)
@settings(max_examples=100, deadline=None)
def test_property_6_get_pvc_status_success_envelope(phase, storage_class, requested):
    """Generated PVC field values stay serializable inside the envelope."""
    pvc = mock.MagicMock()
    pvc.spec.storage_class_name = storage_class
    pvc.spec.access_modes = ["ReadWriteOnce"]
    pvc.spec.volume_name = "pv-1"
    pvc.spec.volume_mode = "Filesystem"
    pvc.spec.resources.requests = {"storage": requested}
    pvc.status.phase = phase
    pvc.status.capacity = {"storage": requested}
    pvc.status.allocated_resource_statuses = None

    core = mock.MagicMock()
    core.read_namespaced_persistent_volume_claim.return_value = pvc
    clients = FakeClients(core, mock.MagicMock(), mock.MagicMock(), mock.MagicMock())

    response = get_pvc_status(clients, make_config(), "pvc", "default")

    assert_envelope(response, "get_pvc_status")
    assert response["status"] == "success"
    json.dumps(response)


@given(ready=st.integers(min_value=0, max_value=8), not_ready=st.integers(min_value=0, max_value=8))
@settings(max_examples=100, deadline=None)
def test_property_6_get_endpoints_success_envelope(ready, not_ready):
    """Generated endpoint counts stay serializable inside the envelope."""

    def address(index):
        addr = mock.MagicMock()
        addr.ip = f"10.0.0.{index % 256}"
        addr.node_name = None
        addr.target_ref = None
        return addr

    subset = mock.MagicMock()
    subset.addresses = [address(i) for i in range(ready)]
    subset.not_ready_addresses = [address(i) for i in range(not_ready)]
    subset.ports = []

    endpoints = mock.MagicMock()
    endpoints.subsets = [subset]
    endpoints.metadata.annotations = None

    core = mock.MagicMock()
    core.read_namespaced_endpoints.return_value = endpoints
    clients = FakeClients(core, mock.MagicMock(), mock.MagicMock(), mock.MagicMock())

    response = get_endpoints(clients, make_config(), "svc", "default")

    assert_envelope(response, "get_endpoints")
    assert response["status"] == "success"
    json.dumps(response)
