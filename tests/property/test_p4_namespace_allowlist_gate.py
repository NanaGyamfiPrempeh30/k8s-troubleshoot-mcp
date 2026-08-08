"""Feature: k8s-troubleshoot-mcp, Property 4: Namespace allowlist gate.

For any namespace not present in allowed_namespaces, and for any namespace-scoped
tool, the tool must return a structured namespace_not_allowed error and must never
invoke any method on any Kubernetes API client.

Validates: REQ-007, REQ-019.

This is the generated-input form of the property. The unit suites assert the same
behavior for one hand-picked namespace per tool; here the disallowed namespace,
the allowlist contents, and the tool are all quantified over.
"""

from __future__ import annotations

import json

from hypothesis import given, settings, strategies as st

from tests.property.strategies import (
    NAMESPACED_TOOLS,
    forbidden_clients,
    make_config,
    namespace_names,
    namespace_sets,
)


@st.composite
def allowlist_and_outsider(draw):
    """An allowlist plus a namespace guaranteed not to be in it."""
    allowed = draw(namespace_sets(min_size=1, max_size=5))
    outsider = draw(namespace_names().filter(lambda n: n not in allowed))
    return frozenset(allowed), outsider


@given(pair=allowlist_and_outsider(), tool_index=st.integers(min_value=0, max_value=len(NAMESPACED_TOOLS) - 1))
@settings(max_examples=100, deadline=None)
def test_property_4_disallowed_namespace_makes_no_api_call(pair, tool_index):
    """No Kubernetes API method is reached for a disallowed namespace."""
    allowed, outsider = pair
    spec = NAMESPACED_TOOLS[tool_index]
    tool_name, invoke = spec.name, spec.invoke
    config = make_config(allowed=allowed)
    clients, recorder = forbidden_clients()

    response = invoke(clients, config, outsider)

    assert recorder == [], f"{tool_name} called {recorder} for namespace {outsider!r}"
    assert response["status"] == "error"
    assert response["error"] == "namespace_not_allowed"
    assert response["tool"] == tool_name


@given(pair=allowlist_and_outsider())
@settings(max_examples=100, deadline=None)
def test_property_4_holds_for_every_tool_simultaneously(pair):
    """Every namespace-scoped tool gates on the same disallowed namespace."""
    allowed, outsider = pair
    config = make_config(allowed=allowed)

    for spec in NAMESPACED_TOOLS:
        tool_name, invoke = spec.name, spec.invoke
        clients, recorder = forbidden_clients()
        response = invoke(clients, config, outsider)

        assert recorder == [], f"{tool_name} reached the API"
        assert response["error"] == "namespace_not_allowed", tool_name


@given(pair=allowlist_and_outsider(), tool_index=st.integers(min_value=0, max_value=len(NAMESPACED_TOOLS) - 1))
@settings(max_examples=100, deadline=None)
def test_property_4_error_is_serializable_and_lists_allowed(pair, tool_index):
    """The gate's error response is well-formed and discloses the allowlist."""
    allowed, outsider = pair
    invoke = NAMESPACED_TOOLS[tool_index].invoke
    config = make_config(allowed=allowed)
    clients, _ = forbidden_clients()

    response = invoke(clients, config, outsider)

    json.dumps(response)
    assert sorted(response["allowed_namespaces"]) == sorted(allowed)
    assert outsider not in response["allowed_namespaces"]
