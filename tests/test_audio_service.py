import httpx
import pytest

from backend import audio_service


class ChunkedStream(httpx.AsyncByteStream):
    """One-shot async stream used by httpx.MockTransport responses."""

    def __init__(self, chunks):
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk


class FakeYDL:
    """Minimal yt-dlp stand-in that records parameters."""

    def __init__(self, params, captured, info):
        captured["params"] = params
        self._captured = captured
        self._info = info

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def extract_info(self, url, download=False):
        self._captured["url"] = url
        self._captured["download"] = download
        return self._info


async def test_search_tracks_refines_query_and_maps_results(monkeypatch):
    captured = {}
    info = {
        "entries": [
            {
                "id": "abc",
                "title": "Song A",
                "uploader": "Uploader A",
                "duration": 215.7,
                "thumbnails": [
                    {"url": "https://img/small.jpg"},
                    {"url": "https://img/large.jpg"},
                ],
            },
            {
                "id": "def",
                "title": "Song B",
                "channel": "Channel B",
                "duration": 240,
                "thumbnail": "https://img/b.jpg",
            },
            {"title": "No id", "duration": 200},
            None,
        ]
    }
    monkeypatch.setattr(
        audio_service.yt_dlp,
        "YoutubeDL",
        lambda params: FakeYDL(params, captured, info),
    )

    results = await audio_service.search_tracks("  Daft   Punk  ", limit=5)

    assert captured["url"] == "ytsearch30:Daft Punk audio"
    assert captured["download"] is False
    assert captured["params"]["extract_flat"] is True
    assert captured["params"]["quiet"] is True
    assert results == [
        {
            "track_id": "abc",
            "title": "Song A",
            "artist": "Uploader A",
            "duration": 215,
            "thumbnail": "https://img/large.jpg",
        },
        {
            "track_id": "def",
            "title": "Song B",
            "artist": "Channel B",
            "duration": 240,
            "thumbnail": "https://img/b.jpg",
        },
    ]


async def test_search_tracks_filters_live_shorts_and_long_mixes(monkeypatch):
    captured = {}
    info = {
        "entries": [
            {"id": "valid45", "title": "45s", "duration": 45},
            {"id": "valid900", "title": "15m", "duration": 900},
            {"id": "valid215", "title": "Song", "duration": 215},
            {"id": "short44", "title": "Short", "duration": 44},
            {"id": "long901", "title": "Mix", "duration": 901},
            {
                "id": "live_status",
                "title": "Live",
                "duration": 300,
                "live_status": "is_live",
            },
            {
                "id": "is_live",
                "title": "Live 2",
                "duration": 300,
                "is_live": True,
            },
            {"id": "unknown", "title": "Unknown", "duration": None},
            {"id": "zero", "title": "Zero", "duration": 0},
            {"id": "negative", "title": "Negative", "duration": -10},
            {"id": "missing_duration", "title": "Missing"},
            {"title": "No id", "duration": 300},
        ]
    }
    monkeypatch.setattr(
        audio_service.yt_dlp,
        "YoutubeDL",
        lambda params: FakeYDL(params, captured, info),
    )

    results = await audio_service.search_tracks("music", limit=10)

    assert [track["track_id"] for track in results] == [
        "valid45",
        "valid900",
        "valid215",
    ]
    assert captured["url"] == "ytsearch30:music audio"


@pytest.mark.parametrize(
    "query", ["Song official audio", "Song lyrics", "Song audio"]
)
async def test_search_tracks_does_not_duplicate_music_hint(
    monkeypatch, query
):
    captured = {}
    monkeypatch.setattr(
        audio_service.yt_dlp,
        "YoutubeDL",
        lambda params: FakeYDL(params, captured, {"entries": []}),
    )

    assert await audio_service.search_tracks(query, limit=3) == []
    assert captured["url"] == f"ytsearch30:{query}"


