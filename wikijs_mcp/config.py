"""Configuration management for WikiJS MCP Server."""

import os
import re

from pydantic import BaseModel, Field, field_validator

from . import session as session_store

VALID_AUTH_MODES = ("apikey", "session")


def normalize_url(url: str) -> str:
    """Trim whitespace and prepend ``https://`` when the URL has no scheme.

    Users routinely pass a bare hostname such as ``wiki.example.com``. Without
    a scheme, ``webbrowser.open`` and the HTTP client treat the value as a
    local path instead of an URL — the default browser then shows a blank
    page. Explicit ``http://``/``https://`` (or ``localhost:8080``) values are
    kept unchanged.
    """
    url = (url or "").strip()
    if not url:
        return ""
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", url):
        return url
    return f"https://{url}"


class WikiJSConfig(BaseModel):
    """Configuration for Wiki.js connection."""

    url: str = Field(default="")
    api_key: str = Field(default="")
    graphql_endpoint: str = Field(default="/graphql")
    debug: bool = Field(default=False)
    # Optional override for the locale used by default for page operations.
    # Takes precedence over the site's primary locale. See client._resolve_locale.
    default_locale: str | None = Field(default=None)
    # Authentication mode:
    #   "apikey"  - admin personal access token (WIKIJS_API_KEY), the default.
    #   "session" - regular user via SSO/Authentik login; the browser `jwt`
    #               cookie is sent as Authorization: Bearer. The token comes
    #               from WIKIJS_SESSION_TOKEN (env) or the token file (see
    #               wikijs_mcp.session), env var taking precedence.
    auth_mode: str = Field(default="apikey")
    session_token: str | None = Field(default=None)

    @field_validator("url")
    @classmethod
    def _normalize_url(cls, value: str) -> str:
        return normalize_url(value)

    @field_validator("auth_mode")
    @classmethod
    def _normalize_auth_mode(cls, value: str) -> str:
        mode = (value or "apikey").strip().lower()
        if mode not in VALID_AUTH_MODES:
            raise ValueError(
                f"Invalid auth mode {value!r}. Must be one of: {', '.join(VALID_AUTH_MODES)}"
            )
        return mode

    @classmethod
    def load_config(cls) -> "WikiJSConfig":
        """Load configuration from environment variables."""
        return cls(
            url=os.getenv("WIKIJS_URL", ""),
            api_key=os.getenv("WIKIJS_API_KEY", ""),
            graphql_endpoint=os.getenv("WIKIJS_GRAPHQL_ENDPOINT", "/graphql"),
            debug=os.getenv("DEBUG", "false").lower() == "true",
            default_locale=os.getenv("WIKIJS_DEFAULT_LOCALE") or None,
            auth_mode=os.getenv("WIKIJS_AUTH_MODE", "apikey"),
            session_token=os.getenv("WIKIJS_SESSION_TOKEN") or None,
        )

    @property
    def graphql_url(self) -> str:
        """Get the full GraphQL endpoint URL."""
        return f"{self.url.rstrip('/')}{self.graphql_endpoint}"

    @property
    def headers(self) -> dict[str, str]:
        """Get authentication headers for API requests.

        ``apikey`` mode: the admin PAT. For ``session`` mode the client
        resolves the token dynamically (env var > token file) and reacts to
        ``new-jwt`` renewals, so this static property is only used in apikey
        mode; clients should prefer ``WikiJSClient._auth_headers()``.
        """
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def validate_config(self) -> None:
        """Validate that required configuration is present."""
        if not self.url:
            raise ValueError("WIKIJS_URL environment variable must be set.")
        if self.auth_mode == "session":
            # Precedence: WIKIJS_SESSION_TOKEN env var > token file.
            session_store.resolve_session_token(self.session_token)
            return
        if not self.api_key:
            raise ValueError("WIKIJS_API_KEY environment variable must be set.")
