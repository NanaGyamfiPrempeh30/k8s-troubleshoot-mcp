"""Feature: k8s-troubleshoot-mcp, Property 15: namespace events limit cap.

For any limit greater than 50, the get_namespace_events response must have
capped: true and at most 50 events.

Validates: REQ-056.

tools/events.py is not implemented yet. These tests are written against the
contract in requirements.md and design.md and are skipped until the module
exists — they are not placeholders, and will execute unchanged the moment
get_namespace_events lands. Do not mark events.py complete while this file is
still reporting as skipped.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest
from hypothesis import given, settings, strategies as st

from tests.property.strategies import FakeClients, make_config

events_module = pytest.importorskip(
    "k8s_troubleshoot_mcp.tools.events",
    reason="tools/events.py not implemented yet (Property 15 pending)",
)

get_namespace_events = events_module.get_namespace_events

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _event(offset):
    event = mock.MagicMock()
    event.event_time = BASE + timedelta(seconds=offset)
    event.deprecated_last_timestamp = None
    event.deprecated_first_timestamp = None
    event.deprecated_count = 1
    event.reason = "Scheduled"
    event.note = "generated note"
    event.type = "Normal"
    event.regarding.kind = "Pod"
    event.regarding.name = "pod-1"
    return event


def _clients_with(count):
    events = mock.MagicMock()
    events.list_namespaced_event.return_value = mock.MagicMock(
        items=[_event(i) for i in range(count)]
    )
    return FakeClients(mock.MagicMock(), mock.MagicMock(), mock.MagicMock(), events)


@given(
    limit=st.integers(min_value=51, max_value=10_000),
    available=st.integers(min_value=51, max_value=200),
)
@settings(max_examples=100, deadline=None)
def test_property_15_over_limit_is_capped(limit, available):
    """Any limit above 50 yields capped: true and at most 50 events."""
    response = get_namespace_events(
        _clients_with(available), make_config(), "default", limit=limit
    )

    assert response["status"] == "success"
    assert response["data"]["capped"] is True
    assert len(response["data"]["events"]) <= 50


@given(
    limit=st.integers(min_value=1, max_value=50),
    available=st.integers(min_value=0, max_value=200),
)
@settings(max_examples=100, deadline=None)
def test_property_15_within_limit_is_not_capped(limit, available):
    """A limit at or below 50 is honored without the cap flag."""
    response = get_namespace_events(
        _clients_with(available), make_config(), "default", limit=limit
    )

    assert response["data"]["capped"] is False
    assert len(response["data"]["events"]) <= limit


@given(available=st.integers(min_value=0, max_value=200))
@settings(max_examples=100, deadline=None)
def test_property_15_never_exceeds_fifty_regardless(available):
    """Under the default limit the hard ceiling of 50 still holds."""
    response = get_namespace_events(_clients_with(available), make_config(), "default")

    assert len(response["data"]["events"]) <= 50
