"""Feature: k8s-troubleshoot-mcp, Property 16: list_namespaces allowlist subset.

For any set of namespaces returned by the cluster API, the list_namespaces
response must contain only namespaces present in config.allowed_namespaces.

Validates: REQ-058, REQ-072.

tools/namespaces.py is not implemented yet. These tests are written against the
contract in requirements.md and design.md and are skipped until the module
exists — they are not placeholders, and will execute unchanged the moment
list_namespaces lands. Do not mark namespaces.py complete while this file is
still reporting as skipped.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest import mock

import pytest
from hypothesis import given, settings, strategies as st

from tests.property.strategies import FakeClients, make_config, namespace_sets

namespaces_module = pytest.importorskip(
    "k8s_troubleshoot_mcp.tools.namespaces",
    reason="tools/namespaces.py not implemented yet (Property 16 pending)",
)

list_namespaces = namespaces_module.list_namespaces


def _namespace(name):
    ns = mock.MagicMock()
    ns.metadata.name = name
    ns.metadata.creation_timestamp = datetime.now(timezone.utc)
    ns.status.phase = "Active"
    return ns


def _clients_returning(names):
    core = mock.MagicMock()
    core.list_namespace.return_value = mock.MagicMock(
        items=[_namespace(n) for n in names]
    )
    return FakeClients(core, mock.MagicMock(), mock.MagicMock(), mock.MagicMock())


def _returned_names(response):
    entries = response["data"]["namespaces"]
    return [e["name"] if isinstance(e, dict) else e for e in entries]


@given(allowed=namespace_sets(min_size=1, max_size=5), cluster=namespace_sets(min_size=0, max_size=10))
@settings(max_examples=100, deadline=None)
def test_property_16_response_is_subset_of_allowed(allowed, cluster):
    """Whatever the cluster returns, the response never exceeds the allowlist."""
    config = make_config(allowed=allowed)

    response = list_namespaces(_clients_returning(cluster), config)

    assert response["status"] == "success"
    assert set(_returned_names(response)) <= set(allowed)


@given(allowed=namespace_sets(min_size=1, max_size=5))
@settings(max_examples=100, deadline=None)
def test_property_16_system_namespaces_never_appear(allowed):
    """A cluster advertising system namespaces cannot get them into the response."""
    cluster = list(allowed) + ["kube-system", "kube-public"]
    config = make_config(allowed=allowed)

    response = list_namespaces(_clients_returning(cluster), config)

    returned = set(_returned_names(response))
    assert "kube-system" not in returned
    assert "kube-public" not in returned


@given(allowed=namespace_sets(min_size=2, max_size=6), data=st.data())
@settings(max_examples=100, deadline=None)
def test_property_16_allowed_but_absent_namespaces_do_not_appear(allowed, data):
    """The response is the INTERSECTION, not the allowlist echoed back.

    Property 16's subset-of-allowed wording is satisfied trivially by an
    implementation that returns config.allowed_namespaces without ever calling
    the cluster — which would claim non-existent namespaces exist. This pins the
    other direction: the response must also be a subset of what the cluster
    actually returned.
    """
    present = data.draw(st.lists(st.sampled_from(allowed), unique=True))
    absent = [n for n in allowed if n not in present]
    config = make_config(allowed=allowed)

    response = list_namespaces(_clients_returning(present), config)

    returned = set(_returned_names(response))
    assert returned <= set(present), "reported a namespace the cluster does not have"
    for name in absent:
        assert name not in returned, f"{name} is allowed but absent from the cluster"


@given(allowed=namespace_sets(min_size=1, max_size=4), rogue=namespace_sets(min_size=1, max_size=4))
@settings(max_examples=100, deadline=None)
def test_property_16_rogue_cluster_entries_filtered(allowed, rogue):
    """Namespaces outside the allowlist are filtered even if the API lists them."""
    outsiders = [n for n in rogue if n not in allowed]
    config = make_config(allowed=allowed)

    response = list_namespaces(_clients_returning(list(allowed) + outsiders), config)

    returned = set(_returned_names(response))
    for outsider in outsiders:
        assert outsider not in returned
