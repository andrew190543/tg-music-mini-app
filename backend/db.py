"""Async SQLite repository for user favorites, search history and playlists."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import aiosqlite

MAX_SEARCH_HISTORY = 10
MAX_PLAYLIST_TITLE = 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS favorites (
    user_id INTEGER NOT NULL,
    track_id TEXT NOT NULL,
    title TEXT NOT NULL,
    artist TEXT NOT NULL DEFAULT '',
    duration INTEGER NOT NULL DEFAULT 0,
    thumbnail TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    added_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, track_id)
);

CREATE INDEX IF NOT EXISTS idx_favorites_user_added
    ON favorites (user_id, added_at DESC);

CREATE TABLE IF NOT EXISTS search_history (
    user_id INTEGER NOT NULL,
    query TEXT NOT NULL,
    searched_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, query)
);

CREATE INDEX IF NOT EXISTS idx_search_history_user_time
    ON search_history (user_id, searched_at DESC);

CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_playlists_user_created
    ON playlists (user_id, created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS playlist_tracks (
    playlist_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    track_id TEXT NOT NULL,
    title TEXT NOT NULL,
    artist TEXT NOT NULL DEFAULT '',
    duration INTEGER NOT NULL DEFAULT 0,
    thumbnail TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    added_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (playlist_id, track_id),
    FOREIGN KEY (playlist_id) REFERENCES playlists(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_playlist_tracks_order
    ON playlist_tracks (playlist_id, position ASC);
"""


def _normalize_duration(value: Any) -> int:
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        return 0
    return max(0, seconds)


def _normalize_query(query: str) -> str:
    return " ".join(str(query or "").split())


def _normalize_playlist_title(title: str) -> str:
    cleaned = " ".join(str(title or "").split())
    return cleaned[:MAX_PLAYLIST_TITLE]