async def test_search_tracks_returns_only_the_requested_limit(monkeypatch):
    captured = {}
    info = {
        "entries": [
            {"id": f"id{i}", "title": f"Song {i}", "duration": 200}
            for i in range(10)
        ]
    }
    monkeypatch.setattr(
        audio_service.yt_dlp,
        "YoutubeDL",
        lambda params: FakeYDL(params, captured, info),
    )

    results = await audio_service.search_tracks("query", limit=3)

    assert [track["track_id"] for track in results] == ["id0", "id1", "id2"]
    assert captured["url"] == "ytsearch30:query audio"


async def test_search_tracks_default_limit_is_15(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        audio_service.yt_dlp,
        "YoutubeDL",
        lambda params: FakeYDL(params, captured, {"entries": []}),
    )

    assert await audio_service.search_tracks("query") == []
    assert captured["url"] == "ytsearch30:query audio"


async def test_search_tracks_wraps_ytdlp_errors(monkeypatch):
    class BrokenYDL:
        def __init__(self, params):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def extract_info(self, url, download=False):
            raise RuntimeError("network is down")

    monkeypatch.setattr(audio_service.yt_dlp, "YoutubeDL", BrokenYDL)

    with pytest.raises(audio_service.AudioServiceError):
        await audio_service.search_tracks("query")


async def test_search_tracks_empty_query_does_not_call_ytdlp(monkeypatch):
    called = False

    def fail_if_called(params):
        nonlocal called
        called = True
        return FakeYDL(params, {}, {})

    monkeypatch.setattr(audio_service.yt_dlp, "YoutubeDL", fail_if_called)

    assert await audio_service.search_tracks("   ") == []
    assert called is False


async def test_get_audio_stream_url_uses_requested_format(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        audio_service.yt_dlp,
        "YoutubeDL",
        lambda params: FakeYDL(
            params, captured, {"url": "https://cdn.example/audio.m4a"}
        ),
    )

    url = await audio_service.get_audio_stream_url("abc123")

    assert url == "https://cdn.example/audio.m4a"
    assert captured["url"] == "https://www.youtube.com/watch?v=abc123"
    assert captured["download"] is False
    assert captured["params"]["format"] == "bestaudio[ext=m4a]/bestaudio"
    assert captured["params"]["quiet"] is True
    assert captured["params"]["noplaylist"] is True


async def test_get_audio_stream_url_keeps_full_urls(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        audio_service.yt_dlp,
        "YoutubeDL",
        lambda params: FakeYDL(
            params, captured, {"url": "https://cdn.example/audio.m4a"}
        ),
    )

    page_url = "https://soundcloud.com/artist/track"
    await audio_service.get_audio_stream_url(page_url)

    assert captured["url"] == page_url


async def test_get_audio_stream_url_falls_back_to_formats(monkeypatch):
    captured = {}
    info = {
        "formats": [
            {
                "url": "https://cdn.example/audio.webm",
                "acodec": "opus",
                "ext": "webm",
            },
            {
                "url": "https://cdn.example/audio.m4a",
                "acodec": "mp4a",
                "ext": "m4a",
            },
        ]
    }
    monkeypatch.setattr(
        audio_service.yt_dlp,
        "YoutubeDL",
        lambda params: FakeYDL(params, captured, info),
    )

    url = await audio_service.get_audio_stream_url("abc123")

    assert url == "https://cdn.example/audio.m4a"


async def test_get_audio_stream_url_raises_without_direct_url(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        audio_service.yt_dlp,
        "YoutubeDL",
        lambda params: FakeYDL(params, captured, {"title": "No stream"}),
    )

    with pytest.raises(audio_service.AudioServiceError):
        await audio_service.get_audio_stream_url("abc123")

    with pytest.raises(ValueError, match="track_id"):
        await audio_service.get_audio_stream_url("   ")


