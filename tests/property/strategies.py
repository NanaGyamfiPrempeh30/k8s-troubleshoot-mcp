"""Shared hypothesis strategies and fakes for the property test suite.

Every property test in this package draws its inputs from here so that the
generated-input space is consistent across properties, and so that a single
tool registry drives the properties that must hold "for any tool" (P4, P6, P7).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, NamedTuple
from unittest import mock

from hypothesis import strategies as st
from kubernetes.client.exceptions import ApiException

from k8s_troubleshoot_mcp.config import ServerConfig
from k8s_troubleshoot_mcp.tools.pods import (
    get_pod_status,
    get_pod_logs,
    get_pod_events,
    list_pods,
)
from k8s_troubleshoot_mcp.tools.nodes import get_node_status, list_nodes
from k8s_troubleshoot_mcp.tools.workloads import (
    get_deployment_status,
    list_deployments,
    get_statefulset_status,
    get_daemonset_status,
)
from k8s_troubleshoot_mcp.tools.services import get_service, get_endpoints
from k8s_troubleshoot_mcp.tools.storage import get_pvc_status
from k8s_troubleshoot_mcp.tools.autoscaling import get_hpa_status
from k8s_troubleshoot_mcp.tools.events import get_namespace_events
from k8s_troubleshoot_mcp.tools.namespaces import list_namespaces

# Tokens the config layer treats specially; excluded from "ordinary namespace"
# generation so P2/P5 can inject them deliberately rather than by accident.
RESERVED_NAMESPACE_TOKENS = frozenset({"*", "all", "kube-system", "kube-public"})

_NS_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789-"


def namespace_names() -> st.SearchStrategy[str]:
    """Valid, non-reserved namespace names (DNS-1123-label shaped)."""
    return st.text(alphabet=_NS_ALPHABET, min_size=1, max_size=20).filter(
        lambda s: (
            s not in RESERVED_NAMESPACE_TOKENS
            and not s.startswith("-")
            and not s.endswith("-")
        )
    )


class PodLogResponse:
    """The object read_namespaced_pod_log actually returns in this codebase.

    get_pod_logs passes ``_preload_content=False``, so the kubernetes client
    returns the raw urllib3 response rather than a deserialized ``str``, and the
    body on it is **bytes**.

    Mocking this call with a plain ``str`` is what let the bytes-repr bug ship:
    every test agreed with every other test on a response shape the real client
    never produces. Any test that stubs this endpoint must go through here.
    """

    def __init__(self, text: str, encoding: str = "utf-8") -> None:
        self.data = text.encode(encoding)
        self.released = False

    def release_conn(self) -> None:
        self.released = True


def pod_log_response(text: str) -> PodLogResponse:
    """Build a realistic read_namespaced_pod_log return value from log text."""
    return PodLogResponse(text)


def namespace_sets(min_size: int = 1, max_size: int = 6) -> st.SearchStrategy[list[str]]:
    """Non-empty lists of distinct valid namespace names."""
    return st.lists(
        namespace_names(), min_size=min_size, max_size=max_size, unique=True
    )


def arbitrary_text() -> st.SearchStrategy[str]:
    """Arbitrary text excluding lone surrogates (which are not UTF-8 encodable)."""
    return st.text(alphabet=st.characters(codec="utf-8"), max_size=200)


def http_status_codes(exclude_404: bool = False) -> st.SearchStrategy[int]:
    """Arbitrary HTTP status codes as returned by the Kubernetes API server."""
    codes = st.integers(min_value=100, max_value=599)
    if exclude_404:
        codes = codes.filter(lambda c: c != 404)
    return codes


@contextmanager
def env(**overrides: str | None):
    """Temporarily set or unset environment variables.

    Used instead of pytest's monkeypatch inside @given tests: function-scoped
    fixtures are not reset between generated examples, so environment state
    must be managed per-example by an explicit context manager.

    Pass None as a value to ensure the variable is unset.
    """
    to_set = {k: v for k, v in overrides.items() if v is not None}
    to_clear = [k for k, v in overrides.items() if v is None]
    with mock.patch.dict(os.environ, to_set, clear=False):
        for key in to_clear:
            os.environ.pop(key, None)
        yield


def make_config(
    allowed: frozenset[str] | set[str] | list[str] | None = None,
    max_log_lines: int = 200,
    api_timeout_seconds: int = 30,
) -> ServerConfig:
    """Build a ServerConfig for property tests."""
    if allowed is None:
        allowed = {"default"}
    return ServerConfig(
        kubeconfig_path="/test/kubeconfig",
        allowed_namespaces=frozenset(allowed),
        log_level="INFO",
        api_timeout_seconds=api_timeout_seconds,
        max_log_lines=max_log_lines,
    )


class ForbiddenApi:
    """Stand-in API client that fails loudly if any method is invoked.

    Used by Property 4: the namespace gate must return before any Kubernetes
    API call happens. Calls are both recorded and raised, so the property holds
    even if a tool were to swallow the exception.
    """

    def __init__(self, recorder: list[str], api_name: str) -> None:
        self._recorder = recorder
        self._api_name = api_name

    def __getattr__(self, name: str) -> Any:
        def _forbidden(*args: Any, **kwargs: Any) -> Any:
            self._recorder.append(f"{self._api_name}.{name}")
            raise AssertionError(
                f"Kubernetes API method {self._api_name}.{name!r} was called "
                "for a disallowed namespace"
            )

        return _forbidden


class RaisingApi:
    """Stand-in API client where every method raises the given exception.

    Used by Properties 6 and 7 to drive every tool down its error path without
    knowing which API method that tool happens to call.
    """

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def __getattr__(self, name: str) -> Any:
        def _raise(*args: Any, **kwargs: Any) -> Any:
            raise self._exc

        return _raise


class FakeClients:
    """Minimal stand-in for K8sClients holding four API objects."""

    def __init__(self, core_v1: Any, apps_v1: Any, autoscaling_v2: Any, events_v1: Any):
        self.core_v1 = core_v1
        self.apps_v1 = apps_v1
        self.autoscaling_v2 = autoscaling_v2
        self.events_v1 = events_v1


def forbidden_clients() -> tuple[FakeClients, list[str]]:
    """Clients whose every API method is forbidden, plus the call recorder."""
    recorder: list[str] = []
    return (
        FakeClients(
            core_v1=ForbiddenApi(recorder, "core_v1"),
            apps_v1=ForbiddenApi(recorder, "apps_v1"),
            autoscaling_v2=ForbiddenApi(recorder, "autoscaling_v2"),
            events_v1=ForbiddenApi(recorder, "events_v1"),
        ),
        recorder,
    )


def raising_clients(exc: BaseException) -> FakeClients:
    """Clients whose every API method raises the given exception."""
    return FakeClients(
        core_v1=RaisingApi(exc),
        apps_v1=RaisingApi(exc),
        autoscaling_v2=RaisingApi(exc),
        events_v1=RaisingApi(exc),
    )


def api_exception(status: int, reason: str) -> ApiException:
    """Build an ApiException with the given status and reason."""
    return ApiException(status=status, reason=reason)


# --------------------------------------------------------------------------
# Tool registry
#
# The single registry that every "for any tool" property enumerates: P4
# (namespace gate), P6 (envelope), P7 (ApiException) and P17 (escaping applied).
#
# `invoke` takes (clients, config, namespace) so properties can iterate
# uniformly regardless of each tool's individual signature.
#
# `api_models` maps each Kubernetes API method the tool calls to the model type
# that method returns, using `openapi_types` type-string syntax. P17 uses it to
# synthesise a fully-populated poisoned object per method. A tool that calls a
# method absent from this mapping fails loudly under P17 rather than silently
# receiving an unusable object.
#
# ADDING A TOOL: add it here in the same change as the tool itself. A tool
# missing from this registry causes P4/P6/P7/P17 to stop covering it while
# still reporting green.
# --------------------------------------------------------------------------


class ToolSpec(NamedTuple):
    """One tool's registration for the "for any tool" properties."""

    name: str
    invoke: Any
    api_models: dict[str, str]


