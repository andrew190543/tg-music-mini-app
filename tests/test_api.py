import hashlib
import hmac
import json
import time
from urllib.parse import quote

import httpx
import pytest
from fastapi.testclient import TestClient

import backend.api as api_module
from backend.api import create_app
from backend.audio_service import AudioServiceError, AudioStream

AUTH_BOT_TOKEN = "7000000000:AAExampleTokenForTests"


def _signed_init_data(user_id: int, bot_token: str = AUTH_BOT_TOKEN) -> str:
    """Build a valid signed initData string for the given user."""
    pairs = [
        ("auth_date", str(int(time.time()))),
        ("user", json.dumps({"id": user_id}, separators=(", ", ": "))),
    ]
    check_string = "\n".join(sorted(f"{k}={v}" for k, v in pairs))
    secret = hmac.new(
        b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256
    ).digest()
    computed = hmac.new(
        secret, check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    encoded = "&".join(
        f"{key}={quote(value, safe='')}" for key, value in pairs
    )
    return f"{encoded}&hash={computed}"


def test_health_frontend_cors_and_root_redirect(tmp_path):
    database_path = tmp_path / "api.db"
    app = create_app(db_path=database_path)

    with TestClient(app) as client:
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json() == {
            "status": "ok",
            "service": "tg-music",
            "version": "0.1.0",
            "database": "ok",
        }

        root_health = client.get("/health")
        assert root_health.status_code == 200
        assert root_health.json() == {"status": "ok"}

        index = client.get("/app/")
        assert index.status_code == 200
        assert "TG Music" in index.text
        assert "./app.js" in index.text
        assert 'id="library-list"' in index.text
        assert 'id="player-bar" hidden' in index.text
        assert "Neon Skyline" not in index.text
        assert "Aurora Wave" not in index.text

        script = client.get("/app/app.js")
        assert script.status_code == 200
        assert "api/health" in script.text
        assert "/api/search" in script.text
        assert "/api/trending" in script.text
        assert "/api/favorites" in script.text
        assert "initDataUnsafe" in script.text
        assert "123456" in script.text
        assert "demo-" not in script.text
        assert "FALLBACK_TRACKS" not in script.text
        assert "jfKfPfyJRdk" not in script.text
        assert "showCatalogSpinner" in script.text

        styles = client.get("/app/style.css")
        assert styles.status_code == 200
        assert "--tg-bg" in styles.text

        root = client.get("/", follow_redirects=False)
        assert root.status_code in (307, 308)
        assert root.headers["location"] == "/app/"

        cors = client.get(
            "/api/health", headers={"Origin": "https://example.com"}
        )
        assert cors.headers["access-control-allow-origin"] == "*"

    assert database_path.is_file()


def test_trending_endpoint_returns_cached_tracks(tmp_path, monkeypatch):
    app = create_app(db_path=tmp_path / "api.db")
    calls = {}

    async def fake_trending(limit=20):
        calls["limit"] = limit
        return [
            {
                "track_id": "trend1",
                "title": "Trending Song",
                "artist": "Trend Artist",
                "duration": 210,
                "thumbnail": "https://img/trend.jpg",
            }
        ]

    monkeypatch.setattr(api_module, "get_trending_tracks", fake_trending)

    with TestClient(app) as client:
        response = client.get("/api/trending")
        assert response.status_code == 200
        assert response.json() == [
            {
                "track_id": "trend1",
                "title": "Trending Song",
                "artist": "Trend Artist",
                "duration": 210,
                "thumbnail": "https://img/trend.jpg",
            }
        ]
        assert calls["limit"] == 20

        limited = client.get("/api/trending", params={"limit": 5})
        assert limited.status_code == 200
        assert calls["limit"] == 5

        assert client.get("/api/trending", params={"limit": 0}).status_code == 422
        assert (
            client.get("/api/trending", params={"limit": 51}).status_code
            == 422
        )


def test_trending_endpoint_propagates_service_results(tmp_path, monkeypatch):
    app = create_app(db_path=tmp_path / "api.db")

    async def empty_trending(limit=20):
        return []

    monkeypatch.setattr(api_module, "get_trending_tracks", empty_trending)

    with TestClient(app) as client:
        response = client.get("/api/trending")
        assert response.status_code == 200
        assert response.json() == []


def test_missing_frontend_is_reported(tmp_path):
    with pytest.raises(RuntimeError, match="index.html"):
        create_app(db_path=tmp_path / "api.db", frontend_dir=tmp_path)


def test_search_endpoint_validates_and_normalizes(tmp_path, monkeypatch):
    app = create_app(db_path=tmp_path / "api.db")
    calls = {}

    async def fake_search(query, limit=15):
        calls["query"] = query
        calls["limit"] = limit
        return [
            {
                "track_id": "abc",
                "title": "Song",
                "artist": "Artist",
                "duration": 100,
                "thumbnail": "https://img/cover.jpg",
            }
        ]

    monkeypatch.setattr(api_module, "search_tracks", fake_search)

    with TestClient(app) as client:
        response = client.get(
            "/api/search", params={"q": "  Daft   Punk  ", "limit": 5}
        )
        assert response.status_code == 200
        assert response.json() == [
            {
                "track_id": "abc",
                "title": "Song",
                "artist": "Artist",
                "duration": 100,
                "thumbnail": "https://img/cover.jpg",
            }
        ]
        assert calls == {"query": "Daft Punk", "limit": 5}

        assert client.get("/api/search").status_code == 422
        assert client.get("/api/search", params={"q": ""}).status_code == 422
        assert (
            client.get("/api/search", params={"q": "   "}).status_code == 422
        )
        assert (
            client.get("/api/search", params={"q": "x", "limit": 0}).status_code
            == 422
        )
        assert (
            client.get("/api/search", params={"q": "x", "limit": 51}).status_code
            == 422
        )


def test_search_endpoint_maps_service_errors_to_502(tmp_path, monkeypatch):
    app = create_app(db_path=tmp_path / "api.db")

    async def failing_search(query, limit=15):
        raise AudioServiceError("search failed")

    monkeypatch.setattr(api_module, "search_tracks", failing_search)

    with TestClient(app) as client:
        response = client.get("/api/search", params={"q": "x"})
        assert response.status_code == 502
        assert "search failed" in response.json()["detail"]


def test_stream_endpoint_proxies_range(tmp_path, monkeypatch):
    app = create_app(db_path=tmp_path / "api.db")
    captured = {}

    async def fake_get_url(track_id):
        captured["track_id"] = track_id
        return "https://cdn.example/audio"

    async def fake_body():
        yield b"0123456789"

    async def fake_start(url, range_header=None, **kwargs):
        captured["url"] = url
        captured["range"] = range_header
        return AudioStream(
            status_code=206,
            headers={
                "Content-Type": "audio/mp4",
                "Content-Range": "bytes 0-9/100",
                "Content-Length": "10",
                "Accept-Ranges": "bytes",
            },
            media_type="audio/mp4",
            body=fake_body(),
        )

    monkeypatch.setattr(api_module, "get_audio_stream_url", fake_get_url)
    monkeypatch.setattr(api_module, "start_audio_stream", fake_start)

    with TestClient(app) as client:
        response = client.get(
            "/api/stream/abc123", headers={"Range": "bytes=0-9"}
        )

        assert response.status_code == 206
        assert response.content == b"0123456789"
        assert response.headers["content-range"] == "bytes 0-9/100"
        assert response.headers["accept-ranges"] == "bytes"
        assert response.headers["content-type"] == "audio/mp4"
        assert captured == {
            "track_id": "abc123",
            "url": "https://cdn.example/audio",
            "range": "bytes=0-9",
        }


def test_stream_endpoint_maps_service_and_range_errors(tmp_path, monkeypatch):
    app = create_app(db_path=tmp_path / "api.db")

    async def failing_url(track_id):
        raise AudioServiceError("no stream")

    monkeypatch.setattr(api_module, "get_audio_stream_url", failing_url)

    with TestClient(app) as client:
        assert client.get("/api/stream/abc").status_code == 502

    async def working_url(track_id):
        return "https://cdn.example/audio"

    async def failing_range(url, range_header=None, **kwargs):
        request = httpx.Request("GET", url)
        response = httpx.Response(416, request=request)
        raise httpx.HTTPStatusError(
            "range not satisfiable", request=request, response=response
        )

    monkeypatch.setattr(api_module, "get_audio_stream_url", working_url)
    monkeypatch.setattr(api_module, "start_audio_stream", failing_range)

    with TestClient(app) as client:
        response = client.get(
            "/api/stream/abc", headers={"Range": "bytes=999-"}
        )
        assert response.status_code == 416


def test_favorites_crud_and_user_isolation(tmp_path):
    app = create_app(db_path=tmp_path / "api.db")
    payload = {
        "user_id": 42,
        "track_id": "abc123",
        "title": "Song A",
        "artist": "Artist A",
        "duration": 180,
        "thumbnail": "https://img/a.jpg",
    }

    with TestClient(app) as client:
        created = client.post("/api/favorites", json=payload)
        assert created.status_code == 200
        body = created.json()
        assert body["added"] is True
        assert (
            body["track"]["url"]
            == "https://www.youtube.com/watch?v=abc123"
        )

        duplicate = client.post("/api/favorites", json=payload)
        assert duplicate.status_code == 200
        assert duplicate.json()["added"] is False

        favorites = client.get("/api/favorites", params={"user_id": 42})
        assert favorites.status_code == 200
        tracks = favorites.json()
        assert len(tracks) == 1
        assert tracks[0]["track_id"] == "abc123"
        assert tracks[0]["title"] == "Song A"
        assert tracks[0]["duration"] == 180

        assert (
            client.get("/api/favorites", params={"user_id": 99}).json() == []
        )

        removed = client.delete(
            "/api/favorites/abc123", params={"user_id": 42}
        )
        assert removed.status_code == 200
        assert removed.json() == {"removed": True}

        again = client.delete(
            "/api/favorites/abc123", params={"user_id": 42}
        )
        assert again.json() == {"removed": False}
        assert (
            client.get("/api/favorites", params={"user_id": 42}).json() == []
        )


def test_favorites_validation_and_custom_url(tmp_path):
    app = create_app(db_path=tmp_path / "api.db")

    with TestClient(app) as client:
        assert client.get("/api/favorites").status_code == 422
        assert (
            client.get("/api/favorites", params={"user_id": 0}).status_code
            == 422
        )

        bad_user = client.post(
            "/api/favorites",
            json={"user_id": 0, "track_id": "x", "title": "T"},
        )
        assert bad_user.status_code == 422

        bad_title = client.post(
            "/api/favorites",
            json={"user_id": 1, "track_id": "x", "title": ""},
        )
        assert bad_title.status_code == 422

        custom_url = client.post(
            "/api/favorites",
            json={
                "user_id": 1,
                "track_id": "x",
                "title": "T",
                "url": "https://example.com/track",
            },
        )
        assert custom_url.status_code == 200
        assert (
            custom_url.json()["track"]["url"]
            == "https://example.com/track"
        )

        assert (
            client.delete(
                "/api/favorites/x", params={"user_id": 0}
            ).status_code
            == 422
        )


def test_favorites_require_signed_init_data_when_token_configured(tmp_path, monkeypatch):
    """With BOT_TOKEN set, the explicit user_id must never be trusted."""
    app = create_app(db_path=tmp_path / "api.db")
    monkeypatch.setattr(api_module, "get_bot_token", lambda: AUTH_BOT_TOKEN)

    with TestClient(app) as client:
        # A request without initData is rejected even with a valid user_id.
        assert (
            client.get("/api/favorites", params={"user_id": 42}).status_code
            == 401
        )
        # A tampered payload is rejected too.
        tampered = _signed_init_data(42).replace(
            quote('{"id": 42}', safe=""), quote('{"id": 99}', safe="")
        )
        assert tampered != _signed_init_data(42)
        assert (
            client.get(
                "/api/favorites",
                headers={"Authorization": f"tma {tampered}"},
            ).status_code
            == 401
        )

        # A valid signature authorizes the request; the user comes from the
        # signed payload (no explicit user_id needed anywhere).
        auth = {"Authorization": f"tma {_signed_init_data(42)}"}
        added = client.post(
            "/api/favorites",
            headers=auth,
            json={"track_id": "abc123", "title": "Song", "duration": 180},
        )
        assert added.status_code == 200

        favorites = client.get("/api/favorites", headers=auth)
        assert favorites.status_code == 200
        assert [track["track_id"] for track in favorites.json()] == ["abc123"]

        # Another signed user must not see user 42's tracks.
        other = client.get(
            "/api/favorites",
            headers={"Authorization": f"tma {_signed_init_data(99)}"},
        )
        assert other.status_code == 200
        assert other.json() == []

        removed = client.delete("/api/favorites/abc123", headers=auth)
        assert removed.status_code == 200
        assert removed.json() == {"removed": True}


def test_favorites_auth_mode_off_accepts_explicit_user_id(tmp_path, monkeypatch):
    """AUTH_MODE=off keeps the fallback even with a configured token."""
    app = create_app(db_path=tmp_path / "api.db")
    monkeypatch.setattr(api_module, "get_bot_token", lambda: AUTH_BOT_TOKEN)
    monkeypatch.setattr(api_module, "get_auth_mode", lambda: "off")

    with TestClient(app) as client:
        added = client.post(
            "/api/favorites",
            json={"user_id": 42, "track_id": "abc123", "title": "Song"},
        )
        assert added.status_code == 200

        favorites = client.get("/api/favorites", params={"user_id": 42})
        assert favorites.status_code == 200
        assert [track["track_id"] for track in favorites.json()] == ["abc123"]
