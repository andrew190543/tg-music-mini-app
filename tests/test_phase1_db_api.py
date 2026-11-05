import sqlite3

import pytest

from backend import db as db_module

TRACK = {
    "track_id": "yt:abc",
    "title": "Song A",
    "artist": "Artist A",
    "url": "https://example.com/watch?v=abc",
    "thumbnail": "https://img/a.jpg",
    "duration": 180,
}


@pytest.fixture
async def phase1_db(tmp_path, monkeypatch):
    path = tmp_path / "phase1.db"
    monkeypatch.setattr(db_module, "_default_database", None)
    await db_module.init_db(path)
    yield path
    await db_module.close_db()


async def test_phase1_favorites_api(phase1_db):
    # init_db() must be safe to call repeatedly.
    await db_module.init_db(phase1_db)
    assert phase1_db.is_file()

    assert await db_module.add_favorite(42, TRACK) is True
    # The same track must not be added twice for one user.
    assert await db_module.add_favorite(42, TRACK) is False

    favorites = await db_module.get_favorites(42)
    assert len(favorites) == 1
    track = favorites[0]
    assert track["track_id"] == "yt:abc"
    assert track["title"] == "Song A"
    assert track["artist"] == "Artist A"
    assert track["duration"] == 180
    assert track["thumbnail"] == "https://img/a.jpg"

    second_track = {
        **TRACK,
        "track_id": "sc:def",
        "title": "Song B",
        "url": "https://example.com/b",
    }
    assert await db_module.add_favorite(42, second_track) is True
    assert await db_module.add_favorite(99, TRACK) is True

    assert [item["track_id"] for item in await db_module.get_favorites(42)] == [
        "sc:def",
        "yt:abc",
    ]
    assert len(await db_module.get_favorites(99)) == 1
    assert await db_module.get_favorites(123) == []

    assert await db_module.remove_favorite(42, "yt:abc") is True
    assert await db_module.remove_favorite(42, "yt:abc") is False
    assert len(await db_module.get_favorites(42)) == 1
    # Removing a favorite must not touch another user's rows.
    assert len(await db_module.get_favorites(99)) == 1


async def test_phase1_schema_has_required_columns_and_index(phase1_db):
    with sqlite3.connect(phase1_db) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(favorites)")
        }
        indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(favorites)")
        }

    assert {
        "id",
        "user_id",
        "track_id",
        "title",
        "artist",
        "duration",
        "thumbnail",
    } <= columns
    assert "idx_favorites_user_id" in indexes


async def test_phase1_add_favorite_requires_track_id_and_title(phase1_db):
    with pytest.raises(ValueError, match="required"):
        await db_module.add_favorite(1, {"track_id": "", "title": "Title", "url": "u"})

    with pytest.raises(ValueError, match="required"):
        await db_module.add_favorite(1, {"track_id": "yt:abc", "title": "", "url": "u"})
