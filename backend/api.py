"""FastAPI application: health, search, audio proxy and favorites API."""

from __future__ import annotations

import asyncio
import logging
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, AsyncIterator, Callable, Literal
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config import BASE_DIR, get_auth_mode, get_bot_token, load_settings

from .audio_service import (
    AudioServiceError,
    AudioStream,
    close_stream_client,
    get_audio_stream_url,
    get_trending_tracks,
    get_wave_by_mood,
    get_wave_personalized,
    search_tracks,
    start_audio_stream,
)
from .db import MAX_SEARCH_HISTORY, Database
from .telegram_auth import validate_init_data

APP_VERSION = "0.1.0"
logger = logging.getLogger(__name__)

LifespanFactory = Callable[[FastAPI], AbstractAsyncContextManager[None]]


class FavoriteCreate(BaseModel):
    """Payload for adding a track to favorites."""

    # In fallback mode (local development/tests) the sender's id comes from
    # the body; with a configured bot token the id is taken from signed
    # initData instead and this field is ignored.
    user_id: int | None = Field(default=None, gt=0)
    track_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    artist: str = Field(default="", max_length=300)
    duration: int = Field(default=0, ge=0)
    thumbnail: str = Field(default="", max_length=2000)
    url: str = Field(default="", max_length=2000)


class PlaylistCreate(BaseModel):
    """Payload for creating a playlist (title is trimmed/limited in the db)."""

    user_id: int | None = Field(default=None, gt=0)
    title: str = Field(min_length=1, max_length=100)


def _init_data_from_request(request: Request) -> str:
    """Extract raw initData from the ``Authorization: tma <initData>`` header."""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("tma "):
        return header[4:].strip()
    return ""


def _resolve_user_id(request: Request, fallback_user_id: int | None) -> int:
    """Resolve the Telegram user id shared by the favorites endpoints.

    * With a configured bot token (``AUTH_MODE=auto``) the id MUST come from
      valid signed initData; anything else is ``401`` \u2014 the explicit
      ``user_id`` from the request is never trusted.
    * Without a token (local development/tests) \u2014 or with ``AUTH_MODE=off``
      \u2014 the explicit ``fallback_user_id`` is used; a missing one is ``422``.
    """
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
        raise HTTPException(
            status_code=422, detail="user_id is required"
        )
    return fallback_user_id


def _resolve_optional_user_id(
    request: Request, fallback_user_id: int | None
) -> int | None:
    """Resolve the user id for best-effort side effects (search history).

    Unlike :func:`_resolve_user_id` this never rejects the request: search is
    a public endpoint. In strict initData mode the side effect is simply
    skipped when the caller cannot be verified; in dev mode (and with
    ``AUTH_MODE=off``) the explicit fallback id wins.
    """
    if get_bot_token() and get_auth_mode() != "off":
        init_data = _init_data_from_request(request)
        if not init_data:
            return None
        result = validate_init_data(init_data, get_bot_token())
        if not result.ok:
            logger.warning("Search history skipped: %s", result.reason)
            return None
        return result.user_id
    return fallback_user_id


def _soft_user_id(raw_user_id: str | None) -> int | None:
    """Parse an explicit ``user_id`` query value without raising errors.

    Unlike Pydantic's strict ``int`` parsing this accepts an absent, empty
    or malformed value and just returns ``None``: a client that appends
    ``user_id=`` in the wrong place (or double-appends after ``?limit=``)
    must not crash the request with a 422.
    """
    text = str(raw_user_id or "").strip()
    if not text:
        return None
    try:
        value = int(text)
    except ValueError:
        return None
    return value if value > 0 else None


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


