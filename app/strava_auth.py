"""Strava OAuth2 authorization-code flow.

Builds the authorization URL and exchanges the returned code for tokens using
direct Strava REST calls (via ``app.strava_api``).

The redirect target is the loopback IP ``http://127.0.0.1:{port}`` (per RFC 8252
for native apps). Using the IP literal rather than ``localhost`` avoids Safari's
"HTTPS-first" upgrade, which would otherwise rewrite the callback to https and
break the local HTTP listener.

A short-lived local HTTP server captures the redirect automatically. If that
doesn't arrive (e.g. a browser that still blocks the loopback request), the
caller can supply ``manual_code_provider`` to prompt the user to paste the
redirected URL (or bare code) instead. This module stays GUI-free; the prompt is
injected by the caller.

Register ``127.0.0.1`` as the Authorization Callback Domain in your Strava API
application settings.
"""

import re
import secrets
import time
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import strava_api
from .logger import Logger

# Seconds to wait for the automatic callback before offering manual entry.
AUTO_CAPTURE_TIMEOUT = 25
# Seconds to wait when there is no manual fallback (e.g. command-line use).
AUTO_ONLY_TIMEOUT = 120
# activity:write to upload; activity:read because the upload flow reads the
# created activity back after processing (GET /activities/{id}).
SCOPES = ["activity:write", "activity:read"]

ManualCodeProvider = Callable[[str], str | None]


class _CallbackHandler(BaseHTTPRequestHandler):
    """Captures the OAuth ``code`` (or ``error``) from the redirect.

    The ``state`` parameter is required and must match the value generated for
    this flow (RFC 8252 §8.9): it stops another local process or a malicious
    web page from injecting its own authorization code into the loopback
    listener, which would silently connect the app to an attacker's account.
    """

    auth_code: str | None = None
    auth_error: str | None = None
    expected_state: str | None = None

    def do_GET(self) -> None:
        params = parse_qs(urlparse(self.path).query)
        state = params.get("state", [None])[0]
        state_ok = (
            _CallbackHandler.expected_state is not None
            and state is not None
            and secrets.compare_digest(state, _CallbackHandler.expected_state)
        )
        # Both successful and denied OAuth responses are terminal. Requiring
        # state for either prevents an unrelated local request from aborting
        # the in-progress authorization flow.
        if ("code" in params or "error" in params) and not state_ok:
            self.send_response(400)
            self.end_headers()
            return
        if "code" in params:
            _CallbackHandler.auth_code = params["code"][0]
            body = (
                b"<html><body><h1>KayakFit is connected to Strava.</h1>"
                b"<p>You can close this tab.</p></body></html>"
            )
        elif "error" in params:
            _CallbackHandler.auth_error = params["error"][0]
            body = (
                b"<html><body><h1>Authorization failed.</h1>"
                b"<p>You can close this tab.</p></body></html>"
            )
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:  # silence server logging
        pass


class StravaAuth:
    """Runs the Strava OAuth authorization-code flow."""

    def __init__(self, client_id: str, client_secret: str, log_level: str = "INFO") -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.logger = Logger.get_logger(name=__name__)

    @staticmethod
    def _start_server(
        start_port: int = 8000, max_attempts: int = 10
    ) -> HTTPServer | None:
        """Bind the loopback callback server on the first free port.

        Binding directly (rather than probing for a free port first) avoids the
        race where another process grabs the port between probe and bind.
        """
        for port in range(start_port, start_port + max_attempts):
            try:
                server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
                server.socket.settimeout(2.0)
                return server
            except OSError:
                continue
        return None

    @staticmethod
    def extract_code(text: str | None, expected_state: str | None = None) -> str | None:
        """Extract the OAuth code from a pasted full redirect URL or a bare code.

        If the pasted text carries a ``state`` parameter and ``expected_state``
        is given, a mismatch is rejected (anti-CSRF, RFC 8252 §8.9). A bare
        pasted code has no state to check; it is accepted as user-mediated.
        """
        if not text:
            return None
        text = text.strip()
        if expected_state is not None and "state=" in text:
            query = urlparse(text).query or text
            states = parse_qs(query).get("state")
            pasted_state = states[0] if states else None
            if pasted_state is None or not secrets.compare_digest(pasted_state, expected_state):
                return None
        if "code=" in text:
            query = urlparse(text).query or text
            codes = parse_qs(query).get("code")
            if codes:
                return codes[0]
            match = re.search(r"code=([^&\s]+)", text)
            return match.group(1) if match else None
        # Assume the user pasted the bare code (Strava codes have no spaces).
        return text if text and " " not in text else None

    def _capture_code(self, server: HTTPServer, auth_url: str, timeout: int) -> str | None:
        """Open the browser and wait for the loopback callback to deliver a code."""
        try:
            self.logger.info(msg="Opening Strava authorization page in your browser...")
            webbrowser.open(auth_url)

            deadline = time.time() + timeout
            while (
                _CallbackHandler.auth_code is None
                and _CallbackHandler.auth_error is None
                and time.time() < deadline
            ):
                try:
                    server.handle_request()
                except TimeoutError:
                    continue
        except OSError as e:
            self.logger.error(msg=f"Local callback server error: {e}")
        finally:
            server.server_close()

        if _CallbackHandler.auth_error:
            self.logger.error(msg=f"Authorization denied: {_CallbackHandler.auth_error}")
        return _CallbackHandler.auth_code

    def start_auth_flow(
        self, manual_code_provider: ManualCodeProvider | None = None
    ) -> dict[str, Any] | None:
        """Run the browser authorization flow and exchange the code for tokens.

        Args:
            manual_code_provider: Optional callback invoked with the auth URL if
                the automatic callback doesn't arrive; it should return the
                pasted redirect URL or bare code (or None if cancelled).

        Returns:
            Dict with ``access_token``, ``refresh_token`` and ``expires_at`` on
            success, or None on failure/timeout.
        """
        server = self._start_server()
        if server is None:
            self.logger.error(msg="Could not find a free local port for the callback.")
            return None

        # Fresh anti-CSRF state per flow; the callback rejects mismatches.
        state = secrets.token_urlsafe(32)
        _CallbackHandler.auth_code = None
        _CallbackHandler.auth_error = None
        _CallbackHandler.expected_state = state

        redirect_uri = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            auth_url = strava_api.build_authorize_url(
                client_id=self.client_id,
                redirect_uri=redirect_uri,
                scope=SCOPES,
                state=state,
            )
        except (ValueError, TypeError) as e:
            self.logger.error(msg=f"Invalid Strava client id: {e}")
            server.server_close()
            return None

        timeout = AUTO_CAPTURE_TIMEOUT if manual_code_provider else AUTO_ONLY_TIMEOUT
        code = self._capture_code(server, auth_url, timeout)

        if code is None and manual_code_provider is not None:
            self.logger.info(msg="Automatic callback not received; requesting manual code entry.")
            code = self.extract_code(manual_code_provider(auth_url), expected_state=state)

        if code is None:
            self.logger.error(msg="No authorization code received.")
            return None

        try:
            info = strava_api.exchange_code_for_token(
                client_id=self.client_id,
                client_secret=self.client_secret,
                code=code,
            )
            self.logger.info(msg="Successfully obtained Strava tokens.")
            return {
                "access_token": info["access_token"],
                "refresh_token": info["refresh_token"],
                "expires_at": int(info["expires_at"]),
            }
        except Exception as e:
            self.logger.error(msg=f"Token exchange failed: {e}")
            return None
