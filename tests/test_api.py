import asyncio
import hashlib
import hmac
import itertools
import json
import time
from urllib.parse import quote

import httpx
import pytest
from fastapi.testclient import TestClient

import backend.api as api_module
from backend.api import create_app
from backend.audio_service import AudioServiceError

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
        assert "<title>Музыка</title>" in index.text
        assert "./app.js" in index.text
        assert 'id="library-list"' in index.text
        assert 'id="player-bar" hidden' in index.text
        assert "Neon Skyline" not in index.text
        assert "Aurora Wave" not in index.text

        script = client.get("/app/app.js")
        assert script.status_code == 200
        assert "/api/stream/" in script.text
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


def test_stream_endpoint_serves_cached_file_with_range(tmp_path, monkeypatch):
    app = create_app(db_path=tmp_path / "api.db")
    captured = {}
    audio_file = tmp_path / "cached.m4a"
    audio_file.write_bytes(b"0123456789" * 10)

    async def fake_cached(track_id):
        captured["track_id"] = track_id
        return audio_file

    monkeypatch.setattr(api_module, "get_cached_audio_file", fake_cached)

    with TestClient(app) as client:
        ranged = client.get(
            "/api/stream/abc123", headers={"Range": "bytes=0-9"}
        )

        assert ranged.status_code == 206
        assert ranged.content == b"0123456789"
        assert ranged.headers["content-range"] == "bytes 0-9/100"
        assert ranged.headers["content-length"] == "10"
        assert ranged.headers["accept-ranges"] == "bytes"
        assert ranged.headers["content-type"] == "audio/mp4"
        assert captured == {"track_id": "abc123"}

        full = client.get("/api/stream/abc123")
        assert full.status_code == 200
        assert full.content == b"0123456789" * 10
        assert full.headers["content-length"] == "100"

        suffix = client.get("/api/stream/abc123", headers={"Range": "bytes=-10"})
        assert suffix.status_code == 206
        assert suffix.content == b"0123456789"
        assert suffix.headers["content-range"] == "bytes 90-99/100"


def test_stream_endpoint_maps_errors_and_unsatisfiable_range(
    tmp_path, monkeypatch
):
    app = create_app(db_path=tmp_path / "api.db")

    async def failing_cache(track_id):
        raise AudioServiceError("no stream")

    monkeypatch.setattr(api_module, "get_cached_audio_file", failing_cache)

    with TestClient(app) as client:
        assert client.get("/api/stream/abc").status_code == 502

    audio_file = tmp_path / "cached.m4a"
    audio_file.write_bytes(b"0123456789" * 10)

    async def cached(track_id):
        return audio_file

    monkeypatch.setattr(api_module, "get_cached_audio_file", cached)

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


def test_search_history_recorded_on_search(tmp_path, monkeypatch):
    """Successful searches with an identified user end up in the history."""
    app = create_app(db_path=tmp_path / "api.db")

    async def fake_search(query, limit=15):
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
        # Without an identified user nothing is stored...
        assert (
            client.get("/api/search", params={"q": "anonymous"}).status_code
            == 200
        )
        assert client.get("/api/search-history").status_code == 422

        history = client.get(
            "/api/search-history", params={"user_id": 42}
        )
        assert history.status_code == 200
        assert history.json() == []

        # ...but an identified search is remembered.
        assert (
            client.get(
                "/api/search", params={"q": "  Daft    Punk  ", "user_id": 42}
            ).status_code
            == 200
        )
        assert (
            client.get(
                "/api/search", params={"q": "Daft Punk", "user_id": 42}
            ).status_code
            == 200
        )

        history = client.get(
            "/api/search-history", params={"user_id": 42, "limit": 10}
        )
        assert history.status_code == 200
        queries = [item["query"] for item in history.json()]
        # The immediately repeated identical query is deduplicated.
        assert queries == ["Daft Punk"]
        assert all(item["created_at"] for item in history.json())

        # Failed searches (service error) are not recorded.
        async def failing_search(query, limit=15):
            raise AudioServiceError("boom")

        monkeypatch.setattr(api_module, "search_tracks", failing_search)
        client.get("/api/search", params={"q": "failing", "user_id": 42})

        history = client.get(
            "/api/search-history", params={"user_id": 42, "limit": 10}
        )
        assert [item["query"] for item in history.json()] == ["Daft Punk"]


