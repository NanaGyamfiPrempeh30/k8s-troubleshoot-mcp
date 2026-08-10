"""Unit tests for tools/events module."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest
from kubernetes.client.exceptions import ApiException

from k8s_troubleshoot_mcp.config import ServerConfig
from k8s_troubleshoot_mcp.tools.events import get_namespace_events, MAX_EVENTS

BASE = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


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
    clients.events_v1 = mock.MagicMock()
    return clients


def make_event(
    offset=0,
    reason="Scheduled",
    note="Successfully assigned default/web-1 to node-a",
    event_type="Normal",
    count=1,
    kind="Pod",
    name="web-1",
    has_regarding=True,
):
    """Build a mock EventsV1Event."""
    event = mock.MagicMock()
    event.event_time = BASE + timedelta(seconds=offset)
    event.deprecated_last_timestamp = None
    event.deprecated_first_timestamp = None
    event.deprecated_count = count
    event.reason = reason
    event.note = note
    event.type = event_type
    if has_regarding:
        event.regarding = mock.MagicMock()
        event.regarding.kind = kind
        event.regarding.name = name
    else:
        event.regarding = None
    return event


def set_events(mock_clients, events):
    """Point the mocked EventsV1Api at a list of events."""
    mock_clients.events_v1.list_namespaced_event.return_value = mock.MagicMock(
        items=events
    )


class TestGetNamespaceEvents:
    """Tests for get_namespace_events function."""

    def test_returns_correct_structure(self, config, mock_clients):
        """REQ-057: Returns all required per-event fields."""
        set_events(mock_clients, [make_event()])

        result = get_namespace_events(mock_clients, config, "default")

        assert result["status"] == "success"
        data = result["data"]
        assert data["namespace"] == "default"
        assert data["total"] == 1
        assert data["capped"] is False

        event = data["events"][0]
        assert event["involved_object_kind"] == "Pod"
        assert event["involved_object_name"] == "web-1"
        assert event["reason"] == "Scheduled"
        assert event["message"] == "Successfully assigned default/web-1 to node-a"
        assert event["count"] == 1
        assert event["type"] == "Normal"
        assert event["first_timestamp"] == BASE.isoformat()
        assert event["last_timestamp"] == BASE.isoformat()

    def test_uses_events_v1_api(self, config, mock_clients):
        """REQ-055: EventsV1Api only; the deprecated CoreV1Api path is never used."""
        set_events(mock_clients, [])

        get_namespace_events(mock_clients, config, "default")

        mock_clients.events_v1.list_namespaced_event.assert_called_once()
        assert not mock_clients.core_v1.method_calls

    def test_no_field_selector_covers_all_resource_types(self, config, mock_clients):
        """REQ-055: events across all resource types, so no regarding filter."""
        set_events(mock_clients, [])

        get_namespace_events(mock_clients, config, "default")

        call_kwargs = mock_clients.events_v1.list_namespaced_event.call_args.kwargs
        assert "field_selector" not in call_kwargs

    def test_sorted_descending_by_last_timestamp(self, config, mock_clients):
        """REQ-055: most recent first."""
        set_events(
            mock_clients,
            [make_event(offset=10), make_event(offset=300), make_event(offset=100)],
        )

        result = get_namespace_events(mock_clients, config, "default")

        stamps = [
            datetime.fromisoformat(e["last_timestamp"])
            for e in result["data"]["events"]
        ]
        assert stamps == sorted(stamps, reverse=True)

    def test_limit_over_50_is_capped(self, config, mock_clients):
        """REQ-056: a limit above 50 is silently capped and flagged."""
        set_events(mock_clients, [make_event(offset=i) for i in range(120)])

        result = get_namespace_events(mock_clients, config, "default", limit=500)

        assert result["data"]["capped"] is True
        assert len(result["data"]["events"]) == MAX_EVENTS

    def test_limit_within_range_not_capped(self, config, mock_clients):
        """REQ-056: a limit at or below 50 is honored without the flag."""
        set_events(mock_clients, [make_event(offset=i) for i in range(120)])

        result = get_namespace_events(mock_clients, config, "default", limit=10)

        assert result["data"]["capped"] is False
        assert len(result["data"]["events"]) == 10

    def test_limit_exactly_50_not_capped(self, config, mock_clients):
        """REQ-056: 50 is inside the cap, not over it."""
        set_events(mock_clients, [make_event(offset=i) for i in range(120)])

        result = get_namespace_events(mock_clients, config, "default", limit=50)

        assert result["data"]["capped"] is False
        assert len(result["data"]["events"]) == 50

    def test_default_limit_is_the_cap(self, config, mock_clients):
        """REQ-055: the default limit matches the hard ceiling."""
        set_events(mock_clients, [make_event(offset=i) for i in range(120)])

        result = get_namespace_events(mock_clients, config, "default")

        assert len(result["data"]["events"]) == MAX_EVENTS
        assert result["data"]["capped"] is False

    def test_capping_keeps_the_newest_events(self, config, mock_clients):
        """REQ-055 + REQ-056: the 50 kept are the most recent, not an arbitrary 50."""
        set_events(mock_clients, [make_event(offset=i) for i in range(120)])

        result = get_namespace_events(mock_clients, config, "default", limit=500)

        returned = {
            datetime.fromisoformat(e["last_timestamp"])
            for e in result["data"]["events"]
        }
        expected = {BASE + timedelta(seconds=i) for i in range(70, 120)}
        assert returned == expected

    def test_zero_limit_returns_no_events(self, config, mock_clients):
        """A request for zero events is answered truthfully, not as an error."""
        set_events(mock_clients, [make_event(offset=i) for i in range(5)])

        result = get_namespace_events(mock_clients, config, "default", limit=0)

        assert result["status"] == "success"
        assert result["data"]["events"] == []
        assert result["data"]["capped"] is False

    def test_negative_limit_returns_no_events(self, config, mock_clients):
        """A negative limit must not slice from the end of the list."""
        set_events(mock_clients, [make_event(offset=i) for i in range(5)])

        result = get_namespace_events(mock_clients, config, "default", limit=-3)

        assert result["status"] == "success"
        assert result["data"]["events"] == []

    def test_empty_namespace_returns_empty_list(self, config, mock_clients):
        """A namespace with no events is a success with an empty list."""
        set_events(mock_clients, [])

        result = get_namespace_events(mock_clients, config, "default")

        assert result["status"] == "success"
        assert result["data"]["total"] == 0
        assert result["data"]["events"] == []

    def test_message_escaped(self, config, mock_clients):
        """REQ-057a: the note is the canonical injection vector."""
        set_events(
            mock_clients,
            [make_event(note='<script>alert("injection")</script>')],
        )

        result = get_namespace_events(mock_clients, config, "default")

        message = result["data"]["events"][0]["message"]
        assert "\\u003c" in message
        assert "\\u003e" in message
        assert "<" not in message

    def test_reason_and_type_escaped(self, config, mock_clients):
        """REQ-057a: any operator may emit an Event with arbitrary reason/type."""
        set_events(
            mock_clients,
            [make_event(reason="<Rogue>", event_type="<Custom>")],
        )

        result = get_namespace_events(mock_clients, config, "default")

        event = result["data"]["events"][0]
        assert "<" not in event["reason"]
        assert "<" not in event["type"]
        assert "\\u003c" in event["reason"]
        assert "\\u003c" in event["type"]

    def test_involved_object_fields_escaped(self, config, mock_clients):
        """REQ-057a: the regarding reference is emitter-controlled, not validated."""
        set_events(
            mock_clients,
            [make_event(kind='<Kind>', name='<name>\ninjected')],
        )

        result = get_namespace_events(mock_clients, config, "default")

        event = result["data"]["events"][0]
        assert "<" not in event["involved_object_kind"]
        assert "<" not in event["involved_object_name"]
        assert "\n" not in event["involved_object_name"]
        assert "\\n" in event["involved_object_name"]

    def test_missing_regarding_yields_none(self, config, mock_clients):
        """An event with no involved object reports null, not a crash."""
        set_events(mock_clients, [make_event(has_regarding=False)])

        result = get_namespace_events(mock_clients, config, "default")

        event = result["data"]["events"][0]
        assert event["involved_object_kind"] is None
        assert event["involved_object_name"] is None

    def test_falls_back_to_deprecated_timestamps(self, config, mock_clients):
        """Events mirrored from core/v1 carry only the deprecated time fields."""
        event = make_event()
        event.event_time = None
        event.deprecated_last_timestamp = BASE + timedelta(seconds=60)
        event.deprecated_first_timestamp = BASE
        set_events(mock_clients, [event])

        result = get_namespace_events(mock_clients, config, "default")

        entry = result["data"]["events"][0]
        assert entry["first_timestamp"] == BASE.isoformat()
        assert entry["last_timestamp"] == (BASE + timedelta(seconds=60)).isoformat()

    def test_missing_count_defaults_to_one(self, config, mock_clients):
        """An event with no count is a single occurrence."""
        set_events(mock_clients, [make_event(count=None)])

        result = get_namespace_events(mock_clients, config, "default")

        assert result["data"]["events"][0]["count"] == 1

    def test_namespace_not_allowed(self, config, mock_clients):
        """Property 4: Disallowed namespace returns error without API call."""
        result = get_namespace_events(mock_clients, config, "forbidden")

        assert result["status"] == "error"
        assert result["error"] == "namespace_not_allowed"
        mock_clients.events_v1.list_namespaced_event.assert_not_called()

    def test_uses_request_timeout(self, config, mock_clients):
        """_request_timeout is passed to API call."""
        set_events(mock_clients, [])

        get_namespace_events(mock_clients, config, "default")

        call_kwargs = mock_clients.events_v1.list_namespaced_event.call_args.kwargs
        assert call_kwargs["_request_timeout"] == 30

    def test_api_exception_handled(self, config, mock_clients):
        """Property 7: ApiException returns structured error."""
        mock_clients.events_v1.list_namespaced_event.side_effect = ApiException(
            status=403, reason="Forbidden"
        )

        result = get_namespace_events(mock_clients, config, "default")

        assert result["status"] == "error"
        assert result["error"] == "kubernetes_api_error"
        assert result["http_status"] == 403

    def test_connection_error_handled(self, config, mock_clients):
        """Property 7: connection failures return structured error."""
        mock_clients.events_v1.list_namespaced_event.side_effect = OSError("refused")

        result = get_namespace_events(mock_clients, config, "default")

        assert result["status"] == "error"
        assert result["error"] == "connection_error"


class TestTotalAvailable:
    """REQ-056a: `total` and `total_available` answer different questions.

    `capped` reports only whether the caller's limit exceeded 50, so
    {"total": 50, "capped": false} was returned both for a namespace holding
    exactly 50 events and one holding 500. The real count is free — the tool
    lists without a limit and sorts the full set before slicing.
    """

    @staticmethod
    def _events(count):
        return [make_event(offset=i, reason=f"R{i}", note=f"note {i}") for i in range(count)]

    def test_distinguishes_50_of_50_from_50_of_500(self, config, mock_clients):
        """The exact ambiguity this field exists to remove."""
        results = {}
        for population in (50, 500):
            mock_clients.events_v1.list_namespaced_event.return_value = mock.MagicMock(
                items=self._events(population), metadata=mock.MagicMock(_continue=None)
            )
            data = get_namespace_events(mock_clients, config, "default")["data"]
            results[population] = (data["total"], data["total_available"], data["capped"])

        assert results[50] == (50, 50, False)
        assert results[500] == (50, 500, False)
        assert results[50] != results[500], "responses are still indistinguishable"

    def test_equal_when_nothing_was_left_behind(self, config, mock_clients):
        mock_clients.events_v1.list_namespaced_event.return_value = mock.MagicMock(
            items=self._events(7), metadata=mock.MagicMock(_continue=None)
        )

        data = get_namespace_events(mock_clients, config, "default")["data"]

        assert data["total"] == data["total_available"] == 7

    def test_counts_before_the_caller_limit_not_just_the_hard_cap(
        self, config, mock_clients
    ):
        """A small explicit limit must not shrink total_available."""
        mock_clients.events_v1.list_namespaced_event.return_value = mock.MagicMock(
            items=self._events(30), metadata=mock.MagicMock(_continue=None)
        )

        data = get_namespace_events(mock_clients, config, "default", limit=5)["data"]

        assert data["total"] == 5
        assert data["total_available"] == 30
        assert data["capped"] is False

    def test_empty_namespace_is_zero_not_null(self, config, mock_clients):
        mock_clients.events_v1.list_namespaced_event.return_value = mock.MagicMock(
            items=[], metadata=mock.MagicMock(_continue=None)
        )

        data = get_namespace_events(mock_clients, config, "default")["data"]

        assert data["total_available"] == 0
        assert data["total_available"] is not None

    def test_paginated_response_reports_null_not_page_one_count(
        self, config, mock_clients, caplog
    ):
        """A page-one count presented as a namespace total is the same defect."""
        mock_clients.events_v1.list_namespaced_event.return_value = mock.MagicMock(
            items=self._events(60), metadata=mock.MagicMock(_continue="tok")
        )

        with caplog.at_level(logging.WARNING):
            data = get_namespace_events(mock_clients, config, "default")["data"]

        assert data["total_available"] is None
        assert data["total"] == 50, "total still describes the response itself"
        assert "paginated event list" in caplog.text

    def test_complete_response_logs_nothing(self, config, mock_clients, caplog):
        mock_clients.events_v1.list_namespaced_event.return_value = mock.MagicMock(
            items=self._events(3), metadata=mock.MagicMock(_continue=None)
        )

        with caplog.at_level(logging.WARNING):
            get_namespace_events(mock_clients, config, "default")

        assert caplog.text == ""

    def test_missing_list_metadata_is_tolerated(self, config, mock_clients):
        """List metadata is optional; absence is not a pagination signal."""
        mock_clients.events_v1.list_namespaced_event.return_value = mock.MagicMock(
            items=self._events(4), metadata=None
        )

        data = get_namespace_events(mock_clients, config, "default")["data"]

        assert data["total_available"] == 4
