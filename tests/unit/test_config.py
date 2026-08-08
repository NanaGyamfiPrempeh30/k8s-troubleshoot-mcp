"""Unit tests for config module."""

from __future__ import annotations

import os
import tempfile
from unittest import mock

import pytest

from k8s_troubleshoot_mcp.config import (
    ServerConfig,
    validate_env,
    _validate_kubeconfig,
    _validate_allowed_namespaces,
    _validate_log_level,
    _validate_api_timeout,
    _validate_max_log_lines,
)


class TestValidateKubeconfig:
    """Tests for KUBECONFIG validation."""

    def test_kubeconfig_not_set_exits(self):
        """REQ-001: Missing KUBECONFIG causes exit(1)."""
        with mock.patch.dict(os.environ, {}, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                _validate_kubeconfig()
            assert exc_info.value.code == 1

    def test_kubeconfig_empty_exits(self):
        """REQ-001: Empty KUBECONFIG causes exit(1)."""
        with mock.patch.dict(os.environ, {"KUBECONFIG": ""}, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                _validate_kubeconfig()
            assert exc_info.value.code == 1

    def test_kubeconfig_file_not_exists_exits(self):
        """REQ-002: Non-existent file causes exit(1)."""
        with mock.patch.dict(os.environ, {"KUBECONFIG": "/nonexistent/path"}, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                _validate_kubeconfig()
            assert exc_info.value.code == 1

    def test_kubeconfig_valid_path_returns_path(self):
        """Valid kubeconfig path is returned."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"test")
            path = f.name
        try:
            with mock.patch.dict(os.environ, {"KUBECONFIG": path}, clear=True):
                result = _validate_kubeconfig()
                assert result == path
        finally:
            os.unlink(path)


class TestValidateAllowedNamespaces:
    """Tests for ALLOWED_NAMESPACES validation."""

    def test_not_set_exits(self):
        """REQ-004: Missing ALLOWED_NAMESPACES causes exit(1)."""
        with mock.patch.dict(os.environ, {}, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                _validate_allowed_namespaces()
            assert exc_info.value.code == 1

    def test_empty_exits(self):
        """REQ-004: Empty ALLOWED_NAMESPACES causes exit(1)."""
        with mock.patch.dict(os.environ, {"ALLOWED_NAMESPACES": ""}, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                _validate_allowed_namespaces()
            assert exc_info.value.code == 1

    def test_wildcard_star_exits(self):
        """REQ-005: Wildcard * causes exit(1)."""
        with mock.patch.dict(os.environ, {"ALLOWED_NAMESPACES": "*"}, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                _validate_allowed_namespaces()
            assert exc_info.value.code == 1

    def test_wildcard_all_exits(self):
        """REQ-005: Wildcard 'all' causes exit(1)."""
        with mock.patch.dict(os.environ, {"ALLOWED_NAMESPACES": "all"}, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                _validate_allowed_namespaces()
            assert exc_info.value.code == 1

    def test_wildcard_mixed_exits(self):
        """REQ-005: Wildcard mixed with valid namespaces causes exit(1)."""
        with mock.patch.dict(os.environ, {"ALLOWED_NAMESPACES": "staging,*,prod"}, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                _validate_allowed_namespaces()
            assert exc_info.value.code == 1

    def test_system_namespaces_removed(self, capsys):
        """REQ-008: kube-system and kube-public are removed with warning."""
        with mock.patch.dict(
            os.environ,
            {"ALLOWED_NAMESPACES": "staging,kube-system,kube-public,prod"},
            clear=True,
        ):
            result = _validate_allowed_namespaces()
            assert "kube-system" not in result
            assert "kube-public" not in result
            assert "staging" in result
            assert "prod" in result
            captured = capsys.readouterr()
            assert "WARNING" in captured.err

    def test_only_system_namespaces_exits(self):
        """REQ-008: Only system namespaces after filtering causes exit(1)."""
        with mock.patch.dict(
            os.environ,
            {"ALLOWED_NAMESPACES": "kube-system,kube-public"},
            clear=True,
        ):
            with pytest.raises(SystemExit) as exc_info:
                _validate_allowed_namespaces()
            assert exc_info.value.code == 1

    def test_valid_namespaces_returns_frozenset(self):
        """REQ-006: Valid namespaces return immutable frozenset."""
        with mock.patch.dict(
            os.environ,
            {"ALLOWED_NAMESPACES": "staging, production, dev"},
            clear=True,
        ):
            result = _validate_allowed_namespaces()
            assert isinstance(result, frozenset)
            assert result == frozenset({"staging", "production", "dev"})

    def test_whitespace_stripped(self):
        """Whitespace is stripped from namespace names."""
        with mock.patch.dict(
            os.environ,
            {"ALLOWED_NAMESPACES": "  staging  ,  production  "},
            clear=True,
        ):
            result = _validate_allowed_namespaces()
            assert result == frozenset({"staging", "production"})


class TestValidateLogLevel:
    """Tests for LOG_LEVEL validation."""

    def test_default_info(self):
        """REQ-009: Default log level is INFO."""
        with mock.patch.dict(os.environ, {}, clear=True):
            result = _validate_log_level()
            assert result == "INFO"

    def test_valid_levels(self):
        """REQ-009: Valid log levels are accepted."""
        for level in ["DEBUG", "INFO", "WARNING", "ERROR"]:
            with mock.patch.dict(os.environ, {"LOG_LEVEL": level}, clear=True):
                result = _validate_log_level()
                assert result == level

    def test_lowercase_converted(self):
        """Log level is converted to uppercase."""
        with mock.patch.dict(os.environ, {"LOG_LEVEL": "debug"}, clear=True):
            result = _validate_log_level()
            assert result == "DEBUG"

    def test_invalid_defaults_to_info(self):
        """Invalid log level defaults to INFO."""
        with mock.patch.dict(os.environ, {"LOG_LEVEL": "INVALID"}, clear=True):
            result = _validate_log_level()
            assert result == "INFO"


class TestValidateApiTimeout:
    """Tests for API_TIMEOUT_SECONDS validation."""

    def test_default_30(self):
        """REQ-069: Default timeout is 30 seconds."""
        with mock.patch.dict(os.environ, {}, clear=True):
            result = _validate_api_timeout()
            assert result == 30

    def test_valid_integer(self):
        """REQ-069: Valid positive integer is accepted."""
        with mock.patch.dict(os.environ, {"API_TIMEOUT_SECONDS": "60"}, clear=True):
            result = _validate_api_timeout()
            assert result == 60

    def test_non_integer_exits(self):
        """REQ-070: Non-integer causes exit(1)."""
        with mock.patch.dict(os.environ, {"API_TIMEOUT_SECONDS": "abc"}, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                _validate_api_timeout()
            assert exc_info.value.code == 1

    def test_zero_exits(self):
        """REQ-070: Zero causes exit(1)."""
        with mock.patch.dict(os.environ, {"API_TIMEOUT_SECONDS": "0"}, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                _validate_api_timeout()
            assert exc_info.value.code == 1

    def test_negative_exits(self):
        """REQ-070: Negative causes exit(1)."""
        with mock.patch.dict(os.environ, {"API_TIMEOUT_SECONDS": "-5"}, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                _validate_api_timeout()
            assert exc_info.value.code == 1


class TestValidateMaxLogLines:
    """Tests for MAX_LOG_LINES validation."""

    def test_default_200(self):
        """REQ-071: Default is 200."""
        with mock.patch.dict(os.environ, {}, clear=True):
            result = _validate_max_log_lines()
            assert result == 200

    def test_valid_integer(self):
        """REQ-071: Valid positive integer is accepted."""
        with mock.patch.dict(os.environ, {"MAX_LOG_LINES": "500"}, clear=True):
            result = _validate_max_log_lines()
            assert result == 500

    def test_clamped_to_1000(self, capsys):
        """REQ-071: Values above 1000 are clamped with warning."""
        with mock.patch.dict(os.environ, {"MAX_LOG_LINES": "2000"}, clear=True):
            result = _validate_max_log_lines()
            assert result == 1000
            captured = capsys.readouterr()
            assert "WARNING" in captured.err
            assert "clamped" in captured.err

    def test_non_integer_exits(self):
        """REQ-071: Non-integer causes exit(1)."""
        with mock.patch.dict(os.environ, {"MAX_LOG_LINES": "abc"}, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                _validate_max_log_lines()
            assert exc_info.value.code == 1

    def test_zero_exits(self):
        """REQ-071: Zero causes exit(1)."""
        with mock.patch.dict(os.environ, {"MAX_LOG_LINES": "0"}, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                _validate_max_log_lines()
            assert exc_info.value.code == 1


class TestValidateEnv:
    """Tests for the main validate_env function."""

    def test_returns_server_config(self):
        """validate_env returns a ServerConfig with all fields."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"test")
            path = f.name
        try:
            with mock.patch.dict(
                os.environ,
                {
                    "KUBECONFIG": path,
                    "ALLOWED_NAMESPACES": "staging,prod",
                    "LOG_LEVEL": "DEBUG",
                    "API_TIMEOUT_SECONDS": "45",
                    "MAX_LOG_LINES": "300",
                },
                clear=True,
            ):
                config = validate_env()
                assert isinstance(config, ServerConfig)
                assert config.kubeconfig_path == path
                assert config.allowed_namespaces == frozenset({"staging", "prod"})
                assert config.log_level == "DEBUG"
                assert config.api_timeout_seconds == 45
                assert config.max_log_lines == 300
        finally:
            os.unlink(path)

    def test_config_is_frozen(self):
        """ServerConfig is immutable."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"test")
            path = f.name
        try:
            with mock.patch.dict(
                os.environ,
                {
                    "KUBECONFIG": path,
                    "ALLOWED_NAMESPACES": "staging",
                },
                clear=True,
            ):
                config = validate_env()
                with pytest.raises(Exception):
                    config.kubeconfig_path = "/other/path"
        finally:
            os.unlink(path)
