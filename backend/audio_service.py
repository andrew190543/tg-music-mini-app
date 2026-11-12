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
import random
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
# Candidate/rotation tuning: the chart pool is wide (so every 30-minute
# cycle can pick a different 20), but only one small batch is actually
# resolved through yt-dlp per cycle.
TRENDING_ROTATE_INTERVAL_SECONDS = 30 * 60
TRENDING_CANDIDATE_POOL_SIZE = 60
TRENDING_RESOLVE_BATCH_SIZE = 25
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
    "Feduk Элджей - Розовое вино official audio",
    "Miyagi Эндшпиль - I Got Love official audio",
    "GAYAZOV$ BROTHER$ - Увезите меня на Дип-хаус official audio",
    "Джизус - Малиновая Лада official audio",
    "Мари Краймбрери - Пряталась в ванной official audio",
    "Дора - Младший лейтенант official audio",
    "Валентин Стрыкало - Наше лето official audio",
    "Мукка - Девочка с каре official audio",
    "Тима Белорусских - Мокрые кроссы official audio",
    "Время и Стекло - На стиле official audio",
    "Руки Вверх - Крошка моя official audio",
    "Земфира - Искала official audio",
    "Би-2 - Полковнику никто не пишет official audio",
    "Сплин - Выхода нет official audio",
    "Мумий Тролль - Утекай official audio",
    "Баста - Сансара official audio",
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
    """Stage 2: one specific RU hit per query so the list stays varied.

    The wide query pool is rotated by the 30-minute cycle counter, so every
    refresh starts at a fresh offset and picks a different set of hits
    instead of always the first `limit` queries in the tuple.
    """
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    artist_counts: dict[str, int] = {}
    queries = list(TRENDING_FALLBACK_QUERIES)
    if len(queries) > limit:
        offset = (int(time.time() // TRENDING_ROTATE_INTERVAL_SECONDS)) % len(
            queries
        )
        queries = queries[offset:] + queries[:offset]
    for query in queries:
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
        # Stage 1: gather a wide candidate pool (up to ~60 chart entries) so
        # every 30-minute cycle can pick a different slice of chart hits.
        # Randomly subsample the batch BEFORE any yt-dlp work: resolving all
        # 60 candidates would waste resources for the same 20 final tracks.
        chart_items = await asyncio.wait_for(
            asyncio.to_thread(
                _get_chart_items_sync,
                TRENDING_CANDIDATE_POOL_SIZE,
            ),
            timeout=TRENDING_CHART_TIMEOUT_SECONDS,
        )
        if len(chart_items) > TRENDING_RESOLVE_BATCH_SIZE:
            chart_items = random.sample(
                chart_items, TRENDING_RESOLVE_BATCH_SIZE
            )
        elif chart_items:
            # A short pool still gets shuffled: each cycle serves the same
            # hits in a fresh order.
            random.shuffle(chart_items)
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


def _rotate_cached_tracks(tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deterministically shuffle the cached pool per 30-minute cycle.

    Uses the cycle counter as the seed, so every request inside the same
    half-hour serves the same order, and the order itself differs each
    cycle even before the background refresh replaces the track set.
    """
    if len(tracks) < 2:
        return list(tracks)
    bucket = int(time.time() // TRENDING_ROTATE_INTERVAL_SECONDS)
    rotated = list(tracks)
    random.Random(bucket).shuffle(rotated)
    return rotated


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
                return _rotate_cached_tracks(cached)[:safe_limit]
            if recently_empty:
                # No data and the last refresh just failed: honor the cooldown
                # instead of hammering the upstream sources every request.
                return _rotate_cached_tracks(cached)[:safe_limit]
            task = _start_trending_refresh_locked(safe_limit)

        # Stale-while-revalidate: keep serving the previous list even when it
        # is older than the TTL, so the frontend never blocks on the refresh.
        if cached:
            return _rotate_cached_tracks(cached)[:safe_limit]

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


# ---------------------------------------------------------------------- wave

# The "Моя волна" (My Wave) feature: vibe-driven search and a personalized
# wave built from the user's favorites. Wave-specific duration window keeps
# DJ compilations and hour mixes out even when a single title slips past the
# word filter; the personal list also never repeats tracks the user liked.
WAVE_MIN_TRACK_SECONDS = 60
WAVE_MAX_TRACK_SECONDS = 480
# Diversity cap shared with trending: at most 2 tracks per artist.
WAVE_MAX_TRACKS_PER_ARTIST = 2
# Whole-wave budget: every mood fires `len(queries)` parallel yt-dlp searches
# and each one already enforces SEARCH_TIMEOUT_SECONDS internally.
WAVE_SEARCH_TIMEOUT_SECONDS = 45.0

# Vibe -> YouTube search queries. The wording matters: a query term such as
# "mix" would surface mixes/compilations whose titles are then wiped out by
# TITLE_EXCLUDE_RE, ending with (near) empty pools. Instead of a handful of
# generic phrases (which YouTube answers with hour-long mixes that get
# filtered out) each vibe carries a wide pool of point queries — concrete
# artists and single-track searches, so yt-dlp finds real 2–5 minute songs.
# ``get_wave_by_mood`` only takes a random 6–7 query sample per run and
# refills from the rest, so every wave is fresh and never static.
WAVE_MOOD_QUERIES: dict[str, tuple[str, ...]] = {
    "calm": (
        "Øneheart snowfall",
        "Idealism lofi beats",
        "Kupla lofi jazzhop",
        "Jinsang lofi",
        "Kudasai lofi",
        "potsu lofi",
        "Nujabes Feather",
        "Petit Biscuit Sunset Lover",
        "Kasbo World I Forgot",
        "Tycho Awake",
        "Bonjr lofi",
        "eevee beats lofi",
        "mommy lofi beats",
        "C418 Sweden",
        "Ludovico Einaudi Experience",
        "Ólafur Arnalds Near Light",
        "Nils Frahm Says",
        "Beach House Space Song",
        "Cigarettes After Sex Apocalypse",
        "Men I Trust Show Me How",
        "Clairo Softly",
        "Novo Amor Anchor",
        "Bon Iver Holocene",
        "Sleeping At Last Saturn",
        "Aphex Twin Avril 14th",
        "Reidenshi snowfall",
        "Antent warm nights",
        "leadwave dawn",
        "soft piano melody instrumental",
        "dreamy ambient track",
    ),
    "energetic": (
        "Dua Lipa Levitating",
        "The Weeknd Blinding Lights",
        "Calvin Harris Summer",
        "David Guetta Titanium",
        "Zivert Life",
        "Zivert Fly 2",
        "ANNA ASTI Царица",
        "Моя Мишель Зима в сердце",
        "Artik Asti Девочка, танцуй",
        "Мари Краймбрери Пряталась в ванной",
        "Daft Punk Get Lucky",
        "Disclosure Latch",
        "Fred again Delilah",
        "Purple Disco Machine Hypnotized",
        "Tiësto The Business",
        "Avicii Wake Me Up",
        "Röyksopp Happy Up Here",
        "Justice D.A.N.C.E.",
        "Bebe Rexha I'm Good",
        "Peggy Gou Nanana",
        "Kaytranada 10%",
        "GAYAZOV$ BROTHER$ Увезите меня на Дип-хаус",
        "Feduk Элджей Розовое вино",
        "Miyagi Эндшпиль I Got Love",
        "Мот Соло",
        "Время и Стекло На стиле",
        "Джиган Мятный кэнди",
        "Drake One Dance",
        "Mahmut Orhan Feel",
        "dance house upbeat track",
    ),
    "aggressive": (
        "Kordhell Murder In My Mind",
        "DVRST Close Eyes",
        "INTERWORLD METAMORPHOSIS",
        "Hensonn Sahara",
        "Pharmacist LIAR",
        "Ghostemane Mercury Retrograde",
        "$uicideboy$ Paris",
        "Scarlxrd 6 Feet",
        "100 gecs money machine",
        "Bring Me The Horizon Shadow Moses",
        "Slaughter To Prevail Demolisher",
        "Mick Gordon BFG Division",
        "shadowraze shadow fiend",
        "zxcursed изнутри",
        "MoonDeity NEON BLADE",
        "KSLV Noh Overkill",
        "Dxrk RAVE",
        "Kordhell Land of Fire",
        "PlayaPhonk Scream Scream",
        "COTIS Look At Me Now",
        "Lumi Athena Ice Cream So Good",
        "odecore Alright",
        "Slowboy Yume",
        "Vierre Cloud moment",
        "CITY MORGUE Stick N Move",
        "sadfriendd Broken",
        "aggressive drift phonk track",
    ),
    "sad": (
        "Три дня дождя Отпускай",
        "Три дня дождя Демоны",
        "Джизус Малиновая Лада",
        "Джизус Бирюзовая",
        "Земфира Искала",
        "Земфира Прогулка",
        "Сплин Выхода нет",
        "Кино Группа крови",
        "Перемотка трек",
        "Молчат Дома Судно",
        "дурной вкус трек",
        "Где Фантом Клюквенный сахар",
        "Мукка Девочка с каре",
        "Joji Slow Dancing in the Dark",
        "Joji Glimpse of Us",
        "Billie Eilish when the party's over",
        "Billie Eilish Ocean Eyes",
        "Lana Del Rey Video Games",
        "Lana Del Rey Summertime Sadness",
        "Radiohead Creep",
        "Radiohead No Surprises",
        "The Neighbourhood Sweater Weather",
        "Arctic Monkeys 505",
        "Arctic Monkeys Do I Wanna Know",
        "XXXTENTACION Jocelyn Flores",
        "Lil Peep Star Shopping",
        "d4vd Romantic Homicide",
        "Vacations Young",
        "Mac DeMarco Chamber of Reflection",
        "melancholy sad indie track",
    ),
}

# Style/genre wording derived from what the user actually liked (slowed,
# reverb, phonk, lofi, post-punk...). These queries are added to the
# personalized wave's first candidate batch so the wave follows the user's
# vibe even when the artist searches come up short.
VIBE_STYLE_QUERIES: dict[str, tuple[str, ...]] = {
    "aggressive": (
        "best slowed reverb phonk track",
        "dark trap slowed audio",
        "hard phonk audio",
    ),
    "calm": (
        "atmospheric slowed lofi track",
        "calm ambient piano instrumental",
    ),
    "sad": (
        "melancholy post-punk indie track",
        "sad acoustic song",
    ),
    "energetic": (
        "dance remix club track",
        "upbeat house edm track",
    ),
}

# Query suffixes alternating in the personalized wave: taste-profile artists
# are searched once each with a rotating wording so the same artist is not
# locked into one result pattern.
TASTE_QUERY_SUFFIXES = ("official audio", "track", "популярные треки")

# Per-run sample sizes: each wave call takes a random handful of queries from
# the vibe pool (and the taste pool tops up from the trending/mood pools), so
# consecutive runs produce different, non-repeating track lists.
WAVE_QUERY_BATCH_SIZE = 7
WAVE_TOPUP_BATCH_SIZE = 6
WAVE_TOPUP_MAX_ROUNDS = 8


def _clean_favorite_text(value: Any) -> str:
    text = re.sub(r"[\u200b-\u200f\uFEFF]", "", str(value or ""))
    text = re.sub(r"(?i)\s*-\s*Topic\b|\bVEVO\b|\bOfficial\s+(?:Audio|Video|Music\s+Video)\b|\bLyrics?\b|\bLyric\s+Video\b", "", text)
    text = re.sub(r"\([^)]*\)|\[[^]]*\]", "", text)
    return " ".join(re.sub(r"\*+", " ", text).split()).strip(" -–—")


def _parse_favorite_item(fav: Mapping[str, Any]) -> dict[str, Any]:
    raw_title, raw_artist = str(fav.get("title") or ""), str(fav.get("artist") or "")
    title = _clean_favorite_text(raw_title)
    artist = _clean_favorite_text(raw_artist)
    parts = re.split(r"\s*[-–—]\s*", title, maxsplit=1)
    real_artist, clean_title = (" ".join(parts[0].split()), " ".join(parts[1].split())) if len(parts) == 2 else (artist, title)
    source = f"{raw_title} {raw_artist}".lower()
    vibe = next((v for v, words in (("aggressive", ("slowed", "reverb", "phonk", "drift", "hard", "murder", "rage", "hyperpop", "trap", "zxcursed", "shadowraze")), ("calm", ("lofi", "lo-fi", "ambient", "chill", "snowfall", "piano", "sleep", "instrumental")), ("sad", ("sad", "doomer", "post-punk", "melancholy", "acoustic", "инди", "пост-панк", "грустн")), ("energetic", ("dance", "remix", "club", "house", "pop", "edm"))) if any(w in source for w in words)), "energetic")
    return {"artist": real_artist, "extra_artist": artist if artist.lower() != real_artist.lower() else "", "title": clean_title, "vibe": vibe}


def _normalize_song_title(title: Any) -> str:
    cleaned = _clean_favorite_text(title).lower()
    return "".join(re.findall(r"[\w\d]", cleaned, flags=re.UNICODE))


def build_taste_profile(
    favorites: list[dict[str, Any]] | None,
    max_artists: int = 8,
    sample_size: int | None = None,
) -> list[str]:
    """Derive yt-dlp queries from the user's favorites (pure helper).

    Artists are ranked by how often they appear in the favorites; the top
    ``max_artists`` distinct (case-insensitive) names become one search each.
    Tracks without an artist cannot anchor a query and are skipped. An empty
    (or artist-less) list means "no taste profile" -> the caller falls back.

    With ``sample_size`` set (used by the personalized wave) the profile is
    randomized per call: up to ``sample_size`` ranked artists are sampled and
    the wording alternates ("official audio" / "track" / "популярные треки"),
    producing a shuffled, differently-worded query batch every time. Without
    it the output stays deterministic ("{artist} official audio") for tests
    and callers that want a stable profile.
    """
    counts: dict[str, dict[str, int]] = {}
    order: dict[str, str] = {}
    favorite_ids: set[str] = set()

    for favorite in favorites or []:
        if not isinstance(favorite, Mapping):
            continue
        track_id = str(favorite.get("track_id") or "").strip()
        if track_id:
            favorite_ids.add(track_id)

        artist = _parse_favorite_item(favorite)["artist"]
        artist = " ".join(artist.split())
        if not artist or len(artist) > 80:
            continue
        key = artist.lower()
        bucket = counts.get(key)
        if bucket is None:
            counts[key] = {"count": 1, "length": len(artist)}
            order[key] = artist
        else:
            bucket["count"] = bucket["count"] + 1

    ranked = sorted(
        counts.items(), key=lambda item: (-item[1]["count"], item[1]["length"])
    )
    ranked = ranked[:max_artists]
    artist_names = [order[key] for key, _ in ranked]

    if sample_size is not None and len(artist_names) > sample_size:
        artist_names = random.sample(list(artist_names), sample_size)

    profile: list[str] = []
    for index, artist in enumerate(artist_names):
        suffix = (
            TASTE_QUERY_SUFFIXES[index % len(TASTE_QUERY_SUFFIXES)]
            if sample_size is not None
            else "official audio"
        )
        profile.append(f"{artist} {suffix}")

    if sample_size is not None:
        random.shuffle(profile)
    return profile


def _is_wave_track(track: Mapping[str, Any]) -> bool:
    """Wave-level acceptance rules on top of the base search filters."""
    title = str(track.get("title") or "").strip()
    if not title or TITLE_EXCLUDE_RE.search(title):
        return False
    duration = _coerce_duration(track.get("duration"))
    if duration <= 0:
        return False
    return WAVE_MIN_TRACK_SECONDS <= duration <= WAVE_MAX_TRACK_SECONDS


def _merge_wave_results(
    results: list[Any],
    limit: int,
    favorite_ids: set[str] | None = None,
    favorite_titles: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Merge concurrent search result lists into one curated wave list.

    Drops duplicates by ``track_id``, favorites the user already saved,
    tracks outside the wave duration window and any artist over the
    diversity cap. ``results`` may contain non-tracks (exceptions /
    ``None``) that are silently ignored.
    """
    seen = set(favorite_ids or set())
    title_keys = set(favorite_titles or set())
    artist_counts: dict[str, int] = {}
    merged: list[dict[str, Any]] = []

    for result in results:
        tracks = result if isinstance(result, list) else []
        for track in tracks:
            if not isinstance(track, Mapping):
                continue
            track_id = str(track.get("track_id") or "").strip()
            if not track_id or track_id in seen:
                continue
            key = _normalize_song_title(track.get("title"))
            if key and any(key == liked or key in liked or liked in key for liked in title_keys if liked):
                continue
            if not _is_wave_track(track):
                continue
            artist_key = str(track.get("artist") or "").strip().lower()
            if (
                artist_key
                and artist_counts.get(artist_key, 0)
                >= WAVE_MAX_TRACKS_PER_ARTIST
            ):
                continue
            if artist_key:
                artist_counts[artist_key] = (
                    artist_counts.get(artist_key, 0) + 1
                )
            seen.add(track_id)
            merged.append(
                {
                    "track_id": track_id,
                    "title": str(track.get("title") or "").strip(),
                    "artist": str(track.get("artist") or "").strip(),
                    "duration": _coerce_duration(track.get("duration")),
                    "thumbnail": str(track.get("thumbnail") or "").strip(),
                }
            )
            if len(merged) >= limit:
                return merged
    return merged


async def _wave_search_queries(queries: list[str], per_query: int) -> list[Any]:
    """Run several wave searches in parallel, tolerating partial failures."""
    async def run_one(query: str) -> list[dict[str, Any]]:
        return await search_tracks(query, limit=per_query)

    try:
        return await asyncio.wait_for(
            asyncio.gather(
                *(run_one(query) for query in queries), return_exceptions=True
            ),
            timeout=WAVE_SEARCH_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError as exc:
        raise AudioServiceError("Сборка волны заняла слишком много времени и была прервана") from exc


def _wave_per_query_limit(limit: int, query_count: int) -> int:
    """Results to ask per query so the final list reaches ``limit``."""
    if query_count <= 0:
        return limit
    # Filters drop a portion of each pool; ask for extra candidates.
    per_query = max(SEARCH_POOL_SIZE // 2, (limit // query_count) + 4)
    return min(per_query, MAX_SEARCH_RESULTS)


async def get_wave_by_mood(vibe: str, limit: int = 15) -> list[dict[str, Any]]:
    """Return a 15-20 track wave matching the requested mood/vibe.

    Every vibe maps to a wide pool of YouTube point-queries (concrete
    artists and single-track searches) run through the regular
    :func:`search_tracks` pipeline (filters, timeouts and the worker thread
    are shared). Each call takes a *random* sample of the pool first and
    refills with further random batches while the list is still short, so
    consecutive runs produce fresh, non-repeating waves. If every search
    fails the service error is propagated.
    """
    cleaned_vibe = str(vibe or "").strip().lower()
    pool = list(WAVE_MOOD_QUERIES.get(cleaned_vibe) or [])
    if not pool:
        raise AudioServiceError(f"Неизвестный вайб волны: {vibe!r}")

    try:
        safe_limit = int(limit)
    except (TypeError, ValueError):
        safe_limit = 15
    safe_limit = max(1, min(safe_limit, MAX_SEARCH_RESULTS))

    # Fresh wave on every run: shuffle the pool and fire a random 6-7 query
    # sketch instead of the same three queries every time.
    random.shuffle(pool)
    first_batch = pool[:WAVE_QUERY_BATCH_SIZE]
    remaining = pool[len(first_batch):]

    merged = await _wave_search_queries(
        first_batch, _wave_per_query_limit(safe_limit, len(first_batch))
    )
    tracks = _merge_wave_results(merged, safe_limit)
    random.shuffle(tracks)
    seen = {track["track_id"] for track in tracks}

    # Filters (duration window, cap per artist) can leave the list short:
    # refill from untouched random portions of this vibe's pool up to limit.
    while len(tracks) < safe_limit and remaining:
        batch = remaining[:WAVE_TOPUP_BATCH_SIZE]
        remaining = remaining[len(batch):]
        merged = await _wave_search_queries(
            batch, _wave_per_query_limit(safe_limit, len(batch))
        )
        more = _merge_wave_results(merged, safe_limit - len(tracks), seen)
        tracks.extend(more)
        seen.update(track["track_id"] for track in more)

    if not tracks:
        raise AudioServiceError(
            f"Не удалось собрать волну для вайба {cleaned_vibe!r}"
        )
    random.shuffle(tracks)
    return tracks


async def get_wave_personalized(
    favorites: list[dict[str, Any]] | None, limit: int = 15
) -> list[dict[str, Any]]:
    """Return a personalized wave built from the user's favorite tracks.

    The likes are parsed once (:func:`_parse_favorite_item`): the real artist
    is taken from the ``"Artist - Song"`` title split, a vibe is detected from
    style keywords, and normalized song titles are collected for the hard
    anti-duplicate filter. Candidates come, in order, from:

    1. a randomized taste profile (top favorite artists, alternating wording);
    2. Deezer discovery (:func:`_discover_similar_tracks_sync`): other songs
       by the favorite artists plus tracks of related artists;
    3. style queries for the detected vibe (e.g. slowed/phonk for aggressive
       likes).

    Only then, while the list is still short, it is topped up from the mood
    pool matching the detected vibe — never from the generic trending pop
    fallback when the user has likes. With no likes at all the wave mixes
    every mood pool plus the trending fallback. A fully empty result is
    ``[]`` so the API layer can fall back to the default chart gracefully.
    """
    try:
        safe_limit = int(limit)
    except (TypeError, ValueError):
        safe_limit = 15
    safe_limit = max(1, min(safe_limit, MAX_SEARCH_RESULTS))

    parsed = [_parse_favorite_item(f) for f in (favorites or []) if isinstance(f, Mapping)]
    artist_names = list(dict.fromkeys(p["artist"] for p in parsed if p["artist"]))[:5]
    # Vibe of the wave = the majority vibe across all likes (not just the
    # first one, which could carry no style keywords at all). Ties are broken
    # towards the most specific vibe — "energetic" is the no-keyword default,
    # so an explicit aggressive/sad/calm vote always wins it.
    vibe_counts: dict[str, int] = {}
    for parsed_item in parsed:
        vibe_counts[parsed_item["vibe"]] = vibe_counts.get(parsed_item["vibe"], 0) + 1
    if vibe_counts:
        best_count = max(vibe_counts.values())
        detected_vibe = next(
            vibe
            for vibe in ("aggressive", "sad", "calm", "energetic")
            if vibe_counts.get(vibe, 0) == best_count
        )
    else:
        detected_vibe = "energetic"
    favorite_titles = {_normalize_song_title(x) for p in parsed for x in (p["title"],)}
    favorite_titles.update(_normalize_song_title(f.get("title")) for f in (favorites or []) if isinstance(f, Mapping))
    profile = build_taste_profile(favorites, max_artists=8, sample_size=5)

    favorite_ids = {
        str(favorite.get("track_id") or "").strip()
        for favorite in (favorites or [])
        if isinstance(favorite, Mapping) and favorite.get("track_id")
    }
    seen: set[str] = set(favorite_ids)
    tracks: list[dict[str, Any]] = []
    used_queries: set[str] = set()

    async def gather_batch(queries: list[str]) -> None:
        fresh = [query for query in queries if query not in used_queries]
        used_queries.update(fresh)
        if not fresh:
            return
        merged = await _wave_search_queries(
            fresh, _wave_per_query_limit(safe_limit, len(fresh))
        )
        more = _merge_wave_results(merged, safe_limit - len(tracks), seen, favorite_titles)
        tracks.extend(more)
        seen.update(track["track_id"] for track in more)

    # Round 1: taste-profile artists, extra wording for new songs by them and
    # the style queries derived from the user's own likes.
    artist_queries = [
        query
        for artist in artist_names[:3]
        for query in (f"{artist} popular track", f"{artist} feat")
    ]
    style_queries = list(VIBE_STYLE_QUERIES.get(detected_vibe, ()))
    await gather_batch(profile + artist_queries + style_queries)

    # Round 2: Deezer — other songs by favorite artists + related artists.
    if artist_names:
        try:
            discovered = await asyncio.wait_for(asyncio.to_thread(_discover_similar_tracks_sync, artist_names, favorite_titles, safe_limit), timeout=10)
            merged = _merge_wave_results([discovered], safe_limit - len(tracks), seen, favorite_titles)
            tracks.extend(merged)
            seen.update(t["track_id"] for t in merged)
        except (asyncio.TimeoutError, Exception):
            logger.debug("Deezer personalized discovery failed", exc_info=True)

    # Still short? Top up from the mood pool matching the user's likes only —
    # the generic trending pop fallback is reserved for users with no likes.
    if parsed:
        topup_pool = list(WAVE_MOOD_QUERIES.get(detected_vibe, ()))
    else:
        topup_pool = [
            query
            for vibe_queries in WAVE_MOOD_QUERIES.values()
            for query in vibe_queries
        ]
        topup_pool += list(TRENDING_FALLBACK_QUERIES)

    rounds = 0
    while len(tracks) < safe_limit and rounds < WAVE_TOPUP_MAX_ROUNDS:
        rounds += 1
        fresh_pool = [query for query in topup_pool if query not in used_queries]
        if not fresh_pool:
            break
        batch = random.sample(
            fresh_pool, min(WAVE_TOPUP_BATCH_SIZE, len(fresh_pool))
        )
        await gather_batch(batch)

    random.shuffle(tracks)
    return tracks


def _discover_similar_tracks_sync(artists: list[str], favorite_titles: set[str], limit: int) -> list[dict[str, Any]]:
    tracks: list[dict[str, Any]] = []
    try:
        with httpx.Client(timeout=8.0) as client:
            for artist in artists[:5]:
                try:
                    found = client.get("https://api.deezer.com/search/artist", params={"q": artist, "limit": 1}).json().get("data", [])
                    if not found: continue
                    aid = found[0]["id"]
                    for endpoint, cap in ((f"artist/{aid}/top", 2), (f"artist/{aid}/related", 3)):
                        data = client.get(f"https://api.deezer.com/{endpoint}", params={"limit": 10 if cap == 2 else 6}).json().get("data", [])
                        if cap == 3:
                            data = [tr for rel in data[:3] for tr in client.get(f"https://api.deezer.com/artist/{rel['id']}/top", params={"limit": 5}).json().get("data", [])]
                        count = 0
                        for item in data:
                            title = str(item.get("title") or "")
                            if title and _normalize_song_title(title) not in favorite_titles:
                                tracks.append({"track_id": f"deezer:{item.get('id')}", "title": f"{item.get('artist', {}).get('name', artist)} - {title}", "artist": item.get("artist", {}).get("name", artist), "duration": item.get("duration", 180), "thumbnail": ""})
                                count += 1
                            if count >= cap: break
                except Exception:
                    logger.debug("Deezer lookup for %r failed", artist, exc_info=True)
                    continue
                if len(tracks) >= limit: break
    except Exception:
        logger.debug("Deezer discovery failed entirely", exc_info=True)
        return []
    return tracks


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
