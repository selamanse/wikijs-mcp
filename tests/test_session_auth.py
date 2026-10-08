"""Tests for session authentication mode (SSO / Authentik login)."""

import base64
import json
import logging
import time
from unittest.mock import AsyncMock, Mock, patch

import pytest
from pydantic import ValidationError

from wikijs_mcp import session as token_store
from wikijs_mcp.client import WikiJSClient
from wikijs_mcp.config import WikiJSConfig
from wikijs_mcp.server import WikiJSMCPServer


def _make_jwt(exp_offset: int = 3600, **claims: object) -> str:
    """Build an unsigned JWT with a future ``exp`` claim (informational only)."""
    header = {"alg": "none", "typ": "JWT"}
    payload = {
        "sub": "session-user",
        "exp": int(time.time()) + exp_offset,
        **claims,
    }

    def _b64(part: dict) -> str:
        raw = json.dumps(part).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{_b64(header)}.{_b64(payload)}.signature"


def get_tool_response_text(result):
    """Extract text from an MCP tool response (old + new formats)."""
    if isinstance(result, tuple):
        content, _ = result
        return content[0].text
    return result[0].text


# ----------------------------------------------------------------------
# Mode resolution & config
# ----------------------------------------------------------------------


@pytest.mark.unit
class TestSessionAuthModeResolution:
    """Auth-mode resolution from config / environment."""

    def test_default_auth_mode_is_apikey(self):
        config = WikiJSConfig()
        assert config.auth_mode == "apikey"

    def test_load_config_apikey_default(self):
        config = WikiJSConfig.load_config()
        assert config.auth_mode == "apikey"
        assert config.session_token is None

    def test_load_config_session_mode(self, monkeypatch):
        monkeypatch.setenv("WIKIJS_AUTH_MODE", "session")
        monkeypatch.setenv("WIKIJS_SESSION_TOKEN", "jwt-from-env")

        config = WikiJSConfig.load_config()

        assert config.auth_mode == "session"
        assert config.session_token == "jwt-from-env"

    def test_invalid_auth_mode_rejected(self):
        with pytest.raises(ValidationError):
            WikiJSConfig(auth_mode="token")

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("session", "session"),
            (" SESSION ", "session"),
            ("apikey", "apikey"),
            ("", "apikey"),
        ],
    )
    def test_auth_mode_normalized(self, raw, expected):
        assert WikiJSConfig(auth_mode=raw).auth_mode == expected

    def test_validate_session_with_env_token_ok(self):
        config = WikiJSConfig(
            url="https://wiki.example.com", auth_mode="session", session_token="jwt"
        )
        config.validate_config()  # must pass without api_key

    def test_validate_session_with_token_file_only(self, tmp_path, monkeypatch):
        target = tmp_path / "session-token"
        monkeypatch.setenv("WIKIJS_SESSION_TOKEN_FILE", str(target))
        target.write_text("file-jwt")

        config = WikiJSConfig(url="https://wiki.example.com", auth_mode="session")
        config.validate_config()

    def test_validate_session_without_token_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WIKIJS_SESSION_TOKEN_FILE", str(tmp_path / "missing"))

        config = WikiJSConfig(url="https://wiki.example.com", auth_mode="session")
        with pytest.raises(ValueError, match="wikijs-mcp login"):
            config.validate_config()

    def test_apikey_mode_still_requires_api_key_even_with_session_token(self):
        config = WikiJSConfig(
            url="https://wiki.example.com", auth_mode="apikey", session_token="jwt"
        )
        with pytest.raises(ValueError, match="WIKIJS_API_KEY"):
            config.validate_config()


# ----------------------------------------------------------------------
# Token store
# ----------------------------------------------------------------------


@pytest.mark.unit
class TestSessionTokenStore:
    """Token-file helpers (write, read, precedence, JWT inspection)."""

    def test_write_token_roundtrip_and_permissions(self, tmp_path, monkeypatch):
        target = tmp_path / "cfg" / "session-token"
        monkeypatch.setenv("WIKIJS_SESSION_TOKEN_FILE", str(target))

        token_store.write_session_token("abc.def.ghi")

        assert token_store.read_session_token() == "abc.def.ghi"
        assert (target.stat().st_mode & 0o777) == 0o600

    def test_write_empty_token_rejected(self):
        with pytest.raises(ValueError):
            token_store.write_session_token("   ")

    def test_read_missing_token_file_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WIKIJS_SESSION_TOKEN_FILE", str(tmp_path / "missing"))
        assert token_store.read_session_token() is None

    def test_resolve_precedence_env_over_file(self, tmp_path, monkeypatch):
        target = tmp_path / "session-token"
        monkeypatch.setenv("WIKIJS_SESSION_TOKEN_FILE", str(target))
        target.write_text("file-token")

        assert token_store.resolve_session_token("env-token") == "env-token"
        assert token_store.resolve_session_token(None) == "file-token"

    def test_resolve_without_any_token_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WIKIJS_SESSION_TOKEN_FILE", str(tmp_path / "missing"))
        with pytest.raises(ValueError, match="wikijs-mcp login"):
            token_store.resolve_session_token(None)

    def test_jwt_expiry_decoded(self):
        token = _make_jwt(exp_offset=3600)
        exp = token_store.jwt_expiry(token)
        assert exp is not None
        assert abs(exp - (time.time() + 3600)) < 5

    def test_jwt_expiry_malformed(self):
        assert token_store.jwt_expiry("not-a-jwt") is None
        assert token_store.jwt_expiry("a.b") is None


