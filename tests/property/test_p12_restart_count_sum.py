"""Feature: k8s-troubleshoot-mcp, Property 12: restart_count is a sum.

For any pod with N containers each having an arbitrary non-negative restart
count, the restart_count in the list_pods response must equal the arithmetic sum
of the per-container counts.

Validates: REQ-032.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest import mock

from hypothesis import given, settings, strategies as st

from k8s_troubleshoot_mcp.tools.pods import list_pods
from tests.property.strategies import FakeClients, make_config

restart_counts = st.lists(st.integers(min_value=0, max_value=10_000), min_size=0, max_size=12)


def _pod(counts, name="pod"):
    statuses = []
    for index, count in enumerate(counts):
        cs = mock.MagicMock()
        cs.restart_count = count
        cs.ready = index % 2 == 0
        statuses.append(cs)

    pod = mock.MagicMock()
    pod.metadata.name = name
    pod.metadata.creation_timestamp = datetime.now(timezone.utc)
    pod.status.phase = "Running"
    pod.status.container_statuses = statuses or None
    pod.spec.node_name = "node-1"
    pod.spec.containers = [mock.MagicMock() for _ in counts]
    return pod


def _clients_with_pods(pods):
    core = mock.MagicMock()
    core.list_namespaced_pod.return_value = mock.MagicMock(items=pods)
    return FakeClients(core, mock.MagicMock(), mock.MagicMock(), mock.MagicMock())


@given(counts=restart_counts)
@settings(max_examples=100, deadline=None)
def test_property_12_restart_count_equals_sum(counts):
    """restart_count is exactly the sum of per-container restart counts."""
    clients = _clients_with_pods([_pod(counts)])

    response = list_pods(clients, make_config(), "default")

    assert response["status"] == "success"
    assert response["data"]["pods"][0]["restart_count"] == sum(counts)


@given(pod_counts=st.lists(restart_counts, min_size=1, max_size=6))
@settings(max_examples=100, deadline=None)
def test_property_12_holds_per_pod_independently(pod_counts):
    """Each pod's sum is computed independently, with no carry-over."""
    pods = [_pod(counts, name=f"pod-{i}") for i, counts in enumerate(pod_counts)]
    clients = _clients_with_pods(pods)

    response = list_pods(clients, make_config(), "default")

    returned = response["data"]["pods"]
    assert len(returned) == len(pod_counts)
    for entry, counts in zip(returned, pod_counts):
        assert entry["restart_count"] == sum(counts)


@given(counts=restart_counts)
@settings(max_examples=100, deadline=None)
def test_property_12_container_totals_are_consistent(counts):
    """total_containers matches the number of container statuses present."""
    clients = _clients_with_pods([_pod(counts)])

    response = list_pods(clients, make_config(), "default")
    entry = response["data"]["pods"][0]

    assert entry["total_containers"] == len(counts)
    assert entry["ready_containers"] <= entry["total_containers"]


@given(counts=st.lists(st.integers(min_value=0, max_value=100), min_size=1, max_size=8))
@settings(max_examples=100, deadline=None)
def test_property_12_sum_is_non_negative_and_bounded(counts):
    """The sum never exceeds the arithmetic maximum of its parts."""
    clients = _clients_with_pods([_pod(counts)])

    response = list_pods(clients, make_config(), "default")
    total = response["data"]["pods"][0]["restart_count"]

    assert total >= 0
    assert total >= max(counts)
    assert total <= sum(counts)