def test_search_history_user_scoped_and_deletion(tmp_path, monkeypatch):
    app = create_app(db_path=tmp_path / "api.db")

    async def fake_search(query, limit=15):
        return []

    monkeypatch.setattr(api_module, "search_tracks", fake_search)

    with TestClient(app) as client:
        for user in (42, 99):
            for query in ("alpha", "beta"):
                client.get(
                    "/api/search",
                    params={"q": query, "user_id": user},
                )

        first = client.get("/api/search-history", params={"user_id": 42})
        assert first.status_code == 200
        assert [item["query"] for item in first.json()] == ["beta", "alpha"]

        # History is strictly per user.
        second = client.get("/api/search-history", params={"user_id": 99})
        assert [item["query"] for item in second.json()] == ["beta", "alpha"]

        # Delete one query only.
        one = client.delete(
            "/api/search-history", params={"user_id": 42, "query": "alpha"}
        )
        assert one.status_code == 200
        assert one.json() == {"deleted": 1}
        assert [
            item["query"]
            for item in client.get(
                "/api/search-history", params={"user_id": 42}
            ).json()
        ] == ["beta"]
        # The other user's history is untouched.
        assert len(
            client.get("/api/search-history", params={"user_id": 99}).json()
        ) == 2

        # Without a query the whole history goes away.
        everything = client.delete("/api/search-history", params={"user_id": 42})
        assert everything.status_code == 200
        assert everything.json() == {"deleted": 1}
        assert (
            client.get("/api/search-history", params={"user_id": 42}).json()
            == []
        )


def test_playlists_crud_and_user_isolation(tmp_path):
    app = create_app(db_path=tmp_path / "api.db")
    track_a = {"user_id": 42, "track_id": "yt:a", "title": "Song A", "artist": "A"}
    track_b = {"user_id": 42, "track_id": "yt:b", "title": "Song B"}

    with TestClient(app) as client:
        created = client.post(
            "/api/playlists", json={"user_id": 42, "title": "  My   Mix  "}
        )
        assert created.status_code == 200
        playlist = created.json()
        assert playlist["title"] == "My Mix"
        assert playlist["user_id"] == 42
        assert playlist["track_count"] == 0
        playlist_id = playlist["id"]

        # Empty title is rejected.
        assert (
            client.post(
                "/api/playlists", json={"user_id": 42, "title": "   "}
            ).status_code
            == 422
        )

        added = client.post(
            f"/api/playlists/{playlist_id}/tracks", json=track_a
        )
        assert added.status_code == 200
        assert added.json()["added"] is True

        duplicate = client.post(
            f"/api/playlists/{playlist_id}/tracks", json=track_a
        )
        assert duplicate.status_code == 200
        assert duplicate.json()["added"] is False

        client.post(f"/api/playlists/{playlist_id}/tracks", json=track_b)

        listing = client.get("/api/playlists", params={"user_id": 42})
        assert listing.status_code == 200
        assert len(listing.json()) == 1
        assert listing.json()[0]["track_count"] == 2

        tracks = client.get(
            f"/api/playlists/{playlist_id}/tracks", params={"user_id": 42}
        )
        assert tracks.status_code == 200
        assert [track["track_id"] for track in tracks.json()] == ["yt:a", "yt:b"]

        # Another user sees nothing and cannot touch user 42's playlist.
        assert (
            client.get("/api/playlists", params={"user_id": 99}).json() == []
        )
        assert (
            client.get(
                f"/api/playlists/{playlist_id}/tracks",
                params={"user_id": 99},
            ).status_code
            == 404
        )
        assert (
            client.post(
                f"/api/playlists/{playlist_id}/tracks",
                json={**track_a, "user_id": 99},
            ).status_code
            == 404
        )
        assert (
            client.delete(
                f"/api/playlists/{playlist_id}/tracks/yt:a",
                params={"user_id": 99},
            ).status_code
            == 404
        )
        assert (
            client.delete(
                f"/api/playlists/{playlist_id}", params={"user_id": 99}
            ).status_code
            == 404
        )

        removed = client.delete(
            f"/api/playlists/{playlist_id}/tracks/yt:a", params={"user_id": 42}
        )
        assert removed.status_code == 200
        assert removed.json() == {"removed": True}
        assert (
            client.delete(
                f"/api/playlists/{playlist_id}/tracks/yt:a",
                params={"user_id": 42},
            ).json()["removed"]
            is False
        )

        after = client.get(
            f"/api/playlists/{playlist_id}/tracks", params={"user_id": 42}
        )
        assert [track["track_id"] for track in after.json()] == ["yt:b"]

        deleted = client.delete(
            f"/api/playlists/{playlist_id}", params={"user_id": 42}
        )
        assert deleted.status_code == 200
        assert deleted.json() == {"removed": True}
        assert client.get("/api/playlists", params={"user_id": 42}).json() == []
        assert (
            client.get(
                f"/api/playlists/{playlist_id}/tracks", params={"user_id": 42}
            ).status_code
            == 404
        )


