"""Session-token store for SSO-based (non-PAT) authentication.

Wiki.js can be used as a regular user instead of an admin personal access
token: the browser `jwt` cookie obtained after an SSO/Authentik login is sent
as `Authorization: Bearer <jwt>` on GraphQL/multipart requests.

This module owns the on-disk token store. Resolution precedence:

1. ``WIKIJS_SESSION_TOKEN`` environment variable.
2. The token file (default ``~/.config/wikijs-mcp/session-token``).

The token file is written with ``chmod 600`` and is never committed to a
repository (it lives outside the project tree). A dedicated environment
variable ``WIKIJS_SESSION_TOKEN_FILE`` overrides the file path — used by tests
and by users who want to relocate the file.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

TOKEN_FILE_ENV = "WIKIJS_SESSION_TOKEN_FILE"
CONFIG_DIR = Path.home() / ".config" / "wikijs-mcp"
DEFAULT_TOKEN_FILE = CONFIG_DIR / "session-token"


def token_path() -> Path:
    """Return the session-token file path.

    ``WIKIJS_SESSION_TOKEN_FILE`` overrides the platform default so tests can
    run against a temporary location without touching ``$HOME``.
    """
    override = os.getenv(TOKEN_FILE_ENV)
    if override:
        return Path(override).expanduser()
    return DEFAULT_TOKEN_FILE


def read_session_token(path: Path | None = None) -> str | None:
    """Read the session token from disk, or ``None`` when absent/empty."""
    target = path or token_path()
    try:
        token = target.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return None
    return token or None


def write_session_token(token: str, path: Path | None = None) -> None:
    """Persist a session token with ``chmod 600``.

    The parent config directory is created (``0700``) on demand. Any existing
    token is overwritten atomically-ish (write then chmod).
    """
    if not token or not token.strip():
        raise ValueError("Refusing to write an empty session token.")
    target = path or token_path()
    created = not target.parent.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    if created:
        try:
            os.chmod(target.parent, 0o700)
        except OSError:  # pragma: no cover - defensive
            pass
    target.write_text(token.strip(), encoding="utf-8")
    os.chmod(target, 0o600)


def resolve_session_token(env_token: str | None = None) -> str:
    """Resolve the effective session token: env var first, token file second.

    Raises:
        ValueError: When neither source provides a token (with instructions on
            how to obtain one).
    """
    token = (env_token or "").strip()
    if token:
        return token
    token = (read_session_token() or "").strip()
    if token:
        return token
    raise ValueError(
        "Session authentication is enabled (WIKIJS_AUTH_MODE=session) but no "
        "session token is available. Run 'wikijs-mcp login' to obtain a JWT "
        f"(stored at {token_path()}), or set the WIKIJS_SESSION_TOKEN "
        "environment variable."
    )


def _b64decode_segment(segment: str) -> bytes | None:
    """Decode a single base64url JWT segment (padding tolerant)."""
    padding = "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(segment + padding)
    except (ValueError, TypeError):  # pragma: no cover - malformed input
        return None


def jwt_claims(token: str) -> dict | None:
    """Decode the payload claims of a JWT (no signature verification).

    Verification is intentionally skipped: the token is only used to display
    expiry information in `wikijs-mcp session-status`. Malformed tokens return
    ``None``.
    """
    try:
        payload_b64 = token.split(".")[1]
    except (IndexError, AttributeError):
        return None
    payload = _b64decode_segment(payload_b64)
    if payload is None:
        return None
    try:
        claims = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return claims if isinstance(claims, dict) else None


def jwt_expiry(token: str) -> float | None:
    """Return the JWT ``exp`` claim as a Unix timestamp, or ``None``."""
    claims = jwt_claims(token)
    if not claims:
        return None
    exp = claims.get("exp")
    return float(exp) if isinstance(exp, (int, float)) else None
