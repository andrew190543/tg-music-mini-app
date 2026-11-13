(() => {
    "use strict";

    const tg = window.Telegram?.WebApp;
    const FALLBACK_USER_ID = 123456;
    const userId =
        Number(window.Telegram?.WebApp?.initDataUnsafe?.user?.id) ||
        FALLBACK_USER_ID;
    const VOLUME_STORAGE_KEY = "tg-music-volume";
    const PLAYER_STATE_STORAGE_KEY = "tg-music-player-state";
    const PLAYER_STATE_PERSIST_MS = 5000;
    const PLAYER_STATE_MAX_QUEUE = 200;
    const SEARCH_LIMIT = 15;
    const SEARCH_DEBOUNCE_MS = 450;
    const TRENDING_LIMIT = 20;

    /**
     * Build request headers for authenticated API calls.
     *
     * When the Mini App runs inside Telegram, the raw initData is included so
     * the backend can verify the sender's identity (HMAC-signed). Outside
     * Telegram (local dev) there is no initData and the backend falls back to
     * the explicit user_id in the query/body.
     */
    function apiHeaders(extra = {}) {
        const headers = { Accept: "application/json", ...extra };
        try {
            const initData = window.Telegram?.WebApp?.initData;
            if (initData) {
                headers.Authorization = `tma ${initData}`;
            }
        } catch {
            // WebApp API may be unavailable outside Telegram.
        }
        return headers;
    }


    /* ---------- DOM ---------- */

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
    const libraryViewFavorites = document.getElementById("library-view-favorites");
    const libraryViewPlaylists = document.getElementById("library-view-playlists");
    const libraryViewTracks = document.getElementById("library-view-playlist-tracks");
    const playlistsCount = document.getElementById("playlists-count");
    const createPlaylistButton = document.getElementById("create-playlist");
    const playlistsList = document.getElementById("playlists-list");
    const playlistsEmpty = document.getElementById("playlists-empty");
    const playlistBack = document.getElementById("playlist-back");
    const playlistPlay = document.getElementById("playlist-play");
    const playlistTracksTitle = document.getElementById("playlist-tracks-title");
    const playlistTracksList = document.getElementById("playlist-tracks-list");
    const playlistTracksEmpty = document.getElementById("playlist-tracks-empty");

    const waveList = document.getElementById("wave-list");
    const waveCount = document.getElementById("wave-count");
    const waveEmpty = document.getElementById("wave-empty");
    const waveEmptyTitle = document.getElementById("wave-empty-title");
    const waveEmptyText = document.getElementById("wave-empty-text");
    const waveMoodChips = document.getElementById("wave-mood-chips");
    const waveModeMood = document.getElementById("wave-mode-mood");
    const waveModePersonal = document.getElementById("wave-mode-personal");
    const waveStartMood = document.getElementById("wave-start-mood");
    const waveStartPersonal = document.getElementById("wave-start-personal");
    const waveModeButtons = document.querySelectorAll("[data-wave-mode]");

    const playlistMenu = document.getElementById("playlist-menu");
    const playlistMenuList = document.getElementById("playlist-menu-list");
    const playlistMenuClose = document.getElementById("playlist-menu-close");
    const playlistMenuNew = document.getElementById("playlist-menu-new");
    const fullAddPlaylist = document.getElementById("full-add-playlist");

    const searchHistoryBlock = document.getElementById("search-history");
    const searchHistoryChips = document.getElementById("search-history-chips");
    const searchHistoryClear = document.getElementById("search-history-clear");

    const fullPlayer = document.getElementById("full-player");
    const expandPlayer = document.getElementById("expand-player");
    const collapsePlayer = document.getElementById("collapse-player");
    const playerDock = document.querySelector(".player-dock");
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
    const playerEyebrow = document.getElementById("player-eyebrow");
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
    // Timestamp of the last user-driven volume change: the async
    // CloudStorage restore must not overwrite a newer local decision.
    let lastUserVolumeChange = 0;
    // Timestamp of the last user-driven playback action (play/pause/next/
    // shuffle…): the async CloudStorage player-state restore uses it the
    // same way, so a late callback cannot clobber a live session.
    let lastUserPlaybackAction = 0;
    // Saved position to feed into the track as soon as its metadata arrives.
    let pendingSeekSeconds = 0;
    // Track (restored from a previous session) the queue should resume into.
    let pendingResume = null;
    // savedAt of the last applied persisted snapshot — used to decide
    // whether a late CloudStorage copy is fresher than the local one.
    let restoredStateSavedAt = 0;
    // Throttle for timeupdate-driven persistence.
    let lastPlayerPersistAt = 0;

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

        // Telegram CloudStorage survives tunnel/domain changes, so it mirrors
        // localStorage as the more durable backup for the saved volume.
        try {
            tg?.CloudStorage?.setItem?.(VOLUME_STORAGE_KEY, String(value));
        } catch {
            // CloudStorage may be unavailable outside Telegram.
        }
    }

    function restoreCloudVolume() {
        try {
            if (typeof tg?.CloudStorage?.getItem !== "function") {
                return;
            }
            const startedAt = Date.now();
            tg.CloudStorage.getItem(VOLUME_STORAGE_KEY, (err, val) => {
                if (err || val === null || val === undefined) {
                    return;
                }
                const value = Number(val);
                if (
                    !Number.isFinite(value) ||
                    value < 0 ||
                    value > 1 ||
                    // The user already picked a volume while the async
                    // Telegram CloudStorage lookup was in flight: the local
                    // decision wins, otherwise this callback fires late and
                    // silently reverts the slider.
                    lastUserVolumeChange > startedAt
                ) {
                    return;
                }
                setVolume(value, { persist: true, source: "cloud" });
            });
        } catch {
            // CloudStorage may be unavailable outside Telegram.
        }
    }

    /* ---------- Player state persistence ---------- */

    function markUserPlaybackAction() {
        lastUserPlaybackAction = Date.now();
    }

    function capturePlayerState(position) {
        const current = PlayerState.queue[PlayerState.currentIndex] || null;
        const index = clamp(
            PlayerState.currentIndex,
            -1,
            PlayerState.queue.length - 1
        );

        return {
            version: 1,
            queue: PlayerState.queue.slice(0, PLAYER_STATE_MAX_QUEUE),
            currentIndex: index,
            trackId: current?.track_id || "",
            isShuffle: PlayerState.isShuffle,
            repeatMode: PlayerState.repeatMode,
            position: Math.max(0, Number(position) || 0),
            savedAt: Date.now(),
        };
    }

    function savePlayerState({ force = false, position = null } = {}) {
        if (!PlayerState.queue.length) {
            return;
        }

        const now = Date.now();
        if (!force && now - lastPlayerPersistAt < PLAYER_STATE_PERSIST_MS) {
            return;
        }
        lastPlayerPersistAt = now;

        const value = position === null ? audio.currentTime : position;
        const payload = JSON.stringify(capturePlayerState(value));

        try {
            window.localStorage.setItem(PLAYER_STATE_STORAGE_KEY, payload);
        } catch {
            // localStorage can be disabled in private/embedded contexts.
        }

        // CloudStorage survives tunnel/domain changes — the durable mirror.
        try {
            tg?.CloudStorage?.setItem?.(
                PLAYER_STATE_STORAGE_KEY,
                payload
            );
        } catch {
            // CloudStorage may be unavailable outside Telegram.
        }
    }

    function readPlayerStateFromStorage(storage) {
        try {
            const raw = storage?.getItem?.(PLAYER_STATE_STORAGE_KEY);
            if (!raw) {
                return null;
            }
            const data = JSON.parse(raw);
            if (!data || !Array.isArray(data.queue)) {
                return null;
            }
            return data;
        } catch {
            return null;
        }
    }

    function applyRestoredState(data, savedAt) {
        const queue = (Array.isArray(data.queue) ? data.queue : [])
            .map(normalizeTrack)
            .filter((track) => track.track_id);
        if (!queue.length) {
            return;
        }

        const rawIndex = Number(data.currentIndex);
        let index =
            Number.isInteger(rawIndex) && rawIndex >= 0 && rawIndex < queue.length
                ? rawIndex
                : queue.findIndex(
                      (track) => track.track_id === data.trackId
                  );

        PlayerState.queue = queue;
        PlayerState.currentIndex = index;
        PlayerState.isShuffle = Boolean(data.isShuffle);
        PlayerState.repeatMode = repeatStates.includes(data.repeatMode)
            ? data.repeatMode
            : "none";

        shuffleButton?.setAttribute(
            "aria-pressed",
            String(PlayerState.isShuffle)
        );
        if (repeatButton) {
            repeatButton.dataset.state = PlayerState.repeatMode;
            repeatButton.setAttribute(
                "aria-pressed",
                String(PlayerState.repeatMode !== "none")
            );
            repeatButton.setAttribute(
                "aria-label",
                repeatLabels[PlayerState.repeatMode]
            );
        }

        const current = PlayerState.queue[PlayerState.currentIndex];
        if (current) {
            // Quiet restore: queue and position are applied on pause; the
            // position resumes only when the user starts this exact track.
            const position = Math.max(0, Number(data.position) || 0);
            pendingResume = { trackId: current.track_id, position };
            showPlayerDock();
            updateTrackUI(current);
            pendingSeekSeconds = 0;
            if (currentTime && position > 0) {
                currentTime.textContent = formatTime(position);
            }
        }

        restoredStateSavedAt = Math.max(0, Number(savedAt) || 0);
        markCurrentCard();
        renderPlayState();
    }

    function restoreSavedPlayerState() {
        const local = readPlayerStateFromStorage(window.localStorage);
        if (local) {
            applyRestoredState(local, Number(local.savedAt) || 0);
        }

        // Telegram CloudStorage may hold a fresher snapshot saved under a
        // previous tunnel/domain. The callback fires asynchronously, so it
        // yields to any user action started in the meantime.
        try {
            if (typeof tg?.CloudStorage?.getItem !== "function") {
                return;
            }
            const startedAt = Date.now();
            tg.CloudStorage.getItem(PLAYER_STATE_STORAGE_KEY, (err, val) => {
                if (err || !val) {
                    return;
                }
                const data = readPlayerStateFromStorage({
                    getItem: () => val,
                });
                const cloudSavedAt = Number(data?.savedAt) || 0;
                if (
                    !data ||
                    cloudSavedAt <= restoredStateSavedAt ||
                    // The user already started interacting: the live session
                    // wins, otherwise this late callback would silently
                    // replace the queue (same race as the volume slider).
                    lastUserPlaybackAction > startedAt
                ) {
                    return;
                }
                applyRestoredState(data, cloudSavedAt);
            });
        } catch {
            // CloudStorage may be unavailable outside Telegram.
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

    let activeTab = "search";

    function activateTab(name) {
        // Cancels a pending delayed hide and closes the live block: switching
        // tabs must never leave a stale timer collapsing the panel later.
        if (searchHistoryHideTimer !== null) {
            window.clearTimeout(searchHistoryHideTimer);
            searchHistoryHideTimer = null;
        }
        if (searchHistoryShown) {
            hideSearchHistory();
        }
        // Drop focus so the keyboard/focus ring does not linger after a tab
        // switch (and so no delayed blur does it for us at a bad moment).
        searchInput?.blur();

        activeTab = name;
        setSearchPlaceholder(name);
        // The field text is shared between the tabs and must survive any
        // switch: a stale-input/invisible-clear mismatch would strand the
        // user in old search results.
        syncClearButton();

        if (name === "library") {
            // The current field text immediately acts as the local filter
            // for the library (favorites / playlists / open playlist).
            libraryFilter = searchInput?.value ? searchInput.value : "";
        } else if (name === "search") {
            const query = searchInput?.value.trim() || "";
            if (!query) {
                // An empty field always shows the trending catalog — never
                // leave stale search results on screen.
                restorePopular();
            } else if (query !== lastOnlineSearchQuery) {
                // The text was typed outside the search tab (or an exact
                // search for it never ran): perform it now.
                performSearch(query);
            }
            // Otherwise the results for this exact query are already on
            // screen: keep them, the field text and the clear button.
        }

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
            // Re-render through the filter so the cached lists respect the
            // current field text, then refresh from SQLite in the background.
            applyLibraryFilter();
            loadFavorites({ force: true });
            if (playlistsLoaded) {
                // Counts may have changed since the last visit.
                void loadPlaylists({ quiet: true });
            }
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
    // AbortController of the catalog request in flight (/api/search or
    // /api/trending). A new request cancels the previous one so fast typing
    // does not leave several yt-dlp lookups running on the backend.
    let activeListAbort = null;

    /* --- Contextual search state -----------------------------------
       The field works differently per active tab: in "Поиск" it queries
       the internet (/api/search), in "Моя музыка" it only filters the
       local collections client-side. The text itself stays in the field
       across tab switches and serves both modes. */
    let libraryFilter = "";

    // The exact query whose results are currently rendered in the search
    // tab ("" = the trending catalog). Set by performSearch/restorePopular
    // so switching tabs never re-requests an already shown answer.
    let lastOnlineSearchQuery = "";

    function setSearchPlaceholder(name) {
        if (!searchInput) {
            return;
        }
        searchInput.placeholder =
            name === "library"
                ? "Поиск в моей музыке"
                : "Трек, исполнитель или ссылка";
        searchInput.setAttribute(
            "aria-label",
            name === "library" ? "Поиск в моей музыке" : "Поиск треков"
        );
    }

    function stopActiveListRequest() {
        if (activeListAbort) {
            activeListAbort.abort();
            activeListAbort = null;
        }
    }

    function forgetActiveListRequest(controller) {
        if (activeListAbort === controller) {
            activeListAbort = null;
        }
    }

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

    function showCatalogSpinner(text = "Загрузка треков…") {
        if (!trackList) {
            return;
        }

        hideSearchEmpty();
        trackList.hidden = false;

        const spinner = document.createElement("div");
        spinner.className = "catalog-spinner";

        const ring = document.createElement("span");
        ring.className = "catalog-spinner__ring";
        ring.setAttribute("aria-hidden", "true");

        const label = document.createElement("p");
        label.className = "catalog-spinner__text";
        label.textContent = text;

        spinner.append(ring, label);
        trackList.replaceChildren(spinner);
        trackList.setAttribute("aria-busy", "true");
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

    async function performSearch(rawQuery) {
        const query = String(rawQuery ?? "").trim();
        if (!query) {
            return;
        }

        // Remember the exact query being rendered so tab switches can tell
        // "results for this query are already on screen" from "not searched".
        lastOnlineSearchQuery = query;

        const requestId = ++searchRequestId;
        if (searchTimer !== null) {
            window.clearTimeout(searchTimer);
            searchTimer = null;
        }
        stopActiveListRequest();
        const controller = new AbortController();
        activeListAbort = controller;

        setSearchHeading("Результаты поиска");
        setSearchCount("Поиск…");
        trackList?.setAttribute("aria-busy", "true");
        hideSearchEmpty();

        try {
            const response = await fetch(
                `/api/search?q=${encodeURIComponent(query)}&limit=${SEARCH_LIMIT}` +
                    `&user_id=${encodeURIComponent(String(PlayerState.userId))}`,
                {
                    cache: "no-store",
                    headers: apiHeaders(),
                    signal: controller.signal,
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

            renderTracks(tracks);
            // A restored queue must survive the first trending render too:
            // `pendingResume` marks the restored current track even though
            // nothing is loaded into the audio element yet.
            syncQueueFromCards({
                preserveCurrent: Boolean(audio.src || pendingResume),
            });
            setSearchCount(pluralizeTracks(tracks.length));

            if (tracks.length) {
                hideSearchEmpty();
                // The backend stores the query too; this keeps the chip row
                // in sync without an extra request.
                rememberSearchQuery(query);
            } else {
                showSearchEmpty(
                    "Ничего не найдено",
                    `По запросу «${query}» ничего не найдено. ` +
                        "Попробуйте изменить запрос."
                );
            }
        } catch (error) {
            if (requestId !== searchRequestId || error?.name === "AbortError") {
                // Aborted means a newer query superseded this one.
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
            forgetActiveListRequest(controller);
            if (requestId === searchRequestId) {
                trackList?.setAttribute("aria-busy", "false");
            }
        }
    }

    async function loadTrending() {
        const requestId = ++searchRequestId;
        if (searchTimer !== null) {
            window.clearTimeout(searchTimer);
            searchTimer = null;
        }
        stopActiveListRequest();
        const controller = new AbortController();
        activeListAbort = controller;

        setSearchHeading("Популярное");
        setSearchCount("Загрузка…");
        showCatalogSpinner();

        try {
            const response = await fetch(
                `/api/trending?limit=${TRENDING_LIMIT}`,
                {
                    cache: "no-store",
                    headers: { Accept: "application/json" },
                    signal: controller.signal,
                }
            );

            if (!response.ok) {
                throw new Error(`Trending HTTP ${response.status}`);
            }

            const payload = await response.json();
            if (requestId !== searchRequestId) {
                return;
            }

            const tracks = (Array.isArray(payload) ? payload : [])
                .map(normalizeTrack)
                .filter((track) => track.track_id);

            popularTracks = tracks;
            popularLoaded = true;
            renderTracks(tracks);
            syncQueueFromCards({
                preserveCurrent: Boolean(audio.src || pendingResume),
            });
            setSearchCount(pluralizeTracks(tracks.length));

            if (tracks.length) {
                hideSearchEmpty();
            } else {
                showSearchEmpty(
                    "Популярное недоступно",
                    "Введите название трека или исполнителя в поиске."
                );
            }
        } catch (error) {
            if (requestId !== searchRequestId || error?.name === "AbortError") {
                // Aborted means a newer tab/search request replaced this one.
                return;
            }
            console.error("Trending failed:", error);
            setSearchCount("Ошибка загрузки");
            popularLoaded = false;

            if (!trackList?.children.length) {
                showSearchEmpty(
                    "Не удалось загрузить треки",
                    "Проверьте подключение к интернету и попробуйте ещё раз."
                );
            }
        } finally {
            forgetActiveListRequest(controller);
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
        // Clearing the field is also "a newer request": kill the in-flight
        // search and invalidate its answer so it cannot overwrite popular.
        stopActiveListRequest();
        searchRequestId += 1;
        lastOnlineSearchQuery = "";

        setSearchHeading("Популярное");

        if (!popularLoaded) {
            loadTrending();
            return;
        }

        renderTracks(popularTracks);
        syncQueueFromCards({
            preserveCurrent: Boolean(audio.src || pendingResume),
        });
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
        // The shared field text always mirrors the local library filter:
        // switching to "library" then shows the same words as a filter.
        libraryFilter = searchInput.value;
        if (searchInput.value.trim()) {
            hideSearchHistory();
        } else {
            // Empty field means "no online search is outstanding" — a later
            // tab switch back to "search" must show the trending catalog.
            lastOnlineSearchQuery = "";
        }
        if (activeTab === "library") {
            // Local filtering only: no /api/search traffic in library tab.
            applyLibraryFilter();
            return;
        }
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
        if (activeTab === "library") {
            searchInput.blur();
            return;
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
        // Reset both modes at once: the library shows every track again and
        // the search tab is guaranteed to fall back to "Популярное".
        libraryFilter = "";
        lastOnlineSearchQuery = "";
        stopActiveListRequest();
        searchRequestId += 1;
        restorePopular();
        if (activeTab === "library") {
            applyLibraryFilter();
        }
        searchInput.focus();
        haptic();
    });

    syncClearButton();

    /* ---------- Search history chips ---------- */

    let searchHistoryItems = [];
    let searchHistoryLoaded = false;
    let searchHistoryShown = false;
    let searchHistoryHideTimer = null;

    async function loadSearchHistory({ clearLoaded = false } = {}) {
        if (searchHistoryLoaded && !clearLoaded) {
            return searchHistoryItems;
        }

        try {
            const response = await fetch(
                authenticatedUrl("/api/search-history?limit=10"),
                {
                    cache: "no-store",
                    headers: apiHeaders(),
                }
            );
            if (!response.ok) {
                throw new Error(`Search history HTTP ${response.status}`);
            }

            const payload = await response.json();
            searchHistoryItems = (Array.isArray(payload) ? payload : [])
                .map((item) => String(item?.query ?? "").trim())
                .filter(Boolean)
                .slice(0, 10);
            searchHistoryLoaded = true;
        } catch (error) {
            console.error("Search history load failed:", error);
        }
        return searchHistoryItems;
    }

    function rememberSearchQuery(query) {
        const cleaned = String(query ?? "").trim();
        if (!cleaned) {
            return;
        }
        // Successive identical queries are also deduplicated on the backend.
        if (searchHistoryItems[0] === cleaned) {
            return;
        }
        searchHistoryItems = [
            cleaned,
            ...searchHistoryItems.filter((item) => item !== cleaned),
        ].slice(0, 10);
        searchHistoryLoaded = true;
    }

    function renderSearchHistoryChips() {
        if (!searchHistoryChips) {
            return;
        }

        const fragment = document.createDocumentFragment();
        searchHistoryItems.forEach((query) => {
            const chip = document.createElement("button");
            chip.className = "search-history__chip";
            chip.type = "button";
            chip.dataset.query = query;

            const glyph = document.createElement("i");
            glyph.dataset.lucide = "history";

            const label = document.createElement("span");
            label.textContent = query;

            chip.append(glyph, label);
            fragment.append(chip);
        });

        searchHistoryChips.replaceChildren(fragment);
        initIcons();
    }

    function updateSearchHistoryVisibility() {
        if (!searchHistoryBlock) {
            return;
        }
        const canShow =
            searchHistoryShown &&
            searchHistoryItems.length > 0 &&
            searchInput instanceof HTMLInputElement &&
            searchInput.value.trim() === "";
        searchHistoryBlock.hidden = !canShow;
    }

    function showSearchHistory() {
        searchHistoryShown = true;
        if (searchHistoryHideTimer !== null) {
            window.clearTimeout(searchHistoryHideTimer);
            searchHistoryHideTimer = null;
        }
        updateSearchHistoryVisibility();
    }

    function hideSearchHistory() {
        searchHistoryShown = false;
        if (searchHistoryHideTimer !== null) {
            window.clearTimeout(searchHistoryHideTimer);
            searchHistoryHideTimer = null;
        }
        if (searchHistoryBlock) {
            searchHistoryBlock.hidden = true;
        }
    }

    // Delayed close: a `pointerdown` elsewhere (tabs, track cards) must not
    // collapse the history block synchronously — the layout would jump before
    // the `click` lands and the tap would miss its target. Scheduling the
    // hide lets the pending click fire at the old coordinates first.
    function scheduleHideSearchHistory(delay = 180) {
        if (searchHistoryHideTimer !== null) {
            window.clearTimeout(searchHistoryHideTimer);
        }
        searchHistoryHideTimer = window.setTimeout(() => {
            searchHistoryHideTimer = null;
            hideSearchHistory();
        }, delay);
    }

    searchInput?.addEventListener("focus", async () => {
        if (activeTab === "library" || searchInput.value.trim()) {
            return;
        }
        await loadSearchHistory();
        renderSearchHistoryChips();
        showSearchHistory();
    });

    searchInput?.addEventListener("blur", () => {
        // A small delay lets a click on a chip land first.
        scheduleHideSearchHistory(180);
    });

    searchHistoryBlock?.addEventListener("pointerdown", () => {
        if (searchHistoryHideTimer !== null) {
            window.clearTimeout(searchHistoryHideTimer);
            searchHistoryHideTimer = null;
        }
    });

    document.addEventListener("pointerdown", (event) => {
        if (!searchHistoryShown) {
            return;
        }
        if (event.target.closest("#search-history, .search-field")) {
            return;
        }
        // Also scheduled (not synchronous): tapping a tab or a track card
        // first fires `click` there, then the block collapses.
        scheduleHideSearchHistory(180);
    });

    searchHistoryChips?.addEventListener("click", (event) => {
        const chip = event.target.closest(".search-history__chip");
        if (!chip || !searchInput || activeTab === "library") {
            return;
        }

        const query = chip.dataset.query || "";
        if (!query) {
            return;
        }

        searchInput.value = query;
        syncClearButton();
        hideSearchHistory();
        performSearch(query);
        haptic();
    });

    searchHistoryClear?.addEventListener("click", async () => {
        try {
            const response = await fetch(
                authenticatedUrl("/api/search-history"),
                {
                    method: "DELETE",
                    cache: "no-store",
                    headers: apiHeaders(),
                }
            );
            if (!response.ok) {
                throw new Error(
                    `Search history clear HTTP ${response.status}`
                );
            }
            searchHistoryItems = [];
            searchHistoryLoaded = true;
            hideSearchHistory();
            haptic();
        } catch (error) {
            console.error("Clearing search history failed:", error);
        }
    });

    /* ---------- My Wave (Моя волна) ---------- */

    const WAVE_LIMIT = 18;
    // Hard ceiling for the infinite wave queue: the append loop trims at
    // this many tracks so a long listening session cannot grow memory
    // without bound.
    const WAVE_MAX_QUEUE_TRACKS = 120;
    const WAVE_MOODS = [
        { vibe: "calm", title: "Спокойный", meta: "Lo-Fi · Ambient" },
        { vibe: "energetic", title: "Бодрый", meta: "Pop · Dance" },
        { vibe: "aggressive", title: "Агрессивный", meta: "Phonk · Hard" },
        { vibe: "sad", title: "Грустный", meta: "Melancholy" },
    ];

    let waveMode = "mood";
    let waveVibe = WAVE_MOODS[0]?.vibe || "calm";
    let waveRequestId = 0;
    // AbortController of the wave request in flight (/api/wave/*): kept
    // separate from the search/trending controller so neither stream of
    // requests can kill the other one.
    let waveAbort = null;
    // Which list the queue was built from: "catalog" (search / trending /
    // library / playlists, loops back to 0 after the last track) or "wave"
    // ("Моя волна", which never loops — it appends fresh tracks instead).
    let queueSource = "catalog";
    // Which wave endpoint produced / should extend the current queue
    // ("mood" -> /api/wave/mood, "personal" -> /api/wave/personalized).
    let waveSource = "mood";
    // Guards the background "append next wave batch" request so rapid
    // track changes near the queue end cannot fire duplicate fetches; the
    // in-flight promise is shared until it settles.
    let waveAppending = false;
    let wavePendingAppend = null;

    function setWaveMode(mode) {
        waveMode = mode === "personal" ? "personal" : "mood";

        waveModeButtons.forEach((button) => {
            const active = button.dataset.waveMode === waveMode;
            button.classList.toggle("is-active", active);
            button.setAttribute("aria-selected", String(active));
        });

        if (waveModeMood) {
            waveModeMood.hidden = waveMode !== "mood";
        }
        if (waveModePersonal) {
            waveModePersonal.hidden = waveMode !== "personal";
        }
    }

    function renderWaveChips() {
        if (!waveMoodChips) {
            return;
        }

        const fragment = document.createDocumentFragment();
        WAVE_MOODS.forEach((mood) => {
            const chip = document.createElement("button");
            chip.className = "wave-chip";
            chip.type = "button";
            chip.dataset.vibe = mood.vibe;
            chip.classList.toggle("is-selected", mood.vibe === waveVibe);

            const title = document.createElement("span");
            title.className = "wave-chip__title";
            title.textContent = mood.title;

            const meta = document.createElement("span");
            meta.className = "wave-chip__meta";
            meta.textContent = mood.meta;

            chip.append(title, meta);
            fragment.append(chip);
        });

        waveMoodChips.replaceChildren(fragment);
        initIcons();
    }

    function showWaveSpinner(text = "Собираем волну…") {
        if (!waveList) {
            return;
        }

        if (waveEmpty) {
            waveEmpty.hidden = true;
        }
        waveList.hidden = false;

        const spinner = document.createElement("div");
        spinner.className = "catalog-spinner";

        const ring = document.createElement("span");
        ring.className = "catalog-spinner__ring";
        ring.setAttribute("aria-hidden", "true");

        const label = document.createElement("p");
        label.className = "catalog-spinner__text";
        label.textContent = text;

        spinner.append(ring, label);
        waveList.replaceChildren(spinner);
        waveList.setAttribute("aria-busy", "true");
    }

    function hideWaveEmpty() {
        if (waveEmpty) {
            waveEmpty.hidden = true;
        }
    }

    function showWaveEmpty(title, text) {
        if (waveEmptyTitle) {
            waveEmptyTitle.textContent = title;
        }
        if (waveEmptyText) {
            waveEmptyText.textContent = text;
        }
        if (waveEmpty) {
            waveEmpty.hidden = false;
        }
    }

    function setWaveCount(text) {
        if (waveCount) {
            waveCount.textContent = text;
        }
    }

    function waveLaunchButtons() {
        return [waveStartMood, waveStartPersonal].filter(Boolean);
    }

    function setWaveButtonsBusy(busy) {
        waveLaunchButtons().forEach((button) => {
            if (busy) {
                if (!button.dataset.idleText) {
                    button.dataset.idleText = button.textContent || "";
                }
                button.disabled = true;
                button.textContent = "Собираем волну…";
            } else {
                button.disabled = false;
                if (button.dataset.idleText) {
                    button.textContent = button.dataset.idleText;
                    delete button.dataset.idleText;
                }
            }
        });
    }

    async function runWave(url, source = waveSource) {
        const requestId = ++waveRequestId;
        if (waveAbort) {
            waveAbort.abort();
        }
        const controller = new AbortController();
        waveAbort = controller;

        setWaveCount("Генерация…");
        showWaveSpinner();
        // Both launch buttons are blocked while a wave is being assembled:
        // a second click would only add another yt-dlp storm on the backend.
        setWaveButtonsBusy(true);

        try {
            const response = await fetch(url, {
                cache: "no-store",
                headers: apiHeaders(),
                signal: controller.signal,
            });

            if (!response.ok) {
                throw new Error(`Wave HTTP ${response.status}`);
            }

            const payload = await response.json();
            if (requestId !== waveRequestId) {
                return;
            }

            const tracks = (Array.isArray(payload) ? payload : [])
                .map(normalizeTrack)
                .filter((track) => track.track_id);

            renderTrackList(waveList, tracks);
            setWaveCount(pluralizeTracks(tracks.length));

            if (tracks.length) {
                hideWaveEmpty();
                // The wave is playable right away: the freshly assembled
                // list becomes the queue and starts from the first track.
                waveSource = source;
                queueSource = "wave";
                markUserPlaybackAction();
                setQueue(tracks, 0);
            } else {
                showWaveEmpty(
                    "Волна пуста",
                    "Ничего не подошло — попробуйте другой вайб или запустите волну ещё раз."
                );
            }
        } catch (error) {
            if (requestId !== waveRequestId || error?.name === "AbortError") {
                // Aborted: a newer wave request superseded this one.
                return;
            }
            console.error("Wave failed:", error);
            setWaveCount("Ошибка загрузки");

            if (!waveList?.children.length) {
                showWaveEmpty(
                    "Не удалось собрать волну",
                    "Проверьте подключение к интернету и попробуйте ещё раз."
                );
            }
        } finally {
            if (waveAbort === controller) {
                waveAbort = null;
            }
            if (requestId === waveRequestId) {
                waveList?.setAttribute("aria-busy", "false");
                // Always restored, even after a failure, so the launch
                // button never stays stuck disabled.
                setWaveButtonsBusy(false);
            }
        }
    }

    function loadWaveMood() {
        waveSource = "mood";
        return runWave(
            `/api/wave/mood?vibe=${encodeURIComponent(waveVibe)}&limit=${WAVE_LIMIT}`,
            "mood"
        );
    }

    function loadWavePersonal() {
        waveSource = "personal";
        return runWave(
            authenticatedUrl(`/api/wave/personalized?limit=${WAVE_LIMIT}`),
            "personal"
        );
    }

    waveMoodChips?.addEventListener("click", (event) => {
        const chip = event.target.closest(".wave-chip");
        if (!chip || activeTab !== "wave") {
            return;
        }

        const vibe = chip.dataset.vibe;
        if (!vibe) {
            return;
        }

        const changed = waveVibe !== vibe;
        waveVibe = vibe;
        if (changed) {
            renderWaveChips();
        }
        haptic();
        // No request here on purpose: picking a vibe is a cheap local
        // switch — only the "Запустить волну" button builds the wave, so
        // flipping through chips does not hammer the backend with yt-dlp
        // searches.
    });

    waveStartMood?.addEventListener("click", () => {
        haptic("medium");
        loadWaveMood();
    });

    waveStartPersonal?.addEventListener("click", () => {
        haptic("medium");
        loadWavePersonal();
    });

    waveModeButtons.forEach((button) => {
        button.addEventListener("click", () => {
            setWaveMode(button.dataset.waveMode);
            haptic();
        });
    });

    /* --- Infinite wave: background append ------------------------- */

    function currentWaveUrl() {
        if (waveSource === "personal") {
            return authenticatedUrl(
                `/api/wave/personalized?limit=${WAVE_LIMIT}`
            );
        }
        return `/api/wave/mood?vibe=${encodeURIComponent(waveVibe)}&limit=${WAVE_LIMIT}`;
    }

    function waveTrackIds() {
        return new Set(PlayerState.queue.map((track) => track.track_id));
    }

    function appendWaveCards(tracks) {
        if (!waveList || !tracks.length) {
            return;
        }
        const startIndex = waveList.querySelectorAll(".track-card").length;
        const fragment = document.createDocumentFragment();
        tracks.forEach((track, offset) => {
            fragment.append(buildTrackCard(track, startIndex + offset));
        });
        waveList.append(fragment);
        initIcons();
        syncLikeUI();
        markCurrentCard();
    }

    /**
     * Fetch the next wave batch in the background, filter out tracks already
     * sitting in the queue, append the fresh ones to the queue and paint
     * their cards at the end of #wave-list. Concurrent callers share one
     * in-flight request; failures are logged and swallowed — the wave then
     * simply loops as a fallback (nothing is appended).
     */
    async function appendMoreWaveTracks() {
        if (queueSource !== "wave") {
            return;
        }
        if (waveAppending) {
            return wavePendingAppend;
        }
        waveAppending = true;
        wavePendingAppend = (async () => {
            try {
                const response = await fetch(currentWaveUrl(), {
                    cache: "no-store",
                    headers: apiHeaders(),
                });
                if (!response.ok) {
                    throw new Error(`Wave append HTTP ${response.status}`);
                }

                const payload = await response.json();
                const known = waveTrackIds();
                const fresh = (Array.isArray(payload) ? payload : [])
                    .map(normalizeTrack)
                    .filter(
                        (track) =>
                            track.track_id && !known.has(track.track_id)
                    );

                const capacity = Math.max(
                    0,
                    WAVE_MAX_QUEUE_TRACKS - PlayerState.queue.length
                );
                const additions = fresh.slice(0, capacity);
                if (!additions.length) {
                    return;
                }

                PlayerState.queue = PlayerState.queue.concat(additions);
                appendWaveCards(additions);
                setWaveCount(pluralizeTracks(PlayerState.queue.length));
                savePlayerState();
            } catch (error) {
                console.error("Wave append failed:", error);
            } finally {
                waveAppending = false;
                wavePendingAppend = null;
            }
        })();
        return wavePendingAppend;
    }

    /* ---------- Now-playing marquee ---------- */

    function setupMarquee() {
        document.querySelectorAll(".marquee").forEach((wrap) => {
            const track = wrap.querySelector(".marquee__track");
            const text = track?.querySelector(".marquee__text");
            if (!track || !text) {
                return;
            }

            // The duplicate copy is only shown when the title actually
            // overflows: short titles stay put and centered.
            track.classList.remove("is-scrolling");
            const overflows =
                text.getBoundingClientRect().width > wrap.clientWidth;
            track.classList.toggle("is-scrolling", overflows);
            wrap.classList.toggle("is-scrolling", overflows);
        });
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
    let favoritesCatchUpPending = false;
    let favoritesAbort = null;
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

        if (fullAddPlaylist) {
            fullAddPlaylist.disabled = !track?.track_id;
        }
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

    /* --- Library local filter ---
       In the "Моя музыка" tab the search field never hits the internet:
       it filters favorites / playlist tracks / playlist cards by a
       case-insensitive substring in title or artist. */
    function libraryFilterQuery() {
        return libraryFilter.trim().toLowerCase();
    }

    function matchesLibraryFilter(track) {
        const query = libraryFilterQuery();
        if (!query) {
            return true;
        }
        const title = String(track?.title || "").toLowerCase();
        const artist = String(track?.artist || "").toLowerCase();
        return title.includes(query) || artist.includes(query);
    }

    function applyLibraryFilter() {
        if (activeTab !== "library") {
            return;
        }

        if (openPlaylistId) {
            // Tracks view: filter the cached playlist track list.
            const tracks = openPlaylistTracks.filter(matchesLibraryFilter);
            renderPlaylistTracksView(
                playlistTracksTitle?.textContent || "Плейлист",
                tracks
            );
            return;
        }

        const activeView =
            document
                .querySelector("[data-library-view].is-active")
                ?.dataset.libraryView || "favorites";

        if (activeView === "playlists") {
            const query = libraryFilterQuery();
            const list = query
                ? playlists.filter((playlist) =>
                      String(playlist.title || "")
                          .toLowerCase()
                          .includes(query)
                  )
                : playlists;
            renderPlaylists(list);
        } else {
            renderLibrary();
        }
    }

    function renderLibrary() {
        if (!libraryList) {
            return;
        }

        const query = libraryFilterQuery();
        const all = [...PlayerState.favorites.values()];
        const tracks = query
            ? all.filter(matchesLibraryFilter)
            : all;
        renderTrackList(libraryList, tracks);

        if (libraryCount) {
            libraryCount.textContent = pluralizeTracks(tracks.length);
        }
        if (libraryEmpty) {
            libraryEmpty.hidden = tracks.length > 0;
        }
    }

    /* ---------- Playlists ---------- */

    let playlists = [];
    let playlistsLoaded = false;
    let playlistsLoading = false;
    let openPlaylistId = null;
    // Full (unfiltered) track list of the currently open playlist, cached so
    // the local search field can filter it without re-fetching.
    let openPlaylistTracks = [];
    let playlistMenuTrack = null;

    function pluralizePlaylists(count) {
        const mod10 = count % 10;
        const mod100 = count % 100;
        if (mod10 === 1 && mod100 !== 11) {
            return `${count} плейлист`;
        }
        if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) {
            return `${count} плейлиста`;
        }
        return `${count} плейлистов`;
    }

    function setPlaylistsCount() {
        if (playlistsCount) {
            playlistsCount.textContent = pluralizePlaylists(playlists.length);
        }
    }

    function authenticatedUrl(path) {
        // The path may already carry its own query string (e.g.
        // "/api/wave/personalized?limit=18"): appending with a literal "?"
        // would produce "…?limit=18?user_id=…" and FastAPI would try to
        // parse "18?user_id=…" as a number -> 422. Join with "&" instead.
        const separator = path.includes("?") ? "&" : "?";
        return `${path}${separator}user_id=${encodeURIComponent(
            String(PlayerState.userId)
        )}`;
    }

    function activateLibraryView(name) {
        document.querySelectorAll("[data-library-view]").forEach((tab) => {
            const active = tab.dataset.libraryView === name;
            tab.classList.toggle("is-active", active);
            tab.setAttribute("aria-selected", String(active));
        });

        if (libraryViewFavorites) {
            libraryViewFavorites.hidden = name !== "favorites";
        }
        if (libraryViewPlaylists) {
            libraryViewPlaylists.hidden = name !== "playlists";
        }
        if (libraryViewTracks) {
            libraryViewTracks.hidden = name !== "tracks";
        }

        if (name === "playlists") {
            loadPlaylists();
        }
    }

    document.querySelectorAll("[data-library-view]").forEach((tab) => {
        tab.addEventListener("click", () => {
            openPlaylistId = null;
            openPlaylistTracks = [];
            activateLibraryView(tab.dataset.libraryView);
            if (libraryFilter) {
                applyLibraryFilter();
            }
            haptic();
        });
    });

    async function loadPlaylists({ quiet = false } = {}) {
        if (playlistsLoading) {
            return;
        }

        playlistsLoading = true;
        if (!quiet && playlistsList) {
            playlistsList.setAttribute("aria-busy", "true");
        }

        try {
            const response = await fetch(authenticatedUrl("/api/playlists"), {
                cache: "no-store",
                headers: apiHeaders(),
            });

            if (!response.ok) {
                throw new Error(`Playlists HTTP ${response.status}`);
            }

            const payload = await response.json();
            playlists = (Array.isArray(payload) ? payload : []).filter(
                (playlist) => Number.isFinite(Number(playlist?.id))
            );
            playlistsLoaded = true;
            renderPlaylists();
        } catch (error) {
            console.error("Playlists load failed:", error);
            if (!quiet) {
                renderPlaylists();
            }
        } finally {
            playlistsLoading = false;
            if (playlistsList) {
                playlistsList.setAttribute("aria-busy", "false");
            }
        }
    }

    function renderPlaylists(list = playlists) {
        if (!playlistsList) {
            return;
        }

        const query = libraryFilterQuery();
        const raw = Array.isArray(list) ? list : [];
        const source = query
            ? raw.filter((playlist) =>
                  String(playlist.title || "").toLowerCase().includes(query)
              )
            : raw;
        const fragment = document.createDocumentFragment();
        source.forEach((playlist) => {
            const id = Number(playlist.id);
            const title = String(playlist.title || "Без названия");

            const card = document.createElement("article");
            card.className = "playlist-card";
            card.tabIndex = 0;
            card.dataset.playlistId = String(id);

            const icon = document.createElement("span");
            icon.className = "playlist-card__icon";
            icon.setAttribute("aria-hidden", "true");
            const iconGlyph = document.createElement("i");
            iconGlyph.dataset.lucide = "list-music";
            icon.append(iconGlyph);

            const body = document.createElement("div");
            body.className = "playlist-card__body";
            const titleElement = document.createElement("h3");
            titleElement.className = "playlist-card__title";
            titleElement.textContent = title;
            const meta = document.createElement("p");
            meta.className = "playlist-card__meta";
            meta.textContent = pluralizeTracks(Number(playlist.track_count) || 0);
            body.append(titleElement, meta);

            const play = document.createElement("button");
            play.className = "playlist-card__play";
            play.type = "button";
            play.setAttribute("aria-label", `Воспроизвести «${title}»`);
            const playGlyph = document.createElement("i");
            playGlyph.dataset.lucide = "play";
            play.append(playGlyph);

            const remove = document.createElement("button");
            remove.className = "playlist-card__delete";
            remove.type = "button";
            remove.setAttribute("aria-label", `Удалить «${title}»`);
            const removeGlyph = document.createElement("i");
            removeGlyph.dataset.lucide = "trash-2";
            remove.append(removeGlyph);

            card.append(icon, body, play, remove);
            fragment.append(card);
        });

        playlistsList.replaceChildren(fragment);
        initIcons();
        if (playlistsEmpty) {
            playlistsEmpty.hidden = source.length > 0;
        }
        setPlaylistsCount();
    }

    async function createPlaylistRequest(title) {
        const response = await fetch("/api/playlists", {
            method: "POST",
            cache: "no-store",
            headers: apiHeaders({ "Content-Type": "application/json" }),
            body: JSON.stringify({
                user_id: PlayerState.userId,
                title,
            }),
        });

        if (!response.ok) {
            throw new Error(`Playlist create HTTP ${response.status}`);
        }

        const playlist = await response.json();
        playlists = [playlist, ...playlists];
        playlistsLoaded = true;
        renderPlaylists();
        return playlist;
    }

    function askPlaylistTitle() {
        let value = null;
        try {
            value = window.prompt("Название плейлиста:", "Новый плейлист");
        } catch {
            // prompt() can be unavailable in some WebView contexts.
        }
        if (value === null) {
            return "";
        }
        return String(value).trim() || "Новый плейлист";
    }

    async function createPlaylist() {
        const title = askPlaylistTitle();
        if (!title) {
            return;
        }

        try {
            await createPlaylistRequest(title);
            haptic("medium");
        } catch (error) {
            console.error("Playlist creation failed:", error);
        }
    }

    async function deletePlaylist(playlist) {
        const id = Number(playlist?.id);
        if (!Number.isFinite(id)) {
            return;
        }

        try {
            const response = await fetch(
                authenticatedUrl(`/api/playlists/${id}`),
                { method: "DELETE", headers: apiHeaders() }
            );

            if (!response.ok) {
                throw new Error(`Playlist delete HTTP ${response.status}`);
            }

            playlists = playlists.filter((item) => Number(item.id) !== id);
            renderPlaylists();
            haptic();
        } catch (error) {
            console.error("Playlist deletion failed:", error);
        }
    }

    function renderPlaylistTracksView(title, tracks, isError = false) {
        if (playlistTracksTitle) {
            playlistTracksTitle.textContent = title;
        }
        renderTrackList(playlistTracksList, tracks);
        if (playlistTracksEmpty) {
            playlistTracksEmpty.hidden = !isError && tracks.length > 0;
        }
    }

    async function fetchPlaylistTracks(playlistId) {
        const response = await fetch(
            authenticatedUrl(`/api/playlists/${Number(playlistId)}/tracks`),
            { cache: "no-store", headers: apiHeaders() }
        );

        if (!response.ok) {
            throw new Error(`Playlist tracks HTTP ${response.status}`);
        }

        const payload = await response.json();
        return (Array.isArray(payload) ? payload : [])
            .map(normalizeTrack)
            .filter((track) => track.track_id);
    }

    async function openPlaylist(playlist) {
        const id = Number(playlist?.id);
        if (!Number.isFinite(id)) {
            return;
        }

        openPlaylistId = id;
        activateLibraryView("tracks");
        if (playlistTracksTitle) {
            playlistTracksTitle.textContent = playlist.title || "Плейлист";
        }
        if (playlistTracksList) {
            playlistTracksList.setAttribute("aria-busy", "true");
        }
        if (playlistTracksEmpty) {
            playlistTracksEmpty.hidden = true;
        }

        try {
            const tracks = await fetchPlaylistTracks(id);
            if (openPlaylistId === id) {
                openPlaylistTracks = tracks;
                renderPlaylistTracksView(playlist.title || "Плейлист", tracks);
            }
        } catch (error) {
            console.error("Playlist tracks load failed:", error);
            if (openPlaylistId === id) {
                openPlaylistTracks = [];
                renderPlaylistTracksView(playlist.title || "Плейлист", [], true);
            }
        } finally {
            if (playlistTracksList) {
                playlistTracksList.setAttribute("aria-busy", "false");
            }
        }
    }

    async function playPlaylist(playlist) {
        const id = Number(playlist?.id);
        if (!Number.isFinite(id)) {
            return;
        }

        haptic("medium");
        try {
            const tracks = await fetchPlaylistTracks(id);
            if (!tracks.length) {
                return;
            }
            queueSource = "catalog";
            markUserPlaybackAction();
            setQueue(tracks, 0);
        } catch (error) {
            console.error("Playlist playback failed:", error);
        }
    }

    playlistsList?.addEventListener("click", (event) => {
        const card = event.target.closest(".playlist-card");
        if (!card) {
            return;
        }
        const playlist = playlists.find(
            (item) => Number(item.id) === Number(card.dataset.playlistId)
        );
        if (!playlist) {
            return;
        }

        if (event.target.closest(".playlist-card__play")) {
            void playPlaylist(playlist);
            return;
        }
        if (event.target.closest(".playlist-card__delete")) {
            void deletePlaylist(playlist);
            return;
        }
        void openPlaylist(playlist);
    });

    createPlaylistButton?.addEventListener("click", () => void createPlaylist());

    playlistBack?.addEventListener("click", () => {
        openPlaylistId = null;
        openPlaylistTracks = [];
        activateLibraryView("playlists");
        if (playlistsLoaded) {
            // Refresh the counts after possible adds/removes.
            void loadPlaylists({ quiet: true });
        }
        haptic();
    });

    playlistPlay?.addEventListener("click", () => {
        const playlist = playlists.find(
            (item) => Number(item.id) === openPlaylistId
        );
        if (playlist) {
            void playPlaylist(playlist);
            return;
        }
        // The playlist list may have been refreshed while tracks view was
        // open: fall back to playing the rendered track list.
        const rendered = [...(playlistTracksList?.children || [])]
            .map((card) => getTrackFromCard(card))
            .filter((track) => track.track_id);
        if (rendered.length) {
            queueSource = "catalog";
            markUserPlaybackAction();
            setQueue(rendered, 0);
        }
    });

    /* ---------- "Add to playlist" picker ---------- */

    function addTrackToPlaylistRequest(playlistId, track) {
        return fetch(
            authenticatedUrl(`/api/playlists/${Number(playlistId)}/tracks`),
            {
                method: "POST",
                cache: "no-store",
                headers: apiHeaders({ "Content-Type": "application/json" }),
                body: JSON.stringify({ user_id: PlayerState.userId, ...track }),
            }
        );
    }

    function updatePlaylistCount(playlistId, delta) {
        const id = Number(playlistId);
        playlists = playlists.map((playlist) =>
            Number(playlist.id) === id
                ? {
                      ...playlist,
                      track_count: Math.max(
                          0,
                          (Number(playlist.track_count) || 0) + delta
                      ),
                  }
                : playlist
        );
        setPlaylistsCount();
    }

    function renderPlaylistMenu() {
        if (!playlistMenuList) {
            return;
        }

        const fragment = document.createDocumentFragment();

        if (!playlistsLoaded) {
            const loading = document.createElement("p");
            loading.className = "playlist-menu__empty";
            loading.textContent = "Загрузка плейлистов…";
            fragment.append(loading);
        } else {
            playlists.forEach((playlist) => {
                const item = document.createElement("button");
                item.className = "playlist-menu__item";
                item.type = "button";
                item.dataset.playlistId = String(Number(playlist.id));

                const glyph = document.createElement("i");
                glyph.dataset.lucide = "list-music";

                const title = document.createElement("span");
                title.className = "playlist-menu__item-title";
                title.textContent = String(playlist.title || "Без названия");

                const state = document.createElement("span");
                state.className = "playlist-menu__item-state";

                item.append(glyph, title, state);
                fragment.append(item);
            });

            if (!playlists.length) {
                const empty = document.createElement("p");
                empty.className = "playlist-menu__empty";
                empty.textContent =
                    "Плейлистов пока нет — создайте первый ниже.";
                fragment.append(empty);
            }
        }

        playlistMenuList.replaceChildren(fragment);
        initIcons();
    }

    function openPlaylistMenu(track) {
        const normalized = normalizeTrack(track);
        if (!normalized.track_id || !playlistMenu) {
            return;
        }

        playlistMenuTrack = normalized;
        renderPlaylistMenu();
        playlistMenu.hidden = false;
        haptic();

        // Re-render with the fresh list when it arrives.
        if (!playlistsLoaded) {
            void loadPlaylists({ quiet: true }).then(renderPlaylistMenu);
        }
    }

    function closePlaylistMenu() {
        if (!playlistMenu) {
            return;
        }
        playlistMenu.hidden = true;
        playlistMenuTrack = null;
    }

    playlistMenu?.addEventListener("click", async (event) => {
        if (event.target.closest("[data-close-playlist-menu]")) {
            closePlaylistMenu();
            return;
        }

        const item = event.target.closest(".playlist-menu__item");
        if (!item || !playlistMenuTrack || item.classList.contains("is-busy")) {
            return;
        }

        const playlistId = Number(item.dataset.playlistId);
        if (!Number.isFinite(playlistId)) {
            return;
        }

        const stateElement = item.querySelector(".playlist-menu__item-state");
        haptic();
        try {
            item.classList.add("is-busy");
            const response = await addTrackToPlaylistRequest(
                playlistId,
                playlistMenuTrack
            );
            if (!response.ok) {
                throw new Error(`Playlist add HTTP ${response.status}`);
            }

            const payload = await response.json();
            if (payload?.added) {
                updatePlaylistCount(playlistId, 1);
            }
            if (stateElement) {
                stateElement.textContent = payload?.added
                    ? "Добавлено ✓"
                    : "Уже в плейлисте";
            }
            item.classList.remove("is-busy");
            window.setTimeout(() => {
                if (playlistMenu && !playlistMenu.hidden) {
                    closePlaylistMenu();
                }
                // The open tracks view may include this playlist.
                if (openPlaylistId === playlistId) {
                    void openPlaylist({ id: playlistId, title: playlistTracksTitle?.textContent });
                }
            }, 700);
        } catch (error) {
            item.classList.remove("is-busy");
            console.error("Adding track to playlist failed:", error);
            if (stateElement) {
                stateElement.textContent = "Ошибка";
            }
        }
    });

    playlistMenuClose?.addEventListener("click", closePlaylistMenu);

    playlistMenuNew?.addEventListener("click", async () => {
        if (!playlistMenuTrack) {
            return;
        }

        const title = askPlaylistTitle();
        if (!title) {
            return;
        }

        const track = playlistMenuTrack;
        try {
            const playlist = await createPlaylistRequest(title);
            await addTrackToPlaylistRequest(playlist.id, track);
            updatePlaylistCount(playlist.id, 1);
            haptic("medium");
        } catch (error) {
            console.error("Creating playlist for track failed:", error);
        } finally {
            closePlaylistMenu();
        }
    });

    async function loadFavorites({ force = false, quiet = false } = {}) {
        if (favoritesLoading && !force) {
            return;
        }

        const requestId = ++favoritesRequestId;
        const revision = favoritesRevision;
        favoritesLoading = true;
        if (!quiet) {
            setLibraryLoading(true);
        }

        if (!quiet && libraryEmpty && !favoritesLoaded) {
            libraryEmpty.hidden = true;
        }

        // A newer list request cancels the previous one so the catch-up
        // refresh never doubles the HTTP traffic.
        if (favoritesAbort) {
            favoritesAbort.abort();
            favoritesAbort = null;
        }
        const controller = new AbortController();
        favoritesAbort = controller;

        try {
            const response = await fetch(
                `/api/favorites?user_id=${encodeURIComponent(
                    String(PlayerState.userId)
                )}`,
                {
                    cache: "no-store",
                    headers: apiHeaders(),
                    signal: controller.signal,
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
                // Either likes are still in flight (they always win) or the
                // revision moved since this request was issued: the server
                // answer cannot be trusted against the optimistic local
                // state. Arm the catch-up reload for the moment the in-flight
                // like set drains.
                favoritesCatchUpPending = true;
                return;
            }

            const tracks = (Array.isArray(payload) ? payload : [])
                .map(normalizeTrack)
                .filter((track) => track.track_id);

            PlayerState.favorites = new Map(
                tracks.map((track) => [track.track_id, track])
            );
            favoritesLoaded = true;
            favoritesCatchUpPending = false;
            renderLibrary();
            syncLikeUI();
        } catch (error) {
            if (
                requestId !== favoritesRequestId ||
                error?.name === "AbortError"
            ) {
                // Aborted: a newer loadFavorites supersedes this request.
                return;
            }
            console.error("Favorites load failed:", error);
            if (quiet) {
                // A silent catch-up reload must not flash error states into
                // an already rendered library.
                return;
            }

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
            forgetFavoritesAbort(controller);
            if (requestId === favoritesRequestId) {
                favoritesLoading = false;
                if (!quiet) {
                    setLibraryLoading(false);
                }
            }
        }
    }

    function forgetFavoritesAbort(controller) {
        if (favoritesAbort === controller) {
            favoritesAbort = null;
        }
    }

    /**
     * Fire the "catch-up" favorites reload once every in-flight like/unlike
     * finished. Until then the optimistic local state is authoritative, so
     * the reconciling request is postponed, not dropped. The reconciled
     * answer carries the server's settled state and still loses to any like
     * started later (the revision guard in loadFavorites keeps that rule).
     */
    function reconcileFavorites() {
        if (pendingLikeRequests.size) {
            favoritesCatchUpPending = true;
            return;
        }
        if (!favoritesCatchUpPending) {
            return;
        }
        favoritesCatchUpPending = false;
        loadFavorites({ force: true, quiet: true });
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
                        ? apiHeaders()
                        : apiHeaders({
                              "Content-Type": "application/json",
                          }),
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
            reconcileFavorites();
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
        // Only search/trending cards sync here: that queue is a normal
        // looping catalog, never a wave.
        queueSource = "catalog";

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
            savePlayerState();
            return;
        }

        PlayerState.queue = tracks;
        PlayerState.currentIndex = -1;
        markCurrentCard();
        savePlayerState();
    }

    function setQueue(tracks, startIndex = -1) {
        PlayerState.queue = (Array.isArray(tracks) ? tracks : [])
            .map(normalizeTrack)
            .filter((track) => track.track_id);

        if (!PlayerState.queue.length) {
            PlayerState.currentIndex = -1;
            markCurrentCard();
            return;
        }

        if (startIndex >= 0) {
            const index = Math.min(
                startIndex,
                PlayerState.queue.length - 1
            );
            playTrack(index);
        } else {
            // No preselection: the player stays idle until the user picks one.
            PlayerState.currentIndex = -1;
            markCurrentCard();
        }

        savePlayerState({ force: true });
    }

    /* ---------- Track UI ---------- */

    // Hosts allowed to be fetched through the backend /api/thumb proxy.
    // The Telegram in-app browser ignores the system proxy, so cover CDNs
    // (i.ytimg.com etc.) are unreachable from it on the direct route.
    const THUMB_PROXY_SUFFIXES = ["ytimg.com", "dzcdn.net", "deezer.com"];

    function thumbUrl(thumbnail) {
        const value = String(thumbnail || "").trim();
        if (!value) {
            return "";
        }
        let url;
        try {
            url = new URL(value, window.location.href);
        } catch {
            return value;
        }
        const host = url.hostname.toLowerCase();
        const allowed = THUMB_PROXY_SUFFIXES.some(
            (suffix) => host === suffix || host.endsWith(`.${suffix}`)
        );
        // Relative (already-proxied) URLs pass through unchanged, so the
        // helper is safe to apply repeatedly to the same value.
        if (url.protocol === "https:" && allowed) {
            return `/api/thumb?u=${encodeURIComponent(url.href)}`;
        }
        return value;
    }

    function applyCover(element, thumbnail) {
        if (!element) {
            return;
        }

        thumbnail = thumbUrl(thumbnail);
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

    function showPlayerDock() {
        if (!playerDock) {
            return;
        }
        playerDock.hidden = false;
        playerDock.classList.add("is-visible");
    }

    function hidePlayerDock() {
        if (!playerDock) {
            return;
        }
        playerDock.classList.remove("is-visible");
        playerDock.hidden = true;
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
            image.src = thumbUrl(track.thumbnail);
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

        const add = document.createElement("button");
        add.className = "track-card__add";
        add.type = "button";
        add.setAttribute("aria-label", `Добавить «${track.title}» в плейлист`);

        const addGlyph = document.createElement("i");
        addGlyph.dataset.lucide = "list-plus";
        add.append(addGlyph);

        card.append(cover, body, duration, like, add);
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
        if (playerEyebrow) {
            playerEyebrow.textContent = "Сейчас играет";
        }

        marqueeTexts.forEach((element) => {
            element.textContent = track.title;
        });

        applyCover(miniCover, track.thumbnail);
        applyCover(playerCover, track.thumbnail);

        document.title = `${track.title} — ${artist}`;

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
        if (playerEyebrow) {
            playerEyebrow.textContent = "Не удалось воспроизвести";
        }
    }

    function playTrack(index, { resumePosition = 0 } = {}) {
        const track = PlayerState.queue[index];
        if (!track?.track_id) {
            return;
        }

        PlayerState.currentIndex = index;
        showPlayerDock();
        updateTrackUI(track);

        audio.src = `/api/stream/${encodeURIComponent(track.track_id)}`;
        audio.load();
        // Loading a new source can reset the element volume, so reapply
        // the persisted value before playback starts.
        audio.volume = PlayerState.volume;
        updateMediaSession(track);
        updatePositionState(0, 0, true);

        // Restored-from-storage position: consumed once, as soon as the
        // track metadata arrives (seeking earlier is unreliable).
        pendingSeekSeconds =
            Number(resumePosition) > 0 ? Number(resumePosition) : 0;

        const playPromise = audio.play();
        if (playPromise?.catch) {
            playPromise.catch(handlePlaybackFailure);
        }

        // Wave prefetch: while the penultimate or last wave track plays, the
        // next batch is already being fetched, so the transition into track
        // #19+ happens seamlessly (nextTrack just awaits the append).
        if (queueSource === "wave" && index >= PlayerState.queue.length - 2) {
            void appendMoreWaveTracks();
        }

        savePlayerState({ force: true, position: pendingSeekSeconds });
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
            // A restored queue has no source yet: resume the saved position
            // when the tracked track matches the restored current one.
            const track = PlayerState.queue[PlayerState.currentIndex];
            const resumePosition =
                pendingResume?.trackId === track?.track_id
                    ? pendingResume.position
                    : 0;
            pendingResume = null;
            playTrack(PlayerState.currentIndex, { resumePosition });
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

    async function nextTrack({ fromEnded = false } = {}) {
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
            if (queueSource !== "wave") {
                // The queue loops: after the last track playback continues
                // from the first one, keeping the music running without
                // interruption.
                next = 0;
                playTrack(next);
                return;
            }

            // "Моя волна" never loops back: instead of restarting from 0
            // the queue grows in the background with fresh tracks, and the
            // very next appended track is played. If nothing new arrives
            // (offline, exhausted backend answer) the wave falls back to
            // looping so playback does not die.
            await appendMoreWaveTracks();
            const grownLength = PlayerState.queue.length;
            if (grownLength > length) {
                playTrack(Math.min(next, grownLength - 1));
                return;
            }
            next = 0;
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
        if (playerEyebrow) {
            playerEyebrow.textContent = "Ошибка загрузки трека";
        }
    }

    function selectCard(card) {
        const trackId = card.dataset.trackId;
        if (!trackId) {
            return;
        }

        // "Моя волна" cards belong to the endless wave queue (it appends
        // new tracks instead of looping); every other list plays as a
        // normal looping catalog queue.
        queueSource = card.closest("#wave-list") ? "wave" : "catalog";

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
        markUserPlaybackAction();
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

        if (pendingSeekSeconds > 0) {
            let target = pendingSeekSeconds;
            pendingSeekSeconds = 0;
            if (Number.isFinite(audio.duration) && audio.duration > 0) {
                target = Math.min(target, Math.max(0, audio.duration - 0.5));
            }
            try {
                audio.currentTime = target;
            } catch {
                // The source may not be seekable yet; playback stays at 0.
            }
        }
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

    function setVolume(value, { persist = true, source = "user" } = {}) {
        const next = clamp(Number(value) || 0, 0, 1);
        if (source === "user") {
            lastUserVolumeChange = Date.now();
        }
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
            let src = thumbUrl(track.thumbnail);
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
                artist: track.artist || "Неизвестный исполнитель",
                album: track.artist || "Музыка",
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
                markUserPlaybackAction();
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
        if (Math.abs(audio.volume - PlayerState.volume) > 0.001) {
            // Some browsers drop the volume when the source is replaced.
            audio.volume = PlayerState.volume;
        }
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
        // Pause is a natural save point: flush the position immediately so
        // a reload never resumes at a stale offset.
        savePlayerState({ force: true });
    });

    audio.addEventListener("timeupdate", () => {
        if (!isSeeking) {
            updateTimeline();
        }
        // Throttled inside savePlayerState (~once per 5 seconds).
        savePlayerState();
    });

    audio.addEventListener("loadedmetadata", handleMetadata);
    audio.addEventListener("durationchange", handleMetadata);
    audio.addEventListener("ended", handleEnded);
    audio.addEventListener("error", handleAudioError);

    audio.addEventListener("volumechange", () => {
        // Never adopt a lowered element volume: metadata loading can silently
        // reset audio.volume, so the stored value always wins instead.
        const value = clamp(PlayerState.volume, 0, 1);
        if (Math.abs(audio.volume - value) > 0.001) {
            audio.volume = value;
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
        // The title becomes measurable only while the player is visible:
        // decide the marquee state now (a resize ticker refines it below).
        setupMarquee();
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
        if (event.key !== "Escape") {
            return;
        }
        if (playlistMenu && !playlistMenu.hidden) {
            closePlaylistMenu();
            return;
        }
        if (searchHistoryShown) {
            hideSearchHistory();
            return;
        }
        closePlayer();
    });

    // Best-effort flush when the Mini App is closed or reloaded.
    function flushPlayerState() {
        savePlayerState({ force: true });
    }
    window.addEventListener("pagehide", flushPlayerState);
    document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "hidden") {
            flushPlayerState();
        }
    });

    /* ---------- Buttons & interactions ---------- */

    miniPlay?.addEventListener("click", () => {
        haptic();
        markUserPlaybackAction();
        togglePlay();
    });

    playButton?.addEventListener("click", () => {
        haptic();
        markUserPlaybackAction();
        togglePlay();
    });

    miniNext?.addEventListener("click", () => {
        haptic();
        markUserPlaybackAction();
        nextTrack();
    });

    nextButton?.addEventListener("click", () => {
        haptic();
        markUserPlaybackAction();
        nextTrack();
    });

    prevButton?.addEventListener("click", () => {
        haptic();
        markUserPlaybackAction();
        prevTrack();
    });

    shuffleButton?.addEventListener("click", () => {
        PlayerState.isShuffle = !PlayerState.isShuffle;
        shuffleButton.setAttribute(
            "aria-pressed",
            String(PlayerState.isShuffle)
        );
        markUserPlaybackAction();
        savePlayerState({ force: true });
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
        markUserPlaybackAction();
        savePlayerState({ force: true });
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
        const addButton = event.target.closest(
            ".track-card__add, #full-add-playlist"
        );
        if (addButton) {
            const card = addButton.closest(".track-card");
            const track = card
                ? getTrackFromCard(card)
                : PlayerState.queue[PlayerState.currentIndex];
            if (track?.track_id) {
                event.preventDefault();
                event.stopPropagation();
                openPlaylistMenu(track);
            }
            return;
        }

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
            if (
                event.target.closest(".track-card__like") ||
                event.target.closest(".track-card__add")
            ) {
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
            if (
                event.target.closest(".track-card__like") ||
                event.target.closest(".track-card__add")
            ) {
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
    setupTrackListInteractions(playlistTracksList);
    setupTrackListInteractions(waveList);

    /* ---------- Init ---------- */

    if (fullPlayer) {
        fullPlayer.inert = true;
    }

    initTelegram();
    initIcons();
    setupMarquee();
    setupMediaSession();

    // "Моя волна": mood chips and the mode switch are rendered up front;
    // the wave itself is generated on demand (chip pick / run buttons).
    setWaveMode("mood");
    renderWaveChips();

    // Restore the previous session's queue/position/modes *before* the
    // catalog loads, always paused (WebView forbids autoplay anyway).
    restoreSavedPlayerState();
    restoreCloudVolume();
    syncVolumeUI();
    loadFavorites();

    // The catalog stays empty (spinner only) until /api/trending answers.
    setSearchHeading("Популярное");
    setSearchCount("Загрузка…");
    hideSearchEmpty();

    updateTimeline();
    loadTrending();

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
        savePlayerState,
        restoreSavedPlayerState,
        loadSearchHistory,
        loadPlaylists,
        openPlaylist,
        playPlaylist,
        createPlaylist,
        deletePlaylist,
        openPlaylistMenu,
        closePlaylistMenu,
        activateLibraryView,
        loadWaveMood,
        loadWavePersonal,
        setWaveMode,
    };
})();
