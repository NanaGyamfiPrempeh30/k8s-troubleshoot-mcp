"""Unit tests for config module."""

from __future__ import annotations

import io
import logging
import os
import re
import tempfile
from pathlib import Path
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

REPO_ROOT = Path(__file__).resolve().parents[2]


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

    def test_system_namespaces_removed(self, capsys, caplog):
        """REQ-008: kube-system and kube-public are removed with warning.

        The warning goes through the logging framework, so it is read from
        caplog rather than stderr. stdout is still asserted empty: REQ-010
        makes a single stray stdout line a protocol-corrupting bug.
        """
        with mock.patch.dict(
            os.environ,
            {"ALLOWED_NAMESPACES": "staging,kube-system,kube-public,prod"},
            clear=True,
        ):
            with caplog.at_level(logging.WARNING):
                result = _validate_allowed_namespaces()

            assert "kube-system" not in result
            assert "kube-public" not in result
            assert "staging" in result
            assert "prod" in result
            assert "WARNING" in caplog.text
            assert capsys.readouterr().out == ""

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

    def test_clamped_to_1000(self, capsys, caplog):
        """REQ-071: Values above 1000 are clamped with warning.

        The warning goes through the logging framework, so it is read from
        caplog rather than stderr. stdout is still asserted empty per REQ-010.
        """
        with mock.patch.dict(os.environ, {"MAX_LOG_LINES": "2000"}, clear=True):
            with caplog.at_level(logging.WARNING):
                result = _validate_max_log_lines()

            assert result == 1000
            assert "WARNING" in caplog.text
            assert "clamped" in caplog.text
            assert capsys.readouterr().out == ""

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


# ---------------------------------------------------------------------------
# Exact-stderr-text contract (REQ-001, REQ-004)
#
# Both requirements specify a verbatim error message. Until this was added,
# nothing verified either one — only exit(1) was asserted — so the spec text and
# the emitted text could drift indefinitely. That is exactly what happened to
# REQ-001, whose message told operators to run `kubectl apply -f kubernetes/`;
# verified against a v1.35.1 API server with --dry-run=server, that command
# reports success while creating role.yaml in the wrong namespace and never
# reading rolebinding.yaml.template at all (kubectl reads only .yaml/.yml/.json
# from a directory).
#
# Each message is compared against requirements.md itself rather than a second
# hand-typed copy, mirroring test_server.py's REQ-063 check.
# ---------------------------------------------------------------------------


def _normalize(text: str) -> str:
    """Collapse whitespace so a Markdown line wrap is not a difference."""
    return re.sub(r"\s+", " ", text).strip()


def _spec_blockquote(req_id: str) -> str:
    """The verbatim message REQ `req_id` specifies, read from requirements.md."""
    spec_text = (REPO_ROOT / "requirements.md").read_text(encoding="utf-8")
    # [^\n] rather than . so the capture cannot run past the blockquote into
    # the surrounding prose.
    block = re.search(rf"\*\*{req_id}:\*\*[\s\S]*?\n\n((?:>[^\n]*\n)+)", spec_text)
    assert block, f"{req_id} blockquote not found in requirements.md"
    quoted = " ".join(
        line.lstrip("> ").strip() for line in block.group(1).splitlines()
    )
    return _normalize(quoted.strip().strip("`").strip('"'))


