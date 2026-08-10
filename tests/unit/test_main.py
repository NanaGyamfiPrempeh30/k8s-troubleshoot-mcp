"""Unit tests for the __main__ entry point."""

from __future__ import annotations

import os
from unittest import mock

import pytest

from k8s_troubleshoot_mcp.__main__ import main


class TestReq002aMalformedKubeconfig:
    """REQ-002a: a malformed kubeconfig is a diagnosis, not a traceback.

    Found by running the packaged container: REQ-002 covers only "missing or
    unreadable", so a file that exists but is malformed took an unhandled path
    and printed nine frames ending in ConfigException.
    """

    SECRET = "eyJhbGciOiJTVVBFUlNFQ1JFVFRPS0VOIn0.PAYLOAD.SIGNATURE"

    MALFORMED = {
        "valid_yaml_missing_current_context":
            "apiVersion: v1\nkind: Config\nusers:\n- name: u\n  user:\n    token: {tok}\n",
        "yaml_with_a_tab":
            "apiVersion: v1\nusers:\n\t- token: {tok}\n",
        "parse_error_on_the_token_line":
            "apiVersion: v1\nkind: Config\nusers:\n- name: u\n  user:\n    token: {tok}: bad\n",
        "empty_file":
            "",
        "binary_garbage":
            "\x00\x01\x02 {tok}\n",
    }

    def _run(self, tmp_path, content, capsys):
        path = tmp_path / "kubeconfig.yaml"
        path.write_text(content.format(tok=self.SECRET))
        env = {
            "KUBECONFIG": str(path),
            "ALLOWED_NAMESPACES": "default",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            with pytest.raises(SystemExit) as exc:
                main()
        return exc.value.code, capsys.readouterr()

    @pytest.mark.parametrize("label", sorted(MALFORMED))
    def test_exits_1_without_a_traceback(self, tmp_path, capsys, label):
        code, captured = self._run(tmp_path, self.MALFORMED[label], capsys)

        assert code == 1
        assert "Traceback" not in captured.err
        assert "  File \"" not in captured.err, "internal frames leaked to stderr"
        assert "is not a valid kubeconfig" in captured.err

    @pytest.mark.parametrize("label", sorted(MALFORMED))
    def test_message_never_quotes_the_file(self, tmp_path, capsys, label):
        """The reason may name the path, line and column — never the contents.

        A kubeconfig carries a cluster bearer token. PyYAML omits the source
        snippet when reading from a file handle, which is what makes including
        the exception text safe; this pins that behaviour so a library change
        that starts quoting lines fails here instead of leaking a token.
        """
        _, captured = self._run(tmp_path, self.MALFORMED[label], capsys)

        assert self.SECRET not in captured.err
        assert "SUPERSECRET" not in captured.err
        assert "SVVBFUl" not in captured.err

    @pytest.mark.parametrize("label", sorted(MALFORMED))
    def test_nothing_is_written_to_stdout(self, tmp_path, capsys, label):
        """REQ-010: stdout carries JSON-RPC; a startup error must not touch it."""
        _, captured = self._run(tmp_path, self.MALFORMED[label], capsys)

        assert captured.out == ""

    def test_message_names_the_path_and_the_reason(self, tmp_path, capsys):
        _, captured = self._run(
            tmp_path, self.MALFORMED["valid_yaml_missing_current_context"], capsys
        )

        assert str(tmp_path) in captured.err
        assert "ConfigException" in captured.err

    @pytest.mark.parametrize("label", sorted(MALFORMED))
    def test_message_is_a_single_line(self, tmp_path, capsys, label):
        """PyYAML embeds a newline before its "in <file>, line N" clause, so
        the reason is collapsed; REQ-002a specifies one line."""
        _, captured = self._run(tmp_path, self.MALFORMED[label], capsys)

        assert captured.err.count("\n") == 1, repr(captured.err)

    def test_a_valid_kubeconfig_still_reaches_create_app(self, tmp_path):
        """The guard must not swallow a working startup."""
        path = tmp_path / "kubeconfig.yaml"
        path.write_text(
            "apiVersion: v1\nkind: Config\n"
            "clusters:\n- name: c\n  cluster:\n    server: https://127.0.0.1:1\n"
            "users:\n- name: u\n  user:\n    token: t\n"
            "contexts:\n- name: c\n  context:\n    cluster: c\n    user: u\n"
            "current-context: c\n"
        )
        env = {"KUBECONFIG": str(path), "ALLOWED_NAMESPACES": "default"}

        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch("k8s_troubleshoot_mcp.__main__.create_app") as create:
                main()

        create.assert_called_once()
        create.return_value.run.assert_called_once_with(transport="stdio")
