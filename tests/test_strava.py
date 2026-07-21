"""
Tests for the direct-``requests`` Strava layer (app/strava_api.py) and the
uploader/auth flows built on it (app/strava_uploader.py, app/strava_auth.py),
after removing the ``stravalib`` dependency.

All HTTP is mocked -- ``app.strava_api.requests`` is swapped for a fake that
routes calls to a per-test handler. No network access and no real Strava
credentials are used (and none are available in this sandbox); see the
NOTE at the bottom about real end-to-end verification.

Run from the project root:

    python tests/test_strava.py
"""

import http.client
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

import app.strava_uploader as strava_uploader
from app import strava_api
from app.strava_auth import StravaAuth, _CallbackHandler
from app.strava_uploader import StravaUploader

_UNSET = object()


class FakeResponse:
    """Stand-in for requests.Response."""

    def __init__(self, status=200, json_data=_UNSET, text=""):
        self.status_code = status
        self._json = json_data
        self.text = text
        self.ok = 200 <= status < 300

    def json(self):
        if self._json is _UNSET:
            raise ValueError("No JSON object could be decoded")
        return self._json


class FakeRequests:
    """Drop-in for the ``requests`` module used inside app.strava_api."""

    exceptions = requests.exceptions  # real exception classes

    def __init__(self, router):
        self._router = router
        self.calls = []

    def request(self, method, url, headers=None, data=None, files=None, timeout=None):
        self.calls.append(
            {"method": method, "url": url, "headers": headers, "data": data, "files": files}
        )
        return self._router(self, method, url, data)


def _install(router):
    """Swap in a FakeRequests; return the fake and the original for restore."""
    original = strava_api.requests
    fake = FakeRequests(router)
    strava_api.requests = fake  # type: ignore[assignment]
    return fake, original


# --------------------------------------------------------------------------
# strava_api: URL building + token endpoints
# --------------------------------------------------------------------------
def test_build_authorize_url():
    url = strava_api.build_authorize_url(
        client_id="12345",
        redirect_uri="http://127.0.0.1:8000",
        scope=["activity:write", "activity:read"],
        state="abc123",
    )
    assert url.startswith("https://www.strava.com/oauth/authorize?")
    q = parse_qs(urlparse(url).query)
    assert q["client_id"] == ["12345"]
    assert q["redirect_uri"] == ["http://127.0.0.1:8000"]
    assert q["response_type"] == ["code"]
    assert q["approval_prompt"] == ["auto"]
    # Scope is comma-joined (matching stravalib / Strava's API).
    assert q["scope"] == ["activity:write,activity:read"]
    assert q["state"] == ["abc123"]
    print("test_build_authorize_url passed")


def test_build_authorize_url_bad_client_id():
    try:
        strava_api.build_authorize_url(
            client_id="not-a-number",
            redirect_uri="http://127.0.0.1:8000",
            scope=["activity:write"],
            state="s",
        )
    except (ValueError, TypeError):
        print("test_build_authorize_url_bad_client_id passed")
        return
    raise AssertionError("expected ValueError/TypeError for non-numeric client id")


def test_oauth_error_callback_requires_matching_state():
    server = StravaAuth._start_server(start_port=18000, max_attempts=20)
    assert server is not None
    _CallbackHandler.auth_error = None
    _CallbackHandler.expected_state = "expected"

    thread = threading.Thread(target=server.handle_request)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request("GET", "/?error=access_denied&state=wrong")
        response = connection.getresponse()
        assert response.status == 400
        response.read()
    finally:
        connection.close()
        thread.join(timeout=2)
        server.server_close()

    assert _CallbackHandler.auth_error is None