def create_app(
    db_path: str | Path | None = None,
    frontend_dir: str | Path | None = None,
    lifespan: LifespanFactory | None = None,
) -> FastAPI:
    """Build the FastAPI application with an isolated database connection.

    A custom ``lifespan`` lets the entry point share one database connection
    with the Telegram bot and own the polling task; the default lifespan keeps
    standalone API usage and tests self-contained.
    """
    resolved_db_path = Path(db_path) if db_path is not None else load_settings().db_path
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

        # Warm the trending cache right after the loop starts: the chart
        # fetches and yt-dlp lookups take tens of seconds, so without this
        # the very first /api/trending request hits the tunnel timeout and
        # the Mini App shows "Ошибка загрузки". From now on it is usually
        # served from the hot cache.
        warm_task = asyncio.create_task(get_trending_tracks(20))

        try:
            yield
        finally:
            warm_task.cancel()
            await close_stream_client()
            await database.close()

    app = FastAPI(
        title="TG Music API",
        description="Backend for the Telegram Mini App music player.",
        version=APP_VERSION,
        lifespan=lifespan or default_lifespan,
    )

    # The Mini App is served from the same origin, but CORS keeps the API
    # usable when the frontend is opened from a tunnel or a separate origin.
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
        """Lightweight readiness endpoint used by uptime checks and probes."""
        return {"status": "ok"}

    @app.get(
        "/v1/models",
        tags=["system"],
        include_in_schema=False,
    )
    async def stub_models() -> dict[str, Any]:
        """Quiet stub for local probes that expect an OpenAI-compatible API."""
        return {"object": "list", "data": []}

    @app.get("/api/search", tags=["music"])
    async def search(
        request: Request,
        q: Annotated[str, Query(min_length=1, max_length=200)],
        limit: Annotated[int, Query(ge=1, le=50)] = 15,
        user_id: Annotated[int | None, Query(gt=0)] = None,
    ) -> list[dict[str, Any]]:
        """Search tracks through yt-dlp and return normalized metadata.

        Any identified user (initData in production, ``user_id`` fallback in
        dev) gets the successful non-empty query saved into its search
        history; the history step is best-effort and never fails the search.
        """
        cleaned_query = " ".join(q.split())
        if not cleaned_query:
            raise HTTPException(
                status_code=422, detail="Query must not be empty"
            )

        try:
            tracks = await search_tracks(cleaned_query, limit=limit)
        except AudioServiceError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc        # Search succeeded (even with zero results): remember the query.
        database: Database = request.app.state.db
        history_user_id = _resolve_optional_user_id(request, user_id)
        if history_user_id is not None:
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
        """Return the real trending list cached in backend memory."""
        return await get_trending_tracks(limit=limit)

    # ------------------------------------------------------------ my wave

    @app.get("/api/wave/mood", tags=["wave"])
    async def wave_mood(
        vibe: Annotated[
            Literal["calm", "aggressive", "energetic", "sad"], Query()
        ],
        limit: Annotated[int, Query(ge=1, le=50)] = 15,
    ) -> list[dict[str, Any]]:
        """Return a 15-20 track wave matching the requested vibe.

        The vibe drives several parallel YouTube queries inside the audio
        service; everything is filtered like regular search results, so the
        Mini App can render the list with the standard track cards.
        """
        try:
            return await get_wave_by_mood(vibe, limit=limit)
        except AudioServiceError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/api/wave/personalized", tags=["wave"])
    async def wave_personalized(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=50)] = 15,
        user_id: Annotated[str | None, Query(max_length=32)] = None,
    ) -> list[dict[str, Any]]:
        """Return the personalized wave built from the user's favorites.

        Same ownership rules as the favorites endpoints: the signed initData
        user in production, the explicit ``user_id`` fallback in dev. The
        ``user_id`` parameter is deliberately soft (parsed by
        :func:`_soft_user_id` instead of Pydantic): an absent, empty or
        malformed value never fails the request — the wave is simply built
        without a taste profile (favorites ``[]``) and the fallback chart
        covers it. With no favorites the default trending chart is served
        instead, so the wave is never empty.
        """
        database: Database = request.app.state.db
        fallback_user_id = _soft_user_id(user_id)
        resolved_user_id = _resolve_optional_user_id(request, fallback_user_id)

        favorites: list[dict[str, Any]] = []
        if resolved_user_id is not None:
            favorites = await database.list_favorites(resolved_user_id)
        try:
            tracks = await get_wave_personalized(favorites, limit=limit)
        except AudioServiceError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        if not tracks:
            # Graceful fallback: a fresh user (or an all-artists-less
            # profile) still gets a playable wave — the "Популярное" chart.
            tracks = await get_trending_tracks(limit=limit)
        return tracks

    @app.get("/api/stream/{track_id}", tags=["music"])
    async def stream(track_id: str, request: Request) -> StreamingResponse:
        """Resolve the direct audio URL and proxy it with Range support.

        YouTube-signed URLs expire after a while, so a 401/403/410 from the
        CDN triggers one fresh yt-dlp re-resolution before the endpoint gives
        up (browsers surface 502 without retrying on their own).
        """
        cleaned_track_id = track_id.strip()
        if not cleaned_track_id:
            raise HTTPException(status_code=422, detail="track_id is required")

        range_header = request.headers.get("range")
        audio: AudioStream | None = None
        last_error: Exception | None = None

        for attempt in (1, 2):
            try:
                direct_url = await get_audio_stream_url(cleaned_track_id)
                audio = await start_audio_stream(direct_url, range_header)
                break
            except AudioServiceError as exc:
                last_error = exc
                if attempt == 1 and isinstance(
                    exc.__cause__, asyncio.TimeoutError
                ):
                    # A timed-out yt-dlp resolution is likely transient
                    # (network hiccup); a plain resolve failure (removed
                    # video, etc.) is not worth the extra latency.
                    logger.warning(
                        "Stream resolve timed out for %r, retrying once",
                        cleaned_track_id,
                    )
                    continue
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 416:
                    raise HTTPException(
                        status_code=416,
                        detail="Requested range not satisfiable",
                    ) from exc
                if (
                    exc.response.status_code in (401, 403, 410)
                    and attempt == 1
                ):
                    # The signed URL is almost certainly stale: force a
                    # fresh yt-dlp resolution instead of failing playback.
                    logger.warning(
                        "Upstream %d for %r; re-resolving the signed URL",
                        exc.response.status_code,
                        cleaned_track_id,
                    )
                    last_error = exc
                    continue
                last_error = exc
                break

        if audio is None:
            if isinstance(last_error, httpx.HTTPStatusError):
                raise HTTPException(
                    status_code=502,
                    detail="Upstream audio server returned an error",
                ) from last_error
            raise HTTPException(
                status_code=502, detail=str(last_error)
            ) from last_error

        headers = dict(audio.headers)
        headers.setdefault("Accept-Ranges", "bytes")

        return StreamingResponse(
            audio.body,
            status_code=audio.status_code,
            headers=headers,
            media_type=audio.media_type,
        )

    @app.get("/api/favorites", tags=["favorites"])
    async def list_favorites(
        request: Request,
        user_id: Annotated[int | None, Query(gt=0)] = None,
    ) -> list[dict[str, Any]]:
        """Return the user's saved tracks.

        The owner of the list is the signed initData user when the bot token
        is configured; otherwise the explicit ``user_id`` query parameter.
        """
        database: Database = request.app.state.db
        resolved_user_id = _resolve_user_id(request, user_id)
        return await database.list_favorites(resolved_user_id)

    @app.post("/api/favorites", tags=["favorites"])
    async def add_favorite(
        payload: FavoriteCreate,
        request: Request,
    ) -> dict[str, Any]:
        """Add a track to favorites (idempotent)."""
        database: Database = request.app.state.db
        resolved_user_id = _resolve_user_id(request, payload.user_id)
        track = _favorite_track(payload)

        try:
            added = await database.add_favorite(resolved_user_id, track)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        return {"added": added, "track": track}

    @app.delete("/api/favorites/{track_id}", tags=["favorites"])
    async def remove_favorite(
        track_id: str,
        request: Request,
        user_id: Annotated[int | None, Query(gt=0)] = None,
    ) -> dict[str, bool]:
        """Remove a track from the user's favorites."""
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
        """Return the user's recent search queries, newest first.

        The resolved limit is additionally clamped to ``MAX_SEARCH_HISTORY``
        by the repository, so the frontend cannot pull the whole table.
        """
        database: Database = request.app.state.db
        resolved_user_id = _resolve_user_id(request, user_id)
        return await database.list_search_history(resolved_user_id, limit=limit)

    @app.delete("/api/search-history", tags=["history"])
    async def delete_search_history(
        request: Request,
        user_id: Annotated[int | None, Query(gt=0)] = None,
        query: Annotated[str | None, Query(max_length=200)] = None,
    ) -> dict[str, Any]:
        """Clear the search history, or only one query when ``query`` is set."""
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
        """Return the user's playlists with a track count for each."""
        database: Database = request.app.state.db
        resolved_user_id = _resolve_user_id(request, user_id)
        return await database.list_playlists(resolved_user_id)

    @app.post("/api/playlists", tags=["playlists"])
    async def create_playlist(
        payload: PlaylistCreate,
        request: Request,
    ) -> dict[str, Any]:
        """Create an empty playlist owned by the resolved user."""
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
        """Delete the user's playlist together with its tracks (cascade)."""
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
        """Return the playlist's tracks in insertion order."""
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
        """Append a track to the end of the playlist (idempotent)."""
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
        "/api/playlists/{playlist_id}/tracks/{track_id}", tags=["playlists"]
    )
    async def remove_playlist_track(
        playlist_id: int,
        track_id: str,
        request: Request,
        user_id: Annotated[int | None, Query(gt=0)] = None,
    ) -> dict[str, bool]:
        """Remove a track from the user's playlist."""
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
        """Send users opening the base URL to the Mini App."""
        return RedirectResponse(url="/app/")

    # API routes are declared above the mount so /api/* is never handled by
    # the static files application.
    app.mount(
        "/app",
        StaticFiles(directory=resolved_frontend, html=True),
        name="frontend",
    )

    return app
