"""Feature: k8s-troubleshoot-mcp, Property 9: no credential-bearing fields.

For any mock pod object, regardless of what is present in spec.containers[*].env,
spec.volumes, or spec.containers[*].volume_mounts, the get_pod_status response
must never contain env/volume keys or leak their values.

Validates: REQ-023.
"""

from __future__ import annotations

import json
from unittest import mock

from hypothesis import given, settings, strategies as st

from k8s_troubleshoot_mcp.tools.pods import get_pod_status
from tests.property.strategies import FakeClients, make_config

FORBIDDEN_KEYS = {
    "env",
    "env_vars",
    "environment",
    "envvars",
    "volumes",
    "volume_mounts",
    "volumemounts",
    "secrets",
    "env_from",
}

# Distinctive values that must not appear anywhere in the serialized response.
secret_values = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", min_size=12, max_size=24
)


def _walk_keys(obj):
    """Yield every dict key appearing anywhere in a nested structure."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key
            yield from _walk_keys(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_keys(item)


def _build_pod(secrets):
    """A pod whose env, volumes and mounts are stuffed with secret values."""
    containers = []
    for index, secret in enumerate(secrets):
        env_var = mock.MagicMock()
        env_var.name = f"SECRET_{index}"
        env_var.value = secret

        mount = mock.MagicMock()
        mount.name = f"vol-{index}"
        mount.mount_path = f"/run/secrets/{secret}"

        container = mock.MagicMock()
        container.name = f"c{index}"
        container.env = [env_var]
        container.volume_mounts = [mount]
        containers.append(container)

    volume = mock.MagicMock()
    volume.name = "creds"
    volume.secret.secret_name = secrets[0] if secrets else "none"

    pod = mock.MagicMock()
    pod.spec.containers = containers
    pod.spec.volumes = [volume]
    pod.spec.node_name = "node-1"
    pod.status.phase = "Running"
    pod.status.qos_class = "Burstable"
    pod.status.conditions = []
    pod.status.container_statuses = []
    return pod


@given(secrets=st.lists(secret_values, min_size=1, max_size=4, unique=True))
@settings(max_examples=100, deadline=None)
def test_property_9_no_forbidden_keys(secrets):
    """No credential-bearing key appears anywhere in the response."""
    core = mock.MagicMock()
    core.read_namespaced_pod.return_value = _build_pod(secrets)
    clients = FakeClients(core, mock.MagicMock(), mock.MagicMock(), mock.MagicMock())

    response = get_pod_status(clients, make_config(), "pod", "default")

    assert response["status"] == "success"
    present = {k.lower() for k in _walk_keys(response)}
    assert not (present & FORBIDDEN_KEYS), f"forbidden keys present: {present & FORBIDDEN_KEYS}"


@given(secrets=st.lists(secret_values, min_size=1, max_size=4, unique=True))
@settings(max_examples=100, deadline=None)
def test_property_9_no_secret_values_leak(secrets):
    """No env value, mount path or secret name reaches the serialized response."""
    core = mock.MagicMock()
    core.read_namespaced_pod.return_value = _build_pod(secrets)
    clients = FakeClients(core, mock.MagicMock(), mock.MagicMock(), mock.MagicMock())

    response = get_pod_status(clients, make_config(), "pod", "default")
    encoded = json.dumps(response)

    for secret in secrets:
        assert secret not in encoded, f"secret value {secret!r} leaked into response"


@given(
    secrets=st.lists(secret_values, min_size=1, max_size=3, unique=True),
    container_count=st.integers(min_value=1, max_value=5),
)
@settings(max_examples=100, deadline=None)
def test_property_9_holds_regardless_of_container_count(secrets, container_count):
    """Adding containers does not open a leak path."""
    core = mock.MagicMock()
    core.read_namespaced_pod.return_value = _build_pod(secrets * container_count)
    clients = FakeClients(core, mock.MagicMock(), mock.MagicMock(), mock.MagicMock())

    response = get_pod_status(clients, make_config(), "pod", "default")
    encoded = json.dumps(response)

    for secret in secrets:
        assert secret not in encoded
    present = {k.lower() for k in _walk_keys(response)}
    assert not (present & FORBIDDEN_KEYS)