def test_exchange_code_success():
    def router(fake, method, url, data):
        assert method == "POST" and url == strava_api.TOKEN_URL
        assert data["grant_type"] == "authorization_code"
        assert data["code"] == "goodcode"
        return FakeResponse(
            200,
            {
                "access_token": "AT",
                "refresh_token": "RT",
                "expires_at": 1_800_000_000,
                "extra": "ignored",
            },
        )

    _, original = _install(router)
    try:
        tokens = strava_api.exchange_code_for_token("123", "secret", "goodcode")
        assert tokens == {
            "access_token": "AT",
            "refresh_token": "RT",
            "expires_at": 1_800_000_000,
        }
    finally:
        strava_api.requests = original
    print("test_exchange_code_success passed")


def test_exchange_code_rejected():
    """An expired/invalid code -> Strava 400 -> StravaApiError with detail."""

    def router(fake, method, url, data):
        return FakeResponse(
            400,
            {
                "message": "Bad Request",
                "errors": [
                    {"resource": "AuthorizationCode", "field": "code", "code": "invalid"}
                ],
            },
        )

    _, original = _install(router)
    try:
        try:
            strava_api.exchange_code_for_token("123", "secret", "expired")
        except strava_api.StravaApiError as e:
            msg = str(e)
            assert "400" in msg and "invalid" in msg, msg
        else:
            raise AssertionError("expected StravaApiError for a rejected code")
    finally:
        strava_api.requests = original
    print("test_exchange_code_rejected passed")


def test_refresh_success():
    def router(fake, method, url, data):
        assert data["grant_type"] == "refresh_token"
        assert data["refresh_token"] == "RT"
        return FakeResponse(
            200,
            {"access_token": "AT2", "refresh_token": "RT2", "expires_at": 1_900_000_000},
        )

    _, original = _install(router)
    try:
        tokens = strava_api.refresh_access_token("123", "secret", "RT")
        assert tokens["access_token"] == "AT2"
        assert tokens["refresh_token"] == "RT2"
        assert tokens["expires_at"] == 1_900_000_000
    finally:
        strava_api.requests = original
    print("test_refresh_success passed")


def test_network_error_becomes_api_error():
    def router(fake, method, url, data):
        raise requests.exceptions.ConnectionError("boom")

    _, original = _install(router)
    try:
        try:
            strava_api.refresh_access_token("123", "secret", "RT")
        except strava_api.StravaApiError as e:
            assert "Network error" in str(e)
        else:
            raise AssertionError("expected StravaApiError on network failure")
    finally:
        strava_api.requests = original
    print("test_network_error_becomes_api_error passed")


def test_rate_limit_becomes_api_error():
    def router(fake, method, url, data):
        return FakeResponse(429, {"message": "Rate Limit Exceeded"})

    _, original = _install(router)
    try:
        try:
            strava_api.refresh_access_token("123", "secret", "RT")
        except strava_api.StravaApiError as e:
            assert "429" in str(e)
        else:
            raise AssertionError("expected StravaApiError on HTTP 429")
    finally:
        strava_api.requests = original
    print("test_rate_limit_becomes_api_error passed")


# --------------------------------------------------------------------------
# StravaUploader: upload flow scenarios
# --------------------------------------------------------------------------
def _uploader(access_token="AT", refresh_token="RT", expires_at=9_999_999_999):
    return StravaUploader(
        access_token=access_token,
        refresh_token=refresh_token,
        client_id="123",
        client_secret="secret",
        config={"log_level": "warning"},
        expires_at=expires_at,
    )


def _fit_file(tmpdir):
    p = Path(tmpdir) / "workout.fit"
    p.write_bytes(b"\x0e\x10FIT dummy bytes")
    return p