NAMESPACED_TOOLS: list[ToolSpec] = [
    ToolSpec(
        "get_pod_status",
        lambda c, cfg, ns: get_pod_status(c, cfg, "obj", ns),
        {"read_namespaced_pod": "V1Pod"},
    ),
    ToolSpec(
        "get_pod_logs",
        lambda c, cfg, ns: get_pod_logs(c, cfg, "obj", ns),
        # Not a model: with _preload_content=False this endpoint yields the raw
        # urllib3 response, whose body is bytes. See PodLogResponse above.
        {"read_namespaced_pod_log": "urllib3_response(bytes)"},
    ),
    ToolSpec(
        "get_pod_events",
        lambda c, cfg, ns: get_pod_events(c, cfg, "obj", ns),
        {"list_namespaced_event": "EventsV1EventList"},
    ),
    ToolSpec(
        "list_pods",
        lambda c, cfg, ns: list_pods(c, cfg, ns),
        {"list_namespaced_pod": "V1PodList"},
    ),
    ToolSpec(
        "get_deployment_status",
        lambda c, cfg, ns: get_deployment_status(c, cfg, "obj", ns),
        {"read_namespaced_deployment": "V1Deployment"},
    ),
    ToolSpec(
        "list_deployments",
        lambda c, cfg, ns: list_deployments(c, cfg, ns),
        {"list_namespaced_deployment": "V1DeploymentList"},
    ),
    ToolSpec(
        "get_statefulset_status",
        lambda c, cfg, ns: get_statefulset_status(c, cfg, "obj", ns),
        {"read_namespaced_stateful_set": "V1StatefulSet"},
    ),
    ToolSpec(
        "get_daemonset_status",
        lambda c, cfg, ns: get_daemonset_status(c, cfg, "obj", ns),
        {"read_namespaced_daemon_set": "V1DaemonSet"},
    ),
    ToolSpec(
        "get_service",
        lambda c, cfg, ns: get_service(c, cfg, "obj", ns),
        # get_service makes a second call to count ready endpoints.
        {
            "read_namespaced_service": "V1Service",
            "read_namespaced_endpoints": "V1Endpoints",
        },
    ),
    ToolSpec(
        "get_endpoints",
        lambda c, cfg, ns: get_endpoints(c, cfg, "obj", ns),
        {"read_namespaced_endpoints": "V1Endpoints"},
    ),
    ToolSpec(
        "get_pvc_status",
        lambda c, cfg, ns: get_pvc_status(c, cfg, "obj", ns),
        {"read_namespaced_persistent_volume_claim": "V1PersistentVolumeClaim"},
    ),
    ToolSpec(
        "get_namespace_events",
        lambda c, cfg, ns: get_namespace_events(c, cfg, ns),
        {"list_namespaced_event": "EventsV1EventList"},
    ),
    ToolSpec(
        "get_hpa_status",
        lambda c, cfg, ns: get_hpa_status(c, cfg, "obj", ns),
        {
            "read_namespaced_horizontal_pod_autoscaler": (
                "V2HorizontalPodAutoscaler"
            )
        },
    ),
]

