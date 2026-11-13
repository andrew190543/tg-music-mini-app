"""FastAPI application: health, search, audio proxy, thumbnails and favorites API."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, AsyncIterator, Callable, Literal
from urllib.parse import quote, urlparse

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config import BASE_DIR, get_auth_mode, get_bot_token, load_settings

from .audio_service import (
    AudioServiceError,
    audio_media_type_for,
    close_stream_client,
    fetch_thumbnail_bytes,
    get_cached_audio_file,
    get_trending_tracks,
    get_wave_by_mood,
    get_wave_personalized,
    search_tracks,
)
from .db import MAX_SEARCH_HISTORY, Database
from .telegram_auth import validate_init_data

APP_VERSION = "0.1.0"
logger = logging.getLogger(__name__)

LifespanFactory = Callable[[FastAPI], AbstractAsyncContextManager[None]]

ALLOWED_THUMB_SUFFIXES = (
    "ytimg.com",
    "dzcdn.net",
    "deezer.com",
    "ggpht.com",
    "googleusercontent.com",
)


class FavoriteCreate(BaseModel):
    """Payload for adding a track to favorites."""

    user_id: int | None = Field(default=None, gt=0)
    track_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    artist: str = Field(default="", max_length=300)
    duration: int = Field(default=0, ge=0)
    thumbnail: str = Field(default="", max_length=2000)
    url: str = Field(default="", max_length=2000)


class PlaylistCreate(BaseModel):
    """Payload for creating a playlist."""

    user_id: int | None = Field(default=None, gt=0)
    title: str = Field(min_length=1, max_length=100)


def _init_data_from_request(request: Request) -> str:
    """Extract raw initData from the ``Authorization: tma <initData>`` header."""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("tma "):
        return header[4:].strip()
    return ""


def _resolve_user_id(request: Request, fallback_user_id: int | None) -> int:
    """Resolve the Telegram user id shared by the favorites endpoints."""
    if get_bot_token() and get_auth_mode() != "off":
        init_data = _init_data_from_request(request)
        if not init_data:
            raise HTTPException(
                status_code=401,
                detail="Missing Telegram initData (Authorization: tma ...)",
            )
        result = validate_init_data(init_data, get_bot_token())
        if not result.ok:
            logger.warning("Rejected Telegram initData: %s", result.reason)
            raise HTTPException(
                status_code=401,
                detail=f"Invalid Telegram initData: {result.reason}",
            )
        return result.user_id

    if fallback_user_id is None:
        raise HTTPException(status_code=422, detail="user_id is required")
    return fallback_user_id


def _resolve_optional_user_id(
    request: Request, fallback_user_id: int | None
) -> int | None:
    """Resolve the user id without rejecting unverified requests."""
    if get_bot_token() and get_auth_mode() != "off":
        init_data = _init_data_from_request(request)
        if not init_data:
            return None
        result = validate_init_data(init_data, get_bot_token())
        if not result.ok:
            logger.warning("Optional auth skipped: %s", result.reason)
            return None
        return result.user_id
    return fallback_user_id


def _soft_user_id(raw_user_id: str | None) -> int | None:
    """Parse an optional user_id query value without raising 422 on bad strings."""
    text = str(raw_user_id or "").strip()
    if not text.isdigit():
        return None
    value = int(text)
    return value if value > 0 else None


def _is_allowed_thumb_url(raw_url: str) -> bool:
    """Validate that the thumbnail URL points to a known cover CDN."""
    try:
        parsed = urlparse(raw_url.strip())
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    return any(
        host == suffix or host.endswith(f".{suffix}")
        for suffix in ALLOWED_THUMB_SUFFIXES
    )


def _favorite_track(payload: FavoriteCreate) -> dict[str, Any]:
    """Normalize the payload and derive a webpage URL when it is omitted."""
    track_id = payload.track_id.strip()
    url = payload.url.strip()

    if not url:
        if "://" in track_id:
            url = track_id
        else:
            url = f"https://www.youtube.com/watch?v={quote(track_id, safe='')}"

    return {
        "track_id": track_id,
        "title": payload.title.strip(),
        "artist": payload.artist.strip(),
        "duration": payload.duration,
        "thumbnail": payload.thumbnail.strip(),
        "url": url,
    }


async def _store_search_query(
    database: Database, user_id: int, query: str
) -> None:
    """Persist a successful search, skipping an immediately repeated one."""
    recent = await database.list_search_history(user_id, limit=1)
    if recent and recent[0].get("query") == query:
        return
    await database.add_search_history(user_id, query)


async def _serve_audio_stream(track_id: str) -> FileResponse:
    """Download/cache the track once and serve it via native FileResponse."""
    cleaned_track_id = str(track_id or "").strip()
    if not cleaned_track_id:
        raise HTTPException(status_code=422, detail="track_id is required")

    try:
        audio_path = await get_cached_audio_file(cleaned_track_id)
    except AudioServiceError as exc:
        logger.warning("Stream resolution failed for %r: %s", cleaned_track_id, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Audio cache failed for %r", cleaned_track_id)
        raise HTTPException(
            status_code=502,
            detail="Upstream audio server returned an error",
        ) from exc

    if not audio_path.is_file() or audio_path.stat().st_size <= 0:
        raise HTTPException(status_code=502, detail="Audio file is empty")

    return FileResponse(
        path=audio_path,
        media_type=audio_media_type_for(audio_path),
        headers={"Cache-Control": "private, max-age=3600"},
    )


def create_app(
    db_path: str | Path | None = None,
    frontend_dir: str | Path | None = None,
    lifespan: LifespanFactory | None = None,
) -> FastAPI:
    """Build the FastAPI application with an isolated database connection."""
    resolved_db_path = (
        Path(db_path) if db_path is not None else load_settings().db_path
    )
    resolved_frontend = (
        Path(frontend_dir) if frontend_dir is not None else BASE_DIR / "frontend"
    )

    if not (resolved_frontend / "index.html").is_file():
        raise RuntimeError(f"Frontend index.html not found in {resolved_frontend}")

    @asynccontextmanager
    async def default_lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = Database(resolved_db_path)
        await database.connect()
        app.state.db = database
        warm_task = asyncio.create_task(get_trending_tracks(20))

        try:
            yield
        finally:
            warm_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await warm_task
            await close_stream_client()
            await database.close()

    app = FastAPI(
        title="TG Music API",
        description="Backend for the Telegram Mini App music player.",
        version=APP_VERSION,
        lifespan=lifespan or default_lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[
            "Accept-Ranges",
            "Content-Range",
            "Content-Length",
            "Content-Type",
            "ETag",
            "Last-Modified",
        ],
    )

    @app.get("/api/health", tags=["system"])
    async def health(request: Request):
        database: Database = request.app.state.db
        try:
            database_ok = await database.ping()
        except Exception:
            database_ok = False

        payload = {
            "status": "ok" if database_ok else "error",
            "service": "tg-music",
            "version": APP_VERSION,
            "database": "ok" if database_ok else "error",
        }
        if not database_ok:
            return JSONResponse(status_code=503, content=payload)
        return payload

    @app.get("/health", tags=["system"], include_in_schema=False)
    async def root_health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/search", tags=["music"])
    async def search(
        request: Request,
        q: Annotated[str, Query(min_length=1, max_length=200)],
        limit: Annotated[int, Query(ge=1, le=50)] = 15,
        user_id: Annotated[int | None, Query(gt=0)] = None,
    ) -> list[dict[str, Any]]:
        cleaned_query = " ".join(q.split())
        if not cleaned_query:
            raise HTTPException(
                status_code=422, detail="Query must not be empty"
            )

        try:
            tracks = await search_tracks(cleaned_query, limit=limit)
        except AudioServiceError as exc:
            logger.warning("Search failed for %r: %s", cleaned_query, exc)
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        database: Database = request.app.state.db
        history_user_id = _resolve_optional_user_id(request, user_id)
        if history_user_id is not None and tracks:
            try:
                await _store_search_query(database, history_user_id, cleaned_query)
            except Exception:
                logger.warning(
                    "Failed to store search history for %s", history_user_id
                )

        return tracks

    @app.get("/api/trending", tags=["music"])
    async def trending(
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
    ) -> list[dict[str, Any]]:
        return await get_trending_tracks(limit=limit)

    @app.get("/api/thumb", tags=["music"])
    async def thumb(
        u: Annotated[str, Query(min_length=1, max_length=2000)],
    ) -> Response:
        """Proxy cover thumbnails through the backend (and Xray proxy)."""
        target = u.strip()
        if not _is_allowed_thumb_url(target):
            raise HTTPException(
                status_code=400, detail="Unsupported thumbnail host"
            )

        try:
            content, media_type = await fetch_thumbnail_bytes(target)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.debug("Thumbnail proxy fallback for %r: %s", target, exc)
            return RedirectResponse(url=target, status_code=302)

        return Response(
            content=content,
            media_type=media_type,
            headers={"Cache-Control": "public, max-age=86400"},
        )

    # ------------------------------------------------------------ my wave

    @app.get("/api/wave/mood", tags=["wave"])
    async def wave_mood(
        vibe: Annotated[
            Literal["calm", "aggressive", "energetic", "sad"], Query()
        ],
        limit: Annotated[int, Query(ge=1, le=50)] = 15,
    ) -> list[dict[str, Any]]:
        try:
            tracks = await get_wave_by_mood(vibe, limit=limit)
        except AudioServiceError as exc:
            logger.warning("Wave mood failed for %r: %s", vibe, exc)
            tracks = await get_trending_tracks(limit=limit)
            if not tracks:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
        return tracks

    @app.get("/api/wave/personalized", tags=["wave"])
    async def wave_personalized(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=50)] = 15,
        user_id: Annotated[str | None, Query(max_length=32)] = None,
    ) -> list[dict[str, Any]]:
        database: Database = request.app.state.db
        fallback_user_id = _soft_user_id(user_id)
        resolved_user_id = _resolve_optional_user_id(request, fallback_user_id)

        favorites: list[dict[str, Any]] = []
        if resolved_user_id is not None:
            favorites = await database.list_favorites(resolved_user_id)
        try:
            tracks = await get_wave_personalized(favorites, limit=limit)
        except AudioServiceError as exc:
            logger.warning("Personalized wave failed: %s", exc)
            tracks = []

        if not tracks:
            tracks = await get_trending_tracks(limit=limit)
        return tracks

    # --------------------------------------------------------------- streaming

    @app.get("/api/stream", tags=["music"])
    async def stream_query(
        track_id: Annotated[str, Query(min_length=1, max_length=500)],
    ) -> FileResponse:
        """Stream track via query parameter (safe for colons, slashes and URLs)."""
        return await _serve_audio_stream(track_id)

    @app.get("/api/stream/{track_id:path}", tags=["music"])
    async def stream_path(track_id: str) -> FileResponse:
        """Backward-compatible path-based stream route."""
        return await _serve_audio_stream(track_id)

    # --------------------------------------------------------------- favorites

    @app.get("/api/favorites", tags=["favorites"])
    async def list_favorites(
        request: Request,
        user_id: Annotated[int | None, Query(gt=0)] = None,
    ) -> list[dict[str, Any]]:
        database: Database = request.app.state.db
        resolved_user_id = _resolve_user_id(request, user_id)
        return await database.list_favorites(resolved_user_id)

    @app.post("/api/favorites", tags=["favorites"])
    async def add_favorite(
        payload: FavoriteCreate,
        request: Request,
    ) -> dict[str, Any]:
        database: Database = request.app.state.db
        resolved_user_id = _resolve_user_id(request, payload.user_id)
        track = _favorite_track(payload)

        try:
            added = await database.add_favorite(resolved_user_id, track)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        return {"added": added, "track": track}

    @app.delete("/api/favorites", tags=["favorites"])
    async def remove_favorite_query(
        request: Request,
        track_id: Annotated[str, Query(min_length=1, max_length=500)],
        user_id: Annotated[int | None, Query(gt=0)] = None,
    ) -> dict[str, bool]:
        cleaned_track_id = track_id.strip()
        if not cleaned_track_id:
            raise HTTPException(status_code=422, detail="track_id is required")

        database: Database = request.app.state.db
        resolved_user_id = _resolve_user_id(request, user_id)
        removed = await database.remove_favorite(resolved_user_id, cleaned_track_id)
        return {"removed": removed}

    @app.delete("/api/favorites/{track_id:path}", tags=["favorites"])
    async def remove_favorite_path(
        track_id: str,
        request: Request,
        user_id: Annotated[int | None, Query(gt=0)] = None,
    ) -> dict[str, bool]:
        cleaned_track_id = track_id.strip()
        if not cleaned_track_id:
            raise HTTPException(status_code=422, detail="track_id is required")

        database: Database = request.app.state.db
        resolved_user_id = _resolve_user_id(request, user_id)
        removed = await database.remove_favorite(resolved_user_id, cleaned_track_id)
        return {"removed": removed}

    # ------------------------------------------------------ search history

    @app.get("/api/search-history", tags=["history"])
    async def get_search_history(
        request: Request,
        user_id: Annotated[int | None, Query(gt=0)] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_SEARCH_HISTORY)] = 10,
    ) -> list[dict[str, Any]]:
        database: Database = request.app.state.db
        resolved_user_id = _resolve_user_id(request, user_id)
        return await database.list_search_history(resolved_user_id, limit=limit)

    @app.delete("/api/search-history", tags=["history"])
    async def delete_search_history(
        request: Request,
        user_id: Annotated[int | None, Query(gt=0)] = None,
        query: Annotated[str | None, Query(max_length=200)] = None,
    ) -> dict[str, Any]:
        cleaned_query = " ".join(query.split()) if query else ""

        database: Database = request.app.state.db
        resolved_user_id = _resolve_user_id(request, user_id)

        if cleaned_query:
            deleted = await database.delete_search_query(
                resolved_user_id, cleaned_query
            )
        else:
            deleted = await database.clear_search_history(resolved_user_id)
        return {"deleted": deleted}

    # ------------------------------------------------------------ playlists

    @app.get("/api/playlists", tags=["playlists"])
    async def list_playlists(
        request: Request,
        user_id: Annotated[int | None, Query(gt=0)] = None,
    ) -> list[dict[str, Any]]:
        database: Database = request.app.state.db
        resolved_user_id = _resolve_user_id(request, user_id)
        return await database.list_playlists(resolved_user_id)

    @app.post("/api/playlists", tags=["playlists"])
    async def create_playlist(
        payload: PlaylistCreate,
        request: Request,
    ) -> dict[str, Any]:
        database: Database = request.app.state.db
        resolved_user_id = _resolve_user_id(request, payload.user_id)

        try:
            playlist = await database.create_playlist(resolved_user_id, payload.title)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        playlist["track_count"] = 0
        return playlist

    @app.delete("/api/playlists/{playlist_id}", tags=["playlists"])
    async def delete_playlist(
        playlist_id: int,
        request: Request,
        user_id: Annotated[int | None, Query(gt=0)] = None,
    ) -> dict[str, bool]:
        database: Database = request.app.state.db
        resolved_user_id = _resolve_user_id(request, user_id)
        removed = await database.delete_playlist(resolved_user_id, playlist_id)
        if not removed:
            raise HTTPException(status_code=404, detail="Playlist not found")
        return {"removed": True}

    @app.get("/api/playlists/{playlist_id}/tracks", tags=["playlists"])
    async def list_playlist_tracks(
        playlist_id: int,
        request: Request,
        user_id: Annotated[int | None, Query(gt=0)] = None,
    ) -> list[dict[str, Any]]:
        database: Database = request.app.state.db
        resolved_user_id = _resolve_user_id(request, user_id)
        tracks = await database.list_playlist_tracks(resolved_user_id, playlist_id)
        if tracks is None:
            raise HTTPException(
                status_code=404,
                detail="Playlist not found or not owned by the user",
            )
        return tracks

    @app.post("/api/playlists/{playlist_id}/tracks", tags=["playlists"])
    async def add_playlist_track(
        playlist_id: int,
        payload: FavoriteCreate,
        request: Request,
    ) -> dict[str, Any]:
        database: Database = request.app.state.db
        resolved_user_id = _resolve_user_id(request, payload.user_id)
        track = _favorite_track(payload)

        try:
            added = await database.add_track_to_playlist(
                resolved_user_id, playlist_id, track
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        if added is None:
            raise HTTPException(
                status_code=404,
                detail="Playlist not found or not owned by the user",
            )
        return {"added": added, "track": track}

    @app.delete(
        "/api/playlists/{playlist_id}/tracks", tags=["playlists"]
    )
    async def remove_playlist_track_query(
        playlist_id: int,
        request: Request,
        track_id: Annotated[str, Query(min_length=1, max_length=500)],
        user_id: Annotated[int | None, Query(gt=0)] = None,
    ) -> dict[str, bool]:
        cleaned_track_id = track_id.strip()
        if not cleaned_track_id:
            raise HTTPException(status_code=422, detail="track_id is required")

        database: Database = request.app.state.db
        resolved_user_id = _resolve_user_id(request, user_id)

        if not await database.playlist_owned(resolved_user_id, playlist_id):
            raise HTTPException(
                status_code=404,
                detail="Playlist not found or not owned by the user",
            )
        removed = await database.remove_track_from_playlist(
            resolved_user_id, playlist_id, cleaned_track_id
        )
        return {"removed": removed}

    @app.delete(
        "/api/playlists/{playlist_id}/tracks/{track_id:path}", tags=["playlists"]
    )
    async def remove_playlist_track_path(
        playlist_id: int,
        track_id: str,
        request: Request,
        user_id: Annotated[int | None, Query(gt=0)] = None,
    ) -> dict[str, bool]:
        cleaned_track_id = track_id.strip()
        if not cleaned_track_id:
            raise HTTPException(status_code=422, detail="track_id is required")

        database: Database = request.app.state.db
        resolved_user_id = _resolve_user_id(request, user_id)

        if not await database.playlist_owned(resolved_user_id, playlist_id):
            raise HTTPException(
                status_code=404,
                detail="Playlist not found or not owned by the user",
            )
        removed = await database.remove_track_from_playlist(
            resolved_user_id, playlist_id, cleaned_track_id
        )
        return {"removed": removed}

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/app/", status_code=302)

    @app.get("/app", include_in_schema=False)
    async def app_slash_redirect() -> RedirectResponse:
        return RedirectResponse(url="/app/", status_code=302)

    app.mount(
        "/app",
        StaticFiles(directory=resolved_frontend, html=True),
        name="frontend",
    )

    return app