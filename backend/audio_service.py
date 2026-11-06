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
import time
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

# Hard ceilings for blocking yt-dlp/httpx work: without them a single wedged
# upstream call would hold a worker thread (and, for trending, the vendored
# request path) forever while clients time out through the tunnel.
SEARCH_TIMEOUT_SECONDS = 30.0
STREAM_RESOLVE_TIMEOUT_SECONDS = 20.0

TRENDING_RESOLVE_POOL_SIZE = 8
TRENDING_MIN_TRACK_SECONDS = 90
TRENDING_MAX_TRACK_SECONDS = 330
TRENDING_CACHE_TTL_SECONDS = 30 * 60
# Timeouts for the three stages of a cache refresh (chart fetch, per-song
# resolution, fallback search). The refresh runs in the background, so these
# bound how long a wedged refresh can occupy the single-flight slot.
TRENDING_CHART_TIMEOUT_SECONDS = 20.0
TRENDING_RESOLVE_TIMEOUT_SECONDS = 30.0
TRENDING_FALLBACK_TIMEOUT_SECONDS = 45.0
# A cold (empty) cache must produce at least some answer before the tunnel
# cuts the request; afterwards the refresh keeps running in the background.
TRENDING_COLD_WAIT_SECONDS = 15.0
# When a refresh finished with no tracks, do not re-run the whole network
# chain on every request for a short cooldown period.
TRENDING_EMPTY_RETRY_COOLDOWN_SECONDS = 60.0

TRENDING_CACHE_LOCK = asyncio.Lock()
TRENDING_CACHE: dict[str, Any] = {
    "tracks": [],
    "updated_at": 0.0,
    "empty_at": 0.0,
}
# Single-flight slot: at most one background refresh exists at a time. The
# variable is read/written under TRENDING_CACHE_LOCK only.
TRENDING_REFRESH_TASK: asyncio.Task[list[dict[str, Any]]] | None = None
# Hard stop-list of title words: chart cutbacks ("Billboard Hot 100 Top 20
# November 2026"), mixes, compilations, talk shows, reviews and podcasts never
# pass as single songs (Russian words block "Что не так с Яндекс Музыкой" etc.).
TITLE_EXCLUDE_RE = re.compile(
    r"\b(?:"
    r"billboard|top\s*100|top\s*50|top\s*20|top\s*10|charts?|countdown|"
    r"compilations?|mix(?:es|tape)?|mashups?|medleys?|playlists?|"
    r"full\s+album|1\s*hour|one\s+hour|non[-\s]stop|live|"
    r"обзор|почему|что\s+не\s+так|где\s+слушать|интервью|подкаст|"
    r"реакция|разбор|плейлист|сборник|микс|чарт|топ\s*\d+"
    r")\b",
    re.IGNORECASE,
)
# Real Russian hits chart served by the public Yandex Music feed.
YANDEX_CHART_URL = "https://music.yandex.ru/handlers/chart.jsx?what=chart&lang=ru"
# Fast backup source: top Russian playlists on Deezer.
DEEZER_PLAYLIST_SEARCH_URLS = (
    "https://api.deezer.com/search/playlist?q=Топ Россия 2026",
    "https://api.deezer.com/search/playlist?q=Русские хиты",
)
# Stage 2 fallback: one specific hit per query, all different artists, so the
# list never degenerates into several songs by the same performer.
TRENDING_FALLBACK_QUERIES = (
    "Баста Моя Мишель - Зима в сердце official audio",
    "MACAN - ASPHALT 8 official audio",
    "Три дня дождя - Отпускай official audio",
    "ANNA ASTI - Царица official audio",
    "Miyagi Эндшпиль - Silhouette official audio",
    "XOLIDAYBOY - Пожары official audio",
    "Женя Трофимов Комната культуры - Поезда official audio",
    "Моя Мишель - Ветер меняет направление official audio",
    "Инстасамка - За деньги да official audio",
    "Король и Шут - Кукла колдуна official audio",
    "Кино - Группа крови official audio",
    "Zivert - Life official audio",
    "Jakone A.V.G - По весне official audio",
    "Bearwolf - Годзилла official audio",
    "Монеточка - Это было в России official audio",
    "Скриптонит - Это любовь official audio",
    "Мот - Соло official audio",
    "Ваня Дмитриенко - Венера-Юпитер official audio",
    "Artik & Asti - Девочка, танцуй official audio",
    "Гио Пика - Космос official audio",
)

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
STREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)

