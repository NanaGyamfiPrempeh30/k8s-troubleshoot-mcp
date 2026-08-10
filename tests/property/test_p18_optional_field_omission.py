"""Feature: k8s-troubleshoot-mcp, Property 18: optional-field omission safety.

For any tool, and for any Kubernetes object in which every optional field has
been omitted, the tool must still return a valid, JSON-serializable response
envelope rather than raising.

This is the mechanised form of the audit that found two real-cluster bugs:
`list_nodes` returning `"unschedulable": null` because the API omits the field
on schedulable nodes, and `list_pods` raising AttributeError on
`pod.metadata.name` because `metadata` is optional in the model.

Validates the non-negotiable rule that no exception reaches the MCP layer, under
the input shape hand-written mocks systematically fail to produce.

Note what this property does NOT assert: that a given response field is
non-null. Which fields may legitimately be null is a per-field contract question
(design.md marks nullable fields as "X|null"), tracked separately; encoding it
here would require a per-field allowlist and is deliberately out of scope for
the crash-safety property.
"""

from __future__ import annotations

import json

from hypothesis import given, settings, strategies as st

from tests.property.omission_fakes import OmittingApi, is_required
from tests.property.strategies import (
    ALL_TOOLS,
    FakeClients,
    assert_envelope,
    make_config,
)

TOOL_INDEX = st.integers(min_value=0, max_value=len(ALL_TOOLS) - 1)


def _invoke(spec, deep: bool):
    api = OmittingApi(spec.api_models, deep, spec.name)
    clients = FakeClients(api, api, api, api)
    return spec.invoke(clients, make_config(allowed={"default"}), "default")


@given(tool_index=TOOL_INDEX, deep=st.booleans())
@settings(max_examples=100, deadline=None)
def test_property_18_omitted_optional_fields_never_raise(tool_index, deep):
    """No omission shape may produce an exception instead of a response."""
    spec = ALL_TOOLS[tool_index]

    response = _invoke(spec, deep)

    assert_envelope(response, spec.name)
    assert response["status"] == "success", (
        f"{spec.name} did not reach its success path on omitted input; the fake "
        f"is not exercising the tool (got {response.get('error')!r})"
    )


@given(tool_index=TOOL_INDEX, deep=st.booleans())
@settings(max_examples=100, deadline=None)
def test_property_18_omitted_responses_are_json_serializable(tool_index, deep):
    """A response full of Nones must still cross the MCP wire."""
    spec = ALL_TOOLS[tool_index]

    json.dumps(_invoke(spec, deep))


def test_property_18_both_shapes_are_actually_different():
    """Guards the fakes themselves.

    SPARSE and DEEP must not collapse into the same object, or half the
    property silently stops testing anything. DEEP reaches scalars that SPARSE
    masks behind a None parent, which is where the contract gaps were found.
    """
    spec = next(s for s in ALL_TOOLS if s.name == "get_hpa_status")

    sparse = _invoke(spec, deep=False)["data"]
    deep = _invoke(spec, deep=True)["data"]

    assert sparse != deep
    # conditions[] sits under an optional status object: SPARSE omits status
    # entirely and gets an empty list, DEEP builds it and reaches the optional
    # scalars inside — which is where DEEP found current_replicas passing None
    # through while the status-absent branch answered 0.
    #
    # Deliberately not asserted on current_replicas itself: that field is now
    # normalized to 0 in both shapes, so using it here would make this guard
    # silently vacuous the moment a similar normalization lands elsewhere.
    assert sparse["conditions"] == []
    assert deep["conditions"] and deep["conditions"][0]["reason"] is None


def test_property_18_required_detection_is_not_vacuous():
    """If required-ness detection broke, every field would look optional and
    the property would still pass while testing far less than it claims."""
    assert is_required("V1ContainerStatus", "ready") is True
    assert is_required("V2HorizontalPodAutoscalerSpec", "max_replicas") is True
    assert is_required("V1Taint", "key") is True

    assert is_required("V1NodeSpec", "unschedulable") is False
    assert is_required("V1Taint", "value") is False
    assert is_required("V2HorizontalPodAutoscalerSpec", "min_replicas") is False


def test_property_18_covers_every_registered_tool():
    """A tool missing from the registry is a tool this property never sees."""
    assert len(ALL_TOOLS) == 16
    assert len({spec.name for spec in ALL_TOOLS}) == 16