async def test_proxy_audio_stream_forwards_range_and_yields_bytes():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["range"] = request.headers.get("range")
        seen["accept_encoding"] = request.headers.get("accept-encoding")
        return httpx.Response(
            206,
            headers={
                "Content-Type": "audio/mp4",
                "Content-Range": "bytes 0-9/100",
                "Content-Length": "10",
                "Accept-Ranges": "bytes",
            },
            stream=ChunkedStream([b"0123", b"456789"]),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        chunks = [
            chunk
            async for chunk in audio_service.proxy_audio_stream(
                "https://cdn.example/audio", "bytes=0-9", client=client
            )
        ]
    finally:
        await client.aclose()

    assert b"".join(chunks) == b"0123456789"
    assert seen["range"] == "bytes=0-9"
    assert seen["accept_encoding"] == "identity"
    assert client.is_closed is True


async def test_start_audio_stream_returns_metadata_and_body():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("range") == "bytes=0-9"
        return httpx.Response(
            206,
            headers={
                "Content-Type": "audio/mp4; codecs=mp4a.40.2",
                "Content-Range": "bytes 0-9/100",
                "Content-Length": "10",
                "Accept-Ranges": "bytes",
            },
            stream=ChunkedStream([b"0123", b"456789"]),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        stream = await audio_service.start_audio_stream(
            "https://cdn.example/audio",
            "bytes=0-9",
            client=client,
            chunk_size=4,
        )

        assert stream.status_code == 206
        assert stream.media_type == "audio/mp4"
        assert stream.headers["Content-Range"] == "bytes 0-9/100"
        assert stream.headers["Content-Length"] == "10"
        assert stream.headers["Accept-Ranges"] == "bytes"

        body = b"".join([chunk async for chunk in stream.body])
        assert body == b"0123456789"
        # A caller-owned client must not be closed by the service.
        assert client.is_closed is False
    finally:
        await client.aclose()


async def test_start_audio_stream_normalizes_mp4_media_type():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "video/mp4"},
            stream=ChunkedStream([b"audio"]),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        stream = await audio_service.start_audio_stream(
            "https://cdn.example/audio", client=client
        )
        assert stream.media_type == "audio/mp4"
        assert b"".join([chunk async for chunk in stream.body]) == b"audio"
    finally:
        await client.aclose()


async def test_start_audio_stream_raises_on_upstream_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"missing")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await audio_service.start_audio_stream(
                "https://cdn.example/missing", client=client
            )
    finally:
        await client.aclose()


async def test_proxy_audio_stream_omits_range_when_not_requested():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["range"] = request.headers.get("range")
        return httpx.Response(200, stream=ChunkedStream([b"audio"]))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        chunks = [
            chunk
            async for chunk in audio_service.proxy_audio_stream(
                "https://cdn.example/audio", None, client=client
            )
        ]
    finally:
        await client.aclose()

    assert b"".join(chunks) == b"audio"
    assert seen["range"] is None


# ------------------------------------------------------------------ trending


class MultiSearchYDL:
    """yt-dlp stand-in answering each queried URL from a lookup table."""

    def __init__(self, responses, queries):
        self._responses = responses
        self._queries = queries

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def extract_info(self, url, download=False):
        if url not in self._responses:
            raise AssertionError(f"Unexpected search url: {url}")
        self._queries.append(url)
        return self._responses[url]


class FakeChartResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self._status_code = status_code

    def raise_for_status(self):
        if self._status_code >= 400:
            raise RuntimeError(f"HTTP {self._status_code}")

    def json(self):
        return self._payload


class FakeChartClient:
    def __init__(self, responses):
        self._responses = responses
        self.requested_urls = []
        self.closed = False

    def get(self, url, headers=None):
        self.requested_urls.append(url)
        payload, status = self._responses.get(url, ({}, 404))
        return FakeChartResponse(payload, status)

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def fresh_trending_cache():
    audio_service.TRENDING_CACHE["tracks"] = []
    audio_service.TRENDING_CACHE["updated_at"] = 0.0
    yield
    audio_service.TRENDING_CACHE["tracks"] = []
    audio_service.TRENDING_CACHE["updated_at"] = 0.0


