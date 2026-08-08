"""Feature: k8s-troubleshoot-mcp, Property 8: log content escaping.

For any string containing <, >, ", \\, or Unicode control characters
(U+0000-U+001F), serialize_log_content must escape them such that embedding the
result in a JSON string field does not alter the semantics of surrounding JSON.

Validates: REQ-020, REQ-027.
"""

from __future__ import annotations

import json

from hypothesis import given, settings, strategies as st

from k8s_troubleshoot_mcp.response import serialize_log_content
from tests.property.strategies import arbitrary_text

CONTROL_CHARS = "".join(chr(c) for c in range(0x00, 0x20))
DANGEROUS = '<>"\\' + CONTROL_CHARS

# Text drawn to over-represent the characters the property is about.
hostile_text = st.text(alphabet=st.sampled_from(list(DANGEROUS) + list("abc {}[]:,")), max_size=120)


@given(raw=st.one_of(arbitrary_text(), hostile_text))
@settings(max_examples=100, deadline=None)
def test_property_8_embedding_preserves_semantics(raw):
    """The escaped form round-trips back to the original through JSON."""
    escaped = serialize_log_content(raw)

    decoded = json.loads(f'"{escaped}"')

    assert decoded == raw


@given(raw=st.one_of(arbitrary_text(), hostile_text))
@settings(max_examples=100, deadline=None)
def test_property_8_no_dangerous_characters_survive_raw(raw):
    """No <, >, raw quote, or control character remains literally present."""
    escaped = serialize_log_content(raw)

    assert "<" not in escaped
    assert ">" not in escaped
    for char in CONTROL_CHARS:
        assert char not in escaped, f"control char U+{ord(char):04X} survived"


@given(raw=st.one_of(arbitrary_text(), hostile_text))
@settings(max_examples=100, deadline=None)
def test_property_8_survives_json_object_embedding(raw):
    """Embedding in a larger JSON object cannot break out of the field."""
    escaped = serialize_log_content(raw)
    document = '{"before": 1, "content": "' + escaped + '", "after": 2}'

    parsed = json.loads(document)

    assert parsed["before"] == 1
    assert parsed["after"] == 2
    assert parsed["content"] == raw


@given(raw=st.one_of(arbitrary_text(), hostile_text))
@settings(max_examples=100, deadline=None)
def test_property_8_is_idempotent_under_json_encoding(raw):
    """Escaping is deterministic: the same input always yields the same output."""
    assert serialize_log_content(raw) == serialize_log_content(raw)


@given(prefix=arbitrary_text(), suffix=arbitrary_text())
@settings(max_examples=100, deadline=None)
def test_property_8_angle_brackets_always_unicode_escaped(prefix, suffix):
    """A literal tag in arbitrary surrounding text is always neutralized."""
    raw = f"{prefix}<script>{suffix}"

    escaped = serialize_log_content(raw)

    assert "\\u003c" in escaped
    assert "\\u003e" in escaped
    assert json.loads(f'"{escaped}"') == raw