def test_playlists_validation_and_404s(tmp_path):
    app = create_app(db_path=tmp_path / "api.db")

    with TestClient(app) as client:
        # Missing/invalid user_id and title.
        assert client.get("/api/playlists").status_code == 422
        assert (
            client.get("/api/playlists", params={"user_id": 0}).status_code
            == 422
        )
        assert client.post("/api/playlists", json={"title": "X"}).status_code == 422
        assert (
            client.post(
                "/api/playlists", json={"user_id": 1, "title": "X" * 101}
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/playlists/1/tracks", json={"user_id": 1, "title": "T"}
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/playlists/1/tracks",
                json={"user_id": 1, "track_id": "a", "title": ""},
            ).status_code
            == 422
        )

        assert (
            client.get(
                "/api/playlists/999/tracks", params={"user_id": 42}
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/api/playlists/999/tracks",
                json={"user_id": 42, "track_id": "a", "title": "T"},
            ).status_code
            == 404
        )
        assert (
            client.delete(
                "/api/playlists/999", params={"user_id": 42}
            ).status_code
            == 404
        )
        assert (
            client.delete(
                "/api/playlists/1/tracks/", params={"user_id": 42}
            ).status_code
            in (404, 405)
        )


def test_playlists_and_history_strict_init_data_mode(tmp_path, monkeypatch):
    """With BOT_TOKEN configured, playlists/history follow initData only."""
    app = create_app(db_path=tmp_path / "api.db")
    monkeypatch.setattr(api_module, "get_bot_token", lambda: AUTH_BOT_TOKEN)

    auth = {"Authorization": f"tma {_signed_init_data(42)}"}
    other = {"Authorization": f"tma {_signed_init_data(99)}"}

    async def fake_search(query, limit=15):
        return [{"track_id": "abc", "title": "Song"}]

    monkeypatch.setattr(api_module, "search_tracks", fake_search)

    with TestClient(app) as client:
        # No initData -> 401 for both features.
        assert (
            client.get("/api/search-history", params={"user_id": 42}).status_code
            == 401
        )
        assert client.get("/api/playlists", params={"user_id": 42}).status_code == 401
        assert (
            client.post(
                "/api/playlists", json={"user_id": 42, "title": "X"}
            ).status_code
            == 401
        )

        # A signed search records the query for the signed user...
        client.get("/api/search", params={"q": "signed query"}, headers=auth)
        history = client.get("/api/search-history", headers=auth)
        assert history.status_code == 200
        assert [item["query"] for item in history.json()] == ["signed query"]
        # ...and never leaks into the anonymous fallback space.
        assert client.get("/api/search-history", params={"user_id": 42}) \
            .status_code == 401

        created = client.post(
            "/api/playlists", headers=auth, json={"title": "Mix"}
        )
        assert created.status_code == 200
        playlist_id = created.json()["id"]

        assert (
            client.post(
                f"/api/playlists/{playlist_id}/tracks",
                headers=auth,
                json={"track_id": "abc", "title": "Song"},
            ).status_code
            == 200
        )

        # Another signed user cannot see or delete user 42's playlist.
        assert client.get("/api/playlists", headers=other).json() == []
        assert (
            client.get(
                f"/api/playlists/{playlist_id}/tracks", headers=other
            ).status_code
            == 404
        )
        assert (
            client.delete(
                f"/api/playlists/{playlist_id}", headers=other
            ).status_code
            == 404
        )

        assert (
            client.delete(
                f"/api/playlists/{playlist_id}", headers=auth
            ).status_code
            == 200
        )


