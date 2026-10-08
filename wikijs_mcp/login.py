"""CLI commands for SSO / Authentik session authentication.

The MCP server can operate as a regular wiki user instead of an admin
personal access token: the browser ``jwt`` cookie set by Wiki.js after an
SSO/Authentik login is captured here and stored on disk (``chmod 600``). All
API requests then send it as ``Authorization: Bearer <jwt>``; Wiki.js hands
back refreshed tokens via the ``new-jwt`` response header, which the client
adopts and re-persists automatically.

Commands (wired into ``wikijs-mcp``):

- ``wikijs-mcp login``            *default:* drive your browser through the
                                  Wiki.js SSO login (``{url}/login`` →
                                  Authentik) and capture the ``jwt`` cookie
                                  automatically via browser automation. Uses
                                  your installed Google Chrome
                                  (``channel="chrome"``) with a persistent
                                  profile, so later logins reuse the SSO
                                  session and are near-instant.
- ``wikijs-mcp login --manual``   fallback: open the login page, then paste
                                  the ``jwt`` cookie from the browser devtools.
- ``wikijs-mcp session-status``   show auth mode, token source, masked token
                                  and expiry — without printing the token.

Why browser automation? The wiki is an OIDC *client* of Authentik and signs
its own session JWT server-side; there is no token endpoint a CLI could poll
like ``gh``/``gcloud`` against an OAuth authorization server. The ``jwt``
cookie is ``httpOnly`` and only reachable from inside a real browser session,
so the automatic flow needs a browser driver (Playwright).

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
import time
import urllib.parse
import webbrowser
from datetime import datetime
from pathlib import Path

from . import session as session_store
from .config import WikiJSConfig, normalize_url

DEFAULT_PROFILE_DIR = Path.home() / ".config" / "wikijs-mcp" / "chrome-profile"
DEFAULT_TIMEOUT_SECONDS = 300


def _build_login_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wikijs-mcp login",
        description=(
            "Drive your browser through the Wiki.js SSO login and store the "
            "session JWT. By default this uses browser automation with your "
            "installed Google Chrome (channel='chrome') and a persistent "
            "profile: the /login redirect to Authentik runs once, then the "
            "'jwt' cookie is captured automatically. Fallbacks: --manual for "
            "pasting the cookie from devtools, --stdin for scripted input. A "
            "missing scheme (e.g. 'wiki.example.com') is completed to https:// "
            "automatically."
        ),
    )
    parser.add_argument(
        "--url",
        default="",
        help="Wiki.js base URL (default: WIKIJS_URL environment variable).",
    )
    parser.add_argument(
        "--browser",
        choices=["chrome", "chromium"],
        default="chrome",
        help="Browser automation backend: 'chrome' uses your installed Google "
        "Chrome (default); 'chromium' uses the Playwright-bundled build "
        "(requires: python -m playwright install chromium).",
    )
    parser.add_argument(
        "--profile",
        default="",
        help="Persistent browser profile directory (default: "
        f"{DEFAULT_PROFILE_DIR}). Reused across logins so the Authentik SSO "
        "session persists.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="How long to wait for the 'jwt' cookie after opening the login "
        f"page, in seconds (default: {DEFAULT_TIMEOUT_SECONDS}).",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run the automated browser capture without a visible window "
        "(CI / scripting).",
    )
    parser.add_argument(
        "--manual",
        action="store_true",
        help="Skip browser automation: open the login page, then paste the "
        "'jwt' cookie value from the browser devtools.",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Manual flow: do not open the browser; only print the login URL "
        "and wait for the pasted cookie.",
    )
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="Read the token from standard input until EOF instead of prompting "
        "interactively (scripting / CI).",
    )
    return parser


def _probe_login_url(login_url: str) -> None:
    """Best-effort reachability check for the login URL (warns, never blocks)."""
    try:
        import httpx

        response = httpx.get(
            login_url,
            follow_redirects=True,
            timeout=8.0,
            headers={"User-Agent": "wikijs-mcp login"},
        )
        content_type = response.headers.get("content-type", "?")
        print(
            f"probe: HTTP {response.status_code} ({content_type}) after redirects",
            file=sys.stderr,
        )
        if response.status_code >= 400:
            print(
                "  hint: the URL may be wrong, or the wiki sits behind a "
                "reverse proxy with a base path (e.g. https://host/wiki/login).",
                file=sys.stderr,
            )
    except Exception as exc:  # noqa: BLE001 - non-fatal diagnostic
        print(f"probe: could not reach {login_url}: {exc}", file=sys.stderr)
        print(
            "  hint: check the hostname / network, or open the URL manually.",
            file=sys.stderr,
        )


def _open_login_page(url: str) -> None:
    """Open the Wiki.js login page in the OS default browser."""
    login_url = f"{url.rstrip('/')}/login"
    print(f"Opening {login_url} in your standard browser ...")
    opened = webbrowser.open(login_url)
    if not opened:
        print(
            f"Could not open a browser automatically. Please open this URL "
            f"manually:\n  {login_url}",
            file=sys.stderr,
        )
    _probe_login_url(login_url)


def _read_pasted_token(use_stdin: bool = False) -> str:
    """Read the ``jwt`` cookie value from the user or from stdin.

    Interactive (TTY) input uses a hidden prompt and re-asks up to three times
    when the user hits Enter with an empty paste. With ``--stdin`` the whole
    input is read until EOF; a plain pipe (non-TTY without the flag) reads a
    single line so that an idle wrapper terminal cannot hang the command.
    """
    if not sys.stdin.isatty():
        if use_stdin:
            return sys.stdin.read().strip()
        return (sys.stdin.readline() or "").strip()

    instructions = (
        "Log in to the wiki (SSO/Authentik) in the browser, then copy the 'jwt'\n"
        "cookie value: devtools (F12) -> Application -> Cookies -> 'jwt'.\n"
        "Paste it below and press Enter. The input is hidden (nothing appears\n"
        "while you paste — that is expected)."
    )
    for _ in range(3):
        print(instructions)
        try:
            token = getpass.getpass("jwt cookie: ").strip()
        except (EOFError, OSError):  # pragma: no cover - non-interactive edge
            token = ""
        if token:
            return token
        print(
            "No token received — paste the jwt cookie value and press Enter.",
            file=sys.stderr,
        )
    return ""


def _has_playwright() -> bool:
    """Whether the optional ``playwright`` package is importable."""
    try:
        import importlib.util

        return importlib.util.find_spec("playwright") is not None
    except (ImportError, ValueError):  # pragma: no cover - defensive
        return False


def _extract_jwt_cookie(context: object, url: str) -> str | None:
    """Return the wiki's ``jwt`` cookie value from a Playwright context.

    Prefers a cookie whose domain matches the wiki host; falls back to the
    first ``jwt`` cookie if none matches.
    """
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    candidates = [
        c
        for c in context.cookies()
        if c.get("name") == "jwt" and c.get("value")
    ]
    for cookie in candidates:
        domain = (cookie.get("domain") or "").lstrip(".").lower()
        if host and (host == domain or host.endswith("." + domain) or domain.endswith("." + host)):
            return cookie["value"]
    if candidates:
        return candidates[0]["value"]
    return None


def _auto_login(
    url: str,
    browser: str,
    profile: Path,
    timeout: int,
    headless: bool,
) -> str | None:
    """Drive the browser through the SSO login and capture the ``jwt`` cookie.

    Returns the token, or ``None`` on failure / timeout. Requires the optional
    ``playwright`` extra.
    """
    from playwright.sync_api import sync_playwright

    login_url = f"{url.rstrip('/')}/login"
    label = "Google Chrome" if browser == "chrome" else "Playwright Chromium"
    print(f"Opening {login_url} in {label} (automated capture) ...")
    print(
        "Log in once in the browser window; the 'jwt' cookie is captured "
        "automatically. The session profile persists for future logins."
    )

    with sync_playwright() as p:
        launch_kwargs: dict = {"headless": headless, "user_data_dir": str(profile)}
        if browser == "chrome":
            launch_kwargs["channel"] = "chrome"
        try:
            context = p.chromium.launch_persistent_context(**launch_kwargs)
        except Exception as exc:  # noqa: BLE001 - user-facing CLI error
            print(f"error: could not launch {label}: {exc}", file=sys.stderr)
            if browser == "chrome":
                print(
                    "  hint: install Google Chrome, or retry with "
                    "--browser chromium.",
                    file=sys.stderr,
                )
            return None
        try:
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(login_url, wait_until="domcontentloaded", timeout=30_000)
            except Exception as exc:  # noqa: BLE001 - best-effort navigation
                print(
                    f"warning: could not load {login_url}: {exc}",
                    file=sys.stderr,
                )
                print(
                    "  The login page may redirect to the SSO provider; "
                    "finish the login in the browser window.",
                    file=sys.stderr,
                )
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                token = _extract_jwt_cookie(context, url)
                if token:
                    return token
                time.sleep(0.75)
            print(
                f"timeout: no 'jwt' cookie within {timeout}s. Complete the "
                "SSO login in the browser window, or retry with --manual to "
                "paste the cookie.",
                file=sys.stderr,
            )
            return None
        finally:
            try:
                context.close()
            except Exception:  # noqa: BLE001, S110 - best-effort cleanup
                pass


def run_login(argv: list[str] | None = None) -> int:
    """Run the ``wikijs-mcp login`` command. Returns an exit code."""
    args = _build_login_parser().parse_args(argv)

    url = normalize_url((args.url or WikiJSConfig.load_config().url).strip()).rstrip("/")
    if not url:
        print(
            "No Wiki.js URL configured: pass --url or set WIKIJS_URL.",
            file=sys.stderr,
        )
        return 2

    # A non-TTY stdin (pipe) always means scripted input, never automation.
    manual = args.manual or args.stdin or not sys.stdin.isatty()

    if manual:
        if sys.stdin.isatty() and not args.no_open:
            _open_login_page(url)
        token = _read_pasted_token(use_stdin=args.stdin)
    else:
        if not _has_playwright():
            print(
                "Automated browser login needs the optional 'playwright' "
                "extra, which is not installed.",
                file=sys.stderr,
            )
            print(
                "  Install it (tool: 'uv tool install --reinstall --extra "
                "login .'; or 'pip install \"wikijs-mcp[login]\"') and retry, "
                "or use --manual to paste the jwt cookie, or use --stdin for "
                "scripted input.",
                file=sys.stderr,
            )
            return 1
        profile = Path(args.profile) if args.profile else DEFAULT_PROFILE_DIR
        token = _auto_login(url, args.browser, profile, args.timeout, args.headless) or ""

    if not token:
        print(
            "No token was provided; nothing stored. Hint: try again and "
            "complete the SSO login in the browser window, or copy the 'jwt' "
            "cookie from the browser devtools (F12 -> Application -> Cookies) "
            "and use --manual to paste it.",
            file=sys.stderr,
        )
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
