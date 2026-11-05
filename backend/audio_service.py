"""Async yt-dlp wrapper and HTTP audio-stream proxy.

The public API is intentionally small:

* :func:`search_tracks` - search YouTube without downloading media.
* :func:`get_audio_stream_url` - resolve a direct audio URL through yt-dlp.
* :func:`start_audio_stream` - open an upstream stream and return metadata
  together with a self-closing body iterator (convenient for FastAPI).
* :func:`proxy_audio_stream` - async generator that proxies the bytes and
  forwards the ``Range`` header, so the browser can seek in the player.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
import yt_dlp

logger = logging.getLogger(__name__)

MAX_SEARCH_RESULTS = 50
MIN_TRACK_SECONDS = 45
MAX_TRACK_SECONDS = 900
SEARCH_POOL_SIZE = 30
CHUNK_SIZE = 64 * 1024
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
STREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)

_FORWARDED_HEADERS = {
    "content-type": "Content-Type",
    "content-length": "Content-Length",
    "content-range": "Content-Range",
    "accept-ranges": "Accept-Ranges",
    "etag": "ETag",
    "last-modified": "Last-Modified",
}


class AudioServiceError(RuntimeError):
    """Raised when yt-dlp cannot search or resolve an audio stream."""


@dataclass(slots=True)
class AudioStream:
    """Metadata and body of an upstream audio response."""

    status_code: int
    headers: dict[str, str]
    media_type: str
    body: AsyncIterator[bytes]


# --------------------------------------------------------------------- search


def _coerce_duration(value: Any) -> int:
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        return 0
    return max(0, seconds)


def _best_thumbnail(entry: Mapping[str, Any]) -> str:
    direct = entry.get("thumbnail")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    thumbnails = entry.get("thumbnails")
    if isinstance(thumbnails, list):
        # yt-dlp usually orders thumbnails from smallest to largest.
        for thumbnail in reversed(thumbnails):
            if not isinstance(thumbnail, Mapping):
                continue
            url = thumbnail.get("url")
            if isinstance(url, str) and url.strip():
                return url.strip()

    return ""


def _refine_search_query(query: str) -> str:
    """Add a music hint unless the user already asked for audio or lyrics."""
    lowered = query.lower()
    if "official audio" in lowered or "lyrics" in lowered:
        return query
    if re.search(r"\baudio\b", lowered):
        return query
    return f"{query} audio"


def _entry_duration(entry: Mapping[str, Any]) -> int | None:
    """Return a usable duration, or None for unknown/live/zero-length items."""
    raw = entry.get("duration")
    if raw is None:
        return None
    try:
        seconds = int(float(raw))
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def _is_music_track(entry: Mapping[str, Any]) -> bool:
    """Filter out live streams, Shorts and long mixes."""
    if entry.get("live_status") == "is_live" or entry.get("is_live"):
        return False

    duration = _entry_duration(entry)
    if duration is None:
        return False
    return MIN_TRACK_SECONDS <= duration <= MAX_TRACK_SECONDS


def _search_tracks_sync(query: str, limit: int) -> list[dict[str, Any]]:
    params = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "noplaylist": True,
    }

    search_query = _refine_search_query(query)
    # Ask for extra results: live streams, Shorts and mixes are filtered out
    # in Python, so a smaller pool may not yield the requested number of tracks.
    pool_size = min(MAX_SEARCH_RESULTS, max(SEARCH_POOL_SIZE, limit * 2))

    try:
        with yt_dlp.YoutubeDL(params) as ydl:
            info = ydl.extract_info(
                f"ytsearch{pool_size}:{search_query}", download=False
            )
    except Exception as exc:  # yt-dlp raises different exception types
        raise AudioServiceError(f"Не удалось выполнить поиск: {query!r}") from exc

    if not info:
        return []

    entries = info.get("entries") or []
    results: list[dict[str, Any]] = []
    skipped = 0

    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        if not _is_music_track(entry):
            skipped += 1
            continue

        track_id = str(entry.get("id") or "").strip()
        if not track_id:
            skipped += 1
            continue

        results.append(
            {
                "track_id": track_id,
                "title": str(entry.get("title") or "Unknown title").strip(),
                "artist": str(
                    entry.get("uploader")
                    or entry.get("channel")
                    or entry.get("artist")
                    or ""
                ).strip(),
                "duration": _coerce_duration(entry.get("duration")),
                "thumbnail": _best_thumbnail(entry),
            }
        )
        if len(results) >= limit:
            break

    logger.debug(
        "Search %r -> %r: kept %d, skipped %d of %d",
        query,
        search_query,
        len(results),
        skipped,
        len(entries),
    )
    return results


async def search_tracks(query: str, limit: int = 15) -> list[dict[str, Any]]:
    """Search for tracks and return normalized metadata.

    yt-dlp is blocking, so it runs in a worker thread and never blocks the
    FastAPI event loop.
    """
    cleaned_query = " ".join(str(query or "").split())
    if not cleaned_query:
        return []

    try:
        safe_limit = int(limit)
    except (TypeError, ValueError):
        safe_limit = 15
    safe_limit = max(1, min(safe_limit, MAX_SEARCH_RESULTS))

    logger.debug("Searching yt-dlp: query=%r limit=%d", cleaned_query, safe_limit)
    return await asyncio.to_thread(_search_tracks_sync, cleaned_query, safe_limit)


# ---------------------------------------------------------------- stream URL


def _track_webpage_url(track_id: str) -> str:
    """Accept both a full URL and a bare YouTube video id."""
    if "://" in track_id:
        return track_id
    return f"https://www.youtube.com/watch?v={quote(track_id, safe='')}"


def _get_audio_stream_url_sync(track_id: str) -> str:
    params = {
        "format": "bestaudio[ext=m4a]/bestaudio",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
    }

    try:
        with yt_dlp.YoutubeDL(params) as ydl:
            info = ydl.extract_info(
                _track_webpage_url(track_id), download=False
            )
    except Exception as exc:  # yt-dlp raises different exception types
        raise AudioServiceError(
            f"Не удалось получить аудиопоток для {track_id!r}"
        ) from exc

    if not isinstance(info, Mapping):
        raise AudioServiceError(f"Пустой ответ yt-dlp для {track_id!r}")

    # noplaylist=True should prevent this, but keep a defensive fallback.
    entries = info.get("entries")
    if entries:
        for entry in entries:
            if isinstance(entry, Mapping):
                info = entry
                break

    stream_url = info.get("url")
    if not stream_url:
        requested = info.get("requested_downloads") or []
        if requested and isinstance(requested[0], Mapping):
            stream_url = requested[0].get("url")

    if not stream_url:
        formats = info.get("formats") or []
        audio_formats = [
            fmt
            for fmt in formats
            if isinstance(fmt, Mapping)
            and fmt.get("url")
            and fmt.get("acodec") not in (None, "none")
        ]
        if audio_formats:
            m4a_formats = [
                fmt for fmt in audio_formats if fmt.get("ext") == "m4a"
            ]
            stream_url = (m4a_formats or audio_formats)[-1].get("url")

    if not stream_url:
        raise AudioServiceError(f"yt-dlp не вернул прямой URL для {track_id!r}")

    return str(stream_url)


async def get_audio_stream_url(track_id: str) -> str:
    """Resolve a direct audio URL for ``track_id`` without downloading it."""
    cleaned_track_id = str(track_id or "").strip()
    if not cleaned_track_id:
        raise ValueError("track_id is required")

    return await asyncio.to_thread(
        _get_audio_stream_url_sync, cleaned_track_id
    )


# -------------------------------------------------------------------- proxying


def _request_headers(range_header: str | None) -> dict[str, str]:
    headers = {
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "User-Agent": BROWSER_USER_AGENT,
    }
    value = (range_header or "").strip()
    if value:
        headers["Range"] = value
    return headers


def _response_headers(response: httpx.Response) -> dict[str, str]:
    headers = {
        output_name: response.headers[input_name]
        for input_name, output_name in _FORWARDED_HEADERS.items()
        if input_name in response.headers
    }
    headers.setdefault(
        "Accept-Ranges", response.headers.get("accept-ranges", "bytes") or "bytes"
    )
    return headers


def _media_type(response: httpx.Response) -> str:
    content_type = response.headers.get("content-type", "audio/mp4")
    media_type = content_type.split(";", 1)[0].strip() or "audio/mp4"
    # YouTube sometimes labels m4a audio as video/mp4.
    if media_type in {"video/mp4", "application/mp4"}:
        return "audio/mp4"
    return media_type


async def start_audio_stream(
    url: str,
    range_header: str | None = None,
    *,
    client: httpx.AsyncClient | None = None,
    chunk_size: int = CHUNK_SIZE,
) -> AudioStream:
    """Open an upstream audio stream and return its status, headers and body.

    The returned body iterator closes the upstream response (and the client,
    when it was created here) in a ``finally`` block, so it is safe to hand it
    to :class:`fastapi.responses.StreamingResponse`.
    """
    target = str(url or "").strip()
    if not target:
        raise ValueError("url is required")
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=STREAM_TIMEOUT,
        )

    response: httpx.Response | None = None
    try:
        request = client.build_request(
            "GET", target, headers=_request_headers(range_header)
        )
        response = await client.send(request, stream=True)
        response.raise_for_status()
    except Exception:
        if response is not None:
            await response.aclose()
        if owns_client:
            await client.aclose()
        raise

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in response.aiter_raw(chunk_size=chunk_size):
                if chunk:
                    yield chunk
        finally:
            await response.aclose()
            if owns_client:
                await client.aclose()

    return AudioStream(
        status_code=response.status_code,
        headers=_response_headers(response),
        media_type=_media_type(response),
        body=body(),
    )


async def proxy_audio_stream(
    url: str,
    range_header: str | None = None,
    *,
    client: httpx.AsyncClient | None = None,
    chunk_size: int = CHUNK_SIZE,
) -> AsyncIterator[bytes]:
    """Yield proxied audio chunks, forwarding ``Range`` for seeking.

    The upstream response status/headers are available through
    :func:`start_audio_stream`; this generator is the minimal streaming API.
    """
    stream = await start_audio_stream(
        url,
        range_header,
        client=client,
        chunk_size=chunk_size,
    )
    async for chunk in stream.body:
        yield chunk