def test_frontend_surfaces_playlists_and_search_history(tmp_path):
    app = create_app(db_path=tmp_path / "api.db")

    with TestClient(app) as client:
        index = client.get("/app/").text
        script = client.get("/app/app.js").text
        styles = client.get("/app/style.css").text

        # The old placeholder is gone; playlists and history chips are in.
        assert "Плейлисты и история поиска появятся" not in index
        assert 'id="playlists-list"' in index
        assert 'id="search-history-chips"' in index
        assert 'id="playlist-menu"' in index
        assert 'id="full-add-playlist"' in index

        assert "/api/playlists" in script
        assert "/api/search-history" in script
        assert "search-history__chip" in script or "searchHistoryChips" in script
        assert "apiHeaders()" in script

        assert ".search-history__chip" in styles
        assert ".playlist-card" in styles
        assert ".playlist-menu" in styles
        assert ".library-hint" not in styles


def _wave_favorites(client, user_id: int, track_ids: list[str]) -> None:
    for track_id in track_ids:
        response = client.post(
            "/api/favorites",
            json={
                "user_id": user_id,
                "track_id": track_id,
                "title": f"Song {track_id}",
                "artist": "Favorite Artist",
                "duration": 210,
            },
        )
        assert response.status_code == 200


def test_wave_mood_endpoint_uses_service(tmp_path, monkeypatch):
    app = create_app(db_path=tmp_path / "api.db")
    captured: dict = {}

    async def fake_mood(vibe: str, limit: int):
        captured["vibe"] = vibe
        captured["limit"] = limit
        return [
            {
                "track_id": f"{vibe}-{index}",
                "title": f"Track {index}",
                "artist": "Wave Artist",
                "duration": 180 + index,
                "thumbnail": "",
            }
            for index in range(limit)
        ]

    async def fake_trending(limit: int):
        return [
            {
                "track_id": f"chart-{index}",
                "title": f"Chart {index}",
                "artist": "Chart",
                "duration": 180,
                "thumbnail": "",
            }
            for index in range(limit)
        ]

    async def failing_mood(vibe: str, limit: int):
        raise AudioServiceError("Сборка волны заняла слишком много времени")

    monkeypatch.setattr(api_module, "get_trending_tracks", fake_trending)
    monkeypatch.setattr(api_module, "get_wave_by_mood", fake_mood)

    with TestClient(app) as client:
        ok = client.get("/api/wave/mood", params={"vibe": "calm", "limit": 18})
        assert ok.status_code == 200, ok.text
        assert captured == {"vibe": "calm", "limit": 18}
        payload = ok.json()
        assert len(payload) == 18
        assert payload[0]["track_id"] == "calm-0"

        bad_vibe = client.get(
            "/api/wave/mood", params={"vibe": "euphoric", "limit": 18}
        )
        assert bad_vibe.status_code == 422

        monkeypatch.setattr(api_module, "get_wave_by_mood", failing_mood)
        broken = client.get("/api/wave/mood", params={"vibe": "sad"})
        assert broken.status_code == 502


