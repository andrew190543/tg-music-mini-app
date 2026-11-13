"""Async yt-dlp wrapper, audio disk-cache, and HTTP stream/thumbnail proxy."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import AsyncIterator, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import random
import re
import socket
import tempfile
import time
from typing import Any
from urllib.parse import quote

from dotenv import load_dotenv
import httpx
import yt_dlp

load_dotenv()

logger = logging.getLogger(__name__)

MAX_SEARCH_RESULTS = 50
MIN_TRACK_SECONDS = 45
MAX_TRACK_SECONDS = 900
SEARCH_POOL_SIZE = 30
CHUNK_SIZE = 64 * 1024

SEARCH_TIMEOUT_SECONDS = 40.0
STREAM_RESOLVE_TIMEOUT_SECONDS = 45.0

TRENDING_RESOLVE_POOL_SIZE = 6
TRENDING_MIN_TRACK_SECONDS = 90
TRENDING_MAX_TRACK_SECONDS = 360
TRENDING_CACHE_TTL_SECONDS = 30 * 60
TRENDING_ROTATE_INTERVAL_SECONDS = 30 * 60
TRENDING_CANDIDATE_POOL_SIZE = 40
TRENDING_RESOLVE_BATCH_SIZE = 20
TRENDING_CHART_TIMEOUT_SECONDS = 12.0
TRENDING_RESOLVE_TIMEOUT_SECONDS = 25.0
TRENDING_FALLBACK_TIMEOUT_SECONDS = 45.0
TRENDING_COLD_WAIT_SECONDS = 25.0
TRENDING_EMPTY_RETRY_COOLDOWN_SECONDS = 15.0

TRENDING_CACHE_LOCK = asyncio.Lock()
TRENDING_CACHE: dict[str, Any] = {
    "tracks": [],
    "updated_at": 0.0,
    "empty_at": 0.0,
}
TRENDING_REFRESH_TASK: asyncio.Task[list[dict[str, Any]]] | None = None

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

DEEZER_PLAYLIST_SEARCH_URLS = (
    "https://api.deezer.com/search/playlist?q=Топ Россия 2026",
    "https://api.deezer.com/search/playlist?q=Русские хиты",
)

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
)

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
STREAM_TIMEOUT = httpx.Timeout(connect=12.0, read=60.0, write=12.0, pool=12.0)

_stream_client: httpx.AsyncClient | None = None
_stream_client_lock = asyncio.Lock()
_detected_proxy_cache: str | None = None
_detected_proxy_checkedAt: float = 0.0

_THUMB_CACHE_MAX_ITEMS = 256
_thumb_cache: OrderedDict[str, tuple[bytes, str]] = OrderedDict()


@dataclass(slots=True)
class ResolvedStreamInfo:
    """Direct media URL plus session headers needed by upstream CDNs."""

    url: str
    headers: dict[str, str] = field(default_factory=dict)
    yt_id: str | None = None


def _is_local_port_open(host: str, port: int) -> bool:
    """Check if a local proxy port (e.g. Xray/Happ 10809/10808) is listening."""
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False


def _detect_proxy_url() -> str:
    """Detect explicit proxy from env or auto-detect local Happ/Xray inbound."""
    global _detected_proxy_cache, _detected_proxy_checkedAt

    explicit = (
        os.environ.get("YTDLP_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("ALL_PROXY")
        or ""
    ).strip()
    if explicit.lower() in {"none", "off", "direct", "false", "0"}:
        return ""
    if explicit:
        return explicit

    now = time.monotonic()
    if _detected_proxy_cache is not None and (now - _detected_proxy_checkedAt) < 30.0:
        return _detected_proxy_cache

    _detected_proxy_checkedAt = now
    for host, port, scheme in (
        ("127.0.0.1", 10808, "socks5h"),
        ("127.0.0.1", 10809, "http"),
        ("127.0.0.1", 1080, "socks5h"),
        ("127.0.0.1", 2080, "http"),
    ):
        if _is_local_port_open(host, port):
            proxy_url = f"{scheme}://{host}:{port}"
            if _detected_proxy_cache != proxy_url:
                logger.info(
                    "Автоматически подключен локальный прокси для YouTube: %s",
                    proxy_url,
                )
            _detected_proxy_cache = proxy_url
            return proxy_url

    _detected_proxy_cache = ""
    return ""


def _resolve_httpx_proxy_url() -> str | None:
    """Choose an httpx-compatible proxy URL (preferring HTTP inbound 10809)."""
    proxy = _detect_proxy_url()
    if not proxy:
        return None

    if (
        any(h in proxy for h in ("127.0.0.1:10808", "localhost:10808"))
        and _is_local_port_open("127.0.0.1", 10809)
    ):
        return "http://127.0.0.1:10809"

    if proxy.startswith("socks5h://"):
        return proxy.replace("socks5h://", "socks5://", 1)
    return proxy


async def _get_stream_client() -> httpx.AsyncClient:
    """Return the shared asynchronous httpx client for upstream streams."""
    global _stream_client
    async with _stream_client_lock:
        if _stream_client is None or _stream_client.is_closed:
            httpx_proxy = _resolve_httpx_proxy_url()
            try:
                _stream_client = httpx.AsyncClient(
                    follow_redirects=True,
                    timeout=STREAM_TIMEOUT,
                    proxy=httpx_proxy,
                )
            except ImportError:
                fallback_http = (
                    "http://127.0.0.1:10809"
                    if _is_local_port_open("127.0.0.1", 10809)
                    else None
                )
                logger.warning(
                    "Пакет socksio не установлен; переключаем httpx на %s",
                    fallback_http or "прямое соединение",
                )
                _stream_client = httpx.AsyncClient(
                    follow_redirects=True,
                    timeout=STREAM_TIMEOUT,
                    proxy=fallback_http,
                )
        return _stream_client


async def fetch_thumbnail_bytes(url: str) -> tuple[bytes, str]:
    """Fetch thumbnail image through the shared proxied HTTP client with LRU cache."""
    target = str(url or "").strip()
    if not target.startswith(("http://", "https://")):
        raise ValueError("Invalid thumbnail URL")

    cached = _thumb_cache.get(target)
    if cached is not None:
        _thumb_cache.move_to_end(target)
        return cached

    client = await _get_stream_client()
    headers = {"User-Agent": BROWSER_USER_AGENT, "Accept": "image/*,*/*;q=0.8"}
    response = await client.get(target, headers=headers)

    if response.status_code in (404, 403) and "maxresdefault" in target:
        fallback_target = target.replace("maxresdefault", "hqdefault")
        response = await client.get(fallback_target, headers=headers)

    response.raise_for_status()
    media_type = (
        response.headers.get("content-type", "image/jpeg")
        .split(";", 1)[0]
        .strip()
        or "image/jpeg"
    )
    result = (response.content, media_type)
    _thumb_cache[target] = result
    while len(_thumb_cache) > _THUMB_CACHE_MAX_ITEMS:
        _thumb_cache.popitem(last=False)
    return result


def _yt_dlp_base_params() -> dict[str, Any]:
    """Return base yt-dlp options tuned for VPN/TUN stability."""
    params: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 20,
        "retries": 3,
        "extractor_retries": 2,
    }
    proxy = _detect_proxy_url()
    if proxy:
        params["proxy"] = proxy
    return params


_TRANSPORT_ERROR_MARKERS = (
    "timed out",
    "timeout",
    "tls handshake",
    "unexpected tls packet",
    "connection reset",
    "connection refused",
    "connection aborted",
    "network is unreachable",
    "unable to download",
    "temporary failure in name resolution",
    "getaddrinfo failed",
)


def _is_transport_error(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in _TRANSPORT_ERROR_MARKERS)


def _ydl_extract_info(
    url: str, params: dict[str, Any]
) -> Mapping[str, Any] | None:
    """Run ``extract_info`` with one extra retry on transport errors."""
    global _detected_proxy_cache

    attempts = 2
    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            with yt_dlp.YoutubeDL(params) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception as exc:
            last_exc = exc
            if attempt >= attempts or not _is_transport_error(exc):
                break
            logger.warning(
                "yt-dlp network failure for %r (попытка %d), повторяю: %s",
                url,
                attempt,
                exc,
            )
            _detected_proxy_cache = None
            refreshed = _detect_proxy_url()
            if refreshed and refreshed != params.get("proxy"):
                params = {**params, "proxy": refreshed}
    raise last_exc  # type: ignore[misc]


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
        for thumbnail in reversed(thumbnails):
            if not isinstance(thumbnail, Mapping):
                continue
            url = thumbnail.get("url")
            if isinstance(url, str) and url.strip():
                return url.strip()

    return ""


def _refine_search_query(query: str) -> str:
    lowered = query.lower()
    if "official audio" in lowered or "lyrics" in lowered:
        return query
    if re.search(r"\baudio\b", lowered):
        return query
    return f"{query} audio"


def _entry_duration(entry: Mapping[str, Any]) -> int | None:
    raw = entry.get("duration")
    if raw is None:
        return None
    try:
        seconds = int(float(raw))
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def _is_music_track(entry: Mapping[str, Any]) -> bool:
    if entry.get("live_status") == "is_live" or entry.get("is_live"):
        return False

    title = str(entry.get("title") or "").strip()
    if TITLE_EXCLUDE_RE.search(title):
        return False

    duration = _entry_duration(entry)
    if duration is None:
        return False
    return MIN_TRACK_SECONDS <= duration <= MAX_TRACK_SECONDS


def _deezer_search_tracks_sync(query: str, limit: int) -> list[dict[str, Any]]:
    cleaned = re.sub(r"(?i)\bofficial\s+audio\b|\baudio\b", "", query).strip()
    if not cleaned:
        cleaned = query.strip()
    try:
        with httpx.Client(timeout=8.0, follow_redirects=True) as client:
            resp = client.get(
                "https://api.deezer.com/search",
                params={"q": cleaned, "limit": max(limit * 2, 20)},
                headers={
                    "Accept": "application/json",
                    "User-Agent": BROWSER_USER_AGENT,
                },
            )
            resp.raise_for_status()
            data = resp.json().get("data") or []
    except Exception:
        logger.debug("Deezer fallback search failed: %r", cleaned, exc_info=True)
        return []

    results: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, Mapping):
            continue
        deezer_id = item.get("id")
        title = str(item.get("title") or "").strip()
        if not deezer_id or not title or TITLE_EXCLUDE_RE.search(title):
            continue
        artist_obj = item.get("artist")
        artist = (
            str(artist_obj.get("name") or "").strip()
            if isinstance(artist_obj, Mapping)
            else ""
        )
        album_obj = item.get("album")
        thumbnail = ""
        if isinstance(album_obj, Mapping):
            thumbnail = str(
                album_obj.get("cover_big")
                or album_obj.get("cover_medium")
                or album_obj.get("cover")
                or ""
            ).strip()
        duration = _coerce_duration(item.get("duration")) or 180
        results.append(
            {
                "track_id": f"deezer:{deezer_id}",
                "title": title,
                "artist": artist,
                "duration": duration,
                "thumbnail": thumbnail,
            }
        )
        if len(results) >= limit:
            break
    return results


def _search_tracks_sync(
    query: str, limit: int, *, pool_size: int | None = None
) -> list[dict[str, Any]]:
    params = {
        **_yt_dlp_base_params(),
        "extract_flat": True,
    }

    is_direct_url = "://" in query
    search_target = query if is_direct_url else None
    search_query = _refine_search_query(query)
    resolved_pool = (
        pool_size
        if pool_size is not None
        else min(MAX_SEARCH_RESULTS, max(SEARCH_POOL_SIZE, limit * 2))
    )

    info: Mapping[str, Any] | None = None
    yt_error: Exception | None = None
    try:
        info = _ydl_extract_info(
            search_target or f"ytsearch{resolved_pool}:{search_query}", params
        )
    except Exception as exc:
        yt_error = exc
        logger.debug("yt-dlp search failed for %r, trying Deezer fallback", query)

    results: list[dict[str, Any]] = []
    skipped = 0

    if info:
        entries = info.get("entries")
        if entries is None and info.get("id"):
            entries = [info]
        entries = entries or []
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            if not is_direct_url and not _is_music_track(entry):
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

    if results:
        return results

    if not is_direct_url:
        deezer_results = _deezer_search_tracks_sync(query, limit)
        if deezer_results:
            return deezer_results

    if yt_error is not None:
        raise AudioServiceError(
            f"Не удалось выполнить поиск: {query!r}"
        ) from yt_error

    return []


async def search_tracks(
    query: str, limit: int = 15, *, pool_size: int | None = None
) -> list[dict[str, Any]]:
    """Search for tracks and return normalized metadata."""
    cleaned_query = " ".join(str(query or "").split())
    if not cleaned_query:
        return []

    try:
        safe_limit = int(limit)
    except (TypeError, ValueError):
        safe_limit = 15
    safe_limit = max(1, min(safe_limit, MAX_SEARCH_RESULTS))

    logger.debug("Searching tracks: query=%r limit=%d", cleaned_query, safe_limit)
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                _search_tracks_sync,
                cleaned_query,
                safe_limit,
                pool_size=pool_size,
            ),
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
    if not _is_music_track(entry):
        return False

    duration = _entry_duration(entry)
    return (
        duration is not None
        and TRENDING_MIN_TRACK_SECONDS <= duration <= TRENDING_MAX_TRACK_SECONDS
    )


def _entry_to_track(entry: Mapping[str, Any]) -> dict[str, Any]:
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


def _chart_track_from_entry(
    item: Mapping[str, Any], entry: Mapping[str, Any]
) -> dict[str, Any] | None:
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
        "thumbnail": str(item.get("thumbnail") or "") or _best_thumbnail(entry),
    }


def _chart_item_to_deezer_track(
    item: Mapping[str, Any]
) -> dict[str, Any] | None:
    deezer_id = str(item.get("deezer_id") or "").strip()
    title = str(item.get("title") or "").strip()
    artist = str(item.get("artist") or "").strip()
    if not deezer_id or not title or not artist:
        return None
    if TITLE_EXCLUDE_RE.search(title):
        return None
    return {
        "track_id": f"deezer:{deezer_id}",
        "title": title,
        "artist": artist,
        "duration": _coerce_duration(item.get("duration")) or 180,
        "thumbnail": str(item.get("thumbnail") or "").strip(),
    }


def _fetch_deezer_ru_playlist_sync(limit: int) -> list[dict[str, str]]:
    if limit <= 0:
        return []

    try:
        with httpx.Client(timeout=8.0, follow_redirects=True) as client:
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
        if not _has_cyrillic(title_name) and not _has_cyrillic(artist_name):
            continue

        album = track.get("album")
        cover = ""
        if isinstance(album, Mapping):
            cover = str(
                album.get("cover_big")
                or album.get("cover_medium")
                or album.get("cover")
                or ""
            ).strip()

        items.append(
            {
                "artist": artist_name,
                "title": title_name,
                "deezer_id": str(track.get("id") or "").strip(),
                "duration": str(_coerce_duration(track.get("duration")) or 180),
                "thumbnail": cover,
            }
        )
        if len(items) >= limit:
            break
    return items


def _get_chart_items_sync(limit: int) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for raw_item in _fetch_deezer_ru_playlist_sync(limit):
        title = str(raw_item.get("title") or "").strip()
        artist = str(raw_item.get("artist") or "").strip()
        if not title or not artist:
            continue
        key = (artist.lower(), title.lower())
        if key in seen:
            continue
        seen.add(key)
        items.append(raw_item)
        if len(items) >= limit:
            break
    return items[:limit]


def _yt_search_entries_sync(query: str, pool: int) -> list[Mapping[str, Any]]:
    params = {
        **_yt_dlp_base_params(),
        "extract_flat": True,
    }

    try:
        info = _ydl_extract_info(f"ytsearch{pool}:{query}", params)
    except Exception as exc:
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
    try:
        entries = _yt_search_entries_sync(query, 2)
        for entry in entries:
            candidate = _chart_track_from_entry(item, entry)
            if candidate:
                return candidate
    except AudioServiceError:
        pass

    deezer_candidate = _chart_item_to_deezer_track(item)
    if deezer_candidate:
        return deezer_candidate

    fallback = _deezer_search_tracks_sync(
        f"{item.get('artist', '')} {item.get('title', '')}", 1
    )
    return fallback[0] if fallback else None


async def _resolve_chart_items(
    chart_items: list[dict[str, str]], limit: int
) -> list[dict[str, Any]]:
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
                return _chart_item_to_deezer_track(item)
            except AudioServiceError:
                return _chart_item_to_deezer_track(item)
            except Exception:
                return _chart_item_to_deezer_track(item)

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


def _search_single_fallback_query_sync(query: str) -> dict[str, Any] | None:
    """Resolve one query in parallel worker pool without aborting the whole batch."""
    try:
        entries = _yt_search_entries_sync(query, 2)
        for entry in entries:
            if isinstance(entry, Mapping) and _is_trending_track(entry):
                track = _entry_to_track(entry)
                if track["track_id"]:
                    return track
    except Exception:
        pass

    deezer_hits = _deezer_search_tracks_sync(query, 1)
    return deezer_hits[0] if deezer_hits else None


def _get_trending_from_search_sync(limit: int) -> list[dict[str, Any]]:
    """Parallel resolution of RU hit queries so trending loads in a few seconds."""
    queries = list(TRENDING_FALLBACK_QUERIES)
    if len(queries) > limit:
        offset = (int(time.time() // TRENDING_ROTATE_INTERVAL_SECONDS)) % len(
            queries
        )
        queries = queries[offset:] + queries[:offset]

    batch = queries[: max(limit + 4, 24)]
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    artist_counts: dict[str, int] = {}

    with ThreadPoolExecutor(max_workers=TRENDING_RESOLVE_POOL_SIZE) as pool:
        futures = {
            pool.submit(_search_single_fallback_query_sync, q): q for q in batch
        }
        try:
            for future in as_completed(
                futures, timeout=TRENDING_FALLBACK_TIMEOUT_SECONDS
            ):
                track = future.result()
                if not track:
                    continue
                track_id = track["track_id"]
                if not track_id or track_id in seen:
                    continue
                artist_key = str(track.get("artist") or "").strip().lower()
                if artist_key and artist_counts.get(artist_key, 0) >= 2:
                    continue
                if artist_key:
                    artist_counts[artist_key] = (
                        artist_counts.get(artist_key, 0) + 1
                    )
                seen.add(track_id)
                results.append(track)
                if len(results) >= limit:
                    for f in futures:
                        f.cancel()
                    break
        except TimeoutError:
            logger.warning(
                "Partial trending fallback timeout: collected %d/%d tracks",
                len(results),
                limit,
            )

    return results[:limit]


def _merge_by_track_id(
    tracks: list[dict[str, Any]], extra: list[dict[str, Any]], limit: int
) -> list[dict[str, Any]]:
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
    """Fetch and resolve a fresh trending list WITHOUT losing partial results."""
    tracks: list[dict[str, Any]] = []
    chart_items: list[dict[str, str]] = []
    try:
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
            random.shuffle(chart_items)
        tracks = await _resolve_chart_items(chart_items, limit)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("Chart stage skipped or timed out, using search pool")

    if len(tracks) < limit:
        try:
            fallback_tracks = await asyncio.to_thread(
                _get_trending_from_search_sync, limit
            )
            tracks = _merge_by_track_id(tracks, fallback_tracks, limit)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Trending fallback search failed")

    if not tracks and chart_items:
        for item in chart_items:
            candidate = _chart_item_to_deezer_track(item)
            if candidate:
                tracks.append(candidate)
                if len(tracks) >= limit:
                    break

    if not tracks:
        logger.warning("Trending sources did not return any tracks")
        async with TRENDING_CACHE_LOCK:
            TRENDING_CACHE["empty_at"] = time.monotonic()
        return []

    async with TRENDING_CACHE_LOCK:
        TRENDING_CACHE["tracks"] = list(tracks)
        TRENDING_CACHE["updated_at"] = time.monotonic()
        TRENDING_CACHE["empty_at"] = 0.0
    logger.info("Trending cache updated: %d tracks", len(tracks))
    return tracks


def _start_trending_refresh_locked(
    limit: int,
) -> asyncio.Task[list[dict[str, Any]]]:
    task = asyncio.create_task(
        _refresh_trending_cache(limit), name="trending-refresh"
    )
    global TRENDING_REFRESH_TASK
    TRENDING_REFRESH_TASK = task
    return task


def _rotate_cached_tracks(tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(tracks) < 2:
        return list(tracks)
    bucket = int(time.time() // TRENDING_ROTATE_INTERVAL_SECONDS)
    rotated = list(tracks)
    random.Random(bucket).shuffle(rotated)
    return rotated


async def get_trending_tracks(limit: int = 20) -> list[dict[str, Any]]:
    """Return the trending list using stale-while-revalidate."""
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
            task = None

        if task is None:
            empty_age = time.monotonic() - TRENDING_CACHE["empty_at"]
            recently_empty = (
                TRENDING_CACHE["empty_at"] > 0
                and empty_age < TRENDING_EMPTY_RETRY_COOLDOWN_SECONDS
            )
            if cached and cache_age <= TRENDING_CACHE_TTL_SECONDS:
                return _rotate_cached_tracks(cached)[:safe_limit]
            if recently_empty:
                return _rotate_cached_tracks(cached)[:safe_limit]
            task = _start_trending_refresh_locked(safe_limit)

    if cached:
        return _rotate_cached_tracks(cached)[:safe_limit]

    try:
        tracks = await asyncio.wait_for(
            asyncio.shield(task), timeout=TRENDING_COLD_WAIT_SECONDS
        )
    except asyncio.CancelledError:
        if task.cancelled():
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

WAVE_MIN_TRACK_SECONDS = 60
WAVE_MAX_TRACK_SECONDS = 480
WAVE_MAX_TRACKS_PER_ARTIST = 2
WAVE_SEARCH_TIMEOUT_SECONDS = 45.0
WAVE_CONCURRENCY_LIMIT = 4

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

TASTE_QUERY_SUFFIXES = ("official audio", "track", "популярные треки")

WAVE_QUERY_BATCH_SIZE = 7
WAVE_TOPUP_BATCH_SIZE = 5
WAVE_TOPUP_MAX_ROUNDS = 6


def _clean_favorite_text(value: Any) -> str:
    text = re.sub(r"[\u200b-\u200f\uFEFF]", "", str(value or ""))
    text = re.sub(
        r"(?i)\s*-\s*Topic\b|\bVEVO\b|\bOfficial\s+(?:Audio|Video|Music\s+Video)\b|\bLyrics?\b|\bLyric\s+Video\b",
        "",
        text,
    )
    text = re.sub(r"\([^)]*\)|\[[^\]]*\]", "", text)
    return " ".join(re.sub(r"\*+", " ", text).split()).strip(" -–—")


def _parse_favorite_item(fav: Mapping[str, Any]) -> dict[str, Any]:
    raw_title, raw_artist = str(fav.get("title") or ""), str(fav.get("artist") or "")
    title = _clean_favorite_text(raw_title)
    artist = _clean_favorite_text(raw_artist)
    parts = re.split(r"\s*[-–—]\s*", title, maxsplit=1)
    real_artist, clean_title = (
        (" ".join(parts[0].split()), " ".join(parts[1].split()))
        if len(parts) == 2
        else (artist, title)
    )
    source = f"{raw_title} {raw_artist}".lower()
    vibe = next(
        (
            v
            for v, words in (
                (
                    "aggressive",
                    (
                        "slowed",
                        "reverb",
                        "phonk",
                        "drift",
                        "hard",
                        "murder",
                        "rage",
                        "hyperpop",
                        "trap",
                        "zxcursed",
                        "shadowraze",
                    ),
                ),
                (
                    "calm",
                    (
                        "lofi",
                        "lo-fi",
                        "ambient",
                        "chill",
                        "snowfall",
                        "piano",
                        "sleep",
                        "instrumental",
                    ),
                ),
                (
                    "sad",
                    (
                        "sad",
                        "doomer",
                        "post-punk",
                        "melancholy",
                        "acoustic",
                        "инди",
                        "пост-панк",
                        "грустн",
                    ),
                ),
                ("energetic", ("dance", "remix", "club", "house", "pop", "edm")),
            )
            if any(w in source for w in words)
        ),
        "energetic",
    )
    return {
        "artist": real_artist,
        "title": clean_title,
        "vibe": vibe,
    }


def _normalize_song_title(title: Any) -> str:
    cleaned = _clean_favorite_text(title).lower()
    return "".join(re.findall(r"[\w\d]", cleaned, flags=re.UNICODE))


def build_taste_profile(
    favorites: list[dict[str, Any]] | None,
    max_artists: int = 8,
    sample_size: int | None = None,
) -> list[str]:
    counts: dict[str, dict[str, int]] = {}
    order: dict[str, str] = {}

    for favorite in favorites or []:
        if not isinstance(favorite, Mapping):
            continue
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
            if key and any(
                key == liked or key in liked or liked in key
                for liked in title_keys
                if liked
            ):
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
    semaphore = asyncio.Semaphore(WAVE_CONCURRENCY_LIMIT)
    pool_size = min(10, max(5, per_query + 2))

    async def run_one(query: str) -> list[dict[str, Any]]:
        async with semaphore:
            return await search_tracks(
                query, limit=per_query, pool_size=pool_size
            )

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
        raise AudioServiceError(
            "Сборка волны заняла слишком много времени и была прервана"
        ) from exc


def _wave_per_query_limit(limit: int, query_count: int) -> int:
    if query_count <= 0:
        return limit
    per_query = max(4, (limit // query_count) + 3)
    return min(per_query, 10)


async def get_wave_by_mood(vibe: str, limit: int = 15) -> list[dict[str, Any]]:
    cleaned_vibe = str(vibe or "").strip().lower()
    pool = list(WAVE_MOOD_QUERIES.get(cleaned_vibe) or [])
    if not pool:
        raise AudioServiceError(f"Неизвестный вайб волны: {vibe!r}")

    try:
        safe_limit = int(limit)
    except (TypeError, ValueError):
        safe_limit = 15
    safe_limit = max(1, min(safe_limit, MAX_SEARCH_RESULTS))

    random.shuffle(pool)
    first_batch = pool[:WAVE_QUERY_BATCH_SIZE]
    remaining = pool[len(first_batch) :]

    merged = await _wave_search_queries(
        first_batch, _wave_per_query_limit(safe_limit, len(first_batch))
    )
    tracks = _merge_wave_results(merged, safe_limit)
    random.shuffle(tracks)
    seen = {track["track_id"] for track in tracks}

    while len(tracks) < safe_limit and remaining:
        batch = remaining[:WAVE_TOPUP_BATCH_SIZE]
        remaining = remaining[len(batch) :]
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


def _discover_similar_queries_sync(
    artists: list[str], favorite_titles: set[str], limit: int
) -> list[str]:
    queries: list[str] = []
    try:
        with httpx.Client(timeout=6.0, follow_redirects=True) as client:
            for artist in artists[:4]:
                try:
                    found = (
                        client.get(
                            "https://api.deezer.com/search/artist",
                            params={"q": artist, "limit": 1},
                        )
                        .json()
                        .get("data", [])
                    )
                    if not found:
                        continue
                    aid = found[0]["id"]
                    related = (
                        client.get(
                            f"https://api.deezer.com/artist/{aid}/related",
                            params={"limit": 4},
                        )
                        .json()
                        .get("data", [])
                    )
                    for rel in related[:3]:
                        rel_name = str(rel.get("name") or "").strip()
                        if not rel_name:
                            continue
                        top_tracks = (
                            client.get(
                                f"https://api.deezer.com/artist/{rel['id']}/top",
                                params={"limit": 3},
                            )
                            .json()
                            .get("data", [])
                        )
                        for tr in top_tracks[:2]:
                            title = str(tr.get("title") or "").strip()
                            if (
                                title
                                and _normalize_song_title(title)
                                not in favorite_titles
                            ):
                                queries.append(
                                    f"{rel_name} - {title} official audio"
                                )
                                if len(queries) >= limit:
                                    return queries
                except Exception:
                    continue
    except Exception:
        pass
    return queries


async def get_wave_personalized(
    favorites: list[dict[str, Any]] | None, limit: int = 15
) -> list[dict[str, Any]]:
    try:
        safe_limit = int(limit)
    except (TypeError, ValueError):
        safe_limit = 15
    safe_limit = max(1, min(safe_limit, MAX_SEARCH_RESULTS))

    parsed = [
        _parse_favorite_item(f)
        for f in (favorites or [])
        if isinstance(f, Mapping)
    ]
    artist_names = list(
        dict.fromkeys(p["artist"] for p in parsed if p["artist"])
    )[:5]
    vibe_counts: dict[str, int] = {}
    for parsed_item in parsed:
        vibe_counts[parsed_item["vibe"]] = (
            vibe_counts.get(parsed_item["vibe"], 0) + 1
        )
    if vibe_counts:
        best_count = max(vibe_counts.values())
        detected_vibe = next(
            vibe
            for vibe in ("aggressive", "sad", "calm", "energetic")
            if vibe_counts.get(vibe, 0) == best_count
        )
    else:
        detected_vibe = "energetic"

    favorite_titles = {
        _normalize_song_title(x) for p in parsed for x in (p["title"],)
    }
    favorite_titles.update(
        _normalize_song_title(f.get("title"))
        for f in (favorites or [])
        if isinstance(f, Mapping)
    )
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
        if not fresh or len(tracks) >= safe_limit:
            return
        merged = await _wave_search_queries(
            fresh, _wave_per_query_limit(safe_limit, len(fresh))
        )
        more = _merge_wave_results(
            merged, safe_limit - len(tracks), seen, favorite_titles
        )
        tracks.extend(more)
        seen.update(track["track_id"] for track in more)

    artist_queries = [
        query
        for artist in artist_names[:3]
        for query in (f"{artist} popular track", f"{artist} feat")
    ]
    style_queries = list(VIBE_STYLE_QUERIES.get(detected_vibe, ()))
    await gather_batch(profile + artist_queries + style_queries)

    if len(tracks) < safe_limit and artist_names:
        try:
            discovered_queries = await asyncio.wait_for(
                asyncio.to_thread(
                    _discover_similar_queries_sync,
                    artist_names,
                    favorite_titles,
                    6,
                ),
                timeout=8.0,
            )
            if discovered_queries:
                await gather_batch(discovered_queries)
        except (asyncio.TimeoutError, Exception):
            pass

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


# ---------------------------------------------------------------- stream URL


def _track_webpage_url(track_id: str) -> str:
    if "://" in track_id:
        return track_id
    return f"https://www.youtube.com/watch?v={quote(track_id, safe='')}"


def _resolve_deezer_to_youtube_id_sync(deezer_id: str) -> tuple[str | None, str]:
    raw_id = deezer_id.split(":", 1)[-1].strip()
    if not raw_id.isdigit():
        raise AudioServiceError(f"Некорректный Deezer track id: {deezer_id!r}")

    url = f"https://api.deezer.com/track/{raw_id}"
    try:
        with httpx.Client(timeout=STREAM_TIMEOUT, follow_redirects=True) as client:
            response = client.get(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": BROWSER_USER_AGENT,
                },
            )
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        raise AudioServiceError(
            f"Не удалось получить трек Deezer {deezer_id!r}"
        ) from exc

    if not isinstance(payload, Mapping) or payload.get("error"):
        raise AudioServiceError(f"Трек Deezer {deezer_id!r} не найден")

    title = str(payload.get("title") or "").strip()
    artist_obj = payload.get("artist")
    artist = (
        str(artist_obj.get("name") or "").strip()
        if isinstance(artist_obj, Mapping)
        else ""
    )
    preview = str(payload.get("preview") or "").strip()

    if artist and title:
        try:
            entries = _yt_search_entries_sync(
                f"{artist} - {title} official audio", 4
            )
            for entry in entries:
                if _is_music_track(entry):
                    yt_id = str(entry.get("id") or "").strip()
                    if yt_id:
                        return yt_id, preview
        except Exception:
            pass

    return None, preview


def _extract_yt_stream_info_sync(track_id: str) -> ResolvedStreamInfo:
    params = {
        **_yt_dlp_base_params(),
        "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best[ext=mp4]/best",
        "ignore_no_formats_error": True,
    }

    try:
        info = _ydl_extract_info(_track_webpage_url(track_id), params)
    except Exception as exc:
        raise AudioServiceError(
            f"Не удалось получить аудиопоток для {track_id!r}"
        ) from exc

    if not isinstance(info, Mapping):
        raise AudioServiceError(f"Пустой ответ yt-dlp для {track_id!r}")

    entries = info.get("entries")
    if entries:
        for entry in entries:
            if isinstance(entry, Mapping):
                info = entry
                break

    stream_url = info.get("url")
    raw_headers = info.get("http_headers")

    if not stream_url:
        requested = info.get("requested_downloads") or []
        if requested and isinstance(requested[0], Mapping):
            stream_url = requested[0].get("url")
            raw_headers = requested[0].get("http_headers") or raw_headers

    if not stream_url:
        formats = info.get("formats") or []
        audio_formats = [
            fmt
            for fmt in formats
            if isinstance(fmt, Mapping)
            and fmt.get("url")
            and fmt.get("acodec") not in (None, "none")
        ]
        if not audio_formats:
            audio_formats = [
                fmt
                for fmt in formats
                if isinstance(fmt, Mapping)
                and fmt.get("url")
                and fmt.get("protocol") in ("https", "http")
            ]
        if audio_formats:
            m4a_formats = [
                fmt for fmt in audio_formats if fmt.get("ext") in ("m4a", "mp4")
            ]
            chosen = (m4a_formats or audio_formats)[-1]
            stream_url = chosen.get("url")
            raw_headers = chosen.get("http_headers") or raw_headers

    if not stream_url:
        raise AudioServiceError(f"yt-dlp не вернул прямой URL для {track_id!r}")

    headers: dict[str, str] = {}
    if isinstance(raw_headers, Mapping):
        for k, v in raw_headers.items():
            if k and v is not None:
                headers[str(k)] = str(v)

    return ResolvedStreamInfo(
        url=str(stream_url),
        headers=headers,
        yt_id=track_id if "://" not in track_id else None,
    )


def _resolve_stream_info_sync(track_id: str) -> ResolvedStreamInfo:
    if track_id.startswith("deezer:"):
        deezer_id = track_id.split(":", 1)[1].strip()
        if not deezer_id:
            raise AudioServiceError("Пустой идентификатор трека Deezer")
        yt_id, preview = _resolve_deezer_to_youtube_id_sync(deezer_id)
        if yt_id:
            try:
                return _extract_yt_stream_info_sync(yt_id)
            except Exception:
                if preview:
                    return ResolvedStreamInfo(url=preview)
                raise
        if not preview:
            raise AudioServiceError(
                f"У трека {track_id!r} нет доступного аудиопотока"
            )
        return ResolvedStreamInfo(url=preview)

    return _extract_yt_stream_info_sync(track_id)


def _get_audio_stream_url_sync(track_id: str) -> str:
    return _resolve_stream_info_sync(track_id).url


async def get_audio_stream_url(track_id: str) -> str:
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


# ------------------------------------------------------------- audio file cache


_AUDIO_CACHE_DIR_NAME = "tg-music-audio-cache"
_AUDIO_CACHE_MAX_FILE_BYTES = 64 * 1024 * 1024
_AUDIO_CACHE_MAX_TOTAL_BYTES = 256 * 1024 * 1024
_DOWNLOAD_CHUNK_SIZE = 256 * 1024

_audio_cache_index: dict[str, Path] = {}
_audio_cache_locks: dict[str, asyncio.Lock] = {}


def _audio_cache_dir() -> Path:
    directory = Path(tempfile.gettempdir()) / _AUDIO_CACHE_DIR_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _audio_cache_key(track_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", track_id)[:120]


def audio_media_type_for(track_or_path: str | Path) -> str:
    if isinstance(track_or_path, Path) or (
        isinstance(track_or_path, str)
        and track_or_path.endswith((".m4a", ".mp3", ".webm", ".ogg"))
    ):
        path = Path(track_or_path)
        if path.is_file():
            try:
                with path.open("rb") as fh:
                    head = fh.read(16)
                if len(head) >= 8 and head[4:8] == b"ftyp":
                    return "audio/mp4"
                if head.startswith(b"ID3") or (
                    len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0
                ):
                    return "audio/mpeg"
                if head.startswith(b"OggS"):
                    return "audio/ogg"
                if head.startswith(b"\x1a\x45\xdf\xa3"):
                    return "audio/webm"
            except OSError:
                pass
        suffix = path.suffix.lower()
        if suffix == ".mp3":
            return "audio/mpeg"
        if suffix == ".webm":
            return "audio/webm"
        if suffix == ".ogg":
            return "audio/ogg"
        return "audio/mp4"

    text = str(track_or_path or "").strip()
    cached = _audio_cache_index.get(text)
    if cached is not None and cached.is_file():
        return audio_media_type_for(cached)
    if text.startswith("deezer:"):
        return "audio/mpeg"
    return "audio/mp4"


def _audio_cache_prune() -> None:
    cache_dir = _audio_cache_dir()
    entries = [
        (path, path.stat().st_size, path.stat().st_mtime)
        for ext in ("*.m4a", "*.mp3", "*.webm", "*.ogg")
        for path in cache_dir.glob(ext)
        if path.is_file()
    ]
    total = sum(size for _, size, _ in entries)
    if total <= _AUDIO_CACHE_MAX_TOTAL_BYTES:
        return
    for path, size, _ in sorted(entries, key=lambda item: item[2]):
        if total <= _AUDIO_CACHE_MAX_TOTAL_BYTES:
            break
        try:
            path.unlink()
        except OSError:
            continue
        total -= size
        for key, value in list(_audio_cache_index.items()):
            if value == path:
                _audio_cache_index.pop(key, None)
                _audio_cache_locks.pop(key, None)


def _download_via_ytdlp_sync(
    track_id: str, destination: Path, resolved_yt_id: str | None = None
) -> None:
    """Reliable chunked download via yt-dlp with native fragment/HTTP retries."""
    target_id = resolved_yt_id or track_id
    if target_id.startswith("deezer:"):
        yt_id, preview = _resolve_deezer_to_youtube_id_sync(target_id)
        if not yt_id:
            if preview:
                with httpx.Client(
                    timeout=STREAM_TIMEOUT, follow_redirects=True
                ) as client:
                    resp = client.get(
                        preview, headers={"User-Agent": BROWSER_USER_AGENT}
                    )
                    resp.raise_for_status()
                    destination.write_bytes(resp.content)
                return
            raise AudioServiceError(
                f"Не удалось скачать трек Deezer {track_id!r}"
            )
        target_id = yt_id

    part_template = str(destination.with_name(destination.name + ".ydl.%(ext)s"))
    params: dict[str, Any] = {
        **_yt_dlp_base_params(),
        "skip_download": False,
        "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best[ext=mp4]/best",
        "outtmpl": part_template,
        "http_chunk_size": 524288,
        "fragment_retries": 5,
    }
    with yt_dlp.YoutubeDL(params) as ydl:
        ydl.extract_info(_track_webpage_url(target_id), download=True)

    candidates = list(
        destination.parent.glob(f"{destination.name}.ydl.*")
    )
    valid = [
        p
        for p in candidates
        if p.is_file() and not p.name.endswith(".part") and p.stat().st_size > 0
    ]
    if not valid:
        for p in candidates:
            p.unlink(missing_ok=True)
        raise AudioServiceError(
            f"yt-dlp не создал аудиофайл для {track_id!r}"
        )
    os.replace(valid[0], destination)
    for p in candidates:
        if p != valid[0]:
            p.unlink(missing_ok=True)


_CDN_RANGE_CHUNK = 1024 * 1024  # 1 MB на запрос — обходит троттлинг googlevideo.com


async def _download_audio_to_cache(
    track_id: str, stream_info: ResolvedStreamInfo, destination: Path
) -> None:
    """Download track in 1MB Range chunks to bypass YouTube CDN throttling."""
    client = await _get_stream_client()
    part_path = destination.with_name(destination.name + ".part")

    base_headers = {
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "User-Agent": BROWSER_USER_AGENT,
    }
    for k, v in stream_info.headers.items():
        if k.lower() != "accept-encoding":
            base_headers[k] = v

    try:
        written = 0
        total_size: int | None = None
        current_url = stream_info.url

        with part_path.open("wb") as fh:
            while True:
                end_byte = written + _CDN_RANGE_CHUNK - 1
                if total_size is not None:
                    end_byte = min(end_byte, total_size - 1)
                if total_size is not None and written >= total_size:
                    break

                headers = dict(base_headers)
                if "googlevideo.com" in current_url:
                    headers["Range"] = f"bytes={written}-{end_byte}"
                elif written > 0:
                    headers["Range"] = f"bytes={written}-"

                response: httpx.Response | None = None
                try:
                    request = client.build_request("GET", current_url, headers=headers)
                    response = await client.send(request, stream=True)
                    if response.status_code == 416:
                        break
                    response.raise_for_status()
                    # Запоминаем финальный URL после 302 редиректов, чтобы не ходить по цепочке каждый чанк
                    current_url = str(response.url)

                    content_range = response.headers.get("content-range", "")
                    if "/" in content_range:
                        total_part = content_range.rsplit("/", 1)[-1].strip()
                        if total_part.isdigit():
                            total_size = int(total_part)
                    elif total_size is None:
                        cl = response.headers.get("content-length", "")
                        if cl.isdigit() and response.status_code == 200:
                            total_size = int(cl)

                    chunk_bytes = 0
                    async for data in response.aiter_bytes(_DOWNLOAD_CHUNK_SIZE):
                        if data:
                            fh.write(data)
                            written += len(data)
                            chunk_bytes += len(data)
                            if written > _AUDIO_CACHE_MAX_FILE_BYTES:
                                raise AudioServiceError(
                                    f"Аудиофайл {track_id!r} слишком большой для кэша"
                                )

                    if chunk_bytes == 0:
                        break
                    if total_size is None and response.status_code == 200:
                        break
                finally:
                    if response is not None:
                        await response.aclose()

        if written == 0:
            raise AudioServiceError(f"Пустой аудиофайл для {track_id!r}")
        os.replace(part_path, destination)
    except Exception as exc:
        part_path.unlink(missing_ok=True)
        logger.warning(
            "Range-скачивание для %r не удалось (%s: %s), пробуем через yt-dlp",
            track_id,
            type(exc).__name__,
            exc,
        )
        try:
            await asyncio.wait_for(
                asyncio.to_thread(
                    _download_via_ytdlp_sync,
                    track_id,
                    destination,
                    stream_info.yt_id,
                ),
                timeout=45.0,
            )
        except Exception as fallback_exc:
            if isinstance(exc, AudioServiceError):
                raise exc
            raise AudioServiceError(
                f"Не удалось скачать аудио для {track_id!r}"
            ) from fallback_exc


async def get_cached_audio_file(track_id: str) -> Path:
    """Return a local, fully downloaded copy of the track's audio."""
    cleaned_track_id = str(track_id or "").strip()
    if not cleaned_track_id:
        raise ValueError("track_id is required")

    cache_dir = _audio_cache_dir()
    key = _audio_cache_key(cleaned_track_id)

    lock = _audio_cache_locks.setdefault(cleaned_track_id, asyncio.Lock())
    async with lock:
        cached = _audio_cache_index.get(cleaned_track_id)
        if cached is not None and cached.is_file() and cached.stat().st_size > 0:
            os.utime(cached, None)
            return cached

        for ext in (".m4a", ".mp3", ".webm", ".ogg"):
            existing = cache_dir / f"{key}{ext}"
            if existing.is_file() and existing.stat().st_size > 0:
                _audio_cache_index[cleaned_track_id] = existing
                os.utime(existing, None)
                return existing

        try:
            stream_info = await asyncio.wait_for(
                asyncio.to_thread(_resolve_stream_info_sync, cleaned_track_id),
                timeout=STREAM_RESOLVE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise AudioServiceError(
                f"Превышено время ожидания ссылки для {cleaned_track_id!r}"
            ) from exc

        ext = ".mp3" if ".mp3" in stream_info.url.lower() else ".m4a"
        destination = cache_dir / f"{key}{ext}"

        await _download_audio_to_cache(cleaned_track_id, stream_info, destination)
        _audio_cache_index[cleaned_track_id] = destination
        _audio_cache_prune()
        return destination


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
    target = str(url or "").strip()
    if not target:
        raise ValueError("url is required")
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")

    if client is None:
        client = await _get_stream_client()

    response: httpx.Response | None = None
    for attempt in (1, 2):
        try:
            request = client.build_request(
                "GET", target, headers=_request_headers(range_header)
            )
            response = await client.send(request, stream=True)
            response.raise_for_status()
            break
        except httpx.TransportError as exc:
            if response is not None:
                await response.aclose()
                response = None
            if attempt == 1:
                logger.warning(
                    "Транспортная ошибка при открытии аудиопотока (%s); "
                    "повторяю попытку",
                    type(exc).__name__,
                )
                continue
            raise
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
    stream = await start_audio_stream(
        url,
        range_header,
        client=client,
        chunk_size=chunk_size,
    )
    async for chunk in stream.body:
        yield chunk