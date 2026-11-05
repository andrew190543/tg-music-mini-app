import pytest

from backend import db as db_module
from backend.db import Database, MAX_SEARCH_HISTORY

TRACK = {
    "track_id": "yt:abc123",
    "title": "Test Song",
    "artist": "Test Artist",
    "url": "https://example.com/watch?v=abc123",
    "thumbnail": "https://example.com/cover.jpg",
    "duration": 215,
}


@pytest.fixture
async def database(tmp_path):
    db = Database(tmp_path / "test.db")
    await db.connect()
    yield db
    await db.close()


async def test_favorites_are_unique_and_user_scoped(database):
    assert await database.add_favorite(1, TRACK) is True
    assert await database.add_favorite(1, TRACK) is False

    favorites = await database.list_favorites(1)
    assert len(favorites) == 1
    assert favorites[0]["track_id"] == "yt:abc123"
    assert favorites[0]["title"] == "Test Song"
    assert favorites[0]["duration"] == 215

    other_track = {**TRACK, "track_id": "yt:other", "title": "Other Song"}
    assert await database.add_favorite(2, other_track) is True
    assert len(await database.list_favorites(1)) == 1
    assert len(await database.list_favorites(2)) == 1

    assert await database.remove_favorite(1, "yt:abc123") is True
    assert await database.remove_favorite(1, "yt:abc123") is False
    assert await database.list_favorites(1) == []


async def test_track_validation(database):
    with pytest.raises(ValueError, match="required"):
        await database.add_favorite(1, {"track_id": "x", "title": ""})

    # URL is optional: the plan's favorites schema identifies tracks by track_id.
    assert await database.add_favorite(1, {"track_id": "no-url", "title": "No URL"})


async def test_search_history_is_limited_and_clearable(database):
    assert await database.add_search_history(1, "  radio   head  ") is True
    assert await database.add_search_history(1, "   ") is False

    for index in range(MAX_SEARCH_HISTORY + 5):
        assert await database.add_search_history(1, f"query {index}") is True

    history = await database.list_search_history(1, limit=100)
    assert len(history) == MAX_SEARCH_HISTORY
    assert history[0]["query"] == f"query {MAX_SEARCH_HISTORY + 4}"
    assert history[0]["created_at"]
    assert "query 4" not in {item["query"] for item in history}

    recent = await database.list_search_history(1)
    assert len(recent) == 20

    assert await database.clear_search_history(1) == MAX_SEARCH_HISTORY
    assert await database.list_search_history(1) == []


async def test_playlists_crud_and_ownership(database):
    playlist = await database.create_playlist(1, "  My   Mix  ")
    assert playlist["title"] == "My Mix"
    assert playlist["user_id"] == 1

    listed = await database.list_playlists(1)
    assert len(listed) == 1
    assert listed[0]["track_count"] == 0

    assert await database.add_track_to_playlist(1, playlist["id"], TRACK) is True
    assert await database.add_track_to_playlist(1, playlist["id"], TRACK) is False

    tracks = await database.list_playlist_tracks(1, playlist["id"])
    assert tracks is not None
    assert len(tracks) == 1
    assert tracks[0]["track_id"] == "yt:abc123"
    assert tracks[0]["position"] == 0

    listed = await database.list_playlists(1)
    assert listed[0]["track_count"] == 1

    assert (
        await database.add_track_to_playlist(2, playlist["id"], TRACK) is None
    )
    assert await database.list_playlist_tracks(2, playlist["id"]) is None
    assert await database.remove_track_from_playlist(2, playlist["id"], "yt:abc123") is False

    assert (
        await database.remove_track_from_playlist(1, playlist["id"], "yt:abc123")
        is True
    )
    assert await database.list_playlist_tracks(1, playlist["id"]) == []

    assert await database.delete_playlist(2, playlist["id"]) is False
    assert await database.delete_playlist(1, playlist["id"]) is True
    assert await database.list_playlists(1) == []
    assert await database.list_playlist_tracks(1, playlist["id"]) is None


async def test_playlist_requires_title(database):
    with pytest.raises(ValueError, match="title"):
        await database.create_playlist(1, "   ")


async def test_connect_is_idempotent_and_ping_works(database):
    await database.connect()
    assert await database.ping() is True


async def test_module_level_helpers_from_plan(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "_default_database", None)

    with pytest.raises(RuntimeError, match="init_db"):
        db_module.get_db()

    await db_module.init_db(tmp_path / "module.db")
    try:
        assert await db_module.add_favorite(1, TRACK) is True
        assert await db_module.add_favorite(1, TRACK) is False

        # Positional call style is supported too.
        assert (
            await db_module.add_favorite(
                1, "yt:positional", "Positional", "Artist", 90, "thumb.jpg"
            )
            is True
        )

        favorites = await db_module.get_favorites(1)
        assert len(favorites) == 2
        assert favorites[0]["track_id"] == "yt:positional"

        assert await db_module.remove_favorite(1, "yt:abc123") is True
        assert await db_module.remove_favorite(1, "yt:positional") is True
        assert await db_module.get_favorites(1) == []
    finally:
        await db_module.close_db()
