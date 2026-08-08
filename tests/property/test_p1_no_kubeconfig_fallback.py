"""Feature: k8s-troubleshoot-mcp, Property 1: No kubeconfig fallback.

For any startup invocation where KUBECONFIG is not set, the config loader must
never read ~/.kube/config or any default path; it must fail fast with exit 1.

Validates: REQ-003.
"""

from __future__ import annotations

from unittest import mock

import pytest
from hypothesis import given, settings, strategies as st

from k8s_troubleshoot_mcp import config as config_module
from k8s_troubleshoot_mcp import k8s_client
from tests.property.strategies import arbitrary_text, env

# Paths the loader must never consult on its own initiative.
DEFAULT_PATH_MARKERS = (".kube/config", ".kube\\config")


# The OS rejects NUL bytes in environment values, so the generator is
# constrained to what an environment can actually hold. This is a limit of the
# harness, not of the property.
env_values = st.text(
    alphabet=st.characters(codec="utf-8", exclude_characters="\x00"), max_size=30
)


@given(
    other_env=st.dictionaries(
        st.sampled_from(["ALLOWED_NAMESPACES", "LOG_LEVEL", "API_TIMEOUT_SECONDS"]),
        env_values,
        max_size=3,
    )
)
@settings(max_examples=100, deadline=None)
def test_property_1_unset_kubeconfig_always_exits(other_env):
    """Whatever else is in the environment, an unset KUBECONFIG exits 1."""
    with env(KUBECONFIG=None, **other_env):
        with pytest.raises(SystemExit) as excinfo:
            config_module._validate_kubeconfig()

    assert excinfo.value.code == 1


@given(blank=st.text(alphabet=" \t", max_size=8))
@settings(max_examples=100, deadline=None)
def test_property_1_blank_kubeconfig_never_probes_default_path(blank):
    """An empty/whitespace KUBECONFIG must not trigger a default-path probe."""
    probed: list[str] = []

    def _record_isfile(path):
        probed.append(str(path))
        return False

    with env(KUBECONFIG=blank):
        with mock.patch.object(config_module.os.path, "isfile", _record_isfile):
            with pytest.raises(SystemExit) as excinfo:
                config_module._validate_kubeconfig()

    assert excinfo.value.code == 1
    for path in probed:
        for marker in DEFAULT_PATH_MARKERS:
            assert marker not in path, f"default kubeconfig path probed: {path}"


@given(path=arbitrary_text().filter(lambda s: s.strip() != ""))
@settings(max_examples=100, deadline=None)
def test_property_1_build_clients_passes_explicit_path_only(path):
    """For any path, load_kube_config gets config_file=path and nothing else.

    The in-cluster and default-chain loaders must never be reached.
    """
    with mock.patch.object(k8s_client, "config") as mock_config, mock.patch.object(
        k8s_client, "client"
    ):
        k8s_client.build_clients(path)

    mock_config.load_kube_config.assert_called_once_with(config_file=path)
    mock_config.load_incluster_config.assert_not_called()

    # No other loader on the config module may have been used.
    for call in mock_config.mock_calls:
        name = call[0]
        assert name in ("load_kube_config", ""), f"unexpected config call: {name}"
