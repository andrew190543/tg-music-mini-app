(() => {
  "use strict";

  const tg = window.Telegram?.WebApp;
  const FALLBACK_USER_ID = 123456;
  const userId =
    Number(window.Telegram?.WebApp?.initDataUnsafe?.user?.id) ||
    FALLBACK_USER_ID;
  const VOLUME_STORAGE_KEY = "tg-music-volume";
  const PLAYER_STATE_STORAGE_KEY = "tg-music-player-state";
  const TRENDING_CACHE_STORAGE_KEY = "tg-music-trending-cache-v1";
  const TRENDING_CACHE_MAX_AGE_MS = 30 * 60 * 1000;
  const PLAYER_STATE_PERSIST_MS = 5000;
  const PLAYER_STATE_MAX_QUEUE = 200;
  const SEARCH_LIMIT = 15;
  const SEARCH_DEBOUNCE_MS = 450;
  const TRENDING_LIMIT = 20;
  const API_TIMEOUT_MS = 35000;
  const WAVE_TIMEOUT_MS = 50000;
  const MAX_STREAM_RETRIES = 1;

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

  function isTransientStatus(status) {
    return status === 408 || status === 429 || status === 502 || status === 503 || status === 504;
  }

  function waitMs(ms, signal) {
    return new Promise((resolve, reject) => {
      if (signal?.aborted) {
        reject(new DOMException("Aborted", "AbortError"));
        return;
      }
      const timer = window.setTimeout(() => {
        signal?.removeEventListener("abort", onAbort);
        resolve();
      }, ms);
      function onAbort() {
        window.clearTimeout(timer);
        reject(new DOMException("Aborted", "AbortError"));
      }
      signal?.addEventListener("abort", onAbort, { once: true });
    });
  }

  async function fetchJson(
    url,
    {
      method = "GET",
      headers = apiHeaders(),
      body = undefined,
      signal = undefined,
      timeoutMs = API_TIMEOUT_MS,
      retries = 1,
    } = {}
  ) {
    let lastError = null;
    for (let attempt = 0; attempt <= retries; attempt += 1) {
      if (signal?.aborted) {
        throw new DOMException("Aborted", "AbortError");
      }

      const controller = new AbortController();
      const onOuterAbort = () => controller.abort();
      signal?.addEventListener("abort", onOuterAbort, { once: true });
      const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);

      try {
        const response = await fetch(url, {
          method,
          cache: "no-store",
          headers,
          body,
          signal: controller.signal,
        });

        if (!response.ok) {
          const err = new Error(`HTTP ${response.status}`);
          err.status = response.status;
          if (attempt < retries && isTransientStatus(response.status)) {
            lastError = err;
            await waitMs(450 * (attempt + 1), signal);
            continue;
          }
          throw err;
        }

        return await response.json();
      } catch (error) {
        if (signal?.aborted) {
          throw new DOMException("Aborted", "AbortError");
        }
        lastError = error;
        const canRetry =
          attempt < retries &&
          (error?.name === "AbortError" ||
            error instanceof TypeError ||
            isTransientStatus(error?.status));
        if (!canRetry) {
          throw error;
        }
        await waitMs(450 * (attempt + 1), signal);
      } finally {
        window.clearTimeout(timeoutId);
        signal?.removeEventListener("abort", onOuterAbort);
      }
    }
    throw lastError || new Error("Request failed");
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
  const searchRetry = document.getElementById("search-retry");
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
    isBuffering: false,
    isShuffle: false,
    repeatMode: "none",
    volume: 1.0,
    userId: userId,
    favorites: new Map(),
  };

  const audio = PlayerState.audio;
  audio.preload = "auto";

  const repeatStates = ["none", "all", "one"];
  const repeatLabels = {
    none: "Повтор: выключен",
    all: "Повтор: весь плейлист",
    one: "Повтор: один трек",
  };

  let lastAudibleVolume = 1;
  let isSeeking = false;
  let lastPositionUpdate = 0;
  let lastUserVolumeChange = 0;
  let lastUserPlaybackAction = 0;
  let pendingSeekSeconds = 0;
  let pendingResume = null;
  let restoredStateSavedAt = 0;
  let lastPlayerPersistAt = 0;
  let currentStreamRetries = 0;
  let consecutiveTrackErrors = 0;
  let retryAction = null;

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
    const index =
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
    if (searchHistoryHideTimer !== null) {
      window.clearTimeout(searchHistoryHideTimer);
      searchHistoryHideTimer = null;
    }
    if (searchHistoryShown) {
      hideSearchHistory();
    }
    searchInput?.blur();

    activeTab = name;
    setSearchPlaceholder(name);
    syncClearButton();

    if (name === "library") {
      libraryFilter = searchInput?.value ? searchInput.value : "";
    } else if (name === "search") {
      const query = searchInput?.value.trim() || "";
      if (!query) {
        restorePopular();
      } else if (query !== lastOnlineSearchQuery) {
        performSearch(query);
      }
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
      applyLibraryFilter();
      loadFavorites({ force: true });
      if (playlistsLoaded) {
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

  /* ---------- Search field & trending cache ---------- */

  let searchTimer = null;
  let searchRequestId = 0;
  let popularTracks = [];
  let popularLoaded = false;
  let activeListAbort = null;
  let libraryFilter = "";
  let lastOnlineSearchQuery = "";

  function readCachedTrending() {
    try {
      const raw = window.localStorage.getItem(TRENDING_CACHE_STORAGE_KEY);
      if (!raw) {
        return null;
      }
      const parsed = JSON.parse(raw);
      if (!parsed || !Array.isArray(parsed.tracks) || !parsed.tracks.length) {
        return null;
      }
      const tracks = parsed.tracks
        .map(normalizeTrack)
        .filter((t) => t.track_id);
      if (!tracks.length) {
        return null;
      }
      const fresh = Date.now() - Number(parsed.savedAt || 0) < TRENDING_CACHE_MAX_AGE_MS;
      return { tracks, fresh };
    } catch {
      return null;
    }
  }

  function writeCachedTrending(tracks) {
    if (!Array.isArray(tracks) || !tracks.length) {
      return;
    }
    try {
      window.localStorage.setItem(
        TRENDING_CACHE_STORAGE_KEY,
        JSON.stringify({
          tracks: tracks.slice(0, 50),
          savedAt: Date.now(),
        })
      );
    } catch {
      // Ignore storage quota errors.
    }
  }

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
    retryAction = null;
    if (searchRetry) {
      searchRetry.hidden = true;
    }
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

  function showSearchEmpty(title, text, onRetry = null) {
    if (searchEmptyTitle) {
      searchEmptyTitle.textContent = title;
    }
    if (searchEmptyText) {
      searchEmptyText.textContent = text;
    }
    retryAction = typeof onRetry === "function" ? onRetry : null;
    if (searchRetry) {
      searchRetry.hidden = !retryAction;
    }
    if (searchEmpty) {
      searchEmpty.hidden = false;
    }
  }

  searchRetry?.addEventListener("click", () => {
    if (typeof retryAction === "function") {
      haptic();
      retryAction();
    }
  });

  async function performSearch(rawQuery) {
    const query = String(rawQuery ?? "").trim();
    if (!query) {
      return;
    }

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
    if (!trackList?.querySelector(".track-card")) {
      showCatalogSpinner("Ищем треки…");
    } else {
      trackList.setAttribute("aria-busy", "true");
      hideSearchEmpty();
    }

    try {
      const payload = await fetchJson(
        `/api/search?q=${encodeURIComponent(query)}&limit=${SEARCH_LIMIT}` +
          `&user_id=${encodeURIComponent(String(PlayerState.userId))}`,
        {
          signal: controller.signal,
          timeoutMs: API_TIMEOUT_MS,
          retries: 1,
        }
      );

      if (requestId !== searchRequestId) {
        return;
      }

      const tracks = (Array.isArray(payload) ? payload : [])
        .map(normalizeTrack)
        .filter((track) => track.track_id);

      renderTracks(tracks);
      syncQueueFromCards({
        preserveCurrent: Boolean(audio.src || pendingResume),
      });
      setSearchCount(pluralizeTracks(tracks.length));

      if (tracks.length) {
        hideSearchEmpty();
        rememberSearchQuery(query);
      } else {
        showSearchEmpty(
          "Ничего не найдено",
          `По запросу «${query}» ничего не найдено. Попробуйте изменить запрос.`
        );
      }
    } catch (error) {
      if (requestId !== searchRequestId || error?.name === "AbortError") {
        return;
      }
      console.error("Search failed:", error);
      setSearchCount("Ошибка поиска");
      trackList?.replaceChildren();

      showSearchEmpty(
        "Не удалось загрузить треки",
        "Сервер временно недоступен или медленно отвечает. Нажмите «Повторить».",
        () => performSearch(query)
      );
    } finally {
      forgetActiveListRequest(controller);
      if (requestId === searchRequestId) {
        trackList?.setAttribute("aria-busy", "false");
      }
    }
  }

  async function loadTrending({ background = false } = {}) {
    const requestId = ++searchRequestId;
    if (searchTimer !== null) {
      window.clearTimeout(searchTimer);
      searchTimer = null;
    }
    stopActiveListRequest();
    const controller = new AbortController();
    activeListAbort = controller;

    if (!background) {
      setSearchHeading("Популярное");
      setSearchCount("Загрузка…");
      showCatalogSpinner();
    }

    try {
      const payload = await fetchJson(
        `/api/trending?limit=${TRENDING_LIMIT}`,
        {
          headers: { Accept: "application/json" },
          signal: controller.signal,
          timeoutMs: API_TIMEOUT_MS,
          retries: 2,
        }
      );

      if (requestId !== searchRequestId) {
        return;
      }

      const tracks = (Array.isArray(payload) ? payload : [])
        .map(normalizeTrack)
        .filter((track) => track.track_id);

      if (tracks.length) {
        popularTracks = tracks;
        popularLoaded = true;
        writeCachedTrending(tracks);
        if (!lastOnlineSearchQuery && activeTab === "search") {
          renderTracks(tracks);
          syncQueueFromCards({
            preserveCurrent: Boolean(audio.src || pendingResume),
          });
          setSearchCount(pluralizeTracks(tracks.length));
          hideSearchEmpty();
        }
      } else if (!popularTracks.length) {
        renderTracks([]);
        setSearchCount("0 треков");
        showSearchEmpty(
          "Популярное недоступно",
          "Введите название трека или исполнителя в поиске.",
          () => loadTrending()
        );
      }
    } catch (error) {
      if (requestId !== searchRequestId || error?.name === "AbortError") {
        return;
      }
      console.error("Trending failed:", error);

      if (!popularTracks.length) {
        setSearchCount("Ошибка загрузки");
        popularLoaded = false;
        trackList?.replaceChildren();
        showSearchEmpty(
          "Не удалось загрузить треки",
          "Сервер не ответил вовремя. Попробуйте ещё раз или воспользуйтесь поиском.",
          () => loadTrending()
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
        "Введите название трека или исполнителя в поиске.",
        () => loadTrending()
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
    libraryFilter = searchInput.value;
    if (searchInput.value.trim()) {
      hideSearchHistory();
    } else {
      lastOnlineSearchQuery = "";
    }
    if (activeTab === "library") {
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
      const payload = await fetchJson(
        authenticatedUrl("/api/search-history?limit=10"),
        { timeoutMs: 10000, retries: 0 }
      );
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
      await fetchJson(authenticatedUrl("/api/search-history"), {
        method: "DELETE",
        timeoutMs: 10000,
        retries: 0,
      });
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
  let waveAbort = null;
  let queueSource = "catalog";
  let waveSource = "mood";
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
    setWaveButtonsBusy(true);

    try {
      const payload = await fetchJson(url, {
        signal: controller.signal,
        timeoutMs: WAVE_TIMEOUT_MS,
        retries: 1,
      });

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
        return;
      }
      console.error("Wave failed:", error);
      setWaveCount("Ошибка загрузки");
      waveList?.replaceChildren();

      showWaveEmpty(
        "Не удалось собрать волну",
        "Сервер не успел подобрать треки или прокси перегружен. Нажмите «Запустить волну» ещё раз."
      );
    } finally {
      if (waveAbort === controller) {
        waveAbort = null;
      }
      if (requestId === waveRequestId) {
        waveList?.setAttribute("aria-busy", "false");
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
        const payload = await fetchJson(currentWaveUrl(), {
          timeoutMs: WAVE_TIMEOUT_MS,
          retries: 1,
        });
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
      const payload = await fetchJson(authenticatedUrl("/api/playlists"), {
        timeoutMs: 12000,
        retries: 1,
      });

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
    const playlist = await fetchJson("/api/playlists", {
      method: "POST",
      headers: apiHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        user_id: PlayerState.userId,
        title,
      }),
      timeoutMs: 12000,
      retries: 1,
    });

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
      await fetchJson(authenticatedUrl(`/api/playlists/${id}`), {
        method: "DELETE",
        timeoutMs: 12000,
        retries: 0,
      });

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
    const payload = await fetchJson(
      authenticatedUrl(`/api/playlists/${Number(playlistId)}/tracks`),
      { timeoutMs: 12000, retries: 1 }
    );
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
    return fetchJson(
      authenticatedUrl(`/api/playlists/${Number(playlistId)}/tracks`),
      {
        method: "POST",
        headers: apiHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ user_id: PlayerState.userId, ...track }),
        timeoutMs: 12000,
        retries: 1,
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
      const payload = await addTrackToPlaylistRequest(
        playlistId,
        playlistMenuTrack
      );
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
        if (openPlaylistId === playlistId) {
          void openPlaylist({
            id: playlistId,
            title: playlistTracksTitle?.textContent,
          });
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

    if (favoritesAbort) {
      favoritesAbort.abort();
      favoritesAbort = null;
    }
    const controller = new AbortController();
    favoritesAbort = controller;

    try {
      const payload = await fetchJson(
        `/api/favorites?user_id=${encodeURIComponent(
          String(PlayerState.userId)
        )}`,
        {
          signal: controller.signal,
          timeoutMs: 12000,
          retries: 1,
        }
      );

      if (requestId !== favoritesRequestId) {
        return;
      }
      if (revision !== favoritesRevision || pendingLikeRequests.size) {
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
        return;
      }
      console.error("Favorites load failed:", error);
      if (quiet) {
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
      const payload = await fetchJson(
        wasFavorite
          ? `/api/favorites?track_id=${encodeURIComponent(
              trackId
            )}&user_id=${encodeURIComponent(String(PlayerState.userId))}`
          : "/api/favorites",
        {
          method: wasFavorite ? "DELETE" : "POST",
          headers: wasFavorite
            ? apiHeaders()
            : apiHeaders({ "Content-Type": "application/json" }),
          body: wasFavorite
            ? undefined
            : JSON.stringify(favoritePayload(normalized)),
          timeoutMs: 12000,
          retries: 1,
        }
      );

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
      const isCurrent = Boolean(currentId) && card.dataset.trackId === currentId;
      card.classList.toggle("is-current", isCurrent);
      card.classList.toggle("is-loading", isCurrent && PlayerState.isBuffering);
      if (!card.hasAttribute("tabindex")) {
        card.tabIndex = 0;
      }
    });
  }

  function syncQueueFromCards({ preserveCurrent = false } = {}) {
    if (!trackList) {
      return;
    }
    queueSource = "catalog";

    const previousTrack =
      PlayerState.queue[PlayerState.currentIndex] || null;
    const previousId = previousTrack?.track_id || "";
    const tracks = [...trackList.querySelectorAll(".track-card")]
      .map(getTrackFromCard)
      .filter((track) => track.track_id);

    if (preserveCurrent && previousTrack) {
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
      PlayerState.currentIndex = -1;
      markCurrentCard();
    }

    savePlayerState({ force: true });
  }

  /* ---------- Track UI ---------- */

  const THUMB_PROXY_SUFFIXES = [
    "ytimg.com",
    "dzcdn.net",
    "deezer.com",
    "ggpht.com",
    "googleusercontent.com",
  ];

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
    if (url.protocol === "https:" && allowed) {
      return `/api/thumb?u=${encodeURIComponent(url.href)}`;
    }
    return value;
  }

  function applyCover(element, thumbnail) {
    if (!element) {
      return;
    }

    const proxied = thumbUrl(thumbnail);
    if (!proxied) {
      element.style.removeProperty("background-image");
      element.classList.remove("has-image");
      return;
    }

    const probe = new Image();
    probe.onload = () => {
      element.style.backgroundImage = `url("${String(proxied).replace(
        /"/g,
        "%22"
      )}")`;
      element.classList.add("has-image");
    };
    probe.onerror = () => {
      element.style.removeProperty("background-image");
      element.classList.remove("has-image");
    };
    probe.src = proxied;
  }

  function showPlayerDock() {
    if (!playerDock) {
      return;
    }
    playerDock.hidden = false;
    playerDock.classList.add("is-visible");
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

  function setPlayerStatus(text) {
    if (playerEyebrow) {
      playerEyebrow.textContent = text;
    }
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
    setPlayerStatus("Сейчас играет");

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

  function buildStreamUrl(trackId, attempt = 0) {
    const base = `/api/stream?track_id=${encodeURIComponent(trackId)}`;
    return attempt > 0 ? `${base}&_r=${attempt}_${Date.now()}` : base;
  }

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

  function setBuffering(buffering) {
    if (PlayerState.isBuffering === buffering) {
      return;
    }
    PlayerState.isBuffering = buffering;
    markCurrentCard();
    if (buffering && PlayerState.currentIndex >= 0) {
      setPlayerStatus("Загрузка потока…");
    } else if (PlayerState.isPlaying) {
      setPlayerStatus("Сейчас играет");
    }
  }

  function handlePlaybackFailure(error) {
    if (error?.name === "AbortError") {
      return;
    }
    console.error("Playback failed:", error);
    setBuffering(false);
    PlayerState.isPlaying = false;
    renderPlayState();
    setMediaPlaybackState("paused");
    setPlayerStatus("Не удалось воспроизвести");
  }

  let activeStreamAbort = null;

  async function playTrack(index, { resumePosition = 0, retryAttempt = 0 } = {}) {
    const track = PlayerState.queue[index];
    if (!track?.track_id) {
      return;
    }

    if (retryAttempt === 0) {
      currentStreamRetries = 0;
    }

    if (activeStreamAbort) {
      activeStreamAbort.abort();
      activeStreamAbort = null;
    }

    PlayerState.currentIndex = index;
    showPlayerDock();
    updateTrackUI(track);
    setBuffering(true);

    pendingSeekSeconds =
      Number(resumePosition) > 0 ? Number(resumePosition) : 0;

    if (queueSource === "wave" && index >= PlayerState.queue.length - 2) {
      void appendMoreWaveTracks();
    }

    savePlayerState({ force: true, position: pendingSeekSeconds });

    if (retryAttempt > 0) {
      // Повтор после ошибки: сбрасываем потенциально «битый» blob.
      if (bgPrefetch.trackId === track.track_id) {
        bgResetPrefetch();
      }
      bgReleaseCurrentBlob();
    } else {
      // Мгновенный старт из предзагруженного blob: без сети и таймеров,
      // работает на заблокированном экране.
      const blobUrl = bgTakePrefetch(track.track_id);
      if (blobUrl) {
        bgStartBlobPlayback(track, blobUrl);
        bgEnsurePrefetch();
        return;
      }
    }

    // Этот трек уже качается предзагрузкой — дождёмся её, а не второго запроса.
    const inflight = bgInflightPrefetch(track.track_id);
    if (inflight) {
      try {
        await inflight;
      } catch {
        // Упало — пойдём обычным путём ниже.
      }
      if (PlayerState.currentIndex !== index) {
        return;
      }
      const blobUrl = bgTakePrefetch(track.track_id);
      if (blobUrl) {
        bgStartBlobPlayback(track, blobUrl);
        bgEnsurePrefetch();
        return;
      }
    }

    const streamUrl = buildStreamUrl(track.track_id, retryAttempt);
    const controller = new AbortController();
    activeStreamAbort = controller;

    try {
      // Прогреваем/дожидаемся скачивания трека в кэш на бэкенде обычным fetch,
      // чтобы HTML5 <audio> в Telegram WebView не отвалился по внутреннему таймауту.
      const probe = await fetch(streamUrl, {
        headers: { Range: "bytes=0-1023" },
        signal: controller.signal,
      });
      if (!probe.ok && probe.status !== 206) {
        throw new Error(`Stream HTTP ${probe.status}`);
      }
      if (probe.status === 206) {
        await probe.arrayBuffer();
      } else {
        // Сервер проигнорировал Range — не качаем весь файл дважды.
        try { await probe.body?.cancel(); } catch { /* ignore */ }
      }
    } catch (err) {
      if (err?.name === "AbortError" || PlayerState.currentIndex !== index) {
        return;
      }
      handleAudioError();
      return;
    } finally {
      if (activeStreamAbort === controller) {
        activeStreamAbort = null;
      }
    }

    if (PlayerState.currentIndex !== index) {
      return;
    }

    bgReleaseCurrentBlob();
    audio.src = streamUrl;
    audio.load();
    audio.volume = PlayerState.volume;
    updateMediaSession(track);
    updatePositionState(0, 0, true);

    const playPromise = audio.play();
    if (playPromise?.catch) {
      playPromise.catch(handlePlaybackFailure);
    }
    bgEnsurePrefetch();
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

    // Индекс, выбранный заранее: совпадает с предзагруженным треком
    // (критично для shuffle и для игры на заблокированном экране).
    let next = bgPlannedNextIndex();

    if (next >= length) {
      if (queueSource !== "wave") {
        next = 0;
      } else {
        await appendMoreWaveTracks();
        const grownLength = PlayerState.queue.length;
        next = grownLength > length ? Math.min(next, grownLength - 1) : 0;
      }
    }

    if (next === PlayerState.currentIndex) {
      // Очередь из одного трека: повторяем без сетевого запроса.
      replayCurrent();
      return;
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
    const mediaErr = audio.error;
    console.error("Audio element error:", mediaErr);

    const index = PlayerState.currentIndex;
    const track = PlayerState.queue[index];
    if (track?.track_id && currentStreamRetries < MAX_STREAM_RETRIES) {
      currentStreamRetries += 1;
      setPlayerStatus("Повторная загрузка трека…");
      window.setTimeout(() => {
        if (PlayerState.currentIndex === index) {
          playTrack(index, { retryAttempt: currentStreamRetries });
        }
      }, 650);
      return;
    }

    setBuffering(false);
    PlayerState.isPlaying = false;
    renderPlayState();
    setMediaPlaybackState("none");
    setPlayerStatus("Ошибка загрузки трека");

    consecutiveTrackErrors += 1;
    if (
      PlayerState.queue.length > 1 &&
      consecutiveTrackErrors < Math.min(3, PlayerState.queue.length)
    ) {
      window.setTimeout(() => {
        if (!PlayerState.isPlaying && PlayerState.currentIndex === index) {
          void nextTrack();
        }
      }, 1200);
    }
  }

  function selectCard(card) {
    const trackId = card.dataset.trackId;
    if (!trackId) {
      return;
    }

    queueSource = card.closest("#wave-list") ? "wave" : "catalog";

    const previousCurrentId =
      PlayerState.queue[PlayerState.currentIndex]?.track_id || "";
    let index = PlayerState.queue.findIndex(
      (track) => track.track_id === trackId
    );

    if (index === -1) {
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
    consecutiveTrackErrors = 0;
    if (previousCurrentId && previousCurrentId === trackId) {
      PlayerState.currentIndex = index;
      togglePlay();
    } else {
      playTrack(index);
    }
  }

  /* ---------- Background playback: предзагрузка следующего трека ---------- */

  /*
   * Пока текущий трек играет, процесс Telegram жив, и мы заранее скачиваем
   * следующий трек в Blob. На "ended" источник переключается синхронно,
   * без сети и таймеров — заморозка WebView на локскрине не успевает помешать.
   */

  const BG_PREFETCH_MAX_BYTES = 64 * 1024 * 1024;
  const BG_PREFETCH_RETRY_MS = 5000;
  const BG_PREFETCH_TIMEOUT_MS = 90000;
  // Если на HyperOS playback всё равно обрывается — поставь true:
  // тихой петлёй не даём системе заморозить процесс во время смены трека.
  const BG_SILENT_KEEPALIVE = false;

  const bgPrefetch = {
    trackId: "",
    index: -1,
    fromIndex: -1,
    fromId: "",
    shuffle: false,
    repeat: "none",
    blobUrl: null,
    promise: null,
    abort: null,
    failedAt: 0,
  };

  let bgCurrentBlobUrl = null;
  let bgPausedByUser = false;
  let bgPlayingWhenHidden = false;
  let bgKeepAliveAudio = null;
  let bgMediaHooked = false;

  function bgResetPrefetch() {
    if (bgPrefetch.abort) {
      try { bgPrefetch.abort.abort(); } catch { /* ignore */ }
    }
    if (bgPrefetch.blobUrl) {
      try { URL.revokeObjectURL(bgPrefetch.blobUrl); } catch { /* ignore */ }
    }
    bgPrefetch.trackId = "";
    bgPrefetch.index = -1;
    bgPrefetch.fromIndex = -1;
    bgPrefetch.fromId = "";
    bgPrefetch.shuffle = false;
    bgPrefetch.repeat = "none";
    bgPrefetch.blobUrl = null;
    bgPrefetch.promise = null;
    bgPrefetch.abort = null;
    bgPrefetch.failedAt = 0;
  }

  function bgPrefetchValid() {
    if (!bgPrefetch.trackId) {
      return false;
    }
    const current = PlayerState.queue[PlayerState.currentIndex];
    return (
      bgPrefetch.fromIndex === PlayerState.currentIndex &&
      bgPrefetch.fromId === (current?.track_id || "") &&
      bgPrefetch.shuffle === PlayerState.isShuffle &&
      bgPrefetch.repeat === PlayerState.repeatMode &&
      PlayerState.queue[bgPrefetch.index]?.track_id === bgPrefetch.trackId
    );
  }

  function bgPlanNextIndex() {
    const length = PlayerState.queue.length;
    if (!length) {
      return -1;
    }
    if (PlayerState.currentIndex === -1) {
      return 0;
    }
    if (PlayerState.isShuffle) {
      return pickRandomIndex();
    }
    const next = PlayerState.currentIndex + 1;
    if (next < length) {
      return next;
    }
    return queueSource === "wave" ? length : 0;
  }

  // Для nextTrack(): заранее выбранный индекс совпадает с предзагруженным треком.
  function bgPlannedNextIndex() {
    if (bgPrefetchValid()) {
      return bgPrefetch.index;
    }
    return bgPlanNextIndex();
  }

  function bgStartPrefetch(index) {
    const track = PlayerState.queue[index];
    const current = PlayerState.queue[PlayerState.currentIndex];
    if (!track?.track_id || !current?.track_id) {
      return;
    }
    if (bgPrefetch.trackId) {
      bgResetPrefetch();
    }

    const controller = new AbortController();
    bgPrefetch.trackId = track.track_id;
    bgPrefetch.index = index;
    bgPrefetch.fromIndex = PlayerState.currentIndex;
    bgPrefetch.fromId = current.track_id;
    bgPrefetch.shuffle = PlayerState.isShuffle;
    bgPrefetch.repeat = PlayerState.repeatMode;
    bgPrefetch.abort = controller;
    bgPrefetch.failedAt = 0;
    bgPrefetch.blobUrl = null;

    const promise = (async () => {
      const response = await fetch(buildStreamUrl(track.track_id), {
        signal: controller.signal,
        headers: { Accept: "*/*" },
      });
      if (!response.ok) {
        throw new Error(`Prefetch HTTP ${response.status}`);
      }
      let blob = await response.blob();
      if (controller.signal.aborted || bgPrefetch.abort !== controller) {
        return;
      }
      if (!blob.size || blob.size > BG_PREFETCH_MAX_BYTES) {
        throw new Error("Prefetch: недопустимый размер файла");
      }
      if (!blob.type || !blob.type.startsWith("audio/")) {
        blob = new Blob([blob], { type: "audio/mp4" });
      }
      bgPrefetch.blobUrl = URL.createObjectURL(blob);
    })();

    bgPrefetch.promise = promise;
    const timeoutId = window.setTimeout(() => {
      if (bgPrefetch.abort === controller && !bgPrefetch.blobUrl) {
        try { controller.abort(); } catch { /* ignore */ }
      }
    }, BG_PREFETCH_TIMEOUT_MS);

    promise.then(
      () => {
        window.clearTimeout(timeoutId);
        if (bgPrefetch.abort === controller) {
          bgPrefetch.promise = null;
        }
      },
      (error) => {
        window.clearTimeout(timeoutId);
        if (bgPrefetch.abort !== controller) {
          return;
        }
        bgPrefetch.promise = null;
        bgPrefetch.failedAt = Date.now();
        if (error?.name !== "AbortError") {
          console.warn("Next-track prefetch failed:", error);
        }
      }
    );
  }

  function bgEnsurePrefetch() {
    const current = PlayerState.queue[PlayerState.currentIndex];
    if (!current?.track_id) {
      if (bgPrefetch.trackId) {
        bgResetPrefetch();
      }
      return;
    }
    if (bgPrefetch.trackId && !bgPrefetchValid()) {
      bgResetPrefetch();
    }
    if (PlayerState.repeatMode === "one") {
      return;
    }
    if (bgPrefetch.trackId) {
      if (bgPrefetch.blobUrl || bgPrefetch.promise) {
        return;
      }
      if (Date.now() - bgPrefetch.failedAt < BG_PREFETCH_RETRY_MS) {
        return;
      }
      bgResetPrefetch();
    }

    const index = bgPlanNextIndex();
    if (index < 0 || index >= PlayerState.queue.length) {
      // «Волна»: очередь дорастит playTrack сам — перепроверим на следующем timeupdate.
      return;
    }
    if (index === PlayerState.currentIndex) {
      return;
    }
    bgStartPrefetch(index);
  }

  function bgTakePrefetch(trackId) {
    if (bgPrefetch.trackId !== trackId || !bgPrefetch.blobUrl) {
      return null;
    }
    const url = bgPrefetch.blobUrl;
    bgPrefetch.blobUrl = null;
    bgResetPrefetch();
    return url;
  }

  function bgInflightPrefetch(trackId) {
    return bgPrefetch.trackId === trackId && bgPrefetch.promise
      ? bgPrefetch.promise
      : null;
  }

  function bgReleaseCurrentBlob() {
    if (!bgCurrentBlobUrl) {
      return;
    }
    try { URL.revokeObjectURL(bgCurrentBlobUrl); } catch { /* ignore */ }
    bgCurrentBlobUrl = null;
  }

  function bgStartBlobPlayback(track, blobUrl) {
    const previous = bgCurrentBlobUrl;
    bgCurrentBlobUrl = blobUrl;
    audio.src = blobUrl;
    audio.load();
    audio.volume = PlayerState.volume;
    updateMediaSession(track);
    updatePositionState(0, 0, true);
    if (previous && previous !== blobUrl) {
      try { URL.revokeObjectURL(previous); } catch { /* ignore */ }
    }
    audio.play()?.catch?.(handlePlaybackFailure);
  }

  /* --- опциональный тихий keep-alive (BG_SILENT_KEEPALIVE) --- */

  function bgSilentWavUrl() {
    const sampleRate = 8000;
    const samples = sampleRate; // 1 секунда тишины
    const buffer = new ArrayBuffer(44 + samples * 2);
    const view = new DataView(buffer);
    const write = (offset, text) => {
      for (let i = 0; i < text.length; i += 1) {
        view.setUint8(offset + i, text.charCodeAt(i));
      }
    };
    write(0, "RIFF");
    view.setUint32(4, 36 + samples * 2, true);
    write(8, "WAVE");
    write(12, "fmt ");
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    write(36, "data");
    view.setUint32(40, samples * 2, true);
    return URL.createObjectURL(new Blob([buffer], { type: "audio/wav" }));
  }

  function bgStartKeepAlive() {
    if (!BG_SILENT_KEEPALIVE) {
      return;
    }
    try {
      if (!bgKeepAliveAudio) {
        bgKeepAliveAudio = new Audio(bgSilentWavUrl());
        bgKeepAliveAudio.loop = true;
        bgKeepAliveAudio.volume = 0.001;
        bgKeepAliveAudio.preload = "auto";
      }
      if (bgKeepAliveAudio.paused) {
        bgKeepAliveAudio.play()?.catch?.(() => {});
      }
    } catch { /* keep-alive — best-effort */ }
  }

  function bgStopKeepAlive() {
    try { bgKeepAliveAudio?.pause(); } catch { /* ignore */ }
  }

  /* --- учёт «пользователь сам поставил паузу» (кнопки + шторка/гарнитура) --- */

  function bgHookMediaSession() {
    if (bgMediaHooked || !("mediaSession" in navigator)) {
      return;
    }
    bgMediaHooked = true;
    const ms = navigator.mediaSession;
    const set = (action, handler) => {
      try { ms.setActionHandler(action, handler); } catch { /* unsupported */ }
    };
    set("play", () => {
      bgPausedByUser = false;
      markUserPlaybackAction();
      if (PlayerState.currentIndex === -1 || !audio.src) {
        togglePlay();
      } else {
        audio.play()?.catch?.(handlePlaybackFailure);
      }
    });
    set("pause", () => {
      bgPausedByUser = true;
      markUserPlaybackAction();
      audio.pause();
    });
    set("stop", () => {
      bgPausedByUser = true;
      markUserPlaybackAction();
      audio.pause();
      try { audio.currentTime = 0; } catch { /* ignore */ }
      PlayerState.isPlaying = false;
      renderPlayState();
      updateTimeline();
      setMediaPlaybackState("none");
    });
  }

  audio.addEventListener("play", () => {
    bgPausedByUser = false;
    bgHookMediaSession();
    bgStartKeepAlive();
  });

  audio.addEventListener("playing", () => {
    bgEnsurePrefetch();
  });

  audio.addEventListener("timeupdate", () => {
    bgEnsurePrefetch();
  });

  audio.addEventListener("pause", () => {
    if (bgPausedByUser) {
      bgStopKeepAlive();
    }
  });

  document.addEventListener("click", (event) => {
    const target = event.target instanceof Element ? event.target : null;
    if (!target) {
      return;
    }
    if (target.closest("#mini-play, #play-btn")) {
      bgPausedByUser = !audio.paused;
      return;
    }
    const card = target.closest(".track-card");
    if (card) {
      const currentId =
        PlayerState.queue[PlayerState.currentIndex]?.track_id;
      bgPausedByUser =
        card.dataset.trackId === currentId ? !audio.paused : false;
      return;
    }
    if (
      target.closest("#mini-next, #next-btn, #prev-btn, #playlist-play, .wave-start")
    ) {
      bgPausedByUser = false;
    }
  }, true);

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") {
      bgPlayingWhenHidden = PlayerState.isPlaying && !audio.paused;
      return;
    }
    // Экран разблокировали: если трек доиграл в «заморозке» и следующий
    // не стартовал — догоняем очередь автоматически.
    bgEnsurePrefetch();
    if (bgPausedByUser || PlayerState.currentIndex < 0) {
      return;
    }
    if (audio.ended && PlayerState.queue.length) {
      void nextTrack({ fromEnded: true });
      return;
    }
    if (bgPlayingWhenHidden && audio.paused && audio.src && !audio.error) {
      audio.play()?.catch?.(() => {});
    }
  });

  window.addEventListener("pagehide", () => {
    bgStopKeepAlive();
  });

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
        src = new URL(src, window.location.href).href;
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

  audio.addEventListener("loadstart", () => {
    setBuffering(true);
  });

  audio.addEventListener("waiting", () => {
    setBuffering(true);
  });

  audio.addEventListener("canplay", () => {
    setBuffering(false);
    consecutiveTrackErrors = 0;
  });

  audio.addEventListener("playing", () => {
    setBuffering(false);
    consecutiveTrackErrors = 0;
    PlayerState.isPlaying = true;
    renderPlayState();
    setMediaPlaybackState("playing");
    setPlayerStatus("Сейчас играет");
  });

  audio.addEventListener("play", () => {
    if (Math.abs(audio.volume - PlayerState.volume) > 0.001) {
      audio.volume = PlayerState.volume;
    }
    PlayerState.isPlaying = true;
    renderPlayState();
    setMediaPlaybackState("playing");
    updatePositionState(undefined, audio.currentTime, true);
  });

  audio.addEventListener("pause", () => {
    setBuffering(false);
    PlayerState.isPlaying = false;
    renderPlayState();
    if (!audio.ended) {
      setMediaPlaybackState("paused");
    }
    savePlayerState({ force: true });
  });

  audio.addEventListener("timeupdate", () => {
    if (!isSeeking) {
      updateTimeline();
    }
    savePlayerState();
  });

  audio.addEventListener("loadedmetadata", handleMetadata);
  audio.addEventListener("durationchange", handleMetadata);
  audio.addEventListener("ended", handleEnded);
  audio.addEventListener("error", handleAudioError);

  audio.addEventListener("volumechange", () => {
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
    consecutiveTrackErrors = 0;
    nextTrack();
  });

  nextButton?.addEventListener("click", () => {
    haptic();
    markUserPlaybackAction();
    consecutiveTrackErrors = 0;
    nextTrack();
  });

  prevButton?.addEventListener("click", () => {
    haptic();
    markUserPlaybackAction();
    consecutiveTrackErrors = 0;
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

  setWaveMode("mood");
  renderWaveChips();

  restoreSavedPlayerState();
  restoreCloudVolume();
  syncVolumeUI();
  loadFavorites();

  updateTimeline();

  const cachedTrending = readCachedTrending();
  if (cachedTrending) {
    popularTracks = cachedTrending.tracks;
    popularLoaded = true;
    setSearchHeading("Популярное");
    renderTracks(popularTracks);
    syncQueueFromCards({
      preserveCurrent: Boolean(audio.src || pendingResume),
    });
    setSearchCount(pluralizeTracks(popularTracks.length));
    hideSearchEmpty();
    if (!cachedTrending.fresh) {
      void loadTrending({ background: true });
    }
  } else {
    setSearchHeading("Популярное");
    setSearchCount("Загрузка…");
    hideSearchEmpty();
    loadTrending();
  }

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