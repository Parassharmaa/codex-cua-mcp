"""Unit tests for cua_mcp — stdlib + pytest only, no Codex install required."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from cua_mcp import (
    CUA_TOOL_SENTINELS,
    DEFAULT_CODEX_BIN,
    InvalidParamsError,
    ProtocolError,
    StdioJsonRpcChannel,
    find_cua_server,
    resolve_codex_bin,
    unwrap_result,
)


def make_channel(stdin_bytes: bytes = b"") -> StdioJsonRpcChannel:
    ch = StdioJsonRpcChannel(debug=False)
    ch._stdin = io.BytesIO(stdin_bytes)
    ch._stdout = io.BytesIO()
    return ch


class TestNdjsonFraming:
    def test_read_single_message(self):
        payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        ch = make_channel(json.dumps(payload).encode() + b"\n")
        assert ch.read_message() == payload

    def test_read_skips_blank_lines(self):
        payload = {"jsonrpc": "2.0", "id": 2, "method": "ping"}
        ch = make_channel(b"\n\n  \n" + json.dumps(payload).encode() + b"\n")
        assert ch.read_message() == payload

    def test_read_returns_none_on_eof(self):
        assert make_channel(b"").read_message() is None

    def test_write_emits_ndjson_not_content_length(self):
        ch = make_channel()
        ch.write_message({"jsonrpc": "2.0", "id": 1, "result": {}})
        written = ch._stdout.getvalue()
        assert written.endswith(b"\n")
        assert b"Content-Length" not in written
        assert json.loads(written.rstrip(b"\n")) == {"jsonrpc": "2.0", "id": 1, "result": {}}

    def test_roundtrip_multiple_messages(self):
        m1 = {"jsonrpc": "2.0", "id": 1, "method": "a"}
        m2 = {"jsonrpc": "2.0", "id": 2, "method": "b"}
        ch = make_channel(json.dumps(m1).encode() + b"\n" + json.dumps(m2).encode() + b"\n")
        assert ch.read_message() == m1
        assert ch.read_message() == m2
        assert ch.read_message() is None


class TestFindCuaServer:
    def test_picks_server_with_all_sentinels(self):
        status = {
            "data": [
                {"name": "other", "tools": {"foo": {}, "bar": {}}},
                {
                    "name": "cua",
                    "tools": {name: {"name": name} for name in CUA_TOOL_SENTINELS | {"extra"}},
                },
            ]
        }
        name, tools = find_cua_server(status)
        assert name == "cua"
        assert "extra" in tools

    def test_raises_when_no_cua_server(self):
        status = {"data": [{"name": "only-this", "tools": {"foo": {}}}]}
        with pytest.raises(ProtocolError, match="Could not find"):
            find_cua_server(status)


class TestUnwrapResult:
    def test_returns_result(self):
        assert unwrap_result({"id": 1, "result": {"x": 1}}) == {"x": 1}

    def test_raises_on_error(self):
        with pytest.raises(ProtocolError):
            unwrap_result({"id": 1, "error": {"code": -1, "message": "boom"}})

    def test_raises_on_missing_result(self):
        with pytest.raises(ProtocolError, match="did not include a result"):
            unwrap_result({"id": 1})


class TestErrorCodes:
    def test_protocol_error_default_code(self):
        assert ProtocolError("x").code == -32000

    def test_invalid_params_uses_jsonrpc_invalid_params_code(self):
        err = InvalidParamsError("bad")
        assert err.code == -32602
        assert str(err) == "bad"


class TestResolveCodexBin:
    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("CODEX_BIN", "/tmp/my-codex")
        assert resolve_codex_bin() == Path("/tmp/my-codex")

    def test_expands_user_in_env(self, monkeypatch):
        monkeypatch.setenv("CODEX_BIN", "~/mycodex")
        assert resolve_codex_bin() == Path.home() / "mycodex"

    def test_default_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("CODEX_BIN", raising=False)
        assert resolve_codex_bin() == DEFAULT_CODEX_BIN
