"""Feature: k8s-troubleshoot-mcp, Property 5: System-namespace exclusion.

For any ALLOWED_NAMESPACES input containing kube-system or kube-public, those
values must never appear in the parsed frozenset, regardless of what else is
present.

Validates: REQ-008.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings, strategies as st

from k8s_troubleshoot_mcp import config as config_module
from tests.property.strategies import env, namespace_sets

SYSTEM_NAMESPACES = ["kube-system", "kube-public"]


@st.composite
def input_with_system_namespaces(draw):
    """A namespace list containing at least one system namespace."""
    ordinary = draw(namespace_sets(min_size=1, max_size=5))
    system = draw(
        st.lists(st.sampled_from(SYSTEM_NAMESPACES), min_size=1, max_size=2, unique=True)
    )

    elements = draw(st.permutations(list(ordinary) + system))
    return ",".join(elements), frozenset(ordinary)


@given(pair=input_with_system_namespaces())
@settings(max_examples=100, deadline=None)
def test_property_5_system_namespaces_never_survive(pair):
    """kube-system and kube-public are stripped whatever else is present."""
    value, ordinary = pair

    with env(ALLOWED_NAMESPACES=value):
        result = config_module._validate_allowed_namespaces()

    assert "kube-system" not in result
    assert "kube-public" not in result
    assert result == ordinary


@given(
    system=st.lists(st.sampled_from(SYSTEM_NAMESPACES), min_size=1, max_size=2, unique=True)
)
@settings(max_examples=100, deadline=None)
def test_property_5_only_system_namespaces_aborts_startup(system):
    """An allowlist that is entirely system namespaces leaves nothing to allow."""
    with env(ALLOWED_NAMESPACES=",".join(system)):
        with pytest.raises(SystemExit) as excinfo:
            config_module._validate_allowed_namespaces()

    assert excinfo.value.code != 0


@given(pair=input_with_system_namespaces())
@settings(max_examples=100, deadline=None)
def test_property_5_exclusion_is_idempotent(pair):
    """Re-parsing an input with system namespaces is stable."""
    value, _ = pair

    with env(ALLOWED_NAMESPACES=value):
        first = config_module._validate_allowed_namespaces()
        second = config_module._validate_allowed_namespaces()

    assert first == second
    assert not (first & frozenset(SYSTEM_NAMESPACES))