class Database:
    """Small async wrapper around an ``aiosqlite`` connection."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn

    async def connect(self) -> None:
        async with self._lock:
            if self._conn is not None:
                return

            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = await aiosqlite.connect(self.path)
            self._conn.row_factory = aiosqlite.Row
            await self._conn.execute("PRAGMA journal_mode=WAL;")
            await self._conn.execute("PRAGMA foreign_keys=ON;")
            await self._conn.executescript(SCHEMA)
            await self._conn.commit()

    async def close(self) -> None:
        async with self._lock:
            if self._conn is not None:
                await self._conn.close()
                self._conn = None

    async def ping(self) -> bool:
        cursor = await self.connection.execute("SELECT 1;")
        row = await cursor.fetchone()
        await cursor.close()
        return bool(row and row[0] == 1)

    async def add_favorite(self, user_id: int, track: dict[str, Any]) -> bool:
        """Insert or update a favorite track."""
        track_id = str(track.get("track_id") or "").strip()
        title = str(track.get("title") or "").strip()
        if not track_id or not title:
            raise ValueError("track_id and title are required")

        artist = str(track.get("artist") or "").strip()
        duration = _normalize_duration(track.get("duration"))
        thumbnail = str(track.get("thumbnail") or "").strip()
        url = str(track.get("url") or "").strip()

        async with self._lock:
            cursor = await self.connection.execute(
                "SELECT 1 FROM favorites WHERE user_id = ? AND track_id = ?;",
                (int(user_id), track_id),
            )
            existed = await cursor.fetchone() is not None
            await cursor.close()

            await self.connection.execute(
                """
                INSERT INTO favorites (
                    user_id, track_id, title, artist, duration, thumbnail, url, added_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, track_id) DO UPDATE SET
                    title = excluded.title,
                    artist = excluded.artist,
                    duration = excluded.duration,
                    thumbnail = excluded.thumbnail,
                    url = excluded.url,
                    added_at = CURRENT_TIMESTAMP;
                """,
                (int(user_id), track_id, title, artist, duration, thumbnail, url),
            )
            await self.connection.commit()
            return not existed

    async def remove_favorite(self, user_id: int, track_id: str) -> bool:
        cleaned_id = str(track_id or "").strip()
        if not cleaned_id:
            return False

        async with self._lock:
            cursor = await self.connection.execute(
                "DELETE FROM favorites WHERE user_id = ? AND track_id = ?;",
                (int(user_id), cleaned_id),
            )
            deleted = cursor.rowcount > 0
            await cursor.close()
            await self.connection.commit()
            return deleted

    async def list_favorites(self, user_id: int) -> list[dict[str, Any]]:
        async with self._lock:
            cursor = await self.connection.execute(
                """
                SELECT track_id, title, artist, duration, thumbnail, url, added_at
                FROM favorites
                WHERE user_id = ?
                ORDER BY added_at DESC, rowid DESC;
                """,
                (int(user_id),),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            return [dict(row) for row in rows]

    async def add_search_history(self, user_id: int, query: str) -> None:
        cleaned = _normalize_query(query)
        if not cleaned:
            return

        async with self._lock:
            await self.connection.execute(
                """
                INSERT INTO search_history (user_id, query, searched_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, query) DO UPDATE SET
                    searched_at = CURRENT_TIMESTAMP;
                """,
                (int(user_id), cleaned),
            )
            await self.connection.execute(
                """
                DELETE FROM search_history
                WHERE user_id = ?
                  AND query NOT IN (
                      SELECT query
                      FROM search_history
                      WHERE user_id = ?
                      ORDER BY searched_at DESC, rowid DESC
                      LIMIT ?
                  );
                """,
                (int(user_id), int(user_id), MAX_SEARCH_HISTORY),
            )
            await self.connection.commit()

    async def list_search_history(
        self, user_id: int, limit: int = MAX_SEARCH_HISTORY
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), MAX_SEARCH_HISTORY))
        async with self._lock:
            cursor = await self.connection.execute(
                """
                SELECT query, searched_at
                FROM search_history
                WHERE user_id = ?
                ORDER BY searched_at DESC, rowid DESC
                LIMIT ?;
                """,
                (int(user_id), safe_limit),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            return [dict(row) for row in rows]

    async def delete_search_query(self, user_id: int, query: str) -> bool:
        cleaned = _normalize_query(query)
        if not cleaned:
            return False

        async with self._lock:
            cursor = await self.connection.execute(
                "DELETE FROM search_history WHERE user_id = ? AND query = ?;",
                (int(user_id), cleaned),
            )
            deleted = cursor.rowcount > 0
            await cursor.close()
            await self.connection.commit()
            return deleted

    async def clear_search_history(self, user_id: int) -> int:
        async with self._lock:
            cursor = await self.connection.execute(
                "DELETE FROM search_history WHERE user_id = ?;",
                (int(user_id),),
            )
            deleted = max(0, cursor.rowcount)
            await cursor.close()
            await self.connection.commit()
            return deleted

    async def create_playlist(
        self, user_id: int, title: str
    ) -> dict[str, Any]:
        cleaned = _normalize_playlist_title(title)
        if not cleaned:
            raise ValueError("Playlist title is required")

        async with self._lock:
            cursor = await self.connection.execute(
                """
                INSERT INTO playlists (user_id, title, created_at)
                VALUES (?, ?, CURRENT_TIMESTAMP);
                """,
                (int(user_id), cleaned),
            )
            playlist_id = cursor.lastrowid
            await cursor.close()
            await self.connection.commit()

            cursor = await self.connection.execute(
                """
                SELECT id, user_id, title, created_at
                FROM playlists
                WHERE id = ?;
                """,
                (playlist_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            return dict(row) if row else {"id": playlist_id, "title": cleaned}

    async def list_playlists(self, user_id: int) -> list[dict[str, Any]]:
        async with self._lock:
            cursor = await self.connection.execute(
                """
                SELECT
                    p.id,
                    p.user_id,
                    p.title,
                    p.created_at,
                    COUNT(pt.track_id) AS track_count
                FROM playlists p
                LEFT JOIN playlist_tracks pt ON pt.playlist_id = p.id
                WHERE p.user_id = ?
                GROUP BY p.id
                ORDER BY p.created_at DESC, p.id DESC;
                """,
                (int(user_id),),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            return [dict(row) for row in rows]

    async def playlist_owned(self, user_id: int, playlist_id: int) -> bool:
        async with self._lock:
            cursor = await self.connection.execute(
                "SELECT 1 FROM playlists WHERE id = ? AND user_id = ?;",
                (int(playlist_id), int(user_id)),
            )
            owned = await cursor.fetchone() is not None
            await cursor.close()
            return owned

    async def delete_playlist(self, user_id: int, playlist_id: int) -> bool:
        async with self._lock:
            cursor = await self.connection.execute(
                "DELETE FROM playlists WHERE id = ? AND user_id = ?;",
                (int(playlist_id), int(user_id)),
            )
            deleted = cursor.rowcount > 0
            await cursor.close()
            await self.connection.commit()
            return deleted

    async def list_playlist_tracks(
        self, user_id: int, playlist_id: int
    ) -> list[dict[str, Any]] | None:
        async with self._lock:
            cursor = await self.connection.execute(
                "SELECT 1 FROM playlists WHERE id = ? AND user_id = ?;",
                (int(playlist_id), int(user_id)),
            )
            owned = await cursor.fetchone() is not None
            await cursor.close()
            if not owned:
                return None

            cursor = await self.connection.execute(
                """
                SELECT track_id, title, artist, duration, thumbnail, url, position, added_at
                FROM playlist_tracks
                WHERE playlist_id = ?
                ORDER BY position ASC, added_at ASC;
                """,
                (int(playlist_id),),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            return [dict(row) for row in rows]

    async def add_track_to_playlist(
        self, user_id: int, playlist_id: int, track: dict[str, Any]
    ) -> bool | None:
        track_id = str(track.get("track_id") or "").strip()
        title = str(track.get("title") or "").strip()
        if not track_id or not title:
            raise ValueError("track_id and title are required")

        artist = str(track.get("artist") or "").strip()
        duration = _normalize_duration(track.get("duration"))
        thumbnail = str(track.get("thumbnail") or "").strip()
        url = str(track.get("url") or "").strip()

        async with self._lock:
            cursor = await self.connection.execute(
                "SELECT 1 FROM playlists WHERE id = ? AND user_id = ?;",
                (int(playlist_id), int(user_id)),
            )
            owned = await cursor.fetchone() is not None
            await cursor.close()
            if not owned:
                return None

            cursor = await self.connection.execute(
                "SELECT 1 FROM playlist_tracks WHERE playlist_id = ? AND track_id = ?;",
                (int(playlist_id), track_id),
            )
            existed = await cursor.fetchone() is not None
            await cursor.close()
            if existed:
                return False

            cursor = await self.connection.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 FROM playlist_tracks WHERE playlist_id = ?;",
                (int(playlist_id),),
            )
            row = await cursor.fetchone()
            await cursor.close()
            next_pos = int(row[0]) if row else 0

            await self.connection.execute(
                """
                INSERT INTO playlist_tracks (
                    playlist_id, position, track_id, title, artist, duration, thumbnail, url, added_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP);
                """,
                (
                    int(playlist_id),
                    next_pos,
                    track_id,
                    title,
                    artist,
                    duration,
                    thumbnail,
                    url,
                ),
            )
            await self.connection.commit()
            return True

    async def remove_track_from_playlist(
        self, user_id: int, playlist_id: int, track_id: str
    ) -> bool:
        cleaned_id = str(track_id or "").strip()
        if not cleaned_id:
            return False

        async with self._lock:
            cursor = await self.connection.execute(
                """
                DELETE FROM playlist_tracks
                WHERE playlist_id = ?
                  AND track_id = ?
                  AND EXISTS (
                      SELECT 1 FROM playlists WHERE id = ? AND user_id = ?
                  );
                """,
                (int(playlist_id), cleaned_id, int(playlist_id), int(user_id)),
            )
            deleted = cursor.rowcount > 0
            await cursor.close()
            await self.connection.commit()
            return deleted