# ----------------------------------------------------------------------
# Client header construction & renewal
# ----------------------------------------------------------------------


@pytest.mark.unit
class TestSessionAuthClientHeaders:
    """Bearer-token abstraction on the WikiJS client."""

    def test_session_header_from_env_token(self):
        config = WikiJSConfig(
            url="https://wiki.example.com",
            auth_mode="session",
            session_token="jwt-env",
        )
        client = WikiJSClient(config)
        assert client._auth_headers()["Authorization"] == "Bearer jwt-env"

    def test_session_header_from_token_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WIKIJS_SESSION_TOKEN_FILE", str(tmp_path / "session-token"))
        (tmp_path / "session-token").write_text("jwt-file")

        config = WikiJSConfig(url="https://wiki.example.com", auth_mode="session")
        client = WikiJSClient(config)
        assert client._auth_headers()["Authorization"] == "Bearer jwt-file"

    def test_apikey_header_unchanged(self):
        config = WikiJSConfig(url="https://wiki.example.com", api_key="test-api-key-123")
        client = WikiJSClient(config)
        headers = client._auth_headers()
        assert headers["Authorization"] == "Bearer test-api-key-123"
        assert headers["Content-Type"] == "application/json"

    def test_session_without_token_raises_descriptive(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WIKIJS_SESSION_TOKEN_FILE", str(tmp_path / "missing"))
        config = WikiJSConfig(url="https://wiki.example.com", auth_mode="session")
        client = WikiJSClient(config)
        with pytest.raises(ValueError, match="wikijs-mcp login"):
            client._auth_headers()

    async def test_execute_query_sends_session_header(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WIKIJS_SESSION_TOKEN_FILE", str(tmp_path / "session-token"))
        (tmp_path / "session-token").write_text("jwt-file")

        config = WikiJSConfig(url="https://wiki.example.com", auth_mode="session")
        client = WikiJSClient(config)

        mock_response = Mock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"data": {}}
        mock_response.headers = {}
        client.client.post = AsyncMock(return_value=mock_response)

        await client._execute_query("query { test }")

        call = client.client.post.call_args
        assert call.kwargs["headers"]["Authorization"] == "Bearer jwt-file"


class TestSessionAuthRenewal:
    """Reactive renewal via the ``new-jwt`` response header."""

    async def test_new_jwt_renewal_updates_token_and_persists(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("WIKIJS_SESSION_TOKEN_FILE", str(tmp_path / "session-token"))
        target = tmp_path / "session-token"
        target.write_text("old-jwt")

        config = WikiJSConfig(url="https://wiki.example.com", auth_mode="session")
        client = WikiJSClient(config)
        assert client._auth_headers()["Authorization"] == "Bearer old-jwt"

        mock_response = Mock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"data": {}}
        mock_response.headers = {"new-jwt": "fresh-jwt"}
        client.client.post = AsyncMock(return_value=mock_response)

        await client._execute_query("query { test }")

        assert client._session_token == "fresh-jwt"
        assert target.read_text() == "fresh-jwt"
        assert client._auth_headers()["Authorization"] == "Bearer fresh-jwt"

    async def test_new_jwt_ignored_in_apikey_mode(self, tmp_path, monkeypatch):
        target = tmp_path / "session-token"
        monkeypatch.setenv("WIKIJS_SESSION_TOKEN_FILE", str(target))

        config = WikiJSConfig(url="https://wiki.example.com", api_key="test-api-key-123")
        client = WikiJSClient(config)

        mock_response = Mock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"data": {}}
        mock_response.headers = {"new-jwt": "fresh-jwt"}
        client.client.post = AsyncMock(return_value=mock_response)

        await client._execute_query("query { test }")

        assert client._auth_headers()["Authorization"] == "Bearer test-api-key-123"
        assert not target.exists()

    async def test_renewal_write_failure_is_tolerated(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setenv("WIKIJS_SESSION_TOKEN_FILE", str(tmp_path / "session-token"))
        (tmp_path / "session-token").write_text("old-jwt")

        config = WikiJSConfig(url="https://wiki.example.com", auth_mode="session")
        client = WikiJSClient(config)

        mock_response = Mock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"data": {}}
        mock_response.headers = {"new-jwt": "fresh-jwt"}
        client.client.post = AsyncMock(return_value=mock_response)

        with patch.object(
            token_store, "write_session_token", side_effect=OSError("disk full")
        ):
            with caplog.at_level(logging.WARNING, logger="wikijs_mcp.client"):
                await client._execute_query("query { test }")

        assert client._session_token == "fresh-jwt"
        assert "could not persist" in caplog.text

    async def test_no_new_jwt_header_leaves_token_unchanged(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WIKIJS_SESSION_TOKEN_FILE", str(tmp_path / "session-token"))
        target = tmp_path / "session-token"
        target.write_text("old-jwt")

        config = WikiJSConfig(url="https://wiki.example.com", auth_mode="session")
        client = WikiJSClient(config)

        mock_response = Mock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"data": {}}
        mock_response.headers = {}
        client.client.post = AsyncMock(return_value=mock_response)

        await client._execute_query("query { test }")

        assert client._session_token == "old-jwt"
        assert target.read_text() == "old-jwt"


