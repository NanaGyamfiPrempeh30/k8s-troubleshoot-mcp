"""Feature: k8s-troubleshoot-mcp, Property 13: label_selector passthrough.

For any non-empty string passed as label_selector to list_pods, the value
forwarded to CoreV1Api.list_namespaced_pod must be byte-for-byte identical to the
input.

Validates: REQ-033.
"""

from __future__ import annotations

from unittest import mock

from hypothesis import given, settings, strategies as st

from k8s_troubleshoot_mcp.tools.pods import list_pods
from tests.property.strategies import FakeClients, arbitrary_text, make_config

# Selector-shaped strings, plus arbitrary text: the tool must not interpret,
# normalize, escape or validate any of it — that is the API server's job.
selector_shaped = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789=!,.()/-_ ", min_size=1, max_size=60
)


def _clients():
    core = mock.MagicMock()
    core.list_namespaced_pod.return_value = mock.MagicMock(items=[])
    return FakeClients(core, mock.MagicMock(), mock.MagicMock(), mock.MagicMock())


@given(selector=st.one_of(selector_shaped, arbitrary_text().filter(bool)))
@settings(max_examples=100, deadline=None)
def test_property_13_forwarded_unchanged(selector):
    """The selector reaches the API client byte-for-byte."""
    clients = _clients()

    list_pods(clients, make_config(), "default", label_selector=selector)

    forwarded = clients.core_v1.list_namespaced_pod.call_args.kwargs["label_selector"]
    assert forwarded == selector
    assert forwarded.encode("utf-8") == selector.encode("utf-8")


@given(selector=selector_shaped)
@settings(max_examples=100, deadline=None)
def test_property_13_no_normalization_applied(selector):
    """No stripping, casing or whitespace collapsing happens in transit."""
    padded = f"  {selector}  "
    clients = _clients()

    list_pods(clients, make_config(), "default", label_selector=padded)

    forwarded = clients.core_v1.list_namespaced_pod.call_args.kwargs["label_selector"]
    assert forwarded == padded
    assert forwarded != forwarded.strip() or padded == padded.strip()


@given(namespace_suffix=st.text(alphabet="abcdefghij", min_size=1, max_size=6))
@settings(max_examples=100, deadline=None)
def test_property_13_absent_selector_forwards_none(namespace_suffix):
    """Omitting the selector forwards None rather than an empty string."""
    namespace = f"ns{namespace_suffix}"
    clients = _clients()

    list_pods(clients, make_config(allowed={namespace}), namespace)

    forwarded = clients.core_v1.list_namespaced_pod.call_args.kwargs["label_selector"]
    assert forwarded is None