def test_is_trending_track_rejects_compilations_and_conference_lengths():
    def entry(**kwargs):
        base = {"id": "x", "title": "Song", "duration": 200}
        base.update(kwargs)
        return base

    assert audio_service._is_trending_track(entry()) is True
    assert audio_service._is_trending_track(entry(duration=60)) is True
    assert audio_service._is_trending_track(entry(duration=420)) is True

    assert audio_service._is_trending_track(entry(duration=59)) is False
    assert audio_service._is_trending_track(entry(duration=421)) is False
    assert (
        audio_service._is_trending_track(entry(title="Summer Mix 2026"))
        is False
    )
    assert (
        audio_service._is_trending_track(
            entry(title="Best Compilation of Hits")
        )
        is False
    )
    assert (
        audio_service._is_trending_track(entry(title="Hits 1 Hour"))
        is False
    )
    assert (
        audio_service._is_trending_track(entry(title="Non-Stop Party"))
        is False
    )
    assert (
        audio_service._is_trending_track(entry(title="Song Live Version"))
        is False
    )
    assert (
        audio_service._is_trending_track(entry(duration=None)) is False
    )
    assert (
        audio_service._is_trending_track(
            entry(duration=3600, title="Long Stream")
        )
        is False
    )


async def test_trending_stage1_maps_apple_chart_to_youtube_ids(monkeypatch):
    chart_payload = {
        "feed": {
            "results": [
                {"name": "Song One", "artistName": "Artist One"},
                {"name": "Song Two", "artistName": "Artist Two"},
                {"name": "", "artistName": "Broken"},
                {"name": "Song Three", "artistName": ""},
            ]
        }
    }
    responses = {
        audio_service.TRENDING_URLS[0]: (chart_payload, 200),
        audio_service.TRENDING_URLS[1]: ({}, 200),
    }
    client = FakeChartClient(responses)
    monkeypatch.setattr(
        audio_service.httpx, "Client", lambda **kwargs: client
    )

    queries = []
    yt_responses = {
        "ytsearch1:Artist One Song One official audio": {
            "entries": [
                {
                    "id": "yt_one",
                    "title": "Song One official audio",
                    "uploader": "Artist One VEVO",
                    "duration": 199,
                    "thumbnail": "https://img/one.jpg",
                }
            ]
        },
        "ytsearch1:Artist Two Song Two official audio": {
            "entries": [
                {
                    "id": "yt_two",
                    "title": "Song Two (visualizer)",
                    "channel": "Artist Two",
                    "duration": 300,
                }
            ]
        },
    }
    monkeypatch.setattr(
        audio_service.yt_dlp,
        "YoutubeDL",
        lambda params: MultiSearchYDL(yt_responses, queries),
    )

    tracks = await audio_service.get_trending_tracks(limit=20)

    assert [track["track_id"] for track in tracks][:2] == ["yt_one", "yt_two"]
    first = tracks[0]
    assert first["title"] == "Song One"
    assert first["artist"] == "Artist One"
    assert first["duration"] == 199
    assert first["thumbnail"] == "https://img/one.jpg"

    # Only the needed artist/title searches were performed.
    assert len(queries) <= 20
    assert client.closed is True


