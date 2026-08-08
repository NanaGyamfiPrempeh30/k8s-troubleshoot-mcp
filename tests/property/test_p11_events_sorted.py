"""Feature: k8s-troubleshoot-mcp, Property 11: pod events sorted and capped.

For any list of events of arbitrary length and arbitrary timestamp ordering, the
get_pod_events response must contain at most 50 events in non-increasing
last_timestamp order.

Validates: REQ-029.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest import mock

from hypothesis import given, settings, strategies as st

from k8s_troubleshoot_mcp.tools.pods import get_pod_events
from tests.property.strategies import FakeClients, make_config

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)

# Offsets in seconds; drawn unsorted so the tool must do the ordering.
offsets = st.lists(st.integers(min_value=0, max_value=500_000), min_size=0, max_size=120)


def _event(offset):
    event = mock.MagicMock()
    event.event_time = BASE + timedelta(seconds=offset)
    event.deprecated_last_timestamp = None
    event.deprecated_first_timestamp = None
    event.deprecated_count = 1
    event.reason = "Scheduled"
    event.note = "generated event note"
    event.type = "Normal"
    return event


def _clients_with_events(offset_list):
    events = mock.MagicMock()
    events.list_namespaced_event.return_value = mock.MagicMock(
        items=[_event(o) for o in offset_list]
    )
    return FakeClients(mock.MagicMock(), mock.MagicMock(), mock.MagicMock(), events)


@given(offset_list=offsets)
@settings(max_examples=100, deadline=None)
def test_property_11_never_more_than_50(offset_list):
    """However many events the API returns, at most 50 come back."""
    clients = _clients_with_events(offset_list)

    response = get_pod_events(clients, make_config(), "pod", "default")

    assert response["status"] == "success"
    assert len(response["data"]["events"]) <= 50


@given(offset_list=offsets)
@settings(max_examples=100, deadline=None)
def test_property_11_descending_order(offset_list):
    """last_timestamp is non-increasing across the returned list."""
    clients = _clients_with_events(offset_list)

    response = get_pod_events(clients, make_config(), "pod", "default")

    stamps = [
        datetime.fromisoformat(e["last_timestamp"])
        for e in response["data"]["events"]
        if e["last_timestamp"] is not None
    ]
    assert stamps == sorted(stamps, reverse=True)


@given(offset_list=offsets)
@settings(max_examples=100, deadline=None)
def test_property_11_returns_the_newest_events(offset_list):
    """When capping, the 50 kept are the newest, not an arbitrary 50."""
    clients = _clients_with_events(offset_list)

    response = get_pod_events(clients, make_config(), "pod", "default")

    returned = {
        datetime.fromisoformat(e["last_timestamp"])
        for e in response["data"]["events"]
        if e["last_timestamp"] is not None
    }
    expected = set(sorted((BASE + timedelta(seconds=o) for o in offset_list), reverse=True)[:50])

    assert returned == expected


@given(offset_list=st.lists(st.integers(min_value=0, max_value=1000), min_size=51, max_size=200))
@settings(max_examples=100, deadline=None)
def test_property_11_cap_binds_when_over_fifty(offset_list):
    """With more than 50 events available, exactly 50 are returned."""
    clients = _clients_with_events(offset_list)

    response = get_pod_events(clients, make_config(), "pod", "default")

    assert len(response["data"]["events"]) == 50