class _ExactMessageContract:
    """Four-test contract shared by every REQ that pins exact stderr text.

    Not named Test* so pytest does not collect it directly; subclasses supply
    `req_id`, the validator that emits the message, and the substrings the
    regression guard requires.
    """

    req_id: str = ""
    must_contain: tuple[str, ...] = ()

    #: Environment the message is triggered from. Applied with clear=True.
    env: dict[str, str] = {}

    #: False for REQ-008, whose warning is emitted without terminating startup.
    expects_exit: bool = True

    #: Placeholder -> literal, for messages the spec states as a template.
    #: REQ-005 interpolates the offending tokens, so its blockquote carries
    #: `<tokens>` and the test substitutes the value it deliberately induced.
    substitutions: dict[str, str] = {}

    @staticmethod
    def _run_validator() -> None:
        raise NotImplementedError

    @classmethod
    def _emitted(cls) -> str:
        """Everything the validator wrote, whether raw or through logging.

        REQ-001/004/005 use a raw stderr write (they precede any logging setup
        and then exit); REQ-008 goes through the module logger. Capturing both
        and concatenating means no subclass has to declare which mechanism it
        uses, and a message that switched mechanisms would still be caught.

        A handler is attached directly rather than using caplog because caplog
        is a fixture and this runs as a classmethod — and because under pytest
        the root logger already has handlers, so logging.lastResort (the
        production path here) never engages and would capture nothing.
        """
        captured = io.StringIO()
        handler = logging.StreamHandler(captured)
        target = logging.getLogger("k8s_troubleshoot_mcp.config")
        previous_level = target.level
        target.addHandler(handler)
        target.setLevel(logging.WARNING)
        try:
            with mock.patch.dict(os.environ, cls.env, clear=True):
                with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                    if cls.expects_exit:
                        with pytest.raises(SystemExit):
                            cls._run_validator()
                    else:
                        # REQ-008 warns and continues; requiring SystemExit
                        # here would assert the opposite of the specified
                        # behaviour.
                        cls._run_validator()
        finally:
            target.removeHandler(handler)
            target.setLevel(previous_level)

        return err.getvalue() + captured.getvalue()

    @classmethod
    def _spec(cls) -> str:
        text = _spec_blockquote(cls.req_id)
        for placeholder, value in cls.substitutions.items():
            assert placeholder in text, (
                f"{cls.req_id} blockquote no longer contains {placeholder!r}; "
                "the substitution would silently do nothing"
            )
            text = text.replace(placeholder, value)
        return text

    def test_emitted_message_matches_requirements_verbatim(self):
        """The contract itself: code and spec agree character for character."""
        assert _normalize(self._emitted()) == self._spec()

    def test_comparison_is_neither_vacuous_nor_brittle(self):
        """Proves the normalization has the sensitivity it claims.

        A semantic change to the spec text must be rejected — otherwise the
        verbatim test passes against anything and guards nothing. A pure
        whitespace reflow must be accepted — otherwise re-wrapping a Markdown
        blockquote breaks the build for no reason.
        """
        emitted = _normalize(self._emitted())

        words = self._spec().split()
        perturbed = " ".join(words[:-1] + ["ZZZ" + words[-1]])
        assert perturbed != self._spec(), "perturbation did not change the text"
        assert emitted != perturbed, "comparison accepts a semantically changed spec"

        reflowed = self._spec().replace(" ", "\n   ", 3)
        assert _normalize(reflowed) == self._spec()
        assert emitted == _normalize(reflowed)

    def test_spec_blockquote_is_actually_found(self):
        """If the regex silently stopped matching, every other test goes vacuous."""
        spec = self._spec()

        assert len(spec) > 80, f"{self.req_id} blockquote suspiciously short: {spec!r}"
        assert not spec.startswith(">"), "blockquote markers were not stripped"
        assert "\n" not in spec, "normalization did not collapse the line wraps"

    def test_regression_guard(self):
        """Wording-independent: the message must still say the essential thing."""
        message = self._emitted()

        for required in self.must_contain:
            assert required in message, (
                f"{self.req_id} message no longer contains {required!r}"
            )


class TestReq001MessageMatchesSpec(_ExactMessageContract):
    """KUBECONFIG-not-set message."""

    req_id = "REQ-001"
    must_contain = ("KUBECONFIG", "scripts/generate-kubeconfig.sh")

    @staticmethod
    def _run_validator() -> None:
        _validate_kubeconfig()

    def test_regression_guard(self):
        """The blanket kubectl apply may appear only as the thing NOT to do."""
        super().test_regression_guard()
        message = self._emitted()

        if "kubectl apply -f kubernetes/" in message:
            assert "Do not apply" in message, (
                "message names the blanket apply without warning against it"
            )


class TestReq004MessageMatchesSpec(_ExactMessageContract):
    """ALLOWED_NAMESPACES-not-set message."""

    req_id = "REQ-004"
    must_contain = ("ALLOWED_NAMESPACES", "comma-separated")

    @staticmethod
    def _run_validator() -> None:
        _validate_allowed_namespaces()

    def test_regression_guard(self):
        """The wildcard refusal must survive any rewording.

        REQ-004's message is the only place an operator is told, before they
        try it, that REQ-005 will reject `*`. Dropping that sentence turns a
        prevented mistake into a failed startup.
        """
        super().test_regression_guard()
        message = self._emitted()

        assert "*" in message
        assert "not accepted" in message.lower() or "not permitted" in message.lower()


class TestReq005MessageMatchesSpec(_ExactMessageContract):
    """Wildcard-rejection message.

    REQ-005 originally specified behaviour only — "an error message stating that
    wildcard namespace access is not permitted" — with no verbatim text, so this
    contract had nothing to compare against. The blockquote was added to the
    requirement as part of wiring this test up.

    Its message is the only templated one of the four: it interpolates the
    offending tokens, so the spec carries a `<tokens>` placeholder.
    """

    req_id = "REQ-005"
    env = {"ALLOWED_NAMESPACES": "*"}
    must_contain = ("ALLOWED_NAMESPACES", "not permitted")
    substitutions = {"<tokens>": "*"}

    @staticmethod
    def _run_validator() -> None:
        _validate_allowed_namespaces()

    def test_regression_guard(self):
        """The message must name what was rejected, not just that something was.

        An operator passing `staging,all,prod` needs to know it was `all`.
        """
        super().test_regression_guard()

        with mock.patch.dict(
            os.environ, {"ALLOWED_NAMESPACES": "staging,all,prod"}, clear=True
        ):
            with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                with pytest.raises(SystemExit):
                    _validate_allowed_namespaces()

        message = err.getvalue()
        assert "all" in message
        assert "staging" not in message, "message names namespaces that were fine"

    def test_both_wildcard_tokens_are_rejected(self):
        """`*` and `all` are both REQ-005 tokens; the message covers either."""
        for token in ("*", "all"):
            with mock.patch.dict(
                os.environ, {"ALLOWED_NAMESPACES": token}, clear=True
            ):
                with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                    with pytest.raises(SystemExit) as exc:
                        _validate_allowed_namespaces()

            assert exc.value.code == 1
            assert token in err.getvalue()