async def test_trending_falls_back_to_pooled_search(monkeypatch):
    client = FakeChartClient({})
    monkeypatch.setattr(
        audio_service.httpx, "Client", lambda **kwargs: client
    )

    queries = []
    pool_1 = {
        "entries": [
            {"id": "ok1", "title": "Hit Song", "duration": 210},
            {"id": "mix", "title": "Best Mix 2026", "duration": 210},
            {"id": "hour", "title": "1 Hour of Hits", "duration": 3600},
            {"id": "ok2", "title": "Another Hit", "duration": 300},
        ]
    }
    pool_2 = {
        "entries": [
            {"id": "ok1", "title": "Hit Song", "duration": 210},
            {"id": "ok3", "title": "Trending Song", "duration": 240},
        ]
    }
    fallback_queries = audio_service.TRENDING_FALLBACK_QUERIES
    yt_responses = {
        f"ytsearch35:{fallback_queries[0]}": pool_1,
        f"ytsearch35:{fallback_queries[1]}": pool_2,
    }
    monkeypatch.setattr(
        audio_service.yt_dlp,
        "YoutubeDL",
        lambda params: MultiSearchYDL(yt_responses, queries),
    )

    tracks = await audio_service.get_trending_tracks(limit=10)

    assert [track["track_id"] for track in tracks] == ["ok1", "ok2", "ok3"]

    expected_queries = [
        f"ytsearch35:{query}" for query in fallback_queries
    ]
    assert queries == expected_queries


async def test_trending_cache_is_used_within_ttl(monkeypatch):
    monkeypatch.setattr(audio_service, "_get_trending_tracks_sync",
                        lambda limit: [{"track_id": "t1", "title": "T",
                                        "artist": "A", "duration": 200,
                                        "thumbnail": None}])

    first = await audio_service.get_trending_tracks()
    second = await audio_service.get_trending_tracks(limit=20)

    assert first == second
    assert first[0]["track_id"] == "t1"


async def test_trending_cache_refreshes_after_ttl(monkeypatch):
    calls = []
    clock = {"value": 1000.0}

    def fake_sync(limit):
        calls.append(limit)
        return [{"track_id": "t1", "title": "T", "artist": "A",
                 "duration": 200, "thumbnail": None}]

    def fake_monotonic():
        return clock["value"]

    monkeypatch.setattr(audio_service.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(audio_service, "_get_trending_tracks_sync", fake_sync)

    await audio_service.get_trending_tracks()
    await audio_service.get_trending_tracks()
    assert len(calls) == 1

    # Advance past the TTL: the cache must be refreshed once.
    clock["value"] += audio_service.TRENDING_CACHE_TTL_SECONDS + 1
    await audio_service.get_trending_tracks()
    assert len(calls) == 2


async def test_trending_keeps_previous_cache_when_refresh_fails(monkeypatch):
    calls = []

    def fake_sync(limit):
        calls.append(limit)
        if len(calls) == 1:
            return [{"track_id": "cached", "title": "T", "artist": "A",
                     "duration": 200, "thumbnail": None}]
        raise RuntimeError("yt-dlp exploded")

    monkeypatch.setattr(audio_service, "_get_trending_tracks_sync", fake_sync)

    first = await audio_service.get_trending_tracks()
    audio_service.TRENDING_CACHE["updated_at"] = (
        audio_service.time.monotonic()
        - audio_service.TRENDING_CACHE_TTL_SECONDS
        - 1
    )
    second = await audio_service.get_trending_tracks()

    # A failed refresh keeps serving the previous (stale) list instead of
    # returning an empty trending screen.
    assert first[0]["track_id"] == "cached"
    assert second == first
    assert len(calls) == 2
    assert audio_service.TRENDING_CACHE["tracks"][0]["track_id"] == "cached"


async def test_trending_limits_are_sane(monkeypatch):
    captured = {}

    def fake_sync(limit):
        captured["limit"] = limit
        return []

    monkeypatch.setattr(audio_service, "_get_trending_tracks_sync", fake_sync)

    await audio_service.get_trending_tracks(limit=3)
    assert captured["limit"] == 3

    await audio_service.get_trending_tracks(limit=1000)
    assert captured["limit"] == audio_service.MAX_SEARCH_RESULTS

    await audio_service.get_trending_tracks(limit="bad")
    assert captured["limit"] == 20