def test_upload_success_poll_to_ready():
    state = {"polls": 0}

    def router(fake, method, url, data):
        if url == strava_api.UPLOADS_URL and method == "POST":
            return FakeResponse(201, {"id": 555, "status": "processing", "activity_id": None})
        if url.startswith(strava_api.UPLOADS_URL + "/") and method == "GET":
            state["polls"] += 1
            if state["polls"] < 2:
                return FakeResponse(200, {"id": 555, "error": None, "activity_id": None})
            return FakeResponse(200, {"id": 555, "error": None, "activity_id": 999})
        if url.startswith(strava_api.ACTIVITIES_URL + "/") and method == "GET":
            return FakeResponse(200, {"id": 999, "name": "Kayak"})
        raise AssertionError(f"unexpected call {method} {url}")

    _, original = _install(router)
    saved_interval = strava_uploader.UPLOAD_POLL_INTERVAL
    strava_uploader.UPLOAD_POLL_INTERVAL = 0
    try:
        with tempfile.TemporaryDirectory() as d:
            result = _uploader().upload_file(str(_fit_file(d)), name="Kayak")
        assert result["success"] is True, result
        assert result["activity_id"] == 999, result
        assert "Activity ID: 999" in result["message"]
        assert state["polls"] >= 2
    finally:
        strava_uploader.UPLOAD_POLL_INTERVAL = saved_interval
        strava_api.requests = original
    print("test_upload_success_poll_to_ready passed")


def test_workout_directory_is_the_strava_external_id():
    """Fixed ``activity.fit`` names must still produce unique uploads."""
    def router(fake, method, url, data):
        if url == strava_api.UPLOADS_URL and method == "POST":
            return FakeResponse(201, {"id": 12, "activity_id": 99})
        if url.startswith(strava_api.UPLOADS_URL + "/") and method == "GET":
            return FakeResponse(200, {"id": 12, "error": None, "activity_id": 99})
        if url.startswith(strava_api.ACTIVITIES_URL + "/") and method == "GET":
            return FakeResponse(200, {"id": 99, "name": "Kayak"})
        raise AssertionError(f"unexpected call {method} {url}")

    fake, original = _install(router)
    try:
        with tempfile.TemporaryDirectory() as d:
            workout_dir = Path(d) / "workout_20260714_120000"
            workout_dir.mkdir()
            fit_path = workout_dir / "activity.fit"
            fit_path.write_bytes(b"\x0e\x10FIT dummy bytes")
            result = _uploader().upload_file(str(fit_path))
        assert result["success"] is True, result
        assert fake.calls[0]["data"]["external_id"] == workout_dir.name
    finally:
        strava_api.requests = original


def test_upload_rejected():
    """Strava reports a processing error -> equivalent of ActivityUploadError."""

    def router(fake, method, url, data):
        if url == strava_api.UPLOADS_URL and method == "POST":
            return FakeResponse(201, {"id": 7, "activity_id": None})
        if url.startswith(strava_api.UPLOADS_URL + "/") and method == "GET":
            return FakeResponse(
                200, {"id": 7, "error": "Corrupt FIT file", "activity_id": None}
            )
        raise AssertionError(f"unexpected call {method} {url}")

    _, original = _install(router)
    strava_uploader.UPLOAD_POLL_INTERVAL = 0
    try:
        with tempfile.TemporaryDirectory() as d:
            result = _uploader().upload_file(str(_fit_file(d)))
        assert result["success"] is False, result
        assert "Strava rejected the upload" in result["message"]
        assert "Corrupt FIT file" in result["message"]
    finally:
        strava_api.requests = original
    print("test_upload_rejected passed")


def test_upload_timeout_still_processing():
    """Poll window elapses before Strava finishes -> success, still processing."""

    def router(fake, method, url, data):
        if url == strava_api.UPLOADS_URL and method == "POST":
            return FakeResponse(201, {"id": 42, "activity_id": None})
        if url.startswith(strava_api.UPLOADS_URL + "/") and method == "GET":
            return FakeResponse(200, {"id": 42, "error": None, "activity_id": None})
        raise AssertionError(f"unexpected call {method} {url}")

    _, original = _install(router)
    saved_timeout = strava_uploader.UPLOAD_WAIT_TIMEOUT
    strava_uploader.UPLOAD_WAIT_TIMEOUT = 0  # first poll then immediate timeout
    strava_uploader.UPLOAD_POLL_INTERVAL = 0
    try:
        with tempfile.TemporaryDirectory() as d:
            result = _uploader().upload_file(str(_fit_file(d)))
        assert result["success"] is True, result
        assert result["activity_id"] is None
        assert "still processing" in result["message"].lower()
    finally:
        strava_uploader.UPLOAD_WAIT_TIMEOUT = saved_timeout
        strava_api.requests = original
    print("test_upload_timeout_still_processing passed")


