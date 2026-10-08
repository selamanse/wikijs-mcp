"""CLI commands for SSO / Authentik session authentication.

The MCP server can operate as a regular wiki user instead of an admin
personal access token: the browser ``jwt`` cookie set by Wiki.js after an
SSO/Authentik login is captured here and stored on disk (``chmod 600``). All
API requests then send it as ``Authorization: Bearer <jwt>``; Wiki.js hands
back refreshed tokens via the ``new-jwt`` response header, which the client
adopts and re-persists automatically.

Commands (wired into ``wikijs-mcp``):

- ``wikijs-mcp login``            open ``{url}/login`` in your standard
                                  browser, then paste the ``jwt`` cookie value
                                  from the browser devtools.
- ``wikijs-mcp session-status``   show auth mode, token source, masked token
                                  and expiry — without printing the token.

When stdin is *not* a terminal the token is read from stdin instead of being
prompted for (scripting / CI use)::

    echo '<jwt>' | wikijs-mcp login --url https://wiki.example.com
"""

from __future__ import annotations

import argparse
import getpass
import os
import stat
import sys
import webbrowser
from datetime import datetime

from . import session as session_store
from .config import WikiJSConfig


def _build_login_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wikijs-mcp login",
        description=(
            "Open the Wiki.js login page in your standard browser, then store "
            "the session JWT. After logging in (SSO/Authentik), copy the 'jwt' "
            "cookie value from the browser devtools and paste it. The token is "
            "stored with chmod 600 and refreshed automatically via the "
            "'new-jwt' response header."
        ),
    )
    parser.add_argument(
        "--url",
        default="",
        help="Wiki.js base URL (default: WIKIJS_URL environment variable).",
    )
    return parser


def _open_login_page(url: str) -> None:
    """Open the Wiki.js login page in the OS default browser."""
    login_url = f"{url.rstrip('/')}/login"
    print(f"Opening {login_url} in your standard browser ...")
    if not webbrowser.open(login_url):
        print(
            f"Could not open a browser automatically. Please open this URL "
            f"manually:\n  {login_url}",
            file=sys.stderr,
        )


def _read_pasted_token() -> str:
    """Read the ``jwt`` cookie value from the user (hidden prompt or stdin)."""
    if not sys.stdin.isatty():
        # Piped usage: read the token from stdin.
        return sys.stdin.read().strip()
    print(
        "Log in to the wiki (SSO/Authentik) in the browser, then copy the 'jwt'\n"
        "cookie value: devtools (F12) -> Application -> Cookies -> 'jwt'.\n"
        "Paste it below and press Enter (input is hidden)."
    )
    try:
        return getpass.getpass("jwt cookie: ").strip()
    except (EOFError, OSError):  # pragma: no cover - non-interactive edge case
        return ""


def run_login(argv: list[str] | None = None) -> int:
    """Run the ``wikijs-mcp login`` command. Returns an exit code."""
    args = _build_login_parser().parse_args(argv)

    url = (args.url or WikiJSConfig.load_config().url).strip().rstrip("/")
    if not url:
        print(
            "No Wiki.js URL configured: pass --url or set WIKIJS_URL.",
            file=sys.stderr,
        )
        return 2

    if sys.stdin.isatty():
        _open_login_page(url)

    token = _read_pasted_token()
    if not token:
        print("No token was provided; nothing stored.", file=sys.stderr)
        return 2

    try:
        session_store.write_session_token(token)
    except (ValueError, OSError) as exc:
        print(f"error: could not store the session token: {exc}", file=sys.stderr)
        return 2

    print(
        f"Session token stored at {session_store.token_path()} (chmod 600).\n"
        "The MCP client refreshes it automatically via the 'new-jwt' response "
        "header.\nRole hint: map the Authentik group (mapGroups) to a Wiki.js "
        "role granting read:pages, manage:pages, read:assets, write:assets, "
        "manage:assets."
    )
    exp = session_store.jwt_expiry(token)
    if exp is not None:
        dt = datetime.fromtimestamp(exp)
        remaining_min = max(0, int((dt - datetime.now()).total_seconds() // 60))
        print(f"Expires: {dt.strftime('%Y-%m-%d %H:%M:%S')} (in {remaining_min} min)")
    else:
        print("Expires: <unknown / not a JWT>")
    return 0


def run_session_status(argv: list[str] | None = None) -> int:
    """Run the ``wikijs-mcp session-status`` command. Returns an exit code."""
    parser = argparse.ArgumentParser(
        prog="wikijs-mcp session-status",
        description="Show the effective authentication mode and session-token "
        "state without printing the token itself.",
    )
    parser.parse_args(argv)

    config = WikiJSConfig.load_config()
    path = session_store.token_path()
    env_token = (config.session_token or "").strip() or None
    file_token = session_store.read_session_token()
    token = env_token or file_token
    source = "environment" if env_token else ("file" if file_token else "none")

    print(f"Auth mode:     {config.auth_mode}")
    print(f"Token file:    {path}")
    try:
        perms = stat.S_IMODE(os.stat(path).st_mode)
        print(f"File perms:    0o{perms:o}")
    except FileNotFoundError:
        print("File perms:    <missing>")
    print(f"Token source:  {source}")
    if token:
        masked = token[:16] + "..." if len(token) > 20 else token[:8] + "..."
        print(f"Token:         {masked}")
        exp = session_store.jwt_expiry(token)
        if exp is not None:
            dt = datetime.fromtimestamp(exp)
            remaining_min = max(0, int((dt - datetime.now()).total_seconds() // 60))
            print(f"Expires:       {dt.strftime('%Y-%m-%d %H:%M:%S')} (in {remaining_min} min)")
        else:
            print("Expires:       <unknown / not a JWT>")
    else:
        print("Token:         <none>")

    if config.auth_mode == "session" and not token:
        print(
            "Hint: the server refuses to start in session mode until you run "
            "'wikijs-mcp login' or set WIKIJS_SESSION_TOKEN."
        )
    print(
        "Role hint: map the Authentik group (mapGroups) to a Wiki.js role "
        "granting read:pages, manage:pages, read:assets, write:assets, "
        "manage:assets."
    )
    return 0