# The streaming proxy reuses one client across requests: creating a fresh
# client per stream would redo the TCP+TLS handshake against the CDN on
# every play/seek request.
_stream_client: httpx.AsyncClient | None = None
_stream_client_lock = asyncio.Lock()


async def _get_stream_client() -> httpx.AsyncClient:
    """Return the shared asynchronous httpx client for upstream streams.

    The client is created lazily (once per process) and lives until
    :func:`close_stream_client` is called from the app lifespan.
    """
    global _stream_client
    async with _stream_client_lock:
        if _stream_client is None or _stream_client.is_closed:
            _stream_client = httpx.AsyncClient(
                follow_redirects=True,
                timeout=STREAM_TIMEOUT,
            )
        return _stream_client


async def close_stream_client() -> None:
    """Close the shared stream client, if one is open. Safe to call twice."""
    global _stream_client
    async with _stream_client_lock:
        if _stream_client is not None and not _stream_client.is_closed:
            await _stream_client.aclose()
        _stream_client = None


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

    title = str(entry.get("title") or "").strip()
    if TITLE_EXCLUDE_RE.search(title):
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
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_search_tracks_sync, cleaned_query, safe_limit),
            timeout=SEARCH_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError as exc:
        raise AudioServiceError(
            f"Поиск занял слишком много времени и был прерван: "
            f"{cleaned_query!r}"
        ) from exc


# ------------------------------------------------------------------ trending


def _is_trending_track(entry: Mapping[str, Any]) -> bool:
    """Strict two-stage trending filter for YouTube-search candidates."""
    if not _is_music_track(entry):
        return False

    duration = _entry_duration(entry)
    if duration is None:
        return False
    if not (
        TRENDING_MIN_TRACK_SECONDS
        <= duration
        <= TRENDING_MAX_TRACK_SECONDS
    ):
        return False

    title = str(entry.get("title") or "").strip()
    if TITLE_EXCLUDE_RE.search(title):
        return False
    return True