# Cluster-scoped tools take no namespace; the invoker signature is kept
# identical so the same loops work, with the namespace argument ignored.
CLUSTER_SCOPED_TOOLS: list[ToolSpec] = [
    ToolSpec(
        "get_node_status",
        lambda c, cfg, ns: get_node_status(c, cfg, "node-1"),
        {"read_node": "V1Node"},
    ),
    ToolSpec(
        "list_namespaces",
        lambda c, cfg, ns: list_namespaces(c, cfg),
        {"list_namespace": "V1NamespaceList"},
    ),
    ToolSpec(
        "list_nodes",
        lambda c, cfg, ns: list_nodes(c, cfg),
        {"list_node": "V1NodeList"},
    ),
]

ALL_TOOLS: list[ToolSpec] = NAMESPACED_TOOLS + CLUSTER_SCOPED_TOOLS


def assert_envelope(response: Any, tool_name: str) -> None:
    """Assert the Property 6 envelope invariants on a tool response."""
    assert isinstance(response, dict), f"{tool_name} did not return a dict"
    assert isinstance(response.get("tool"), str) and response["tool"], (
        f"{tool_name} response has no non-empty 'tool' key"
    )
    assert response.get("status") in ("success", "error"), (
        f"{tool_name} response status is not success/error"
    )
    if response["status"] == "success":
        assert "data" in response, f"{tool_name} success response has no 'data'"
    else:
        assert "error" in response, f"{tool_name} error response has no 'error'"
        assert "message" in response, f"{tool_name} error response has no 'message'"
