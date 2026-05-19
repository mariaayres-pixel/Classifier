"""
qbo_auth.py
One-time OAuth2 flow for QuickBooks Online.

Run:
    python qbo_auth.py

This will:
  1. Open your browser to the QuickBooks authorization page
  2. Start a local server on localhost:8000 to catch the callback
  3. Exchange the authorization code for access + refresh tokens
  4. Write QBO_ACCESS_TOKEN and QBO_REFRESH_TOKEN into your .env file
"""

import base64
import http.server
import os
import re
import secrets
import threading
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
import json

# ── Config ────────────────────────────────────────────────────────────────────
ENV_PATH = Path(__file__).parent / ".env"

QBO_AUTH_URL   = "https://appcenter.intuit.com/connect/oauth2"
QBO_TOKEN_URL  = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
QBO_SCOPE      = "com.intuit.quickbooks.accounting"
CALLBACK_PORT  = 8000
CALLBACK_PATH  = "/callback"


def load_env(path: Path) -> dict:
    env = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


def update_env(path: Path, updates: dict) -> None:
    """Add or replace key=value lines in the .env file."""
    text = path.read_text()
    for key, value in updates.items():
        pattern = rf"^{re.escape(key)}\s*=.*$"
        replacement = f"{key}={value}"
        if re.search(pattern, text, flags=re.MULTILINE):
            text = re.sub(pattern, replacement, text, flags=re.MULTILINE)
        else:
            text = text.rstrip("\n") + f"\n{replacement}\n"
    path.write_text(text)
    print(f"  ✓ .env updated with new tokens.")


def exchange_code_for_tokens(code: str, client_id: str, client_secret: str, redirect_uri: str) -> dict:
    """POST the auth code to QBO's token endpoint."""
    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    data = urllib.parse.urlencode({
        "grant_type":   "authorization_code",
        "code":         code,
        "redirect_uri": redirect_uri,
    }).encode()

    req = urllib.request.Request(
        QBO_TOKEN_URL,
        data=data,
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type":  "application/x-www-form-urlencoded",
            "Accept":        "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Minimal HTTP handler that captures the OAuth callback."""

    received_code  = None
    received_state = None
    received_error = None
    server_done    = threading.Event()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self._respond(404, "Not found")
            return

        params = dict(urllib.parse.parse_qsl(parsed.query))

        if "error" in params:
            CallbackHandler.received_error = params.get("error_description", params["error"])
            self._respond(400, f"<h2>Authorization failed</h2><p>{CallbackHandler.received_error}</p>")
        else:
            CallbackHandler.received_code  = params.get("code")
            CallbackHandler.received_state = params.get("state")
            self._respond(
                200,
                "<h2 style='font-family:sans-serif;color:green'>✅ Authorization successful!</h2>"
                "<p style='font-family:sans-serif'>You can close this tab and return to the terminal.</p>",
            )

        CallbackHandler.server_done.set()

    def _respond(self, status: int, body: str):
        encoded = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(encoded))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args):
        pass  # silence default request logging


def run_oauth_flow(client_id: str, client_secret: str, redirect_uri: str) -> tuple[str, str]:
    """
    Full OAuth2 flow.
    Returns (access_token, refresh_token).
    """
    state = secrets.token_urlsafe(16)

    auth_params = urllib.parse.urlencode({
        "client_id":     client_id,
        "response_type": "code",
        "scope":         QBO_SCOPE,
        "redirect_uri":  redirect_uri,
        "state":         state,
    })
    auth_url = f"{QBO_AUTH_URL}?{auth_params}"

    # Start local callback server in a background thread
    server = http.server.HTTPServer(("localhost", CALLBACK_PORT), CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print("\n──────────────────────────────────────────────────")
    print("  Opening QuickBooks authorization in your browser…")
    print(f"\n  If the browser doesn't open, visit:\n  {auth_url}")
    print("──────────────────────────────────────────────────\n")
    webbrowser.open(auth_url)

    # Wait for the callback (timeout after 3 minutes)
    completed = CallbackHandler.server_done.wait(timeout=180)
    server.shutdown()

    if not completed:
        raise TimeoutError("Authorization timed out after 3 minutes. Please try again.")

    if CallbackHandler.received_error:
        raise RuntimeError(f"QuickBooks authorization failed: {CallbackHandler.received_error}")

    if not CallbackHandler.received_code:
        raise RuntimeError("No authorization code received.")

    if CallbackHandler.received_state != state:
        raise RuntimeError("State mismatch — possible CSRF. Please try again.")

    print("  ✓ Authorization code received. Exchanging for tokens…")
    tokens = exchange_code_for_tokens(
        CallbackHandler.received_code, client_id, client_secret, redirect_uri
    )

    access_token  = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")

    if not access_token or not refresh_token:
        raise RuntimeError(f"Token exchange failed. Response: {tokens}")

    expires_in = tokens.get("expires_in", "unknown")
    x_expires  = tokens.get("x_refresh_token_expires_in", "unknown")
    print(f"  ✓ Access token received  (expires in {expires_in}s ≈ 1 hour)")
    print(f"  ✓ Refresh token received (expires in {x_expires}s ≈ 100 days)")

    return access_token, refresh_token


def main():
    if not ENV_PATH.exists():
        raise FileNotFoundError(f".env file not found at {ENV_PATH}")

    env = load_env(ENV_PATH)

    client_id     = env.get("QBO_CLIENT_ID", "").strip()
    client_secret = env.get("QBO_CLIENT_SECRET", "").strip()
    redirect_uri  = env.get("QBO_REDIRECT_URI", f"http://localhost:{CALLBACK_PORT}{CALLBACK_PATH}").strip()

    if not client_id or not client_secret:
        raise ValueError("QBO_CLIENT_ID and QBO_CLIENT_SECRET must be set in .env")

    print("BRASA – QuickBooks OAuth2 Setup")
    print(f"  Client ID : {client_id[:8]}…")
    print(f"  Realm ID  : {env.get('QBO_REALM_ID', '(not set)')}")
    print(f"  Callback  : {redirect_uri}")

    access_token, refresh_token = run_oauth_flow(client_id, client_secret, redirect_uri)

    update_env(ENV_PATH, {
        "QBO_ACCESS_TOKEN":  access_token,
        "QBO_REFRESH_TOKEN": refresh_token,
    })

    print("\n✅ Done! Tokens saved to .env")
    print("   You can now run the classifier pipeline.\n")


if __name__ == "__main__":
    main()
