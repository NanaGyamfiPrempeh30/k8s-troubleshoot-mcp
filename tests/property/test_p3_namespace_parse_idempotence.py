"""Feature: k8s-troubleshoot-mcp, Property 3: Namespace parse idempotence.

For any valid comma-separated namespace string, parsing once and parsing twice
must produce identical frozensets, and every element must be a stripped token of
the original with no surrounding whitespace.

Validates: REQ-006.
"""

from __future__ import annotations

from hypothesis import given, settings, strategies as st

from k8s_troubleshoot_mcp import config as config_module
from tests.property.strategies import env, namespace_sets

_PAD = st.text(alphabet=" \t", max_size=3)


@st.composite
def padded_namespace_strings(draw):
    """A valid namespace list with arbitrary whitespace padding per element."""
    names = draw(namespace_sets(min_size=1, max_size=6))
    parts = [draw(_PAD) + n + draw(_PAD) for n in names]
    return ",".join(parts), names


@given(pair=padded_namespace_strings())
@settings(max_examples=100, deadline=None)
def test_property_3_parse_is_idempotent(pair):
    """Parsing the same input twice yields identical frozensets."""
    value, _ = pair

    with env(ALLOWED_NAMESPACES=value):
        first = config_module._validate_allowed_namespaces()
        second = config_module._validate_allowed_namespaces()

    assert first == second
    assert isinstance(first, frozenset)
    assert isinstance(second, frozenset)


@given(pair=padded_namespace_strings())
@settings(max_examples=100, deadline=None)
def test_property_3_elements_are_stripped_tokens(pair):
    """Every parsed element is a stripped token drawn from the input."""
    value, names = pair

    with env(ALLOWED_NAMESPACES=value):
        result = config_module._validate_allowed_namespaces()

    raw_tokens = value.split(",")
    for element in result:
        assert element == element.strip(), f"{element!r} carries whitespace"
        assert any(element == token.strip() for token in raw_tokens)

    assert result == frozenset(names)


@given(pair=padded_namespace_strings(), extra_commas=st.integers(min_value=1, max_value=4))
@settings(max_examples=100, deadline=None)
def test_property_3_empty_tokens_are_dropped(pair, extra_commas):
    """Trailing and repeated separators do not introduce empty namespaces."""
    value, names = pair

    with env(ALLOWED_NAMESPACES=value + "," * extra_commas):
        result = config_module._validate_allowed_namespaces()

    assert "" not in result
    assert result == frozenset(names)
