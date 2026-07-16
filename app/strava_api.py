"""Minimal Strava REST client built directly on ``requests``.

Replaces the ``stravalib`` dependency (which dragged ``pydantic`` +
``pydantic_core`` into the packaged app) for the handful of endpoints KayakFit
actually uses: the OAuth authorize URL, the token endpoint (authorization-code
exchange and refresh), the activity upload endpoint, and the upload/activity
read-backs. All of these are simple, well-documented calls.

This module is the single HTTP seam: it owns the endpoint URLs, turns any
non-success into a clear :class:`StravaApiError` (HTTP status, network error,
rate limiting, or malformed JSON), and is the only place tests need to stub
``requests``. The higher-level flow (browser callback, token-refresh margin,
upload polling) stays in ``strava_auth`` / ``strava_uploader``.
"""

from collections.abc import Sequence
from typing import Any, cast
from urllib.parse import urlencode

# Explicit re-export (redundant alias) so mypy --strict's no_implicit_reexport
# treats `strava_api.requests` as public: the test suite monkeypatches this
# attribute to swap in a fake requests module.
import requests as requests

# ---- endpoints -------------------------------------------------------------
AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
UPLOADS_URL = "https://www.strava.com/api/v3/uploads"
ACTIVITIES_URL = "https://www.strava.com/api/v3/activities"

# Default per-request timeout (seconds). Uploads/polls are short REST calls.
DEFAULT_TIMEOUT = 30


class StravaApiError(Exception):
    """Any non-successful Strava interaction.

    Covers an HTTP error, network failure, rate limiting, or a
    malformed/unexpected response body.
    """


class ActivityUploadError(StravaApiError):
    """Strava accepted the upload but reported an error while processing it.

    The upload object's ``error`` field is set. Equivalent to stravalib's
    ``exc.ActivityUploadError``.
    """


class UploadTimeoutError(StravaApiError):
    """Client-side timeout while Strava was still processing the upload.

    Raised when the caller's poll window elapsed before Strava finished.
    Equivalent to stravalib's ``exc.TimeoutExceeded``.
    """


def _message_from_response(resp: requests.Response) -> str:
    """Build the clearest human-readable message a Strava error response offers.

    Strava error bodies look like ``{"message": "Bad Request", "errors":
    [{"resource": "AuthorizationCode", "field": "code", "code": "invalid"}]}``.
    Fall back to the raw body (truncated) or the bare status code.
    """
    try:
        payload = resp.json()
    except ValueError:
        text = (resp.text or "").strip()
        return text[:200] if text else f"HTTP {resp.status_code}"

    if isinstance(payload, dict):
        parts: list[str] = []
        message = payload.get("message")
        if message:
            parts.append(str(message))
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            detail = "; ".join(
                " ".join(
                    str(v)
                    for v in (
                        e.get("resource"),
                        e.get("field"),
                        e.get("code"),
                    )
                    if v
                )
                for e in errors
                if isinstance(e, dict)
            ).strip()
            if detail:
                parts.append(f"({detail})")
        if parts:
            return " ".join(parts)
    return f"HTTP {resp.status_code}"


def _request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    data: dict[str, Any] | None = None,
    files: dict[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Perform one Strava request and return the parsed JSON body.

    Raises:
        StravaApiError: on a network error, a rate-limit (429), any non-2xx
            status, or a body that is not valid JSON.
    """
    try:
        resp = requests.request(
            method,
            url,
            headers=headers,
            data=data,
            files=files,
            timeout=timeout,
        )
    except requests.exceptions.RequestException as e:
        raise StravaApiError(f"Network error contacting Strava: {e}") from e

    if resp.status_code == 429:
        raise StravaApiError(
            "Strava rate limit exceeded (HTTP 429). Please try again later."
        )
    if not resp.ok:
        raise StravaApiError(
            f"Strava API error (HTTP {resp.status_code}): "
            f"{_message_from_response(resp)}"
        )

    try:
        # resp.json() is typed Any; the Strava endpoints we call return JSON
        # objects, so narrow to the declared dict return.
        return cast(dict[str, Any], resp.json())
    except ValueError as e:
        raise StravaApiError(f"Strava returned a malformed response: {e}") from e


def _extract_tokens(payload: Any) -> dict[str, Any]:
    """Pull the token triple out of a token-endpoint response."""
    try:
        return {
            "access_token": payload["access_token"],
            "refresh_token": payload["refresh_token"],
            "expires_at": int(payload["expires_at"]),
        }
    except (KeyError, TypeError, ValueError) as e:
        raise StravaApiError(
            f"Strava token response missing expected fields: {e}"
        ) from e


def build_authorize_url(
    client_id: Any,
    redirect_uri: str,
    scope: Sequence[str],
    state: str,
    approval_prompt: str = "auto",
) -> str:
    """Build Strava's OAuth authorization URL.

    Mirrors stravalib's ``Client.authorization_url``: scope is comma-joined and
    ``approval_prompt`` defaults to ``"auto"``. ``client_id`` is coerced to int
    so a non-numeric value fails fast (the caller reports it as an invalid id).
    """
    params = {
        "client_id": int(client_id),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "approval_prompt": approval_prompt,
        "scope": ",".join(scope),
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def exchange_code_for_token(
    client_id: Any, client_secret: str, code: str
) -> dict[str, Any]:
    """Exchange an authorization code for tokens (grant_type=authorization_code)."""
    payload = _request(
        "POST",
        TOKEN_URL,
        data={
            "client_id": int(client_id),
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
        },
    )
    return _extract_tokens(payload)


def refresh_access_token(
    client_id: Any, client_secret: str, refresh_token: str
) -> dict[str, Any]:
    """Refresh an access token (grant_type=refresh_token)."""
    payload = _request(
        "POST",
        TOKEN_URL,
        data={
            "client_id": int(client_id),
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
    )
    return _extract_tokens(payload)


def upload_activity(
    access_token: str,
    file_obj: Any,
    data_type: str,
    name: str | None = None,
    description: str | None = None,
    external_id: str | None = None,
) -> dict[str, Any]:
    """POST an activity file to Strava's upload endpoint.

    Returns the upload object (``{"id": ..., "status": ..., "error": ...,
    "activity_id": ...}``); ``activity_id`` is null until processing finishes.
    """
    data: dict[str, Any] = {"data_type": data_type}
    if name is not None:
        data["name"] = name
    if description is not None:
        data["description"] = description
    if external_id is not None:
        data["external_id"] = external_id
    return _request(
        "POST",
        UPLOADS_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        data=data,
        files={"file": file_obj},
    )


def get_upload(access_token: str, upload_id: Any) -> dict[str, Any]:
    """Read back an upload's processing status."""
    return _request(
        "GET",
        f"{UPLOADS_URL}/{upload_id}",
        headers={"Authorization": f"Bearer {access_token}"},
    )


def get_activity(access_token: str, activity_id: Any) -> dict[str, Any]:
    """Read back a created activity (used to confirm the final activity id)."""
    return _request(
        "GET",
        f"{ACTIVITIES_URL}/{activity_id}",
        headers={"Authorization": f"Bearer {access_token}"},
    )
