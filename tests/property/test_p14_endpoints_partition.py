"""Feature: k8s-troubleshoot-mcp, Property 14: endpoints partitioning.

For any endpoints object with N ready and M not-ready addresses, the
get_endpoints response must have exactly N items in data.ready and exactly M in
data.not_ready.

Validates: REQ-049, REQ-049a, REQ-050.

Scope reminder, per the documented limitation on Property 14 in design.md: this
validates faithful reflection of the Endpoints object's contents only, never
completeness against the Service's real backend count. A response can satisfy
every assertion here and still under-report a Service with more than 1000
backends, because the object itself is truncated before this tool reads it.
"""

from __future__ import annotations

from unittest import mock

from hypothesis import given, settings, strategies as st

from k8s_troubleshoot_mcp.tools.services import OVER_CAPACITY_ANNOTATION, get_endpoints
from tests.property.strategies import FakeClients, make_config

# Each element is one subset: (ready_count, not_ready_count).
subset_shapes = st.lists(
    st.tuples(st.integers(min_value=0, max_value=15), st.integers(min_value=0, max_value=15)),
    min_size=0,
    max_size=8,
)


def _address(index, with_pod, with_node):
    address = mock.MagicMock()
    address.ip = f"10.{index // 65536 % 256}.{index // 256 % 256}.{index % 256}"
    address.node_name = f"node-{index}" if with_node else None
    if with_pod:
        address.target_ref = mock.MagicMock()
        address.target_ref.name = f"pod-{index}"
    else:
        address.target_ref = None
    return address


def _clients_with_shapes(shapes, annotations=None, with_pod=True, with_node=True):
    subsets = []
    counter = 0
    for ready_count, not_ready_count in shapes:
        subset = mock.MagicMock()
        subset.addresses = [
            _address(counter + i, with_pod, with_node) for i in range(ready_count)
        ]
        counter += ready_count
        subset.not_ready_addresses = [
            _address(counter + i, with_pod, with_node) for i in range(not_ready_count)
        ]
        counter += not_ready_count
        subset.ports = []
        subsets.append(subset)

    endpoints = mock.MagicMock()
    endpoints.subsets = subsets or None
    endpoints.metadata.annotations = annotations

    core = mock.MagicMock()
    core.read_namespaced_endpoints.return_value = endpoints
    return FakeClients(core, mock.MagicMock(), mock.MagicMock(), mock.MagicMock())


@given(shapes=subset_shapes)
@settings(max_examples=100, deadline=None)
def test_property_14_exact_partition_counts(shapes):
    """Exactly N ready and M not-ready entries come back, summed over subsets."""
    expected_ready = sum(r for r, _ in shapes)
    expected_not_ready = sum(n for _, n in shapes)
    clients = _clients_with_shapes(shapes)

    response = get_endpoints(clients, make_config(), "svc", "default")

    assert response["status"] == "success"
    assert len(response["data"]["ready"]) == expected_ready
    assert len(response["data"]["not_ready"]) == expected_not_ready


@given(shapes=subset_shapes)
@settings(max_examples=100, deadline=None)
def test_property_14_no_address_crosses_partitions(shapes):
    """Ready and not-ready sets are disjoint — nothing is double-counted."""
    clients = _clients_with_shapes(shapes)

    response = get_endpoints(clients, make_config(), "svc", "default")

    ready_ips = [e["ip"] for e in response["data"]["ready"]]
    not_ready_ips = [e["ip"] for e in response["data"]["not_ready"]]

    assert not (set(ready_ips) & set(not_ready_ips))
    assert len(ready_ips) + len(not_ready_ips) == sum(r + n for r, n in shapes)


@given(shapes=subset_shapes)
@settings(max_examples=100, deadline=None)
def test_property_14_field_shapes_differ_by_partition(shapes):
    """ready entries carry node_name; not_ready entries do not."""
    clients = _clients_with_shapes(shapes)

    response = get_endpoints(clients, make_config(), "svc", "default")

    for entry in response["data"]["ready"]:
        assert set(entry.keys()) == {"ip", "pod_name", "node_name"}
    for entry in response["data"]["not_ready"]:
        assert set(entry.keys()) == {"ip", "pod_name"}


@given(shapes=subset_shapes, marker=st.sampled_from(["truncated", "warning"]))
@settings(max_examples=100, deadline=None)
def test_property_14_partition_holds_under_truncation_flag(shapes, marker):
    """The partition counts stay exact even when the object is truncated.

    This is the documented limitation made concrete: the counts faithfully
    reflect the object, and `truncated` is what tells the caller the object is
    not the whole picture.
    """
    clients = _clients_with_shapes(shapes, annotations={OVER_CAPACITY_ANNOTATION: marker})

    response = get_endpoints(clients, make_config(), "svc", "default")

    assert len(response["data"]["ready"]) == sum(r for r, _ in shapes)
    assert len(response["data"]["not_ready"]) == sum(n for _, n in shapes)
    assert response["data"]["truncated"] is True


@given(shapes=subset_shapes)
@settings(max_examples=100, deadline=None)
def test_property_14_holds_without_target_refs(shapes):
    """Addresses with no backing pod still partition correctly."""
    clients = _clients_with_shapes(shapes, with_pod=False, with_node=False)

    response = get_endpoints(clients, make_config(), "svc", "default")

    assert len(response["data"]["ready"]) == sum(r for r, _ in shapes)
    assert len(response["data"]["not_ready"]) == sum(n for _, n in shapes)
    assert all(e["pod_name"] is None for e in response["data"]["ready"])
