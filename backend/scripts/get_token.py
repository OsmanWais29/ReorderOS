#!/usr/bin/env python3
"""Minimal local auth flow for testing the live DO API.

What this does:
  1. Opens your browser to the WorkOS hosted login page.
  2. Listens on http://localhost:3000/callback for the redirect.
  3. Exchanges the auth code for a real RS256 JWT.
  4. Prints the token — or runs the full smoke test automatically.

Prerequisites:
  a) Add http://localhost:3000/callback as a Redirect URI in WorkOS Dashboard
     → Authentication → Redirects → Add URI
  b) Export your WorkOS secret key:
       export WORKOS_CLIENT_SECRET=sk_...

Usage:
  python scripts/get_token.py              # prints token then runs smoke test
  python scripts/get_token.py --print-only # just prints the token
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import os
import secrets
import subprocess
import sys
import threading
import urllib.parse
import webbrowser

import httpx

CLIENT_ID = "client_01KQT0CRCZP8W8AMAYE5Y1F9SP"
REDIRECT_URI = "http://localhost:3000/callback"
PORT = 3000

# WorkOS endpoints
AUTH_URL = "https://api.workos.com/user_management/authorize"
TOKEN_URL = "https://api.workos.com/user_management/authenticate"


# ── PKCE helpers ──────────────────────────────────────────────────────────────


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


# ── Tiny local callback server ────────────────────────────────────────────────


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    code: str | None = None
    error: str | None = None

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if "code" in params:
            _CallbackHandler.code = params["code"][0]
            body = b"<html><body><h2>Login successful.</h2><p>You can close this tab.</p></body></html>"
        elif "error" in params:
            _CallbackHandler.error = params.get("error_description", params.get("error", ["unknown"]))[0]
            body = b"<html><body><h2>Login failed.</h2><p>Check the terminal.</p></body></html>"
        else:
            body = b"<html><body><p>Waiting...</p></body></html>"

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        pass  # suppress request logging


def _wait_for_code() -> str:
    server = http.server.HTTPServer(("localhost", PORT), _CallbackHandler)
    server.timeout = 120  # 2 min to log in

    print(f"Listening on http://localhost:{PORT}/callback …\n")

    while _CallbackHandler.code is None and _CallbackHandler.error is None:
        server.handle_request()

    server.server_close()

    if _CallbackHandler.error:
        sys.exit(f"WorkOS returned an error: {_CallbackHandler.error}")

    code = _CallbackHandler.code
    assert code is not None
    return code


# ── Token exchange ─────────────────────────────────────────────────────────────


def _exchange(code: str, verifier: str) -> str:
    secret = os.environ.get("WORKOS_CLIENT_SECRET", "")
    if not secret:
        sys.exit(
            "\nWORKOS_CLIENT_SECRET is not set.\n"
            "Export it first:\n"
            "  export WORKOS_CLIENT_SECRET=sk_...\n"
            "Find it in WorkOS Dashboard → API Keys."
        )

    r = httpx.post(
        TOKEN_URL,
        json={
            "client_id": CLIENT_ID,
            "client_secret": secret,
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": REDIRECT_URI,
        },
        timeout=15,
    )

    if r.status_code != 200:
        sys.exit(f"Token exchange failed ({r.status_code}): {r.text}")

    token: str = r.json()["access_token"]
    return token


# ── Main ───────────────────────────────────────────────────────────────────────


def main() -> None:
    print_only = "--print-only" in sys.argv

    secret = os.environ.get("WORKOS_CLIENT_SECRET", "")
    if not secret:
        print(
            "ERROR: WORKOS_CLIENT_SECRET not set.\n\n"
            "  1. Go to WorkOS Dashboard → API Keys → copy your secret key\n"
            "  2. Run:  export WORKOS_CLIENT_SECRET=sk_...\n"
            "  3. Also make sure http://localhost:3000/callback is listed under\n"
            "     WorkOS Dashboard → Authentication → Redirects\n"
        )
        sys.exit(1)

    verifier, challenge = _pkce_pair()

    params = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "provider": "authkit",
    })
    login_url = f"{AUTH_URL}?{params}"

    print("Opening WorkOS login in your browser…")
    print(f"If the browser doesn't open, visit:\n  {login_url}\n")
    webbrowser.open(login_url)

    code = _wait_for_code()
    print("Got auth code. Exchanging for token…")

    token = _exchange(code, verifier)

    print(f"\nJWT ({len(token)} chars):")
    print(f"  {token[:60]}…\n")

    if print_only:
        print("Full token (copy this):")
        print(token)
        return

    # Run the smoke test with the real token
    smoke_script = os.path.join(os.path.dirname(__file__), "smoke_auth.py")
    subprocess.run(
        [sys.executable, smoke_script, "--token", token],
        check=False,
    )


if __name__ == "__main__":
    main()
