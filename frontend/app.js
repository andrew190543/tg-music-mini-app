(() => {
    "use strict";

    const tg = window.Telegram?.WebApp;
    const FALLBACK_USER_ID = 123456;
    const userId =
        Number(window.Telegram?.WebApp?.initDataUnsafe?.user?.id) ||
        FALLBACK_USER_ID;
    const VOLUME_STORAGE_KEY = "tg-music-volume";
    const SEARCH_LIMIT = 15;
    const SEARCH_DEBOUNCE_MS = 450;
    const POPULAR_QUERY = "top hits";

    // Real YouTube IDs shown before the first /api/search response arrives.
    // They are replaced with live yt-dlp results as soon as the search finishes.
    const FALLBACK_TRACKS = [
        {
            track_id: "jfKfPfyJRdk",
            title: "Lofi Hip Hop Radio — Beats to Relax / Study To",
            artist: "Lofi Girl",
            duration: 0,
            thumbnail: "https://i.ytimg.com/vi/jfKfPfyJRdk/hqdefault.jpg",
        },
        {
            track_id: "4xDzrJKXOOY",
            title: "Blinding Lights",
            artist: "The Weeknd",
            duration: 200,
            thumbnail: "https://i.ytimg.com/vi/4xDzrJKXOOY/hqdefault.jpg",
        },
        {
            track_id: "dQw4w9WgXcQ",
            title: "Never Gonna Give You Up",
            artist: "Rick Astley",
            duration: 213,
            thumbnail: "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
        },
        {
            track_id: "kJQP7kiw5Fk",
            title: "Despacito",
            artist: "Luis Fonsi, Daddy Yankee",
            duration: 282,
            thumbnail: "https://i.ytimg.com/vi/kJQP7kiw5Fk/hqdefault.jpg",
        },
        {
            track_id: "JGwWNGJdvx8",
            title: "Shape of You",
            artist: "Ed Sheeran",
            duration: 233,
            thumbnail: "https://i.ytimg.com/vi/JGwWNGJdvx8/hqdefault.jpg",
        },
        {
            track_id: "OPf0YbXqDm0",
            title: "Uptown Funk",
            artist: "Mark Ronson, Bruno Mars",
            duration: 270,
            thumbnail: "https://i.ytimg.com/vi/OPf0YbXqDm0/hqdefault.jpg",
        },
    ];

    /* ---------- DOM ---------- */

    const badge = document.getElementById("api-badge");
    const searchInput = document.getElementById("search-input");
    const clearSearch = document.getElementById("clear-search");
    const tabs = document.querySelectorAll("[data-tab]");
    const tabPanels = document.querySelectorAll("[data-panel]");
    const tabTargets = document.querySelectorAll("[data-tab-target]");
    const trackList = document.getElementById("track-list");
    const searchTitle = document.getElementById("search-title");
    const searchCount = document.getElementById("search-count");
    const searchEmpty = document.getElementById("search-empty");
    const searchEmptyTitle = document.getElementById("search-empty-title");
    const searchEmptyText = document.getElementById("search-empty-text");
    const libraryList = document.getElementById("library-list");
    const libraryCount = document.getElementById("library-count");
    const libraryEmpty = document.getElementById("library-empty");
    const libraryHint = document.getElementById("library-hint");

    const fullPlayer = document.getElementById("full-player");
    const expandPlayer = document.getElementById("expand-player");
    const collapsePlayer = document.getElementById("collapse-player");
    const miniPlay = document.getElementById("mini-play");
    const miniNext = document.getElementById("mini-next");
    const miniCover = document.querySelector(".mini-player__cover");
    const miniArtist = document.querySelector(".mini-player__artist");
    const marqueeTexts = document.querySelectorAll(".marquee__text");
    const miniProgress = document.querySelector(".mini-player__progress span");

    const playButton = document.getElementById("play-btn");
    const nextButton = document.getElementById("next-btn");
    const prevButton = document.getElementById("prev-btn");
    const shuffleButton = document.getElementById("shuffle-btn");
    const repeatButton = document.getElementById("repeat-btn");
    const seekBar = document.getElementById("seek-bar");
    const volumeBar = document.getElementById("volume-bar");
    const volumeValue = document.getElementById("volume-value");
    const muteButton = document.getElementById("mute-btn");
    const currentTime = document.getElementById("current-time");
    const durationTime = document.getElementById("duration-time");
    const playerCover = document.getElementById("player-cover");
    const playerTitle = document.getElementById("player-title");
    const playerArtist = document.getElementById("player-artist");
    const playerSource = document.querySelector(".full-player__source");
    const fullLike = document.getElementById("full-like");

    /* ---------- Player state ---------- */

    const PlayerState = {
        audio: new Audio(),
        queue: [],
        currentIndex: -1,
        isPlaying: false,
        isShuffle: false,
        repeatMode: "none",
        volume: 1.0,
        userId: userId,
        favorites: new Map(),
    };

    const audio = PlayerState.audio;
    audio.preload = "metadata";

    const repeatStates = ["none", "all", "one"];
    const repeatLabels = {
        none: "Повтор: выключен",
        all: "Повтор: весь плейлист",
        one: "Повтор: один трек",
    };

    let lastAudibleVolume = 1;
    let isSeeking = false;
    let lastPositionUpdate = 0;

    PlayerState.volume = readStoredVolume();
    lastAudibleVolume = PlayerState.volume > 0 ? PlayerState.volume : 1;
    audio.volume = PlayerState.volume;

    /* ---------- Small helpers ---------- */

    function clamp(value, min, max) {
        return Math.min(Math.max(value, min), max);
    }

    function haptic(type = "light") {
        try {
            tg?.HapticFeedback?.impactOccurred?.(type);
        } catch {
            // Haptics are optional and unsupported in browsers.
        }
    }

    function initIcons() {
        try {
            window.lucide?.createIcons?.();
        } catch (error) {
            console.warn("Lucide icons are unavailable:", error);
        }
    }

    function setIcon(button, name) {
        if (!button || button.dataset.iconName === name) {
            return;
        }
        button.dataset.iconName = name;
        button.innerHTML = `<i data-lucide="${name}"></i>`;
        initIcons();
    }

    function formatTime(totalSeconds) {
        const seconds = Math.max(0, Math.round(Number(totalSeconds) || 0));
        const hours = Math.floor(seconds / 3600);
        const minutes = Math.floor((seconds % 3600) / 60);
        const rest = seconds % 60;

        if (hours > 0) {
            return `${hours}:${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
        }
        return `${minutes}:${String(rest).padStart(2, "0")}`;
    }

    function parseDuration(value) {
        if (typeof value === "number" && Number.isFinite(value)) {
            return Math.max(0, Math.floor(value));
        }

        const text = String(value ?? "").trim();
        if (!text) {
            return 0;
        }
        if (/^\d+(?:[.,]\d+)?$/.test(text)) {
            return Math.max(0, Math.floor(Number(text.replace(",", "."))));
        }

        const parts = text.split(":").map((part) => Number(part));
        if (parts.some((part) => !Number.isFinite(part))) {
            return 0;
        }
        return Math.round(
            parts.reduce((total, part) => total * 60 + part, 0)
        );
    }

    function paintRange(input) {
        if (!input) {
            return;
        }
        const min = Number(input.min) || 0;
        const max = Number(input.max) || 100;
        const value = Number(input.value) || 0;
        const percent = max > min ? ((value - min) / (max - min)) * 100 : 0;
        input.style.setProperty(
            "--value",
            `${clamp(percent, 0, 100).toFixed(2)}%`
        );
    }

    function readStoredVolume() {
        try {
            const stored = window.localStorage.getItem(VOLUME_STORAGE_KEY);
            if (stored !== null) {
                const value = Number(stored);
                if (Number.isFinite(value)) {
                    return clamp(value, 0, 1);
                }
            }
        } catch {
            // localStorage can be disabled in private/embedded contexts.
        }
        return 1.0;
    }

    function saveVolume(value) {
        try {
            window.localStorage.setItem(VOLUME_STORAGE_KEY, String(value));
        } catch {
            // Ignore storage errors: volume still works for this session.
        }
    }

    /* ---------- API status ---------- */

    function setBadge(state, text) {
        if (!badge) {
            return;
        }
        badge.className = `badge badge--${state}`;
        badge.textContent = text;
    }

    async function checkApi() {
        setBadge("pending", "Проверка…");

        try {
            const response = await fetch("/api/health", {
                cache: "no-store",
                headers: { Accept: "application/json" },
            });
            const data = await response.json();

            if (!response.ok || data.status !== "ok") {
                throw new Error(`API status: ${response.status}`);
            }

            setBadge("ok", "API онлайн");
        } catch (error) {
            console.error("Health check failed:", error);
            setBadge("error", "API офлайн");
        }
    }

    /* ---------- Telegram init & theme ---------- */

    function applyTheme() {
        const scheme = tg?.colorScheme === "light" ? "light" : "dark";
        document.documentElement.dataset.theme = scheme;

        if (tg && typeof tg.setHeaderColor === "function") {
            try {
                tg.setHeaderColor("secondary_bg_color");
                tg.setBackgroundColor?.("bg_color");
            } catch {
                // Old Telegram clients do not support header/background colors.
            }
        }
    }

    function initTelegram() {
        PlayerState.userId = userId;

        if (!tg) {
            applyTheme();
            return;
        }

        try {
            tg.ready();
            tg.expand();
        } catch {
            // The Mini App SDK may be loaded outside Telegram.
        }

        applyTheme();
        if (typeof tg.onEvent === "function") {
            tg.onEvent("themeChanged", applyTheme);
        }
    }

    /* ---------- Tabs ---------- */

    function activateTab(name) {
        tabs.forEach((tab) => {
            const active = tab.dataset.tab === name;
            tab.classList.toggle("is-active", active);
            tab.setAttribute("aria-selected", String(active));
        });

        tabPanels.forEach((panel) => {
            const active = panel.dataset.panel === name;
            panel.classList.toggle("is-active", active);
            panel.hidden = !active;
        });

        if (name === "library") {
            // Show the cached list instantly, then refresh it from SQLite.
            renderLibrary();
            loadFavorites({ force: true });
        }
    }

    tabs.forEach((tab) => {
        tab.addEventListener("click", () => {
            activateTab(tab.dataset.tab);
            haptic();
        });
    });

    tabTargets.forEach((button) => {
        button.addEventListener("click", () => {
            activateTab(button.dataset.tabTarget);
            haptic();
        });
    });

    /* ---------- Search field ---------- */

    let searchTimer = null;
    let searchRequestId = 0;
    let popularTracks = [];
    let popularLoaded = false;

    function syncClearButton() {
        if (!searchInput || !clearSearch) {
            return;
        }
        clearSearch.hidden = searchInput.value.length === 0;
    }

    function setSearchHeading(title) {
        if (searchTitle) {
            searchTitle.textContent = title;
        }
    }

    function setSearchCount(text) {
        if (searchCount) {
            searchCount.textContent = text;
        }
    }

    function pluralizeTracks(count) {
        const mod10 = count % 10;
        const mod100 = count % 100;
        if (mod10 === 1 && mod100 !== 11) {
            return `${count} трек`;
        }
        if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) {
            return `${count} трека`;
        }
        return `${count} треков`;
    }

    function hideSearchEmpty() {
        if (searchEmpty) {
            searchEmpty.hidden = true;
        }
    }

    function showSearchEmpty(title, text) {
        if (searchEmptyTitle) {
            searchEmptyTitle.textContent = title;
        }
        if (searchEmptyText) {
            searchEmptyText.textContent = text;
        }
        if (searchEmpty) {
            searchEmpty.hidden = false;
        }
    }

    async function performSearch(rawQuery, { popular = false } = {}) {
        const query = String(rawQuery ?? "").trim();
        if (!query) {
            return;
        }

        const requestId = ++searchRequestId;
        if (searchTimer !== null) {
            window.clearTimeout(searchTimer);
            searchTimer = null;
        }

        setSearchHeading(popular ? "Популярное" : "Результаты поиска");
        setSearchCount(popular && !popularLoaded ? "Загрузка…" : "Поиск…");
        trackList?.setAttribute("aria-busy", "true");
        hideSearchEmpty();

        try {
            const response = await fetch(
                `/api/search?q=${encodeURIComponent(query)}&limit=${SEARCH_LIMIT}`,
                {
                    cache: "no-store",
                    headers: { Accept: "application/json" },
                }
            );

            if (!response.ok) {
                throw new Error(`Search HTTP ${response.status}`);
            }

            const payload = await response.json();
            if (requestId !== searchRequestId) {
                return;
            }

            const tracks = (Array.isArray(payload) ? payload : [])
                .map(normalizeTrack)
                .filter((track) => track.track_id);

            if (popular) {
                popularTracks = tracks;
                popularLoaded = true;
            }

            renderTracks(tracks);
            syncQueueFromCards({ preserveCurrent: Boolean(audio.src) });
            setSearchCount(pluralizeTracks(tracks.length));

            if (tracks.length) {
                hideSearchEmpty();
            } else if (popular) {
                showSearchEmpty(
                    "Популярное недоступно",
                    "Введите название трека или исполнителя в поиске."
                );
            } else {
                showSearchEmpty(
                    "Ничего не найдено",
                    `По запросу «${query}» ничего не найдено. ` +
                        "Попробуйте изменить запрос."
                );
            }
        } catch (error) {
            if (requestId !== searchRequestId) {
                return;
            }
            console.error("Search failed:", error);
            setSearchCount("Ошибка поиска");

            if (!trackList?.children.length) {
                showSearchEmpty(
                    "Не удалось загрузить треки",
                    "Проверьте подключение к интернету и попробуйте ещё раз."
                );
            }
        } finally {
            if (requestId === searchRequestId) {
                trackList?.setAttribute("aria-busy", "false");
            }
        }
    }

    function restorePopular() {
        if (searchTimer !== null) {
            window.clearTimeout(searchTimer);
            searchTimer = null;
        }
        setSearchHeading("Популярное");

        if (!popularLoaded) {
            performSearch(POPULAR_QUERY, { popular: true });
            return;
        }

        renderTracks(popularTracks);
        syncQueueFromCards({ preserveCurrent: Boolean(audio.src) });
        setSearchCount(pluralizeTracks(popularTracks.length));
        trackList?.setAttribute("aria-busy", "false");

        if (popularTracks.length) {
            hideSearchEmpty();
        } else {
            showSearchEmpty(
                "Популярное недоступно",
                "Введите название трека или исполнителя в поиске."
            );
        }
    }

    function scheduleSearch() {
        if (!searchInput) {
            return;
        }

        if (searchTimer !== null) {
            window.clearTimeout(searchTimer);
            searchTimer = null;
        }

        const query = searchInput.value.trim();
        if (!query) {
            searchTimer = window.setTimeout(() => {
                searchTimer = null;
                restorePopular();
            }, 150);
            return;
        }

        searchTimer = window.setTimeout(() => {
            searchTimer = null;
            performSearch(query);
        }, SEARCH_DEBOUNCE_MS);
    }

    searchInput?.addEventListener("input", () => {
        syncClearButton();
        scheduleSearch();
    });

    searchInput?.addEventListener("keydown", (event) => {
        if (event.key !== "Enter") {
            return;
        }
        event.preventDefault();
        if (searchTimer !== null) {
            window.clearTimeout(searchTimer);
            searchTimer = null;
        }
        const query = searchInput.value.trim();
        if (query) {
            performSearch(query);
            searchInput.blur();
        } else {
            restorePopular();
        }
    });

    clearSearch?.addEventListener("click", () => {
        if (!searchInput) {
            return;
        }
        if (searchTimer !== null) {
            window.clearTimeout(searchTimer);
            searchTimer = null;
        }
        searchInput.value = "";
        syncClearButton();
        restorePopular();
        searchInput.focus();
        haptic();
    });

    syncClearButton();

    /* ---------- Now-playing marquee ---------- */

    function setupMarquee() {
        const wrap = document.querySelector(".marquee");
        const track = wrap?.querySelector(".marquee__track");
        const text = track?.querySelector(".marquee__text");
        if (!wrap || !track || !text) {
            return;
        }

        // The duplicate copy is only shown when the title actually overflows.
        track.classList.remove("is-scrolling");
        const overflows = text.getBoundingClientRect().width > wrap.clientWidth;
        track.classList.toggle("is-scrolling", overflows);
    }

    window.addEventListener("resize", setupMarquee);
    document.fonts?.ready?.then(setupMarquee).catch(() => {});

    /* ---------- Queue ---------- */

    function getTrackFromCard(card) {
        const title =
            card.dataset.title ||
            card.querySelector(".track-card__title")?.textContent.trim() ||
            "Без названия";
        const artist =
            card.dataset.artist ||
            card.querySelector(".track-card__artist")?.textContent.trim() ||
            "";
        const duration =
            card.dataset.duration ||
            card.querySelector(".track-card__duration")?.textContent ||
            "";
        const thumbnail =
            card.dataset.thumbnail ||
            card.querySelector(".track-card__cover img")?.getAttribute("src") ||
            "";

        return {
            track_id: card.dataset.trackId || "",
            title,
            artist,
            duration: parseDuration(duration),
            thumbnail,
        };
    }

    function normalizeTrack(track) {
        return {
            track_id: String(track?.track_id ?? track?.id ?? "").trim(),
            title: String(track?.title ?? "").trim() || "Без названия",
            artist: String(track?.artist ?? track?.uploader ?? "").trim(),
            duration: parseDuration(track?.duration),
            thumbnail: String(track?.thumbnail ?? track?.cover ?? "").trim(),
        };
    }

    /* ---------- Favorites ---------- */

    let favoritesLoading = false;
    let favoritesLoaded = false;
    let favoritesRequestId = 0;
    let favoritesRevision = 0;
    const pendingLikeRequests = new Set();

    function isFavorite(trackId) {
        return Boolean(trackId) && PlayerState.favorites.has(String(trackId));
    }

    function storeFavorite(track) {
        const normalized = normalizeTrack(track);
        if (!normalized.track_id) {
            return null;
        }

        // New favorites go first, matching the backend's newest-first order.
        const next = new Map([[normalized.track_id, normalized]]);
        PlayerState.favorites.forEach((value, key) => {
            if (key !== normalized.track_id) {
                next.set(key, value);
            }
        });
        PlayerState.favorites = next;
        return normalized;
    }

    function syncFullLike(
        track = PlayerState.queue[PlayerState.currentIndex]
    ) {
        if (!fullLike) {
            return;
        }

        const liked =
            Boolean(track?.track_id) && isFavorite(track.track_id);
        fullLike.setAttribute("aria-pressed", String(liked));
        fullLike.setAttribute(
            "aria-label",
            liked ? "Убрать из избранного" : "Добавить в избранное"
        );
        fullLike.disabled = !track?.track_id;
    }

    function syncLikeUI() {
        document.querySelectorAll(".track-card").forEach((card) => {
            const button = card.querySelector(".track-card__like");
            if (!button) {
                return;
            }

            const liked = isFavorite(card.dataset.trackId);
            const title = card.dataset.title || "трек";
            button.setAttribute("aria-pressed", String(liked));
            button.setAttribute(
                "aria-label",
                liked
                    ? `Убрать «${title}» из избранного`
                    : `Добавить «${title}» в избранное`
            );
        });

        syncFullLike();
    }

    function setLibraryLoading(loading) {
        libraryList?.setAttribute("aria-busy", String(loading));
        if (loading && libraryCount && !favoritesLoaded) {
            libraryCount.textContent = "Загрузка…";
        }
    }

    function renderLibrary() {
        if (!libraryList) {
            return;
        }

        const tracks = [...PlayerState.favorites.values()];
        renderTrackList(libraryList, tracks);

        if (libraryCount) {
            libraryCount.textContent = pluralizeTracks(tracks.length);
        }
        if (libraryEmpty) {
            libraryEmpty.hidden = tracks.length > 0;
        }
        if (libraryHint) {
            libraryHint.hidden = false;
        }
    }

    async function loadFavorites({ force = false } = {}) {
        if (favoritesLoading && !force) {
            return;
        }

        const requestId = ++favoritesRequestId;
        const revision = favoritesRevision;
        favoritesLoading = true;
        setLibraryLoading(true);

        if (libraryEmpty && !favoritesLoaded) {
            libraryEmpty.hidden = true;
        }

        try {
            const response = await fetch(
                `/api/favorites?user_id=${encodeURIComponent(
                    String(PlayerState.userId)
                )}`,
                {
                    cache: "no-store",
                    headers: { Accept: "application/json" },
                }
            );

            if (!response.ok) {
                throw new Error(`Favorites HTTP ${response.status}`);
            }

            const payload = await response.json();
            if (requestId !== favoritesRequestId) {
                return;
            }
            if (revision !== favoritesRevision || pendingLikeRequests.size) {
                // A like/unlike is in flight; keep the optimistic local state.
                return;
            }

            const tracks = (Array.isArray(payload) ? payload : [])
                .map(normalizeTrack)
                .filter((track) => track.track_id);

            PlayerState.favorites = new Map(
                tracks.map((track) => [track.track_id, track])
            );
            favoritesLoaded = true;
            renderLibrary();
            syncLikeUI();
        } catch (error) {
            if (requestId !== favoritesRequestId) {
                return;
            }
            console.error("Favorites load failed:", error);

            if (!favoritesLoaded) {
                if (libraryCount) {
                    libraryCount.textContent = "Ошибка загрузки";
                }
                if (libraryEmpty) {
                    libraryEmpty.hidden = false;
                }
            } else {
                renderLibrary();
            }
        } finally {
            if (requestId === favoritesRequestId) {
                favoritesLoading = false;
                setLibraryLoading(false);
            }
        }
    }

    function favoritePayload(track) {
        const normalized = normalizeTrack(track);
        return {
            user_id: PlayerState.userId,
            track_id: normalized.track_id,
            title: normalized.title,
            artist: normalized.artist,
            duration: normalized.duration,
            thumbnail: normalized.thumbnail,
        };
    }

    async function toggleFavorite(track, button) {
        const normalized = normalizeTrack(track);
        const trackId = normalized.track_id;
        if (!trackId || pendingLikeRequests.has(trackId)) {
            return;
        }

        const wasFavorite = isFavorite(trackId);
        const previous = PlayerState.favorites.get(trackId) || normalized;
        pendingLikeRequests.add(trackId);
        favoritesRevision += 1;
        button?.setAttribute("aria-busy", "true");

        if (wasFavorite) {
            PlayerState.favorites.delete(trackId);
        } else {
            storeFavorite(normalized);
        }
        syncLikeUI();
        renderLibrary();
        haptic();

        try {
            const response = await fetch(
                wasFavorite
                    ? `/api/favorites/${encodeURIComponent(
                          trackId
                      )}?user_id=${encodeURIComponent(
                          String(PlayerState.userId)
                      )}`
                    : "/api/favorites",
                {
                    method: wasFavorite ? "DELETE" : "POST",
                    cache: "no-store",
                    headers: wasFavorite
                        ? { Accept: "application/json" }
                        : {
                              Accept: "application/json",
                              "Content-Type": "application/json",
                          },
                    body: wasFavorite
                        ? undefined
                        : JSON.stringify(favoritePayload(normalized)),
                }
            );

            if (!response.ok) {
                throw new Error(`Favorites HTTP ${response.status}`);
            }

            const payload = await response.json();
            if (!wasFavorite) {
                storeFavorite(payload?.track || normalized);
            }

            favoritesLoaded = true;
            favoritesRevision += 1;
            renderLibrary();
            syncLikeUI();
        } catch (error) {
            console.error("Favorite update failed:", error);

            if (wasFavorite) {
                PlayerState.favorites.set(trackId, previous);
            } else {
                PlayerState.favorites.delete(trackId);
            }
            favoritesRevision += 1;
            renderLibrary();
            syncLikeUI();
        } finally {
            pendingLikeRequests.delete(trackId);
            button?.removeAttribute("aria-busy");
        }
    }

    function markCurrentCard() {
        const currentId = PlayerState.queue[PlayerState.currentIndex]?.track_id;

        document.querySelectorAll(".track-card").forEach((card) => {
            card.classList.toggle(
                "is-current",
                Boolean(currentId) && card.dataset.trackId === currentId
            );
            if (!card.hasAttribute("tabindex")) {
                card.tabIndex = 0;
            }
        });
    }

    function syncQueueFromCards({ preserveCurrent = false } = {}) {
        if (!trackList) {
            return;
        }

        const previousTrack =
            PlayerState.queue[PlayerState.currentIndex] || null;
        const previousId = previousTrack?.track_id || "";
        const tracks = [...trackList.querySelectorAll(".track-card")]
            .map(getTrackFromCard)
            .filter((track) => track.track_id);

        if (preserveCurrent && previousTrack) {
            // Search results replace the visible list, but an already loaded
            // track must keep playing. Keep it in the queue even when it is
            // absent from the new results.
            let index = tracks.findIndex(
                (track) => track.track_id === previousId
            );
            if (index === -1) {
                const insertAt = clamp(
                    PlayerState.currentIndex,
                    0,
                    tracks.length
                );
                tracks.splice(insertAt, 0, previousTrack);
                index = insertAt;
            }
            PlayerState.queue = tracks;
            PlayerState.currentIndex = index;
            markCurrentCard();
            return;
        }

        PlayerState.queue = tracks;

        if (tracks.length) {
            PlayerState.currentIndex = 0;
            updateTrackUI(tracks[0]);
        } else {
            PlayerState.currentIndex = -1;
            markCurrentCard();
        }
    }

    function setQueue(tracks, startIndex = -1) {
        PlayerState.queue = (Array.isArray(tracks) ? tracks : [])
            .map(normalizeTrack)
            .filter((track) => track.track_id);
        PlayerState.currentIndex = -1;

        if (!PlayerState.queue.length) {
            markCurrentCard();
            return;
        }

        const index =
            startIndex >= 0 && startIndex < PlayerState.queue.length
                ? startIndex
                : 0;

        if (startIndex >= 0) {
            playTrack(index);
            return;
        }

        // Preselect the first track so pressing Play works immediately,
        // but do not start a network request until the user asks for it.
        PlayerState.currentIndex = index;
        updateTrackUI(PlayerState.queue[index]);
    }

    /* ---------- Track UI ---------- */

    function applyCover(element, thumbnail) {
        if (!element) {
            return;
        }

        if (thumbnail) {
            element.style.backgroundImage = `url("${String(thumbnail).replace(
                /"/g,
                '\\"'
            )}")`;
            element.classList.add("has-image");
        } else {
            element.style.removeProperty("background-image");
            element.classList.remove("has-image");
        }
    }

    function buildTrackCard(track, index) {
        const card = document.createElement("article");
        card.className = "track-card";
        card.dataset.trackId = track.track_id;
        card.dataset.title = track.title;
        card.dataset.artist = track.artist;
        card.dataset.duration = String(track.duration || "");
        card.dataset.thumbnail = track.thumbnail;
        card.tabIndex = 0;

        const cover = document.createElement("div");
        cover.className = `track-card__cover cover cover--${
            (index % 3) + 1
        }`;
        cover.setAttribute("aria-hidden", "true");

        const icon = document.createElement("i");
        icon.dataset.lucide = "music-2";
        cover.append(icon);

        if (track.thumbnail) {
            const image = document.createElement("img");
            image.className = "track-card__cover-img";
            image.src = track.thumbnail;
            image.alt = "";
            image.loading = "lazy";
            image.decoding = "async";
            image.addEventListener("error", () => image.remove(), {
                once: true,
            });
            cover.append(image);
        }

        const body = document.createElement("div");
        body.className = "track-card__body";

        const title = document.createElement("h3");
        title.className = "track-card__title";
        title.textContent = track.title;

        const artist = document.createElement("p");
        artist.className = "track-card__artist";
        artist.textContent = track.artist || "Неизвестный исполнитель";

        body.append(title, artist);

        const duration = document.createElement("span");
        duration.className = "track-card__duration";
        duration.textContent =
            track.duration > 0 ? formatTime(track.duration) : "—";

        const like = document.createElement("button");
        like.className = "track-card__like";
        like.type = "button";
        like.setAttribute("aria-pressed", "false");
        like.setAttribute(
            "aria-label",
            `Добавить «${track.title}» в избранное`
        );

        const heart = document.createElement("i");
        heart.dataset.lucide = "heart";
        like.append(heart);

        card.append(cover, body, duration, like);
        return card;
    }

    function renderTrackList(container, tracks) {
        if (!container) {
            return [];
        }

        const normalized = (Array.isArray(tracks) ? tracks : [])
            .map(normalizeTrack)
            .filter((track) => track.track_id);

        const fragment = document.createDocumentFragment();
        normalized.forEach((track, index) => {
            fragment.append(buildTrackCard(track, index));
        });

        container.replaceChildren(fragment);
        container.setAttribute("aria-busy", "false");
        initIcons();
        syncLikeUI();
        return normalized;
    }

    function renderTracks(tracks) {
        return renderTrackList(trackList, tracks);
    }

    function updateTrackUI(track) {
        if (!track) {
            return;
        }

        const artist = track.artist || "Неизвестный исполнитель";

        if (playerTitle) {
            playerTitle.textContent = track.title;
        }
        if (playerArtist) {
            playerArtist.textContent = artist;
        }
        if (miniArtist) {
            miniArtist.textContent = artist;
        }
        if (playerSource) {
            playerSource.textContent = "TG Music";
        }

        marqueeTexts.forEach((element) => {
            element.textContent = track.title;
        });

        applyCover(miniCover, track.thumbnail);
        applyCover(playerCover, track.thumbnail);

        document.title = `${track.title} — ${artist} · TG Music`;

        if (seekBar) {
            seekBar.max = String(track.duration > 0 ? track.duration : 100);
            seekBar.value = "0";
            paintRange(seekBar);
        }
        if (currentTime) {
            currentTime.textContent = "0:00";
        }
        if (durationTime) {
            durationTime.textContent = formatTime(track.duration);
        }
        if (miniProgress) {
            miniProgress.style.width = "0%";
        }

        markCurrentCard();
        setupMarquee();
        syncFullLike(track);
        lastPositionUpdate = 0;
    }

    /* ---------- Playback ---------- */

    function getEffectiveDuration() {
        if (Number.isFinite(audio.duration) && audio.duration > 0) {
            return audio.duration;
        }
        return PlayerState.queue[PlayerState.currentIndex]?.duration || 0;
    }

    function renderPlayState() {
        const icon = PlayerState.isPlaying ? "pause" : "play";
        const label = PlayerState.isPlaying ? "Пауза" : "Воспроизвести";

        [miniPlay, playButton].forEach((button) => {
            if (!button) {
                return;
            }
            button.setAttribute("aria-label", label);
            setIcon(button, icon);
        });
    }

    function handlePlaybackFailure(error) {
        if (error?.name === "AbortError") {
            // A new source interrupted the previous play() call.
            return;
        }
        console.error("Playback failed:", error);
        PlayerState.isPlaying = false;
        renderPlayState();
        setMediaPlaybackState("paused");
        if (playerSource) {
            playerSource.textContent = "Не удалось воспроизвести";
        }
    }

    function playTrack(index) {
        const track = PlayerState.queue[index];
        if (!track?.track_id) {
            return;
        }

        PlayerState.currentIndex = index;
        updateTrackUI(track);

        audio.src = `/api/stream/${encodeURIComponent(track.track_id)}`;
        audio.load();
        updateMediaSession(track);
        updatePositionState(0, 0, true);

        const playPromise = audio.play();
        if (playPromise?.catch) {
            playPromise.catch(handlePlaybackFailure);
        }
    }

    function replayCurrent() {
        if (PlayerState.currentIndex < 0) {
            return;
        }
        try {
            audio.currentTime = 0;
        } catch {
            // The source may not be seekable yet.
        }
        audio.play()?.catch?.(handlePlaybackFailure);
    }

    function togglePlay() {
        if (!PlayerState.queue.length) {
            syncQueueFromCards();
        }

        if (PlayerState.currentIndex === -1) {
            if (PlayerState.queue.length) {
                playTrack(0);
            }
            return;
        }

        if (!audio.src) {
            playTrack(PlayerState.currentIndex);
            return;
        }

        if (audio.paused) {
            audio.play()?.catch?.(handlePlaybackFailure);
        } else {
            audio.pause();
        }
    }

    function pickRandomIndex() {
        const length = PlayerState.queue.length;
        if (length <= 1) {
            return 0;
        }

        let next = PlayerState.currentIndex;
        while (next === PlayerState.currentIndex) {
            next = Math.floor(Math.random() * length);
        }
        return next;
    }

    function stopAtQueueEnd() {
        audio.pause();
        try {
            audio.currentTime = 0;
        } catch {
            // Ignore seek errors on unloaded audio.
        }
        PlayerState.isPlaying = false;
        renderPlayState();
        updateTimeline();
        setMediaPlaybackState("none");
    }

    function nextTrack({ fromEnded = false } = {}) {
        const length = PlayerState.queue.length;
        if (!length) {
            return;
        }

        if (fromEnded && PlayerState.repeatMode === "one") {
            replayCurrent();
            return;
        }

        if (PlayerState.currentIndex === -1) {
            playTrack(0);
            return;
        }

        let next;
        if (PlayerState.isShuffle) {
            next = pickRandomIndex();
        } else {
            next = PlayerState.currentIndex + 1;
        }

        if (next >= length) {
            if (PlayerState.repeatMode === "all") {
                next = 0;
            } else if (fromEnded) {
                stopAtQueueEnd();
                return;
            } else {
                next = 0;
            }
        }

        playTrack(next);
    }

    function prevTrack() {
        const length = PlayerState.queue.length;
        if (!length) {
            return;
        }

        if (PlayerState.currentIndex === -1) {
            playTrack(0);
            return;
        }

        let index = PlayerState.isShuffle
            ? pickRandomIndex()
            : PlayerState.currentIndex - 1;

        if (index < 0) {
            index =
                PlayerState.repeatMode === "all" ? length - 1 : 0;
        }

        playTrack(index);
    }

    function handleEnded() {
        if (!PlayerState.queue.length) {
            return;
        }

        if (PlayerState.repeatMode === "one") {
            replayCurrent();
            return;
        }

        nextTrack({ fromEnded: true });
    }

    function handleAudioError() {
        console.error("Audio element error:", audio.error);
        PlayerState.isPlaying = false;
        renderPlayState();
        setMediaPlaybackState("none");
        if (playerSource) {
            playerSource.textContent = "Ошибка загрузки трека";
        }
    }

    function selectCard(card) {
        const trackId = card.dataset.trackId;
        if (!trackId) {
            return;
        }

        const previousCurrentId =
            PlayerState.queue[PlayerState.currentIndex]?.track_id || "";
        let index = PlayerState.queue.findIndex(
            (track) => track.track_id === trackId
        );

        if (index === -1) {
            // Library cards live in a separate list. Build the queue from the
            // list the user actually clicked, not only from search results.
            const container = card.closest(".track-list");
            const tracks = container
                ? [...container.querySelectorAll(".track-card")]
                      .map(getTrackFromCard)
                      .filter((track) => track.track_id)
                : [];

            if (tracks.length) {
                PlayerState.queue = tracks;
                index = tracks.findIndex(
                    (track) => track.track_id === trackId
                );
            }
        }

        if (index === -1) {
            syncQueueFromCards();
            index = PlayerState.queue.findIndex(
                (track) => track.track_id === trackId
            );
        }

        if (index === -1) {
            return;
        }

        haptic();
        if (previousCurrentId && previousCurrentId === trackId) {
            PlayerState.currentIndex = index;
            togglePlay();
        } else {
            playTrack(index);
        }
    }

    /* ---------- Timeline & volume ---------- */

    function updateTimeline() {
        const duration = getEffectiveDuration();
        const current = Number.isFinite(audio.currentTime)
            ? audio.currentTime
            : 0;

        if (seekBar) {
            if (duration > 0) {
                seekBar.max = String(duration);
            }
            if (!isSeeking) {
                seekBar.value = String(
                    Math.min(current, Number(seekBar.max) || current)
                );
            }
            paintRange(seekBar);
        }

        if (currentTime) {
            currentTime.textContent = formatTime(
                isSeeking ? Number(seekBar?.value) || 0 : current
            );
        }
        if (durationTime) {
            durationTime.textContent = formatTime(duration);
        }
        if (miniProgress) {
            const percent =
                duration > 0 ? Math.min(100, (current / duration) * 100) : 0;
            miniProgress.style.width = `${percent}%`;
        }

        updatePositionState(duration, current);
    }

    function handleMetadata() {
        updateTimeline();
        updatePositionState(undefined, audio.currentTime, true);
    }

    function syncVolumeUI() {
        const percent = Math.round(PlayerState.volume * 100);

        if (volumeBar) {
            volumeBar.value = String(percent);
            paintRange(volumeBar);
        }
        if (volumeValue) {
            volumeValue.textContent = `${percent}%`;
        }
        if (muteButton) {
            const muted = PlayerState.volume === 0;
            muteButton.setAttribute("aria-pressed", String(muted));
            muteButton.setAttribute(
                "aria-label",
                muted ? "Включить звук" : "Выключить звук"
            );
            setIcon(muteButton, muted ? "volume-x" : "volume-2");
        }
    }

    function setVolume(value, { persist = true } = {}) {
        const next = clamp(Number(value) || 0, 0, 1);
        PlayerState.volume = next;
        audio.volume = next;

        if (next > 0) {
            lastAudibleVolume = next;
        }
        syncVolumeUI();

        if (persist) {
            saveVolume(next);
        }
    }

    /* ---------- Media Session API ---------- */

    function setMediaPlaybackState(state) {
        try {
            if ("mediaSession" in navigator) {
                navigator.mediaSession.playbackState = state;
            }
        } catch {
            // The Media Session API is best-effort.
        }
    }

    function updateMediaSession(track) {
        if (
            !("mediaSession" in navigator) ||
            typeof MediaMetadata === "undefined"
        ) {
            return;
        }

        const artwork = [];
        if (track.thumbnail) {
            let src = track.thumbnail;
            try {
                src = new URL(track.thumbnail, window.location.href).href;
            } catch {
                // Keep the original value if URL construction fails.
            }
            artwork.push({ src });
        }

        try {
            navigator.mediaSession.metadata = new MediaMetadata({
                title: track.title,
                artist: track.artist || "TG Music",
                album: "TG Music",
                artwork,
            });
        } catch (error) {
            console.warn("Media Session metadata is unavailable:", error);
        }
    }

    function updatePositionState(duration, position, force = false) {
        if (
            !("mediaSession" in navigator) ||
            typeof navigator.mediaSession.setPositionState !== "function"
        ) {
            return;
        }

        const total =
            Number.isFinite(duration) && duration > 0
                ? duration
                : getEffectiveDuration();
        if (!Number.isFinite(total) || total <= 0) {
            return;
        }

        const now = Date.now();
        if (!force && now - lastPositionUpdate < 900) {
            return;
        }
        lastPositionUpdate = now;

        const current = Number.isFinite(position)
            ? position
            : audio.currentTime;

        try {
            navigator.mediaSession.setPositionState({
                duration: total,
                playbackRate: audio.playbackRate || 1,
                position: clamp(Number(current) || 0, 0, total),
            });
        } catch {
            // Browsers can reject position updates while the state changes.
        }
    }

    function seekBy(delta) {
        if (!Number.isFinite(audio.duration) || audio.duration <= 0) {
            return;
        }
        audio.currentTime = clamp(
            audio.currentTime + delta,
            0,
            audio.duration
        );
        updateTimeline();
    }

    function setupMediaSession() {
        if (!("mediaSession" in navigator)) {
            return;
        }

        const handlers = {
            play: () => {
                if (PlayerState.currentIndex === -1 || !audio.src) {
                    togglePlay();
                } else {
                    audio.play()?.catch?.(handlePlaybackFailure);
                }
            },
            pause: () => audio.pause(),
            stop: () => {
                audio.pause();
                try {
                    audio.currentTime = 0;
                } catch {
                    // Ignore seek errors.
                }
                PlayerState.isPlaying = false;
                renderPlayState();
                updateTimeline();
                setMediaPlaybackState("none");
            },
            previoustrack: () => prevTrack(),
            nexttrack: () => nextTrack(),
            seekbackward: (details) =>
                seekBy(-(Number(details.seekOffset) || 10)),
            seekforward: (details) =>
                seekBy(Number(details.seekOffset) || 10),
            seekto: (details) => {
                const time = Number(details.seekTime);
                if (!Number.isFinite(time)) {
                    return;
                }
                if (details.fastSeek && typeof audio.fastSeek === "function") {
                    audio.fastSeek(time);
                } else {
                    audio.currentTime = time;
                }
                updateTimeline();
            },
        };

        Object.entries(handlers).forEach(([action, handler]) => {
            try {
                navigator.mediaSession.setActionHandler(action, handler);
            } catch {
                // Some actions are not supported by every browser/client.
            }
        });
    }

    /* ---------- Audio element events ---------- */

    audio.addEventListener("play", () => {
        PlayerState.isPlaying = true;
        renderPlayState();
        setMediaPlaybackState("playing");
        updatePositionState(undefined, audio.currentTime, true);
    });

    audio.addEventListener("pause", () => {
        PlayerState.isPlaying = false;
        renderPlayState();
        if (!audio.ended) {
            setMediaPlaybackState("paused");
        }
    });

    audio.addEventListener("timeupdate", () => {
        if (!isSeeking) {
            updateTimeline();
        }
    });

    audio.addEventListener("loadedmetadata", handleMetadata);
    audio.addEventListener("durationchange", handleMetadata);
    audio.addEventListener("ended", handleEnded);
    audio.addEventListener("error", handleAudioError);

    audio.addEventListener("volumechange", () => {
        const value = clamp(audio.volume, 0, 1);
        if (Math.abs(value - PlayerState.volume) > 0.001) {
            PlayerState.volume = value;
            if (value > 0) {
                lastAudibleVolume = value;
            }
            syncVolumeUI();
        }
    });

    /* ---------- Player open / close ---------- */

    function openPlayer() {
        if (!fullPlayer) {
            return;
        }
        fullPlayer.classList.add("is-open");
        fullPlayer.setAttribute("aria-hidden", "false");
        fullPlayer.inert = false;
        haptic();
    }

    function closePlayer() {
        if (!fullPlayer) {
            return;
        }
        fullPlayer.classList.remove("is-open");
        fullPlayer.setAttribute("aria-hidden", "true");
        fullPlayer.inert = true;
    }

    expandPlayer?.addEventListener("click", openPlayer);
    collapsePlayer?.addEventListener("click", closePlayer);

    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
            closePlayer();
        }
    });

    /* ---------- Buttons & interactions ---------- */

    miniPlay?.addEventListener("click", () => {
        haptic();
        togglePlay();
    });

    playButton?.addEventListener("click", () => {
        haptic();
        togglePlay();
    });

    miniNext?.addEventListener("click", () => {
        haptic();
        nextTrack();
    });

    nextButton?.addEventListener("click", () => {
        haptic();
        nextTrack();
    });

    prevButton?.addEventListener("click", () => {
        haptic();
        prevTrack();
    });

    shuffleButton?.addEventListener("click", () => {
        PlayerState.isShuffle = !PlayerState.isShuffle;
        shuffleButton.setAttribute(
            "aria-pressed",
            String(PlayerState.isShuffle)
        );
        haptic();
    });

    repeatButton?.addEventListener("click", () => {
        const position = repeatStates.indexOf(PlayerState.repeatMode);
        PlayerState.repeatMode =
            repeatStates[(position + 1) % repeatStates.length];
        repeatButton.dataset.state = PlayerState.repeatMode;
        repeatButton.setAttribute(
            "aria-pressed",
            String(PlayerState.repeatMode !== "none")
        );
        repeatButton.setAttribute(
            "aria-label",
            repeatLabels[PlayerState.repeatMode]
        );
        haptic();
    });

    /* ---------- Seek ---------- */

    function beginSeek() {
        isSeeking = true;
    }

    function applySeek() {
        if (!seekBar) {
            return;
        }
        const value = Number(seekBar.value);
        if (!Number.isFinite(value)) {
            return;
        }

        if (Number.isFinite(audio.duration) && audio.duration > 0) {
            audio.currentTime = clamp(value, 0, audio.duration);
        }

        if (currentTime) {
            currentTime.textContent = formatTime(value);
        }
        if (miniProgress) {
            const duration = getEffectiveDuration();
            miniProgress.style.width = `${
                duration > 0 ? Math.min(100, (value / duration) * 100) : 0
            }%`;
        }
        paintRange(seekBar);
        updatePositionState(undefined, value, true);
    }

    function endSeek() {
        isSeeking = false;
        updateTimeline();
    }

    seekBar?.addEventListener("pointerdown", beginSeek);
    seekBar?.addEventListener("input", () => {
        beginSeek();
        applySeek();
    });
    seekBar?.addEventListener("change", () => {
        applySeek();
        endSeek();
        haptic();
    });
    seekBar?.addEventListener("pointerup", endSeek);
    seekBar?.addEventListener("blur", endSeek);

    /* ---------- Volume controls ---------- */

    volumeBar?.addEventListener("input", () => {
        setVolume(Number(volumeBar.value) / 100);
    });

    volumeBar?.addEventListener("change", () => {
        setVolume(Number(volumeBar.value) / 100);
        haptic();
    });

    muteButton?.addEventListener("click", () => {
        if (PlayerState.volume > 0) {
            lastAudibleVolume = PlayerState.volume;
            setVolume(0);
        } else {
            setVolume(lastAudibleVolume || 1);
        }
        haptic();
    });

    /* ---------- Favorites: like buttons ---------- */

    document.addEventListener("click", (event) => {
        const button = event.target.closest(
            ".track-card__like, #full-like"
        );
        if (!button) {
            return;
        }

        const card = button.closest(".track-card");
        const track = card
            ? getTrackFromCard(card)
            : PlayerState.queue[PlayerState.currentIndex];
        if (!track?.track_id) {
            return;
        }

        event.preventDefault();
        event.stopPropagation();
        toggleFavorite(track, button);
    });

    /* ---------- Track list interactions ---------- */

    function setupTrackListInteractions(list) {
        list?.addEventListener("click", (event) => {
            if (event.target.closest(".track-card__like")) {
                return;
            }
            const card = event.target.closest(".track-card");
            if (card) {
                selectCard(card);
            }
        });

        list?.addEventListener("keydown", (event) => {
            if (event.key !== "Enter" && event.key !== " ") {
                return;
            }
            if (event.target.closest(".track-card__like")) {
                return;
            }
            const card = event.target.closest(".track-card");
            if (!card) {
                return;
            }
            event.preventDefault();
            selectCard(card);
        });
    }

    setupTrackListInteractions(trackList);
    setupTrackListInteractions(libraryList);

    /* ---------- Init ---------- */

    if (fullPlayer) {
        fullPlayer.inert = true;
    }

    initTelegram();
    initIcons();
    setupMarquee();
    setupMediaSession();
    checkApi();
    syncVolumeUI();
    loadFavorites();

    // Paint real YouTube fallback tracks immediately, then replace them with
    // live yt-dlp results as soon as /api/search answers.
    popularTracks = FALLBACK_TRACKS.slice();
    renderTracks(popularTracks);
    syncQueueFromCards();
    setSearchHeading("Популярное");
    setSearchCount(pluralizeTracks(popularTracks.length));
    hideSearchEmpty();

    updateTimeline();
    performSearch(POPULAR_QUERY, { popular: true });

    // Expose a small API so the search/favorites phases can replace the queue.
    window.TGMusicApp = {
        state: PlayerState,
        setQueue,
        playTrack,
        nextTrack,
        prevTrack,
        syncQueueFromCards,
        renderTracks,
        search: performSearch,
        showPopular: restorePopular,
        loadFavorites,
        renderLibrary,
        toggleFavorite,
        isFavorite,
    };
})();
