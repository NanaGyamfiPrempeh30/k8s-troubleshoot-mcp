"""Feature: k8s-troubleshoot-mcp, Property 17: escaping is actually applied.

For any tool and any dangerous payload injected into every string field of the
Kubernetes object that tool reads, no dangerous character may survive into the
response — except at field paths explicitly allowlisted with a written
justification.

Validates: REQ-020, REQ-021a, REQ-027, REQ-030a, REQ-035a, REQ-051a, REQ-051b.

Property 8 proves serialize_log_content is correct in isolation. This property
proves it is *reached*: the fakes are generated from each Kubernetes model's own
openapi_types schema, so a tool that reads a field nobody remembered to mock
still receives poisoned input. That is what makes this an omission detector
rather than only a regression guard.

What this property does NOT cover: a field the data model omits produces no
poison and passes silently. Omission is safe from a leakage standpoint, so this
is correct — but it means P17 does not subsume the point-of-omission comment
rule in CLAUDE.md. Different mechanisms, different failure modes.
"""

from __future__ import annotations

from hypothesis import given, settings, strategies as st

from tests.property.escaping_allowlist import (
    ALLOWLIST,
    NOT_YET_ESCAPED,
    VALID_JUSTIFICATION_TYPES,
    all_entries,
    allowed_paths,
)
from tests.property.fakes import (
    CONTROL_CHARS,
    PoisonedClients,
    find_dangerous_strings,
)
from tests.property.strategies import ALL_TOOLS, make_config

TOOL_INDEX = st.integers(min_value=0, max_value=len(ALL_TOOLS) - 1)

# Payloads built from the characters Property 8 is responsible for neutralizing,
# mixed with benign filler so the poison appears in realistic positions.
poison_payloads = st.text(
    alphabet=st.sampled_from(list('<>"\\' + "".join(sorted(CONTROL_CHARS))) + list("ab {}")),
    min_size=1,
    max_size=40,
).filter(lambda s: set(s) & (frozenset("<>") | CONTROL_CHARS))


@given(tool_index=TOOL_INDEX, poison=poison_payloads)
@settings(max_examples=100, deadline=None)
def test_property_17_no_unescaped_content_reaches_response(tool_index, poison):
    """No dangerous character survives except at allowlisted paths."""
    spec = ALL_TOOLS[tool_index]
    clients = PoisonedClients(spec.api_models, poison, spec.name)

    response = spec.invoke(clients, make_config(allowed={"default"}), "default")

    assert response["status"] == "success", (
        f"{spec.name} did not reach its success path; the poisoned fake is not "
        "exercising the field-extraction code this property tests"
    )

    permitted = allowed_paths(spec.name)
    violations = {
        path for path, _ in find_dangerous_strings(response) if path not in permitted
    }

    assert not violations, (
        f"{spec.name} leaked unescaped content at {sorted(violations)}. Either "
        "route the field through serialize_log_content, or add it to "
        "tests/property/escaping_allowlist.py with a written justification."
    )


@given(tool_index=TOOL_INDEX, poison=poison_payloads)
@settings(max_examples=100, deadline=None)
def test_property_17_message_fields_are_always_escaped(tool_index, poison):
    """No field named `message` may ever appear on the allowlist or leak.

    Message fields are the canonical injection vector and carry no
    enum-or-validated justification anywhere in the data model, so this holds
    unconditionally rather than by allowlist.
    """
    spec = ALL_TOOLS[tool_index]
    clients = PoisonedClients(spec.api_models, poison, spec.name)

    response = spec.invoke(clients, make_config(allowed={"default"}), "default")

    leaked_messages = [
        path
        for path, _ in find_dangerous_strings(response)
        if path.endswith(".message") or path.endswith(".content")
    ]

    assert not leaked_messages, (
        f"{spec.name} leaked an unescaped message/content field at "
        f"{leaked_messages}"
    )


def test_property_17_allowlist_has_no_unresolved_gaps():
    """A `not_yet_escaped` entry is a stop-ship signal, not a steady state."""
    gaps = [
        (tool, entry.field_path, entry.justification)
        for tool, entry in all_entries()
        if entry.justification_type == NOT_YET_ESCAPED
    ]

    assert not gaps, (
        "STOP-SHIP: the escaping allowlist contains unresolved gaps — these are "
        f"known-unescaped fields, not accepted tradeoffs: {gaps}"
    )


def test_property_17_allowlist_entries_are_well_formed():
    """Every entry carries a valid justification type and a real justification."""
    for tool_name, entry in all_entries():
        assert entry.justification_type in VALID_JUSTIFICATION_TYPES, (
            f"{tool_name}:{entry.field_path} has unknown justification_type "
            f"{entry.justification_type!r}"
        )
        assert len(entry.justification.strip()) > 20, (
            f"{tool_name}:{entry.field_path} has no substantive justification"
        )


def test_property_17_every_registered_tool_has_an_allowlist_entry():
    """Each tool must have an explicit allowlist key, even if empty.

    An empty tuple is a positive statement that the tool leaks nothing. A
    missing key would let a new tool default to "no expectations recorded".
    """
    registered = {spec.name for spec in ALL_TOOLS}
    listed = set(ALLOWLIST)

    assert registered == listed, (
        f"registry/allowlist mismatch — missing from allowlist: "
        f"{sorted(registered - listed)}; stale allowlist keys: "
        f"{sorted(listed - registered)}"
    )


@given(tool_index=TOOL_INDEX, poison=poison_payloads)
@settings(max_examples=100, deadline=None)
def test_property_17_allowlist_is_not_over_broad(tool_index, poison):
    """Allowlisted paths must actually be reachable, not stale entries.

    An allowlist that accumulates paths no longer produced would slowly lose its
    deny-by-default force. Every listed path must still appear in the poisoned
    response.
    """
    spec = ALL_TOOLS[tool_index]
    clients = PoisonedClients(spec.api_models, poison, spec.name)

    response = spec.invoke(clients, make_config(allowed={"default"}), "default")
    produced = {path for path, _ in find_dangerous_strings(response)}

    stale = allowed_paths(spec.name) - produced

    assert not stale, (
        f"{spec.name} has allowlist entries that no longer correspond to any "
        f"response field: {sorted(stale)}"
    )
