"""Asynchronous SQLite storage for favorites, playlists and search history."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Mapping

import aiosqlite

MAX_SEARCH_HISTORY = 50

SCHEMA = """
CREATE TABLE IF NOT EXISTS favorites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    track_id TEXT NOT NULL,
    title TEXT NOT NULL,
    artist TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL,
    thumbnail TEXT NOT NULL DEFAULT '',
    duration INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (user_id, track_id)
);
CREATE INDEX IF NOT EXISTS idx_favorites_user_created
    ON favorites (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_favorites_user_id
    ON favorites (user_id);

CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_playlists_user_created
    ON playlists (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS playlist_tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    track_id TEXT NOT NULL,
    title TEXT NOT NULL,
    artist TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL,
    thumbnail TEXT NOT NULL DEFAULT '',
    duration INTEGER NOT NULL DEFAULT 0,
    position INTEGER NOT NULL DEFAULT 0,
    added_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (playlist_id, track_id)
);
CREATE INDEX IF NOT EXISTS idx_playlist_tracks_order
    ON playlist_tracks (playlist_id, position);

CREATE TABLE IF NOT EXISTS search_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    query TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_search_history_user
    ON search_history (user_id, id DESC);
"""


def _track_values(track: Mapping[str, Any]) -> tuple[str, str, str, str, str, int]:
    """Validate and normalize a track payload for storage."""
    track_id = str(track.get("track_id") or track.get("id") or "").strip()
    title = str(track.get("title") or "").strip()
    if not track_id or not title:
        raise ValueError("track_id and title are required")

    artist = str(track.get("artist") or "").strip()
    url = str(track.get("url") or "").strip()
    thumbnail = str(track.get("thumbnail") or track.get("thumbnail_url") or "").strip()
    try:
        duration = max(0, int(track.get("duration") or 0))
    except (TypeError, ValueError):
        duration = 0

    return track_id, title, artist, url, thumbnail, duration


class Database:
    """Small async repository around a single SQLite connection."""

    def __init__(self, path: str | Path) -> None:
        self.path: str | Path = ":memory:" if str(path) == ":memory:" else Path(path)
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        """Open the database and create the schema if needed."""
        if self._connection is not None:
            return

        if isinstance(self.path, Path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            target = str(self.path)
        else:
            target = self.path

        connection = await aiosqlite.connect(target)
        try:
            connection.row_factory = aiosqlite.Row
            await connection.execute("PRAGMA foreign_keys = ON")
            await connection.execute("PRAGMA busy_timeout = 5000")
            await connection.execute("PRAGMA journal_mode = WAL")
            await connection.executescript(SCHEMA)
            await connection.commit()
        except Exception:
            await connection.close()
            raise

        self._connection = connection

    async def close(self) -> None:
        """Close the connection, if it is open."""
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    async def ping(self) -> bool:
        """Return True when the database answers a trivial query."""
        row = await self._fetch_one("SELECT 1 AS ok")
        return bool(row and row["ok"] == 1)

    def _db(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("Database is not connected")
        return self._connection

    async def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        connection = self._db()
        async with self._lock:
            cursor = await connection.execute(sql, params)
            try:
                await connection.commit()
                return cursor.rowcount if cursor.rowcount is not None else 0
            except Exception:
                await connection.rollback()
                raise
            finally:
                await cursor.close()

    async def _fetch_all(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> list[dict[str, Any]]:
        connection = self._db()
        async with self._lock:
            cursor = await connection.execute(sql, params)
            try:
                rows = await cursor.fetchall()
            finally:
                await cursor.close()
        return [dict(row) for row in rows]

    async def _fetch_one(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> dict[str, Any] | None:
        connection = self._db()
        async with self._lock:
            cursor = await connection.execute(sql, params)
            try:
                row = await cursor.fetchone()
            finally:
                await cursor.close()
        return dict(row) if row is not None else None

    # ------------------------------------------------------------------ favorites

    async def add_favorite(self, user_id: int, track: Mapping[str, Any]) -> bool:
        """Add a track to favorites. Returns False when it was already there."""
        values = _track_values(track)
        changed = await self._execute(
            """
            INSERT OR IGNORE INTO favorites
                (user_id, track_id, title, artist, url, thumbnail, duration)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (int(user_id), *values),
        )
        return changed > 0

    async def remove_favorite(self, user_id: int, track_id: str) -> bool:
        """Remove a track from favorites. Returns False when it was absent."""
        changed = await self._execute(
            "DELETE FROM favorites WHERE user_id = ? AND track_id = ?",
            (int(user_id), str(track_id)),
        )
        return changed > 0

    async def list_favorites(self, user_id: int) -> list[dict[str, Any]]:
        """Return the user's favorite tracks, newest first."""
        return await self._fetch_all(
            """
            SELECT track_id, title, artist, url, thumbnail, duration, created_at
            FROM favorites
            WHERE user_id = ?
            ORDER BY created_at DESC, id DESC
            """,
            (int(user_id),),
        )

    # ------------------------------------------------------------------- history

    async def add_search_history(self, user_id: int, query: str) -> bool:
        """Store a search query and keep only the latest MAX_SEARCH_HISTORY rows."""
        cleaned = " ".join(str(query or "").split())
        if not cleaned:
            return False

        connection = self._db()
        async with self._lock:
            try:
                await connection.execute(
                    "INSERT INTO search_history (user_id, query) VALUES (?, ?)",
                    (int(user_id), cleaned),
                )
                await connection.execute(
                    """
                    DELETE FROM search_history
                    WHERE user_id = ?
                      AND id NOT IN (
                          SELECT id
                          FROM search_history
                          WHERE user_id = ?
                          ORDER BY id DESC
                          LIMIT ?
                      )
                    """,
                    (int(user_id), int(user_id), MAX_SEARCH_HISTORY),
                )
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise
        return True

    async def list_search_history(
        self, user_id: int, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Return recent search queries, newest first."""
        safe_limit = max(1, min(int(limit), MAX_SEARCH_HISTORY))
        return await self._fetch_all(
            """
            SELECT query, created_at
            FROM search_history
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (int(user_id), safe_limit),
        )

    async def clear_search_history(self, user_id: int) -> int:
        """Delete every stored query for the user and return the row count."""
        return await self._execute(
            "DELETE FROM search_history WHERE user_id = ?", (int(user_id),)
        )

    async def delete_search_query(self, user_id: int, query: str) -> int:
        """Delete every occurrence of one query; return the removed count."""
        cleaned = " ".join(str(query or "").split())
        if not cleaned:
            return 0
        return await self._execute(
            "DELETE FROM search_history WHERE user_id = ? AND query = ?",
            (int(user_id), cleaned),
        )

    # ------------------------------------------------------------------ playlists

    async def create_playlist(self, user_id: int, title: str) -> dict[str, Any]:
        """Create a playlist and return its stored representation."""
        cleaned = " ".join(str(title or "").split())
        if not cleaned:
            raise ValueError("playlist title is required")
        cleaned = cleaned[:100]

        connection = self._db()
        async with self._lock:
            try:
                cursor = await connection.execute(
                    "INSERT INTO playlists (user_id, title) VALUES (?, ?)",
                    (int(user_id), cleaned),
                )
                playlist_id = int(cursor.lastrowid or 0)
                await cursor.close()
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise

            cursor = await connection.execute(
                "SELECT id, user_id, title, created_at FROM playlists WHERE id = ?",
                (playlist_id,),
            )
            try:
                row = await cursor.fetchone()
            finally:
                await cursor.close()

        if row is None:  # pragma: no cover - the row was created in this transaction
            raise RuntimeError("Failed to read the created playlist")
        return dict(row)

    async def list_playlists(self, user_id: int) -> list[dict[str, Any]]:
        """Return playlists with the number of tracks in each."""
        return await self._fetch_all(
            """
            SELECT p.id, p.user_id, p.title, p.created_at,
                   COUNT(pt.id) AS track_count
            FROM playlists AS p
            LEFT JOIN playlist_tracks AS pt ON pt.playlist_id = p.id
            WHERE p.user_id = ?
            GROUP BY p.id
            ORDER BY p.created_at DESC, p.id DESC
            """,
            (int(user_id),),
        )

    async def delete_playlist(self, user_id: int, playlist_id: int) -> bool:
        """Delete a playlist owned by the user. Tracks are removed by cascade."""
        changed = await self._execute(
            "DELETE FROM playlists WHERE id = ? AND user_id = ?",
            (int(playlist_id), int(user_id)),
        )
        return changed > 0

    async def playlist_owned(self, user_id: int, playlist_id: int) -> bool:
        """Return True when the playlist exists and belongs to the user."""
        return await self._playlist_owned(user_id, playlist_id)

    async def _playlist_owned(self, user_id: int, playlist_id: int) -> bool:
        row = await self._fetch_one(
            "SELECT 1 AS ok FROM playlists WHERE id = ? AND user_id = ?",
            (int(playlist_id), int(user_id)),
        )
        return row is not None

    async def add_track_to_playlist(
        self, user_id: int, playlist_id: int, track: Mapping[str, Any]
    ) -> bool | None:
        """Add a track to a playlist.

        Returns None when the playlist does not belong to the user, False for a
        duplicate track and True when the track was added.
        """
        values = _track_values(track)
        connection = self._db()

        async with self._lock:
            try:
                cursor = await connection.execute(
                    "SELECT 1 AS ok FROM playlists WHERE id = ? AND user_id = ?",
                    (int(playlist_id), int(user_id)),
                )
                try:
                    owned = await cursor.fetchone()
                finally:
                    await cursor.close()
                if owned is None:
                    return None

                cursor = await connection.execute(
                    """
                    SELECT COALESCE(MAX(position), -1) + 1
                    FROM playlist_tracks
                    WHERE playlist_id = ?
                    """,
                    (int(playlist_id),),
                )
                try:
                    row = await cursor.fetchone()
                finally:
                    await cursor.close()
                position = int(row[0]) if row is not None else 0

                cursor = await connection.execute(
                    """
                    INSERT OR IGNORE INTO playlist_tracks
                        (playlist_id, track_id, title, artist, url, thumbnail,
                         duration, position)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (int(playlist_id), *values, position),
                )
                try:
                    changed = cursor.rowcount > 0
                finally:
                    await cursor.close()

                await connection.commit()
                return changed
            except Exception:
                await connection.rollback()
                raise

    async def list_playlist_tracks(
        self, user_id: int, playlist_id: int
    ) -> list[dict[str, Any]] | None:
        """Return playlist tracks in order, or None if the playlist is not owned."""
        if not await self._playlist_owned(user_id, playlist_id):
            return None
        return await self._fetch_all(
            """
            SELECT track_id, title, artist, url, thumbnail, duration,
                   position, added_at
            FROM playlist_tracks
            WHERE playlist_id = ?
            ORDER BY position, id
            """,
            (int(playlist_id),),
        )

    async def remove_track_from_playlist(
        self, user_id: int, playlist_id: int, track_id: str
    ) -> bool:
        """Remove a track from a playlist owned by the user."""
        changed = await self._execute(
            """
            DELETE FROM playlist_tracks
            WHERE playlist_id = ?
              AND track_id = ?
              AND EXISTS (
                  SELECT 1 FROM playlists
                  WHERE id = ? AND user_id = ?
              )
            """,
            (int(playlist_id), str(track_id), int(playlist_id), int(user_id)),
        )
        return changed > 0


# ---------------------------------------------------------------------------
# Module-level helpers required by the project plan (Phase 1, step 1.4).
# ---------------------------------------------------------------------------

_default_database: Database | None = None


async def init_db(path: str | Path | None = None) -> Database:
    """Initialize the shared database and return it.

    When ``path`` is omitted, ``DB_PATH`` from the application settings is used.
    Calling the function again for the same database is safe and keeps the
    existing connection.
    """
    global _default_database

    if _default_database is not None:
        if path is None or str(path) == str(_default_database.path):
            return _default_database
        await _default_database.close()
        _default_database = None

    if path is None:
        from config import load_settings

        path = load_settings().db_path

    database = Database(path)
    await database.connect()
    _default_database = database
    return database


async def close_db() -> None:
    """Close the shared database opened by :func:`init_db`."""
    global _default_database
    if _default_database is not None:
        await _default_database.close()
        _default_database = None


def get_db() -> Database:
    """Return the shared database, raising a clear error before initialization."""
    if _default_database is None:
        raise RuntimeError("Database is not initialized; call init_db() first")
    return _default_database


async def add_favorite(
    user_id: int,
    track: Mapping[str, Any] | str,
    title: str | None = None,
    artist: str = "",
    duration: int = 0,
    thumbnail: str = "",
) -> bool:
    """Add a track to the shared database's favorites.

    Accepts either a track mapping with ``track_id``/``title`` or positional
    ``track_id``, ``title``, ``artist``, ``duration`` and ``thumbnail`` values.
    """
    if isinstance(track, Mapping):
        payload: Mapping[str, Any] = track
    else:
        payload = {
            "track_id": str(track),
            "title": title or "",
            "artist": artist,
            "duration": duration,
            "thumbnail": thumbnail,
        }
    return await get_db().add_favorite(user_id, payload)


async def remove_favorite(user_id: int, track_id: str) -> bool:
    """Remove a track from the shared database's favorites."""
    return await get_db().remove_favorite(user_id, track_id)


async def get_favorites(user_id: int) -> list[dict[str, Any]]:
    """Return favorites of a user from the shared database."""
    return await get_db().list_favorites(user_id)