def _entry_to_track(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Map one yt-dlp entry to the compact track shape used by the app."""
    return {
        "track_id": str(entry.get("id") or "").strip(),
        "title": str(entry.get("title") or "").strip(),
        "artist": str(
            entry.get("uploader")
            or entry.get("channel")
            or entry.get("artist")
            or ""
        ).strip(),
        "duration": _coerce_duration(entry.get("duration")),
        "thumbnail": _best_thumbnail(entry),
    }


def _chart_track_from_entry(item: Mapping[str, Any], entry: Mapping[str, Any]):
    """Use Deezer/Apple chart metadata, but resolve the playable YouTube id here."""
    if not _is_trending_track(entry):
        return None

    track_id = str(entry.get("id") or "").strip()
    if not track_id:
        return None

    return {
        "track_id": track_id,
        "title": str(item.get("title") or entry.get("title") or "").strip(),
        "artist": str(
            item.get("artist")
            or entry.get("uploader")
            or entry.get("channel")
            or ""
        ).strip(),
        "duration": _coerce_duration(entry.get("duration")),
        "thumbnail": _best_thumbnail(entry),
    }


def _fetch_yandex_chart_sync(limit: int) -> list[dict[str, str]]:
    """Return the real Russian hits chart from the public Yandex Music feed."""
    if limit <= 0:
        return []

    try:
        with httpx.Client(timeout=STREAM_TIMEOUT) as client:
            response = client.get(
                YANDEX_CHART_URL,
                headers={
                    "Accept": "application/json",
                    "User-Agent": BROWSER_USER_AGENT,
                    "Referer": "https://music.yandex.ru/",
                },
            )
            response.raise_for_status()
            payload = response.json()
    except Exception:
        logger.debug("Yandex chart failed: %s", YANDEX_CHART_URL, exc_info=True)
        return []

    entries: Any = None
    if isinstance(payload, Mapping):
        chart = payload.get("chart")
        if isinstance(chart, Mapping):
            entries = chart.get("tracks")
        if not isinstance(entries, list):
            entries = payload.get("tracks")
    if not isinstance(entries, list):
        return []

    items: list[dict[str, str]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        track = entry.get("track")
        track = track if isinstance(track, Mapping) else entry
        title = str(track.get("title") or track.get("name") or "").strip()
        artists = track.get("artists")
        artist_name = ""
        if isinstance(artists, list):
            for artist in artists:
                if isinstance(artist, Mapping) and artist.get("name"):
                    artist_name = str(artist["name"]).strip()
                    break
        if not artist_name or not title:
            continue
        items.append({"artist": artist_name, "title": title})
        if len(items) >= limit:
            break
    return items


def _fetch_deezer_ru_playlist_sync(limit: int) -> list[dict[str, str]]:
    """Take Russian-chart tracks from a top Russian Deezer playlist."""
    if limit <= 0:
        return []

    try:
        with httpx.Client(timeout=STREAM_TIMEOUT) as client:
            playlist_id: int | None = None
            headers = {
                "Accept": "application/json",
                "User-Agent": BROWSER_USER_AGENT,
            }
            for search_url in DEEZER_PLAYLIST_SEARCH_URLS:
                try:
                    response = client.get(search_url, headers=headers)
                    response.raise_for_status()
                    results = response.json().get("data") or []
                except Exception:
                    logger.debug(
                        "Deezer playlist search failed: %s",
                        search_url,
                        exc_info=True,
                    )
                    continue
                first = next(
                    (
                        item.get("id")
                        for item in results
                        if isinstance(item, Mapping) and item.get("id") is not None
                    ),
                    None,
                )
                if first is not None:
                    playlist_id = int(first)
                    break

            if not playlist_id:
                return []

            response = client.get(
                f"https://api.deezer.com/playlist/{playlist_id}"
                f"/tracks?limit={max(limit, 30)}",
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
    except Exception:
        logger.debug("Deezer RU playlist failed", exc_info=True)
        return []

    data = payload.get("data")
    if not isinstance(data, list):
        return []

    def _has_cyrillic(text: str) -> bool:
        return bool(re.search(r"[а-яё]", text, re.IGNORECASE))

    items: list[dict[str, str]] = []
    for wrapped in data:
        if not isinstance(wrapped, Mapping):
            continue
        track = wrapped.get("track")
        track = track if isinstance(track, Mapping) else wrapped
        artist = track.get("artist")
        artist_name = (
            str(artist.get("name") or "").strip()
            if isinstance(artist, Mapping)
            else ""
        )
        title_name = str(track.get("title") or "").strip()
        if not artist_name or not title_name:
            continue
        # Keep only tracks that clearly belong to the Russian chart.
        if not _has_cyrillic(title_name) and not _has_cyrillic(artist_name):
            continue
        items.append({"artist": artist_name, "title": title_name})
        if len(items) >= limit:
            break
    return items


def _get_chart_items_sync(limit: int) -> list[dict[str, str]]:
    """Real single songs, stage 1: Yandex RU chart first, Deezer RU playlist as fallback."""
    items = _fetch_yandex_chart_sync(limit)
    remaining = limit - len(items)
    if remaining <= 0:
        return items[:limit]

    seen: set[tuple[str, str]] = {
        (item["artist"].lower(), item["title"].lower()) for item in items
    }
    for raw_item in _fetch_deezer_ru_playlist_sync(remaining):
        title = str(raw_item.get("title") or "").strip()
        artist = str(raw_item.get("artist") or "").strip()
        if not title or not artist:
            continue
        # Yandex and the Deezer playlist can host the same song.
        key = (artist.lower(), title.lower())
        if key in seen:
            continue
        seen.add(key)
        items.append({"artist": artist, "title": title})
        if len(items) >= limit:
            break
    return items[:limit]


def _yt_search_entries_sync(query: str, pool: int) -> list[Mapping[str, Any]]:
    """Run one flat yt-dlp search and return the raw entries list."""
    params = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "noplaylist": True,
    }

    try:
        with yt_dlp.YoutubeDL(params) as ydl:
            info = ydl.extract_info(
                f"ytsearch{pool}:{query}", download=False
            )
    except Exception as exc:  # yt-dlp raises different exception types
        logger.debug("Trending search failed: %r", query, exc_info=True)
        raise AudioServiceError(
            f"Не удалось получить тренды: {query!r}"
        ) from exc

    if not info:
        return []

    entries = info.get("entries") or []
    return [entry for entry in entries if isinstance(entry, Mapping)]


def _resolve_chart_track_sync(
    item: Mapping[str, str], query: str
) -> dict[str, Any] | None:
    """One yt-search for a chart song -> track with a real YouTube id."""
    entries = _yt_search_entries_sync(query, 1)
    for entry in entries:
        candidate = _chart_track_from_entry(item, entry)
        if candidate:
            return candidate
    return None


async def _resolve_chart_items(
    chart_items: list[dict[str, str]], limit: int
) -> list[dict[str, Any]]:
    """Resolve chart songs into playable YouTube ids, 8 lookups in parallel."""
    semaphore = asyncio.Semaphore(TRENDING_RESOLVE_POOL_SIZE)

    async def resolve(item: Mapping[str, str]):
        query = f"{item['artist']} - {item['title']} official audio"
        async with semaphore:
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(_resolve_chart_track_sync, item, query),
                    timeout=TRENDING_RESOLVE_TIMEOUT_SECONDS,
                )
            except (asyncio.CancelledError, asyncio.TimeoutError):
                logger.debug(
                    "Trending resolve timed out: %r", item.get("title")
                )
                return None
            except AudioServiceError:
                return None
            except Exception:
                logger.exception(
                    "Unexpected trending search failure: %r", item.get("title")
                )
                return None

    candidates = await asyncio.gather(*(resolve(item) for item in chart_items))

    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    artist_counts: dict[str, int] = {}
    for candidate in candidates:
        if not candidate:
            continue
        track_id = candidate["track_id"]
        if not track_id or track_id in seen:
            continue
        # Diversity cap: at most 2 songs from the same artist in the list.
        artist_key = str(candidate.get("artist") or "").strip().lower()
        if artist_key and artist_counts.get(artist_key, 0) >= 2:
            continue
        if artist_key:
            artist_counts[artist_key] = artist_counts.get(artist_key, 0) + 1
        seen.add(track_id)
        results.append(candidate)
        if len(results) >= limit:
            break
    return results


def _get_trending_from_search_sync(limit: int) -> list[dict[str, Any]]:
    """Stage 2: one specific RU hit per query so the list stays varied."""
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    artist_counts: dict[str, int] = {}
    for query in TRENDING_FALLBACK_QUERIES:
        if len(results) >= limit:
            break
        try:
            # A tiny pool is enough: each query is one well-known song.
            entries = _yt_search_entries_sync(query, 2)
        except AudioServiceError:
            continue
        except Exception:
            logger.exception("Unexpected trending search failure: %r", query)
            continue

        for entry in entries:
            if not isinstance(entry, Mapping) or not _is_trending_track(entry):
                continue
            track = _entry_to_track(entry)
            track_id = track["track_id"]
            if not track_id or track_id in seen:
                continue
            # Diversity cap: at most 2 songs from the same artist.
            artist_key = str(track.get("artist") or "").strip().lower()
            if artist_key and artist_counts.get(artist_key, 0) >= 2:
                continue
            if artist_key:
                artist_counts[artist_key] = artist_counts.get(artist_key, 0) + 1
            seen.add(track_id)
            results.append(track)
            break

    return results[:limit]


def _merge_by_track_id(
    tracks: list[dict[str, Any]], extra: list[dict[str, Any]], limit: int
) -> list[dict[str, Any]]:
    """Append extra tracks in order while dropping duplicates."""
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for track in [*tracks, *extra]:
        track_id = str(track.get("track_id") or "")
        if not track_id or track_id in seen:
            continue
        seen.add(track_id)
        merged.append(track)
        if len(merged) >= limit:
            break
    return merged


async def _refresh_trending_cache(limit: int) -> list[dict[str, Any]]:
    """Fetch and resolve a fresh trending list WITHOUT holding the cache lock.

    This is the single-flight refresh body. It never blocks other requests on
    the lock while doing network work; the cache state is updated under the
    lock only after a successful fetch. On failure (or an empty result) the
    previous cache is left untouched and a short "empty" cooldown is recorded
    so that requests stop re-running the whole network chain.
    """
    tracks: list[dict[str, Any]] = []
    try:
        chart_items = await asyncio.wait_for(
            asyncio.to_thread(
                # Ask the chart source for extra entries: some fail the
                # resolve step, so a larger pool keeps the list full.
                _get_chart_items_sync,
                max(limit, 25),
            ),
            timeout=TRENDING_CHART_TIMEOUT_SECONDS,
        )
        tracks = await _resolve_chart_items(chart_items, limit)
        if len(tracks) < limit:
            fallback_tracks = await asyncio.wait_for(
                asyncio.to_thread(_get_trending_from_search_sync, limit),
                timeout=TRENDING_FALLBACK_TIMEOUT_SECONDS,
            )
            tracks = _merge_by_track_id(tracks, fallback_tracks, limit)
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        logger.warning("Trending refresh timed out")
    except Exception:
        logger.exception("Trending fetch failed")

    if not tracks:
        logger.warning("Trending sources did not return any tracks")
        async with TRENDING_CACHE_LOCK:
            TRENDING_CACHE["empty_at"] = time.monotonic()
        return []

    async with TRENDING_CACHE_LOCK:
        TRENDING_CACHE["tracks"] = list(tracks)
        TRENDING_CACHE["updated_at"] = time.monotonic()
        TRENDING_CACHE["empty_at"] = 0.0
    return tracks


def _start_trending_refresh_locked(limit: int) -> asyncio.Task[list[dict[str, Any]]]:
    """Create the (single) trending refresh task. Must hold the cache lock."""
    task = asyncio.create_task(
        _refresh_trending_cache(limit), name="trending-refresh"
    )
    global TRENDING_REFRESH_TASK
    TRENDING_REFRESH_TASK = task
    return task


async def get_trending_tracks(limit: int = 20) -> list[dict[str, Any]]:
    """Return the trending list using stale-while-revalidate.

    Semantics:

    * fresh cache -> served immediately, no network work;
    * stale cache -> served immediately while one background task refreshes
      it (single-flight: concurrent callers never trigger duplicate
      refreshes, and slow refreshes no longer block this request);
    * cold cache  -> waits a bounded time for the in-flight refresh, then
      gives up without holding anything so the request cannot hit the
      tunnel timeout; the refresh keeps running and fills the cache.
    """
    try:
        safe_limit = int(limit)
    except (TypeError, ValueError):
        safe_limit = 20

    safe_limit = max(1, min(safe_limit, MAX_SEARCH_RESULTS))
    global TRENDING_REFRESH_TASK

    async with TRENDING_CACHE_LOCK:
        cached = list(TRENDING_CACHE["tracks"])
        cache_age = time.monotonic() - TRENDING_CACHE["updated_at"]

        task = TRENDING_REFRESH_TASK
        if task is not None and task.done():
            # Reap the finished refresh; its result is already in the cache.
            task = None

        if task is None:
            empty_age = time.monotonic() - TRENDING_CACHE["empty_at"]
            # The previous refresh returned nothing: throttle retries per cooldown.
            recently_empty = (
                TRENDING_CACHE["empty_at"] > 0
                and empty_age < TRENDING_EMPTY_RETRY_COOLDOWN_SECONDS
            )
            if cached and cache_age <= TRENDING_CACHE_TTL_SECONDS:
                return cached[:safe_limit]
            if recently_empty:
                # No data and the last refresh just failed: honor the cooldown
                # instead of hammering the upstream sources every request.
                return cached[:safe_limit]
            task = _start_trending_refresh_locked(safe_limit)

        # Stale-while-revalidate: keep serving the previous list even when it
        # is older than the TTL, so the frontend never blocks on the refresh.
        if cached:
            return cached[:safe_limit]

    # Cold start with an empty cache: wait a bounded time for the refresh.
    # asyncio.shield keeps the background task alive on timeout/cancellation.
    try:
        tracks = await asyncio.wait_for(
            asyncio.shield(task), timeout=TRENDING_COLD_WAIT_SECONDS
        )
    except asyncio.CancelledError:
        if task.cancelled():
            # The refresh itself was cancelled (e.g. shutdown), not us.
            return cached[:safe_limit]
        raise
    except asyncio.TimeoutError:
        logger.warning(
            "Cold trending refresh did not finish in %.0fs; "
            "it continues in the background",
            TRENDING_COLD_WAIT_SECONDS,
        )
        return cached[:safe_limit]
    except Exception:
        logger.exception("Trending refresh task failed unexpectedly")
        return cached[:safe_limit]

    return list(tracks)[:safe_limit]


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

    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_get_audio_stream_url_sync, cleaned_track_id),
            timeout=STREAM_RESOLVE_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError as exc:
        raise AudioServiceError(
            f"Не удалось получить аудиопоток для {cleaned_track_id!r}: "
            f"превышен лимит времени"
        ) from exc


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

    When no explicit ``client`` is passed, the process-wide client from
    :func:`_get_stream_client` is used, so consecutive streams reuse pooled
    connections instead of repeating the TCP+TLS handshake.

    The returned body iterator closes the upstream response in a ``finally``
    block, so it is safe to hand it to
    :class:`fastapi.responses.StreamingResponse`. The client itself is never
    closed here: the shared one is owned by the app lifespan (call
    :func:`close_stream_client` on shutdown), and an explicitly passed client
    is owned by its caller.
    """
    target = str(url or "").strip()
    if not target:
        raise ValueError("url is required")
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")

    if client is None:
        client = await _get_stream_client()

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
        raise

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in response.aiter_raw(chunk_size=chunk_size):
                if chunk:
                    yield chunk
        finally:
            await response.aclose()

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
