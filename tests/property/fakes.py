"""Schema-driven fake Kubernetes objects for Property 17.

Rather than hand-building a mock per tool — which only poisons the fields the
author remembered — these fakes are generated from each Kubernetes model's own
`openapi_types` schema. Every `str` field in the object graph carries the poison
payload, so a tool that reads a field nobody thought about still receives
poisoned input. That is what makes Property 17 detect *omissions* rather than
only regressions.
"""

from __future__ import annotations

import datetime
from typing import Any

from kubernetes import client as kc

# How deep to recurse before returning None. Kubernetes models nest a few levels
# (status.conditions[].message is depth 3); this is comfortably past anything the
# tools read, while keeping graph construction bounded.
MAX_DEPTH = 6

# Characters that must never appear raw in a tool response. U+0000-U+001F are the
# JSON-illegal control characters; < and > are the structural prompt-injection
# mitigation added on top of JSON escaping.
CONTROL_CHARS = frozenset(chr(c) for c in range(0x00, 0x20))
DANGEROUS_CHARS = frozenset('<>') | CONTROL_CHARS


class FakeModel:
    """A stand-in for a Kubernetes model object with arbitrary attributes."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"FakeModel({sorted(self.__dict__)})"


def build_poisoned(type_str: str, poison: str, depth: int = 0) -> Any:
    """Build a fully-populated fake for a model type, poisoning every string.

    Args:
        type_str: A `openapi_types` type string, e.g. "V1Pod", "list[V1Pod]",
            "str". "str" is accepted directly for APIs that return a bare string
            (read_namespaced_pod_log).
        poison: The payload injected into every string-typed field.
        depth: Current recursion depth.

    Returns:
        A FakeModel graph, primitive, list or dict as appropriate.
    """
    if depth > MAX_DEPTH:
        return None

    if type_str == "str":
        return poison
    if type_str == "urllib3_response(bytes)":
        # Not an openapi_types string. read_namespaced_pod_log is called with
        # _preload_content=False, so the client returns the raw urllib3 response
        # with a bytes body rather than a deserialized str. Poisoning the
        # decoded-text layer only would test a shape the real client never
        # returns — the bytes-repr bug is exactly what that blind spot allowed.
        response = FakeModel()
        response.data = poison.encode("utf-8")
        return response
    if type_str == "int":
        return 1
    if type_str == "float":
        return 1.0
    if type_str == "bool":
        return True
    if type_str == "datetime":
        return datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    if type_str == "date":
        return datetime.date(2026, 1, 1)
    if type_str == "object":
        # Untyped fields can hold anything; a poisoned string is the hostile case.
        return poison

    if type_str.startswith("list["):
        inner = type_str[len("list[") : -1]
        built = build_poisoned(inner, poison, depth + 1)
        return [built] if built is not None else []

    if type_str.startswith("dict[") or type_str.startswith("dict("):
        # Both bracket styles appear across generator versions.
        opener = "dict[" if type_str.startswith("dict[") else "dict("
        inner = type_str[len(opener) : -1]
        _, _, value_type = inner.partition(", ")
        built = build_poisoned(value_type or "str", poison, depth + 1)
        return {poison: built}

    model = getattr(kc.models, type_str, None)
    if model is None or not hasattr(model, "openapi_types"):
        return None

    obj = FakeModel()
    for field, field_type in model.openapi_types.items():
        setattr(obj, field, build_poisoned(field_type, poison, depth + 1))
    return obj


class DispatchingApi:
    """Fake API client that returns a poisoned model per method name.

    A tool that calls a method the registry did not declare raises loudly rather
    than silently receiving an unusable object — an undeclared call means the
    registry entry is stale, which is exactly the drift Property 17 exists to
    catch.
    """

    def __init__(self, api_models: dict[str, str], poison: str, tool_name: str) -> None:
        self._api_models = api_models
        self._poison = poison
        self._tool_name = tool_name

    def __getattr__(self, method_name: str) -> Any:
        def _call(*args: Any, **kwargs: Any) -> Any:
            if method_name not in self._api_models:
                raise AssertionError(
                    f"{self._tool_name} called undeclared API method "
                    f"{method_name!r}; add it to the tool's api_models in "
                    "tests/property/strategies.py"
                )
            return build_poisoned(self._api_models[method_name], self._poison)

        return _call


class PoisonedClients:
    """K8sClients stand-in whose every API dispatches poisoned models."""

    def __init__(self, api_models: dict[str, str], poison: str, tool_name: str) -> None:
        api = DispatchingApi(api_models, poison, tool_name)
        self.core_v1 = api
        self.apps_v1 = api
        self.autoscaling_v2 = api
        self.events_v1 = api


def normalize_path(path: str) -> str:
    """Collapse list indices so `$.data.conditions[0].x` matches `[*]`."""
    result = []
    index = 0
    while index < len(path):
        if path[index] == "[":
            end = path.index("]", index)
            result.append("[*]")
            index = end + 1
        else:
            result.append(path[index])
            index += 1
    return "".join(result)


def find_dangerous_strings(obj: Any, path: str = "$") -> list[tuple[str, str]]:
    """Return (normalized_path, offending_value) for every unescaped string.

    A string is unescaped if it literally contains <, > or a control character.
    Correctly escaped content carries these as the two-character sequences \\u003c,
    \\u003e or \\n, which contain no dangerous character.
    """
    hits: list[tuple[str, str]] = []

    if isinstance(obj, str):
        if set(obj) & DANGEROUS_CHARS:
            hits.append((normalize_path(path), obj))
    elif isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str) and (set(key) & DANGEROUS_CHARS):
                # A poisoned key would otherwise embed the payload in the path
                # of its own value, making that path unmatchable.
                hits.append((normalize_path(f"{path}.{{key}}"), key))
                child_path = f"{path}.*"
            else:
                child_path = f"{path}.{key}"
            hits.extend(find_dangerous_strings(value, child_path))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            hits.extend(find_dangerous_strings(value, f"{path}[{index}]"))

    return hits
