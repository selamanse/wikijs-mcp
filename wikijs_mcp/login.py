"""CLI commands for SSO / Authentik session authentication.

The MCP server can operate as a regular wiki user instead of an admin
personal access token: the browser ``jwt`` cookie set by Wiki.js after an
SSO/Authentik login is captured here and stored on disk (``chmod 600``). All
API requests then send it as ``Authorization: Bearer <jwt>``; Wiki.js hands
back refreshed tokens via the ``new-jwt`` response header, which the client
adopts and re-persists automatically.

Commands (wired into ``wikijs-mcp``):

- ``wikijs-mcp login``            one-time browser login, extracts the ``jwt``
                                  cookie and stores it.
- ``wikijs-mcp session-status``   show auth mode, token source, masked token
                                  and expiry — without printing the token.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
import time
from datetime import datetime
from importlib import util as _import_util

from . import session as session_store
from .config import WikiJSConfig

DEFAULT_LOGIN_TIMEOUT = 300  # seconds to wait for the user to complete SSO


def _has_playwright() -> bool:
    return _import_util.find_spec("playwright") is not None


def _build_login_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wikijs-mcp login",
        description=(
            "One-time browser login via SSO/Authentik. Opens the Wiki.js login "
            "page, waits for the user to authenticate, extracts the 'jwt' "
            "cookie and stores it in the session-token file (chmod 600)."
        ),
    )
    parser.add_argument(
        "--url",
        default="",
        help="Wiki.js base URL (default: WIKIJS_URL environment variable).",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run Chromium headless (no visible browser window).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_LOGIN_TIMEOUT,
        help=f"Seconds to wait for SSO to complete (default: {DEFAULT_LOGIN_TIMEOUT}).",
    )
    return parser


def _extract_jwt_cookie(browser_context) -> str | None:
    """Return the first non-empty ``jwt`` cookie from the browser context."""
    for cookie in browser_context.cookies():
        if cookie.get("name") == "jwt" and cookie.get("value"):
            return cookie["value"]
    return None


def _browser_login(url: str, headless: bool, timeout: int) -> str | None:
    """Drive the SSO login and return the ``jwt`` cookie value (or ``None``).

    Behaviour on only-OIDC setups: Wiki.js renders a login page that usually
    shows the SSO provider button and may auto-redirect straight to Authentik.
    Either way, after the user authenticates the browser lands back on Wiki.js
    and the ``jwt`` cookie is set — so this function simply polls for the
    cookie until the deadline, regardless of how many redirects occurred.
    """
    from playwright.sync_api import sync_playwright

    login_url = f"{url.rstrip('/')}/login"
    print(f"Opening {login_url} in the browser...")
    print(
        "Please complete the SSO login in the browser window. "
        f"(waiting up to {timeout}s)"
    )
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(login_url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as exc:  # noqa: BLE001 - page may still render
            print(f"Warning: could not load {login_url}: {exc}", file=sys.stderr)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            token = _extract_jwt_cookie(context)
            if token:
                return token
            time.sleep(0.75)

        # The login page may need a manual click on the SSO button; surface the
        # most likely cause clearly instead of a bare timeout.
        print(
            f"No 'jwt' cookie appeared within {timeout}s. Make sure you "
            "completed the SSO login (if the wiki only exposes OIDC, the login "
            "page usually shows an Authentik button or redirects automatically).",
            file=sys.stderr,
        )
        return None


def run_login(argv: list[str] | None = None) -> int:
    """Run the interactive ``wikijs-mcp login`` command. Returns an exit code."""
    args = _build_login_parser().parse_args(argv)

    if not _has_playwright():
        print(
            "The 'login' command requires the optional Playwright extra.\n"
            "  pip install 'wikijs-mcp[login]'     (or: uv tool install . --extra login)\n"
            "  playwright install chromium",
            file=sys.stderr,
        )
        return 1

    url = (args.url or WikiJSConfig.load_config().url).strip().rstrip("/")
    if not url:
        print(
            "No Wiki.js URL configured: pass --url or set WIKIJS_URL.",
            file=sys.stderr,
        )
        return 2

    token = _browser_login(url, headless=args.headless, timeout=args.timeout)
    if token is None:
        return 2

    session_store.write_session_token(token)
    print(
        f"Session token stored at {session_store.token_path()} (chmod 600).\n"
        "The MCP client refreshes it automatically via the 'new-jwt' response "
        "header. Role hint: map the Authentik group (mapGroups) to a Wiki.js "
        "role granting read:pages, manage:pages, read:assets, write:assets, "
        "manage:assets."
    )
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