def test_wave_personalized_uses_favorites_and_fallback(tmp_path, monkeypatch):
    app = create_app(db_path=tmp_path / "api.db")
    calls: dict = {}

    async def fake_personalized(favorites, limit: int):
        calls["favorites"] = [track.get("track_id") for track in favorites]
        calls["limit"] = limit
        if not favorites:
            # No taste profile: the empty result triggers the API-level
            # graceful fallback to the trending chart.
            return []
        # Echo back the favorites: enough to answer deterministically.
        return [
            {
                "track_id": track_id,
                "title": f"Personal {track_id}",
                "artist": "Personal Artist",
                "duration": 200,
                "thumbnail": "",
            }
            for track_id in calls["favorites"]
        ]

    async def fake_trending(limit: int):
        return [
            {
                "track_id": f"chart-{index}",
                "title": f"Chart {index}",
                "artist": "Chart",
                "duration": 180,
                "thumbnail": "",
            }
            for index in range(limit)
        ]

    monkeypatch.setattr(api_module, "get_trending_tracks", fake_trending)
    monkeypatch.setattr(api_module, "get_wave_personalized", fake_personalized)

    with TestClient(app) as client:
        _wave_favorites(client, 42, ["yt-like-1", "yt-like-2"])

        ok = client.get(
            "/api/wave/personalized", params={"limit": 18, "user_id": 42}
        )
        assert ok.status_code == 200, ok.text
        assert calls["limit"] == 18
        # Newest favorite first (backend order), same pair of tracks.
        assert sorted(calls["favorites"]) == ["yt-like-1", "yt-like-2"]
        favorite_tracks = set(calls.get("favorites", []))
        assert ok.json() and all(
            track["track_id"] in favorite_tracks for track in ok.json()
        )

        # The service returned nothing for this user (no favorites): the
        # graceful trending-chart fallback keeps the wave playable.
        other_user = client.get(
            "/api/wave/personalized", params={"limit": 5, "user_id": 43}
        )
        assert other_user.status_code == 200, other_user.text
        assert other_user.json()[0]["track_id"] == "chart-0"
        assert len(other_user.json()) == 5

    # The database file exists.
    assert (tmp_path / "api.db").is_file()


def test_wave_personalized_soft_user_id_never_422(tmp_path, monkeypatch):
    app = create_app(db_path=tmp_path / "api.db")
    seen: list = []

    async def fake_personalized(favorites, limit: int):
        seen.append(list(favorites))
        return []

    async def fake_trending(limit: int):
        return [
            {
                "track_id": f"chart-{index}",
                "title": f"Chart {index}",
                "artist": "Chart",
                "duration": 180,
                "thumbnail": "",
            }
            for index in range(limit)
        ]

    monkeypatch.setattr(api_module, "get_trending_tracks", fake_trending)
    monkeypatch.setattr(api_module, "get_wave_personalized", fake_personalized)

    with TestClient(app) as client:
        # A missing, empty, malformed, zero or negative user_id (e.g. the
        # historic "?limit=18?user_id=…" broken URL) must never fail the
        # request with 422 or 401: the wave just has no taste profile.
        variants = (
            None,
            "",
            "not-a-number",
            "0",
            "-5",
        )
        for raw in variants:
            params: dict = {"limit": 4}
            if raw is not None:
                params["user_id"] = raw
            response = client.get("/api/wave/personalized", params=params)
            assert response.status_code == 200, (raw, response.text)
            assert len(response.json()) == 4

        assert len(seen) == len(variants)
        assert all(favorites == [] for favorites in seen)

        # A valid explicit user_id still reaches the favourites lookup.
        _wave_favorites(client, 7, ["yt-al"])
        ok = client.get(
            "/api/wave/personalized", params={"limit": 4, "user_id": "7"}
        )
        assert ok.status_code == 200
        assert [track.get("track_id") for track in seen[-1]] == ["yt-al"]


