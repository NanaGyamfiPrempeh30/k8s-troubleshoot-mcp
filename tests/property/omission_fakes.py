"""Schema-driven fakes with optional fields omitted, for Property 18.

Kubernetes omits unset optional fields from its JSON, so the client leaves them
None. Hand-written mocks do the opposite: they populate everything the author
thought of. That mismatch shipped two bugs found only on a real cluster
(`unschedulable: null`, and the bytes-repr in get_pod_logs), so these fakes
generate the omitted-field shapes from each model's own schema.

Required-ness comes from the generated setter: kubernetes-client emits a
"must not be `None`" guard for required properties only.

Two shapes are needed because they miss different things:

* SPARSE  — every optional field is None, including nested objects. This is a
  freshly created or partially reconciled object. A None parent hides its
  children, so it cannot reach inner scalars.
* DEEP    — every nested object is built, but every optional *scalar* is None.
  This reaches the inner fields SPARSE masks (condition.reason, taint.value,
  status.current_replicas) which are exactly where the contract gaps live.
"""

from __future__ import annotations

import datetime
import inspect
from typing import Any

from kubernetes import client as kc

MAX_DEPTH = 6

PRIMITIVES = {"str", "int", "float", "bool", "datetime", "date", "object"}

_PRIMITIVE_VALUES: dict[str, Any] = {
    "str": "x",
    "int": 1,
    "float": 1.0,
    "bool": True,
    "object": "x",
    "datetime": datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
    "date": datetime.date(2026, 1, 1),
}

_required_cache: dict[tuple[str, str], bool] = {}


def is_required(model_name: str, field: str) -> bool:
    """Whether the Kubernetes API marks a model field required."""
    key = (model_name, field)
    if key in _required_cache:
        return _required_cache[key]

    prop = getattr(getattr(kc.models, model_name, None), field, None)
    result = False
    if prop is not None and getattr(prop, "fset", None) is not None:
        try:
            result = "must not be `None`" in inspect.getsource(prop.fset)
        except OSError:  # pragma: no cover - source unavailable
            result = False

    _required_cache[key] = result
    return result


class OmittedModel:
    """Stand-in for a Kubernetes model with some attributes set to None."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"OmittedModel({sorted(self.__dict__)})"


def build_omitted(
    type_str: str,
    deep: bool,
    depth: int = 0,
    model_name: str | None = None,
    field: str | None = None,
) -> Any:
    """Build a fake with optional fields omitted.

    Args:
        type_str: An `openapi_types` type string, or the pseudo-type
            "urllib3_response(bytes)" for read_namespaced_pod_log.
        deep: False for the SPARSE shape, True for the DEEP shape.
        depth: Current recursion depth.
        model_name: Owning model of `type_str`, for the required-ness lookup.
        field: Field name of `type_str` on `model_name`.
    """
    if depth > MAX_DEPTH:
        return None

    if type_str == "urllib3_response(bytes)":
        response = OmittedModel()
        response.data = b"log line\n"
        return response

    optional = (
        model_name is not None
        and field is not None
        and not is_required(model_name, field)
    )

    if type_str in PRIMITIVES:
        return None if optional else _PRIMITIVE_VALUES[type_str]

    # SPARSE omits optional containers and nested objects outright; DEEP builds
    # them so the recursion can reach the scalars inside.
    if optional and not deep:
        return None

    if type_str.startswith("list["):
        built = build_omitted(type_str[len("list[") : -1], deep, depth + 1)
        return [built] if built is not None else []

    if type_str.startswith(("dict[", "dict(")):
        opener = "dict[" if type_str.startswith("dict[") else "dict("
        _, _, value_type = type_str[len(opener) : -1].partition(", ")
        built = build_omitted(value_type or "str", deep, depth + 1)
        return {"x": built}

    model = getattr(kc.models, type_str, None)
    if model is None or not hasattr(model, "openapi_types"):
        return None

    obj = OmittedModel()
    for name, field_type in model.openapi_types.items():
        setattr(obj, name, build_omitted(field_type, deep, depth + 1, type_str, name))
    return obj


class OmittingApi:
    """Fake API returning omitted-field objects, one per declared method."""

    def __init__(self, api_models: dict[str, str], deep: bool, tool_name: str) -> None:
        self._api_models = api_models
        self._deep = deep
        self._tool_name = tool_name

    def __getattr__(self, method_name: str):
        def call(*_args: Any, **_kwargs: Any) -> Any:
            if method_name not in self._api_models:
                raise AssertionError(
                    f"{self._tool_name} called an API method the registry does not "
                    f"declare: {method_name!r}. Add it to the tool's api_models in "
                    "tests/property/strategies.py."
                )
            return build_omitted(self._api_models[method_name], self._deep)

        return call


def collect_nulls(value: Any, path: str = "") -> list[str]:
    """Every dotted path in a response whose value is None."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            found += collect_nulls(item, f"{path}.{key}" if path else key)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += collect_nulls(item, f"{path}[{index}]")
    elif value is None:
        found.append(path)
    return found
