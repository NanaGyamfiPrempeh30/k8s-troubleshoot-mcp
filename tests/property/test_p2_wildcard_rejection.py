"""Feature: k8s-troubleshoot-mcp, Property 2: Wildcard namespace rejection.

For any ALLOWED_NAMESPACES string containing the token `*` or `all` as a
comma-separated element, startup validation must reject it with a non-zero exit
code before tool registration.

Validates: REQ-005.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings, strategies as st

from k8s_troubleshoot_mcp import config as config_module
from tests.property.strategies import env, namespace_sets

WILDCARD_TOKENS = ["*", "all"]


@st.composite
def wildcard_bearing_strings(draw):
    """A comma-separated namespace string with a wildcard token spliced in."""
    names = draw(namespace_sets(min_size=0, max_size=5))
    token = draw(st.sampled_from(WILDCARD_TOKENS))
    position = draw(st.integers(min_value=0, max_value=len(names)))

    elements = list(names)
    elements.insert(position, token)

    # Arbitrary surrounding whitespace must not let a wildcard slip through.
    padded = [
        draw(st.text(alphabet=" ", max_size=2)) + e + draw(st.text(alphabet=" ", max_size=2))
        for e in elements
    ]
    return ",".join(padded)


@given(value=wildcard_bearing_strings())
@settings(max_examples=100, deadline=None)
def test_property_2_wildcard_always_rejected(value):
    """Any wildcard token anywhere in the list aborts startup."""
    with env(ALLOWED_NAMESPACES=value):
        with pytest.raises(SystemExit) as excinfo:
            config_module._validate_allowed_namespaces()

    assert excinfo.value.code != 0


@given(names=namespace_sets(min_size=1, max_size=5))
@settings(max_examples=100, deadline=None)
def test_property_2_wildcard_free_input_is_accepted(names):
    """Control: the same generator without a wildcard must not be rejected.

    Without this, the property above could pass trivially by rejecting
    everything.
    """
    with env(ALLOWED_NAMESPACES=",".join(names)):
        result = config_module._validate_allowed_namespaces()

    assert result == frozenset(names)


@given(names=namespace_sets(min_size=1, max_size=4))
@settings(max_examples=100, deadline=None)
def test_property_2_wildcard_as_substring_is_not_a_wildcard(names):
    """A name merely containing 'all' is a normal namespace, not a wildcard.

    Rejection must key on the whole token, so `gallery` or `smallish` stay
    valid. This guards the property against an over-broad implementation.
    """
    padded = [f"x{n}all" for n in names]
    with env(ALLOWED_NAMESPACES=",".join(padded)):
        result = config_module._validate_allowed_namespaces()

    assert result == frozenset(padded)