class TestReq008MessageMatchesSpec(_ExactMessageContract):
    """kube-system / kube-public removal warning.

    The only one of the four that does **not** exit: REQ-008 strips the system
    namespaces and lets startup continue, so asserting SystemExit here would
    assert the opposite of the specified behaviour.
    """

    req_id = "REQ-008"
    env = {"ALLOWED_NAMESPACES": "default,kube-system"}
    expects_exit = False
    must_contain = ("kube-system", "kube-public", "ALLOWED_NAMESPACES")

    @staticmethod
    def _run_validator() -> None:
        _validate_allowed_namespaces()

    def test_regression_guard(self):
        """The warning must say the namespaces were removed, not merely listed.

        REQ-008's silent-strip behaviour is invisible in the returned set; this
        line is the only signal an operator gets that what they asked for is not
        what they got.
        """
        super().test_regression_guard()

        assert "removed" in self._emitted().lower()

    def test_warning_accompanies_the_actual_strip(self, caplog):
        """The message must not be able to drift away from the behaviour."""
        with mock.patch.dict(
            os.environ,
            {"ALLOWED_NAMESPACES": "default,kube-system,kube-public"},
            clear=True,
        ):
            with caplog.at_level(logging.WARNING):
                result = _validate_allowed_namespaces()

        assert result == frozenset({"default"}), "system namespaces were not stripped"
        assert caplog.text, "namespaces were stripped without warning"

    def test_no_warning_when_nothing_was_stripped(self, caplog):
        """A warning on a clean value would train operators to ignore it."""
        with mock.patch.dict(
            os.environ, {"ALLOWED_NAMESPACES": "default,staging"}, clear=True
        ):
            with caplog.at_level(logging.WARNING):
                _validate_allowed_namespaces()

        assert caplog.text == ""

    def test_warning_is_logged_not_written_raw(self, caplog):
        """REQ-008 says "log a warning"; a raw stderr write bypasses LOG_LEVEL
        and the formatter, and is invisible to every logging-based consumer."""
        with mock.patch.dict(
            os.environ, {"ALLOWED_NAMESPACES": "default,kube-system"}, clear=True
        ):
            with caplog.at_level(logging.WARNING):
                _validate_allowed_namespaces()

        assert caplog.records, "message did not go through the logging framework"
        assert caplog.records[0].name == "k8s_troubleshoot_mcp.config"
        assert caplog.records[0].levelno == logging.WARNING


class TestReq071MessageMatchesSpec(_ExactMessageContract):
    """MAX_LOG_LINES clamp warning.

    The last raw `sys.stderr.write` outside `_fatal`. Like REQ-008 it warns and
    continues rather than exiting, and like REQ-005 its specified text is a
    template — here interpolating the offending value.
    """

    req_id = "REQ-071"
    env = {"MAX_LOG_LINES": "5000"}
    expects_exit = False
    must_contain = ("MAX_LOG_LINES", "clamped")
    substitutions = {"<value>": "5000"}

    @staticmethod
    def _run_validator() -> None:
        _validate_max_log_lines()

    def test_regression_guard(self):
        """The message must name the ceiling and the value that exceeded it."""
        super().test_regression_guard()
        message = self._emitted()

        assert "5000" in message, "message does not say what was rejected"
        assert "1000" in message, "message does not say what the ceiling is"

    def test_warning_is_logged_not_written_raw(self, caplog):
        """A raw stderr write bypasses the formatter and every logging consumer."""
        with mock.patch.dict(os.environ, {"MAX_LOG_LINES": "5000"}, clear=True):
            with caplog.at_level(logging.WARNING):
                result = _validate_max_log_lines()

        assert result == 1000, "value was not actually clamped"
        assert caplog.records, "message did not go through the logging framework"
        assert caplog.records[0].name == "k8s_troubleshoot_mcp.config"
        assert caplog.records[0].levelno == logging.WARNING

    def test_stdout_stays_empty(self, capsys, caplog):
        """REQ-010: one stray stdout line corrupts the JSON-RPC stream."""
        with mock.patch.dict(os.environ, {"MAX_LOG_LINES": "5000"}, clear=True):
            with caplog.at_level(logging.WARNING):
                _validate_max_log_lines()

        assert capsys.readouterr().out == ""

    def test_no_warning_at_or_below_the_ceiling(self, caplog):
        """Warning on a value that was honoured would train operators to ignore it."""
        for value in ("1000", "200"):
            caplog.clear()
            with mock.patch.dict(os.environ, {"MAX_LOG_LINES": value}, clear=True):
                with caplog.at_level(logging.WARNING):
                    result = _validate_max_log_lines()

            assert result == int(value)
            assert caplog.text == "", f"{value} is within the ceiling but warned"
