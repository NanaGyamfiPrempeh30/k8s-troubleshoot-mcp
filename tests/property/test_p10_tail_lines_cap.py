"""Feature: k8s-troubleshoot-mcp, Property 10: tail_lines cap.

For any tail_lines greater than config.max_log_lines, get_pod_logs must report
truncated: true and lines_returned at most max_log_lines. For any tail_lines at
or below the cap, truncated must be false.

Validates: REQ-025, REQ-071.
"""

from __future__ import annotations

from unittest import mock

from hypothesis import given, settings, strategies as st

from k8s_troubleshoot_mcp.tools.pods import get_pod_logs
from tests.property.strategies import FakeClients, make_config, pod_log_response

# REQ-071: max_log_lines defaults to 200 with a hard ceiling of 1000.
max_log_lines_values = st.integers(min_value=1, max_value=1000)


def _clients_returning_requested_lines():
    """Clients whose log API returns exactly as many lines as it was asked for."""

    def _side_effect(**kwargs):
        count = kwargs["tail_lines"]
        return pod_log_response("".join(f"log line {i}\n" for i in range(count)))

    core = mock.MagicMock()
    core.read_namespaced_pod_log.side_effect = _side_effect
    return FakeClients(core, mock.MagicMock(), mock.MagicMock(), mock.MagicMock())


@given(max_lines=max_log_lines_values, excess=st.integers(min_value=1, max_value=5000))
@settings(max_examples=100, deadline=None)
def test_property_10_over_cap_is_truncated(max_lines, excess):
    """Any request above the cap is reported truncated and clamped."""
    clients = _clients_returning_requested_lines()
    config = make_config(max_log_lines=max_lines)
    tail_lines = max_lines + excess

    response = get_pod_logs(clients, config, "pod", "default", tail_lines=tail_lines)

    assert response["status"] == "success"
    assert response["data"]["truncated"] is True
    assert response["data"]["lines_returned"] <= max_lines


@given(max_lines=max_log_lines_values, data=st.data())
@settings(max_examples=100, deadline=None)
def test_property_10_at_or_under_cap_is_not_truncated(max_lines, data):
    """Any request at or below the cap is not reported truncated."""
    tail_lines = data.draw(st.integers(min_value=1, max_value=max_lines))
    clients = _clients_returning_requested_lines()
    config = make_config(max_log_lines=max_lines)

    response = get_pod_logs(clients, config, "pod", "default", tail_lines=tail_lines)

    assert response["data"]["truncated"] is False
    assert response["data"]["lines_returned"] == tail_lines


@given(max_lines=max_log_lines_values, excess=st.integers(min_value=1, max_value=5000))
@settings(max_examples=100, deadline=None)
def test_property_10_api_never_asked_for_more_than_cap(max_lines, excess):
    """The clamp happens before the API call, not after the response."""
    clients = _clients_returning_requested_lines()
    config = make_config(max_log_lines=max_lines)

    get_pod_logs(clients, config, "pod", "default", tail_lines=max_lines + excess)

    call_kwargs = clients.core_v1.read_namespaced_pod_log.call_args.kwargs
    assert call_kwargs["tail_lines"] <= max_lines


@given(max_lines=max_log_lines_values, data=st.data())
@settings(max_examples=100, deadline=None)
def test_property_10_boundary_exactly_at_cap(max_lines, data):
    """tail_lines == max_log_lines is inside the cap, not over it."""
    clients = _clients_returning_requested_lines()
    config = make_config(max_log_lines=max_lines)

    response = get_pod_logs(clients, config, "pod", "default", tail_lines=max_lines)

    assert response["data"]["truncated"] is False
    assert response["data"]["lines_returned"] == max_lines
