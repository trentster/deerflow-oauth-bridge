#!/usr/bin/env python3
"""Standalone OpenAI Codex OAuth PKCE module for DeerFlow OAuth Bridge.

Public API:
- login() -> dict(access_token, account_id)
- get_valid_token() -> dict(access_token, account_id)
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, Optional

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTH_ENDPOINT = "https://auth.openai.com/oauth/authorize"
TOKEN_ENDPOINT = "https://auth.openai.com/oauth/token"
REDIRECT_URI = "http://localhost:1455/auth/callback"
SCOPES = "openid profile email offline_access"
CALLBACK_HOST = "localhost"
CALLBACK_PORT = 1455
CALLBACK_PATH = "/auth/callback"

STORE_DIR = Path.home() / ".deerflow-bridge"
AUTH_FILE = STORE_DIR / "auth.json"


class OAuthError(RuntimeError):
    pass


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_code_verifier() -> str:
    """Generate PKCE verifier from 32 random bytes, base64url encoded."""
    return _b64url(secrets.token_bytes(32))


def generate_code_challenge(code_verifier: str) -> str:
    """Generate S256 PKCE challenge from verifier."""
    digest = hashlib.sha256(code_verifier.encode("utf-8")).digest()
    return _b64url(digest)


def _parse_jwt_payload(jwt_token: str) -> Dict[str, Any]:
    parts = jwt_token.split(".")
    if len(parts) < 2:
        raise OAuthError("Invalid JWT: missing payload segment")
    payload = parts[1]
    padded = payload + "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        return json.loads(decoded)
    except Exception as exc:
        raise OAuthError(f"Failed to decode JWT payload: {exc}") from exc


def extract_account_id(access_token: str) -> str:
    payload = _parse_jwt_payload(access_token)
    auth_claim = payload.get("https://api.openai.com/auth")
    if isinstance(auth_claim, dict):
        account_id = auth_claim.get("chatgpt_account_id")
        if account_id:
            return str(account_id)
    raise OAuthError("chatgpt_account_id not found in access token claims")


def _post_form(url: str, form: Dict[str, str]) -> Dict[str, Any]:
    body = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
        raise OAuthError(f"Token endpoint HTTP {e.code}: {raw or e.reason}") from e
    except Exception as exc:
        raise OAuthError(f"Token request failed: {exc}") from exc

    try:
        return json.loads(payload)
    except Exception as exc:
        raise OAuthError(f"Token endpoint returned non-JSON response: {payload}") from exc


def _ensure_store_dir() -> None:
    STORE_DIR.mkdir(parents=True, exist_ok=True)


def save_credentials(creds: Dict[str, Any]) -> None:
    _ensure_store_dir()
    AUTH_FILE.write_text(json.dumps(creds, indent=2), encoding="utf-8")


def load_credentials() -> Optional[Dict[str, Any]]:
    if not AUTH_FILE.exists():
        return None
    try:
        return json.loads(AUTH_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        raise OAuthError(f"Failed to read credentials file {AUTH_FILE}: {exc}") from exc


def _now_ts() -> int:
    return int(time.time())


def _is_expired(creds: Dict[str, Any], skew_seconds: int = 30) -> bool:
    expires_at = creds.get("expires") or creds.get("expires_at")
    if expires_at is None:
        return True
    return _now_ts() >= int(expires_at) - skew_seconds


def _normalize_token_response(token_data: Dict[str, Any]) -> Dict[str, Any]:
    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")
    if not access_token:
        raise OAuthError("Token response missing access_token")

    # Some providers return expires_in seconds; persist as absolute unix timestamp.
    expires_in = int(token_data.get("expires_in", 3600))
    expires = _now_ts() + max(1, expires_in)

    account_id = extract_account_id(access_token)

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires": expires,
        "account_id": account_id,
    }


@dataclass
class _CallbackState:
    code: Optional[str] = None
    error: Optional[str] = None
    done: threading.Event = field(default_factory=threading.Event)


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    callback_state: _CallbackState
    expected_state: str

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self.send_error(404, "Not Found")
            return

        params = urllib.parse.parse_qs(parsed.query)
        state = params.get("state", [""])[0]
        code = params.get("code", [""])[0]
        error = params.get("error", [""])[0]

        if state != self.expected_state:
            self.callback_state.error = "Invalid OAuth state"
        elif error:
            self.callback_state.error = error
        elif not code:
            self.callback_state.error = "Missing authorization code"
        else:
            self.callback_state.code = code

        self.callback_state.done.set()

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<html><body><h3>Authentication complete.</h3><p>You can close this window.</p></body></html>"
        )

    def log_message(self, format: str, *args: Any) -> None:  # silence server logs
        return


def _build_auth_url(code_challenge: str, state: str, originator: str = "deerflow-bridge") -> str:
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "codex_cli_simplified_flow": "true",
        "id_token_add_organizations": "true",
        "originator": originator,
    }
    return AUTH_ENDPOINT + "?" + urllib.parse.urlencode(params)


def _exchange_code_for_tokens(code: str, code_verifier: str, redirect_uri: str = REDIRECT_URI) -> Dict[str, Any]:
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": code_verifier,
        "redirect_uri": redirect_uri,
        "client_id": CLIENT_ID,
    }
    token_data = _post_form(TOKEN_ENDPOINT, form)
    creds = _normalize_token_response(token_data)

    # Some providers may omit refresh token on subsequent auth.
    if not creds.get("refresh_token"):
        creds["refresh_token"] = token_data.get("refresh_token")
    return creds


def refresh_tokens(existing_creds: Dict[str, Any]) -> Dict[str, Any]:
    refresh_token = existing_creds.get("refresh_token")
    if not refresh_token:
        raise OAuthError("Cannot refresh token: refresh_token is missing")

    form = {
        "grant_type": "refresh_token",
        "refresh_token": str(refresh_token),
        "client_id": CLIENT_ID,
    }
    token_data = _post_form(TOKEN_ENDPOINT, form)
    new_creds = _normalize_token_response(token_data)
    if not new_creds.get("refresh_token"):
        new_creds["refresh_token"] = refresh_token

    save_credentials(new_creds)
    return new_creds


def login(timeout_seconds: int = 300) -> Dict[str, str]:
    """Run interactive browser OAuth PKCE login and persist credentials."""
    code_verifier = generate_code_verifier()
    code_challenge = generate_code_challenge(code_verifier)
    state = secrets.token_urlsafe(24)

    callback_state = _CallbackState(done=threading.Event())

    class Handler(_OAuthCallbackHandler):
        pass

    Handler.callback_state = callback_state
    Handler.expected_state = state

    server = HTTPServer((CALLBACK_HOST, CALLBACK_PORT), Handler)
    redirect_uri = REDIRECT_URI
    auth_url = _build_auth_url(code_challenge, state)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print("Opening browser for OAuth login...")
    opened = webbrowser.open(auth_url)
    if not opened:
        print(f"Please open this URL manually:\n{auth_url}")

    completed = callback_state.done.wait(timeout_seconds)
    server.shutdown()
    server.server_close()

    if not completed:
        raise OAuthError("Timed out waiting for OAuth callback")
    if callback_state.error:
        raise OAuthError(f"OAuth callback error: {callback_state.error}")
    if not callback_state.code:
        raise OAuthError("OAuth callback did not include authorization code")

    creds = _exchange_code_for_tokens(callback_state.code, code_verifier, redirect_uri)
    save_credentials(creds)

    return {"access_token": creds["access_token"], "account_id": creds["account_id"]}


def get_valid_token() -> Dict[str, str]:
    """Return a valid access token + account ID, refreshing or logging in as needed."""
    creds = load_credentials()

    if not creds:
        return login()

    if _is_expired(creds):
        try:
            creds = refresh_tokens(creds)
        except OAuthError:
            # Fallback to full login flow if refresh fails.
            return login()

    access_token = creds.get("access_token")
    account_id = creds.get("account_id")

    if not access_token:
        return login()

    if not account_id:
        account_id = extract_account_id(access_token)
        creds["account_id"] = account_id
        save_credentials(creds)

    return {"access_token": str(access_token), "account_id": str(account_id)}


if __name__ == "__main__":
    try:
        token_info = get_valid_token()
        print("✅ OAuth ready")
        print(f"account_id: {token_info['account_id']}")
        print(f"access_token: {token_info['access_token']}")
    except Exception as exc:
        print(f"❌ OAuth failed: {exc}")
        raise