def test_wave_personalized_strict_mode_skips_unsigned(tmp_path, monkeypatch):
    app = create_app(db_path=tmp_path / "api.db")
    profile_calls: list = []

    async def fake_personalized(favorites, limit: int):
        profile_calls.append([track.get("track_id") for track in favorites])
        return []

    async def fake_trending(limit: int):
        return [
            {
                "track_id": f"chart-{index}",
                "title": f"Chart {index}",
                "artist": "Chart",
                "duration": 180,
                "thumbnail": "",
            }
            for index in range(limit)
        ]

    monkeypatch.setattr(api_module, "get_bot_token", lambda: AUTH_BOT_TOKEN)
    monkeypatch.setattr(api_module, "get_auth_mode", lambda: "auto")
    monkeypatch.setattr(api_module, "get_trending_tracks", fake_trending)
    monkeypatch.setattr(api_module, "get_wave_personalized", fake_personalized)

    with TestClient(app) as client:
        # Strict initData mode: an unsigned request (even claiming a
        # foreign user_id) is not rejected with 401/422 — the side effect
        # (building a taste profile from someone else's favourites) is
        # simply skipped and the chart covers the answer.
        anon = client.get(
            "/api/wave/personalized", params={"limit": 5, "user_id": 999}
        )
        assert anon.status_code == 200
        assert len(anon.json()) == 5
        assert profile_calls[-1] == []

        signed = client.get(
            "/api/wave/personalized",
            params={"limit": 5},
            headers={"Authorization": f"tma {_signed_init_data(777)}"},
        )
        assert signed.status_code == 200
        assert profile_calls[-1] == []


def test_wave_mood_profile_pool_is_randomized(tmp_path, monkeypatch):
    """The wave backend must keep fresh, wide query pools per vibe."""
    from backend.audio_service import (
        WAVE_MOOD_QUERIES,
        build_taste_profile,
        get_wave_by_mood,
    )

    # The wave engine consumes the module's shared PRNG (shuffle/samples):
    # snapshot and restore its state so later tests (e.g. the trending
    # order test in test_audio_service.py) keep their expected sequence.
    import random as random_module

    rng_state = random_module.getstate()
    try:
        for vibe, queries in WAVE_MOOD_QUERIES.items():
            assert len(queries) >= 25, vibe
            assert len(set(queries)) == len(queries), vibe

        # Deterministic mode without a sample: every ranked artist, fixed
        # suffix.
        favorites = [
            {
                "track_id": f"t{index}",
                "artist": f"Artist {index}",
                "title": "Song",
            }
            for index in range(10)
        ]
        profile = build_taste_profile(favorites)
        assert len(profile) == 8
        assert profile[0] == "Artist 0 official audio"

        # Randomized mode: 5 sampled artists, shuffled wording.
        sampled = build_taste_profile(favorites, sample_size=5)
        assert len(sampled) == 5
        assert all(
            any(query.startswith(f"Artist {index}") for query in sampled)
            for index in range(10)
        ) is False  # only 5 of 10 artists can be present

        handles = {"calm": tuple(f"probe-query-{i}" for i in range(12))}
        monkeypatch.setattr("backend.audio_service.WAVE_MOOD_QUERIES", handles)

        seen_calls: list[list[str]] = []
        call_counter = itertools.count()

        async def fake_search(query, limit):
            seen_calls.append([query])
            batch = next(call_counter)
            return [
                {
                    "track_id": f"{query}-b{batch}-{index}",
                    "title": f"Wave {query} {batch} {index}",
                    "artist": f"Artist {query}-b{batch}-{index}",
                    "duration": 150,
                    "thumbnail": "",
                }
                for index in range(3)
            ]

        monkeypatch.setattr("backend.audio_service.search_tracks", fake_search)

        random_module.seed(1)
        first_batch = asyncio.run(get_wave_by_mood("calm", limit=18))
        assert len(first_batch) == 18
        assert seen_calls, "get_wave_by_mood must fire its query batch"
        # One limited-18 run with 3 tracks per answer needs exactly the
        # 7-query sketch (3*7 = 21 candidates >= 18), never the whole pool.
        assert len(seen_calls) == 7
        assert len({call[0] for call in seen_calls}) == 7
        sorted_first_calls = sorted(seen_calls)

        # A different seed samples a different sketch: every run of the
        # wave is fresh instead of repeating the same three queries.
        seen_calls.clear()
        random_module.seed(2)
        second_batch = asyncio.run(get_wave_by_mood("calm", limit=18))
        assert len(second_batch) == 18
        assert len(seen_calls) == 7
        assert sorted(seen_calls) != sorted_first_calls

        # Both runs: randomized track order on top of the merged batches.
        assert [track["track_id"] for track in first_batch] != [
            track["track_id"] for track in sorted(
                first_batch, key=lambda track: track["track_id"]
            )
        ]
    finally:
        random_module.setstate(rng_state)


