"""Feature: k8s-troubleshoot-mcp, Property 7: ApiException never propagates.

For any tool and any ApiException, the tool must return a structured error dict
rather than raising to the MCP layer.

Validates: REQ-017.

Note on the 404 carve-out: design.md's Property 7 requires the error dict to
include the HTTP status code and reason for every status except 404, which tools
translate into a domain error (`pod_not_found`, `service_not_found`, ...) and MAY
omit both fields. The never-raises half admits no carve-out. These tests mirror
that split: the universal assertions run across all statuses, the
status/reason assertions exclude 404, and the 404 contract is pinned separately.
"""

from __future__ import annotations

import json

from hypothesis import given, settings, strategies as st

from tests.property.strategies import (
    ALL_TOOLS,
    api_exception,
    arbitrary_text,
    assert_envelope,
    http_status_codes,
    make_config,
    raising_clients,
)

TOOL_INDEX = st.integers(min_value=0, max_value=len(ALL_TOOLS) - 1)


@given(tool_index=TOOL_INDEX, status=http_status_codes(), reason=arbitrary_text())
@settings(max_examples=100, deadline=None)
def test_property_7_never_raises(tool_index, status, reason):
    """No ApiException, of any status, escapes any tool."""
    spec = ALL_TOOLS[tool_index]
    tool_name, invoke = spec.name, spec.invoke
    clients = raising_clients(api_exception(status, reason))

    response = invoke(clients, make_config(), "default")

    assert_envelope(response, tool_name)
    assert response["status"] == "error"
    json.dumps(response)


@given(
    tool_index=TOOL_INDEX,
    status=http_status_codes(exclude_404=True),
    reason=arbitrary_text(),
)
@settings(max_examples=100, deadline=None)
def test_property_7_carries_status_and_reason(tool_index, status, reason):
    """For any non-404 status, the error dict carries http_status and reason."""
    spec = ALL_TOOLS[tool_index]
    tool_name, invoke = spec.name, spec.invoke
    clients = raising_clients(api_exception(status, reason))

    response = invoke(clients, make_config(), "default")

    assert response["error"] == "kubernetes_api_error", tool_name
    assert response["http_status"] == status
    assert "reason" in response


@given(tool_index=TOOL_INDEX, reason=arbitrary_text())
@settings(max_examples=100, deadline=None)
def test_property_7_404_translates_to_domain_error(tool_index, reason):
    """404 becomes a structured error too, though a domain-specific one.

    This pins the documented divergence from Property 7's literal wording: the
    response is still structured and still never raises, but for the read-single
    tools it carries a *_not_found code with no http_status field.
    """
    spec = ALL_TOOLS[tool_index]
    tool_name, invoke = spec.name, spec.invoke
    clients = raising_clients(api_exception(404, reason))

    response = invoke(clients, make_config(), "default")

    assert_envelope(response, tool_name)
    assert response["status"] == "error"
    if response["error"] == "kubernetes_api_error":
        assert response["http_status"] == 404
    else:
        assert response["error"].endswith("_not_found"), tool_name
        assert "http_status" not in response


@given(
    tool_index=TOOL_INDEX,
    status=http_status_codes(),
    reason=arbitrary_text(),
)
@settings(max_examples=100, deadline=None)
def test_property_7_reason_is_serializable(tool_index, status, reason):
    """Arbitrary reason text survives into a JSON-serializable response."""
    invoke = ALL_TOOLS[tool_index].invoke
    clients = raising_clients(api_exception(status, reason))

    response = invoke(clients, make_config(), "default")

    encoded = json.dumps(response)
    assert json.loads(encoded) == response