# ----------------------------------------------------------------------
# Login command (standard browser + manual cookie paste)
# ----------------------------------------------------------------------


@pytest.mark.unit
class TestSessionAuthLoginCommand:
    """``wikijs-mcp login`` opens the standard browser and stores the pasted JWT."""

    def test_run_login_no_url_returns_error(self, capsys):
        from wikijs_mcp import login

        rc = login.run_login([])

        assert rc == 2
        assert "WIKIJS_URL" in capsys.readouterr().err

    def test_run_login_stores_pasted_token(self, tmp_path, monkeypatch, capsys):
        from wikijs_mcp import login

        monkeypatch.setenv("WIKIJS_URL", "https://wiki.example.com")
        monkeypatch.setenv("WIKIJS_SESSION_TOKEN_FILE", str(tmp_path / "session-token"))
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr(login.webbrowser, "open", lambda url: True)
        monkeypatch.setattr(login.getpass, "getpass", lambda prompt: "jwt.abc.def")

        rc = login.run_login([])

        assert rc == 0
        target = tmp_path / "session-token"
        assert target.read_text() == "jwt.abc.def"
        assert (target.stat().st_mode & 0o777) == 0o600
        # The login page is opened in the standard browser.
        assert "wiki.example.com/login" in capsys.readouterr().out

    def test_run_login_reads_piped_input(self, tmp_path, monkeypatch):
        from wikijs_mcp import login

        monkeypatch.setenv("WIKIJS_SESSION_TOKEN_FILE", str(tmp_path / "session-token"))
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        monkeypatch.setattr(
            "sys.stdin",
            type(
                "FakeStdin",
                (),
                {
                    "read": staticmethod(lambda: "pipe.jwt.xyz\n"),
                    "isatty": staticmethod(lambda: False),
                },
            )(),
        )

        rc = login.run_login(["--url", "https://wiki.example.com"])

        assert rc == 0
        assert (tmp_path / "session-token").read_text() == "pipe.jwt.xyz"

    def test_run_login_empty_token_errors(self, tmp_path, monkeypatch):
        from wikijs_mcp import login

        monkeypatch.setenv("WIKIJS_URL", "https://wiki.example.com")
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr(login.webbrowser, "open", lambda url: True)
        monkeypatch.setattr(login.getpass, "getpass", lambda prompt: "   ")

        rc = login.run_login([])

        assert rc == 2
        assert not (tmp_path / "session-token").exists()


# ----------------------------------------------------------------------
# Server integration in session mode
# ----------------------------------------------------------------------


@pytest.mark.integration
class TestSessionAuthServer:
    """The MCP server works with a session-mode config (no admin PAT)."""

    @patch("wikijs_mcp.server.WikiJSConfig.load_config")
    def test_server_accepts_session_config(self, mock_load_config):
        mock_load_config.return_value = WikiJSConfig(
            url="https://wiki.example.com",
            auth_mode="session",
            session_token="jwt-token",
        )
        server = WikiJSMCPServer()
        assert server.config.auth_mode == "session"
        server.config.validate_config()  # must not require WIKIJS_API_KEY

    @patch("wikijs_mcp.server.WikiJSConfig.load_config")
    @patch("wikijs_mcp.server.WikiJSClient")
    async def test_server_tool_call_in_session_mode(
        self, mock_client_class, mock_load_config
    ):
        mock_load_config.return_value = WikiJSConfig(
            url="https://wiki.example.com",
            auth_mode="session",
            session_token="jwt-token",
        )
        mock_client_instance = AsyncMock()
        mock_client_instance.__aenter__.return_value = mock_client_instance
        mock_client_instance.__aexit__.return_value = None
        mock_client_instance.get_site_info.return_value = {
            "title": "Test Wiki",
            "description": "A session-authenticated wiki",
            "host": "https://wiki.example.com",
        }
        mock_client_class.return_value = mock_client_instance

        server = WikiJSMCPServer()
        result = await server.app.call_tool("wiki_get_site_info", {})

        text = get_tool_response_text(result)
        assert "Test Wiki" in text
        assert "A session-authenticated wiki" in text
        # The client was constructed with the session-mode config.
        mock_client_class.assert_called_once()
        assert mock_client_class.call_args.args[0].auth_mode == "session"