def test_wave_favorite_parser_and_title_dedupe():
    from backend.audio_service import _merge_wave_results, _parse_favorite_item, build_taste_profile

    for sep in ("-", "–", "—"):
        parsed = _parse_favorite_item({"title": f"Miyagi & Andy Panda {sep} Tantra (Official Audio)", "artist": "Hajime"})
        assert parsed["artist"] == "Miyagi & Andy Panda"
        assert parsed["title"] == "Tantra"
        assert "Miyagi & Andy Panda" in build_taste_profile([{"title": f"Miyagi & Andy Panda {sep} Tantra", "artist": "Hajime"}])[0]

    assert _parse_favorite_item({"title": "Artist - slowed phonk song", "artist": "Channel"})["vibe"] == "aggressive"
    assert _parse_favorite_item({"title": "Artist - lofi ambient", "artist": "Channel"})["vibe"] == "calm"
    result = _merge_wave_results([[{"track_id": "reupload", "title": "Tantra [Official Video]", "artist": "Other", "duration": 180}]], 2, set(), {"tantra"})
    assert result == []


def test_personal_wave_refills_from_detected_vibe(monkeypatch):
    import asyncio
    from backend import audio_service

    calls = []
    async def fake_search(queries, per_query):
        calls.extend(queries)
        return [[{"track_id": f"new-{len(calls)}", "title": f"New song {len(calls)}", "artist": "Artist", "duration": 180}] for _ in queries]
    monkeypatch.setattr(audio_service, "_wave_search_queries", fake_search)
    monkeypatch.setattr(audio_service, "_discover_similar_tracks_sync", lambda *args: [])
    favorites = [{"track_id": "liked", "title": "Artist - lofi song", "artist": "Channel"}]
    asyncio.run(audio_service.get_wave_personalized(favorites, limit=1))
    assert calls
    assert all(query in audio_service.WAVE_MOOD_QUERIES["calm"] for query in calls if query.startswith(tuple(audio_service.WAVE_MOOD_QUERIES["calm"])))


def test_personal_wave_style_queries_follow_likes(monkeypatch):
    """Phonk/slowed likes -> wave fires aggressive style queries, not pop."""
    import asyncio
    from backend import audio_service

    calls = []

    async def fake_search(queries, per_query):
        calls.extend(queries)
        return [
            [
                {
                    "track_id": f"yt-{len(calls)}-{i}",
                    "title": f"New track {len(calls)}-{i}",
                    "artist": f"Performer {len(calls)}-{i}",
                    "duration": 180,
                }
                for i in range(3)
            ]
        ]

    monkeypatch.setattr(audio_service, "_wave_search_queries", fake_search)
    monkeypatch.setattr(audio_service, "_discover_similar_tracks_sync", lambda *args: [])
    favorites = [
        {"track_id": "liked-1", "title": "Miyagi & Andy Panda - Tantra (Official Audio)", "artist": "Hajime"},
        {"track_id": "liked-2", "title": "Kordhell - Murder In My Mind (slowed reverb)", "artist": "zardy"},
    ]
    tracks = asyncio.run(audio_service.get_wave_personalized(favorites, limit=10))
    # Real artist wins over the channel name, vibe = aggressive.
    assert any("Miyagi" in query for query in calls)
    assert any(query in audio_service.VIBE_STYLE_QUERIES["aggressive"] for query in calls)
    # Likes exist: the generic trending pop fallback must never be used.
    assert not any(query in audio_service.TRENDING_FALLBACK_QUERIES for query in calls)
    #Liked songs themselves never come back.
    returned_ids = {track["track_id"] for track in tracks}
    assert not returned_ids & {"liked-1", "liked-2"}