def test_upload_readback_fails_but_activity_exists():
    """Polling assigns an activity id, but the final /activities read-back
    fails -> still treated as a successful upload (with the polled id)."""

    def router(fake, method, url, data):
        if url == strava_api.UPLOADS_URL and method == "POST":
            return FakeResponse(201, {"id": 8, "activity_id": None})
        if url.startswith(strava_api.UPLOADS_URL + "/") and method == "GET":
            return FakeResponse(200, {"id": 8, "error": None, "activity_id": 314})
        if url.startswith(strava_api.ACTIVITIES_URL + "/") and method == "GET":
            # e.g. token without activity:read scope, or eventual consistency.
            return FakeResponse(403, {"message": "Authorization Error"})
        raise AssertionError(f"unexpected call {method} {url}")

    _, original = _install(router)
    strava_uploader.UPLOAD_POLL_INTERVAL = 0
    try:
        with tempfile.TemporaryDirectory() as d:
            result = _uploader().upload_file(str(_fit_file(d)))
        assert result["success"] is True, result
        assert result["activity_id"] == 314, result
    finally:
        strava_api.requests = original
    print("test_upload_readback_fails_but_activity_exists passed")


def test_upload_file_not_found():
    result = _uploader().upload_file("/no/such/file.fit")
    assert result["success"] is False
    assert "File not found" in result["message"]
    print("test_upload_file_not_found passed")


def test_upload_token_refresh_failed():
    """No usable token and refresh impossible -> clean failure, no HTTP call."""

    def router(fake, method, url, data):
        raise AssertionError("no request should be made without a token")

    _, original = _install(router)
    try:
        with tempfile.TemporaryDirectory() as d:
            uploader = _uploader(access_token="", refresh_token="", expires_at=0)
            result = uploader.upload_file(str(_fit_file(d)))
        assert result["success"] is False
        assert "reconnect" in result["message"].lower()
    finally:
        strava_api.requests = original
    print("test_upload_token_refresh_failed passed")


def test_uploader_refreshes_expired_token():
    """An expired access token is refreshed before uploading."""
    calls = {"refresh": 0}

    def router(fake, method, url, data):
        if url == strava_api.TOKEN_URL and method == "POST":
            calls["refresh"] += 1
            return FakeResponse(
                200,
                {"access_token": "NEW", "refresh_token": "NEWRT", "expires_at": 9_999_999_999},
            )
        if url == strava_api.UPLOADS_URL and method == "POST":
            # Must be using the refreshed token.
            assert fake.calls[-1]["headers"]["Authorization"] == "Bearer NEW"
            return FakeResponse(201, {"id": 1, "activity_id": None})
        if url.startswith(strava_api.UPLOADS_URL + "/") and method == "GET":
            return FakeResponse(200, {"id": 1, "error": None, "activity_id": 77})
        if url.startswith(strava_api.ACTIVITIES_URL + "/") and method == "GET":
            return FakeResponse(200, {"id": 77})
        raise AssertionError(f"unexpected call {method} {url}")

    _, original = _install(router)
    strava_uploader.UPLOAD_POLL_INTERVAL = 0
    try:
        with tempfile.TemporaryDirectory() as d:
            uploader = _uploader(access_token="OLD", refresh_token="RT", expires_at=1)
            result = uploader.upload_file(str(_fit_file(d)))
        assert result["success"] is True, result
        assert result["activity_id"] == 77
        assert calls["refresh"] == 1
        assert uploader.get_updated_tokens() == ("NEW", "NEWRT", 9_999_999_999)
    finally:
        strava_api.requests = original
    print("test_uploader_refreshes_expired_token passed")