def test_personal_wave_reupload_same_title_filtered(monkeypatch):
    """A re-upload with a different track_id but the same song title is dropped."""
    import asyncio
    from backend import audio_service

    reupload = {
        "track_id": "other-upload-999",
        "title": "Tantra (Official Audio 2026)",
        "artist": "Random Channel",
        "duration": 180,
    }
    fresh = {
        "track_id": "brand-new-1",
        "title": "Completely Different Song",
        "artist": "Other Artist",
        "duration": 180,
    }

    async def fake_search(queries, per_query):
        return [[reupload, fresh] for _ in queries]

    monkeypatch.setattr(audio_service, "_wave_search_queries", fake_search)
    monkeypatch.setattr(audio_service, "_discover_similar_tracks_sync", lambda *args: [])
    favorites = [{"track_id": "liked-1", "title": "Miyagi & Andy Panda - Tantra (Official Audio)", "artist": "Hajime"}]
    tracks = asyncio.run(audio_service.get_wave_personalized(favorites, limit=5))
    ids = {track["track_id"] for track in tracks}
    assert "other-upload-999" not in ids
    assert "liked-1" not in ids
    assert "brand-new-1" in ids


def test_personal_wave_empty_favorites_uses_trending_mix(monkeypatch):
    """No likes -> mix from every mood pool plus the trending fallback."""
    import asyncio
    from backend import audio_service

    calls = []

    async def fake_search(queries, per_query):
        calls.extend(queries)
        return [
            [
                {
                    "track_id": f"yt-{len(calls)}-{i}",
                    "title": f"Fresh {len(calls)}-{i}",
                    "artist": f"Artist {len(calls)}-{i}",
                    "duration": 180,
                }
                for i in range(3)
            ]
        ]

    monkeypatch.setattr(audio_service, "_wave_search_queries", fake_search)
    all_mood = {q for pool in audio_service.WAVE_MOOD_QUERIES.values() for q in pool}
    tracks = asyncio.run(audio_service.get_wave_personalized([], limit=15))
    assert tracks
    assert calls
    # Mixes the whole mood space and the trending fallback, not a single vibe.
    assert any(query in all_mood for query in calls)
    assert any(query in audio_service.TRENDING_FALLBACK_QUERIES for query in calls)


def test_discover_similar_tracks_sync_uses_deezer(monkeypatch):
    """Deezer discovery: top songs of the liked artist minus already-liked titles."""
    from backend import audio_service

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, params=None):
            import types

            def _json():
                if "search/artist" in url:
                    return {"data": [{"id": 42}]}
                if "/top" in url:
                    return {"data": [
                        {"id": 1, "title": "Tantra", "artist": {"name": "Miyagi"}, "duration": 200},
                        {"id": 2, "title": "Utopia", "artist": {"name": "Miyagi"}, "duration": 210},
                    ]}
                if "/related" in url:
                    return {"data": [{"id": 77}]}
                return {"data": []}

            return types.SimpleNamespace(json=_json)

    monkeypatch.setattr(audio_service.httpx, "Client", FakeClient)
    discovered = audio_service._discover_similar_tracks_sync(["Miyagi"], {"tantra"}, 10)
    titles = [track["title"] for track in discovered]
    # Already-liked "Tantra" is skipped, the other top song comes through.
    assert not any("Tantra" in title for title in titles)
    assert any("Utopia" in title for title in titles)
    assert all(track["track_id"].startswith("deezer:") for track in discovered)
