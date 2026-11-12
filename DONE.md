# ✅ DONE — Выполнено: полная хронология и структурная спецификация

**Проект:** TG Music — Telegram Mini App музыкальный плеер.
**Стек:** Python 3.13 + FastAPI + uvicorn + aiogram 3 + yt-dlp + SQLite (aiosqlite, WAL) + ванильный JS/HTML/CSS без сборщика.
**Жизненный цикл проекта:** 5 ноября 2026 (коммит `48cb340`) → 7 ноября 2026 (этап 5) — коммитов: 7.
**Тесты:** `pytest tests -q` → **111 passed, 0 failed** (~26 с, все сетевые вызовы замоканы, прогон ×2 — флейков нет).
**Ссылочная документация:** `SPEC.md` (ТЗ, Фазы 1–6 + хотфикс), `README.md` (запуск/туннель).

---

## 📅 Часть I. Хронология разработки (с первого дня до сегодня)

### День 1 — 5 ноября 2026 (коммит `48cb340` «Initial commit: TG Music Telegram Mini App»)

Проект создан «с нуля» в один день по ТЗ (`SPEC.md`), wszystkie фазы сданы с тестами:

| Фаза | Что сделано | Проверка |
|------|-------------|----------|
| **1. Каркас + БД** | Структура каталогов `bot/` `backend/` `frontend/` `tests/`; `requirements.txt` (aiogram, fastapi, uvicorn, yt-dlp, aiosqlite, python-dotenv, httpx); `backend/db.py` на aiosqlite: таблица `favorites` (UNIQUE `user_id+track_id`, индексы) и функции `init_db / add_favorite / remove_favorite / get_favorites` | 19 тестов |
| **2. Аудио-сервис** | `search_tracks()` — yt-dlp в `asyncio.to_thread`, `extract_flat=True`, `ytsearch15:`; `get_audio_stream_url()` — `bestaudio[ext=m4a]/bestaudio`; **стрим-прокси через `httpx.AsyncClient` с поддержкой `Range`** (CORS/регион-обход, перемотка) | 31 тест |
| **3. FastAPI** | `CORSMiddleware`, статика на `/app/`, `GET /api/search`, `GET /api/stream/{id}`, CRUD `/api/favorites` | 38 тестов |
| **4. Frontend-вёрстка** | `telegram-web-app.js` + Lucide; 3 зоны (панель поиска + вкладки / карточки треков / мини-плеер + полноэкранный плеер: таймлайн-range, Shuffle, Prev/Play/Next, Repeat off/all/one, громкость + Mute); CSS на Telegram theme-переменных (`--tg-theme-*`) | смоук |
| **5. Логика плеера** | `tg.ready()/expand()`, `PlayerState` (queue, currentIndex, isShuffle, repeatMode), `playTrack/togglePlay/next/prev`, автопереход по `onended`, volume → `localStorage`, `navigator.mediaSession`, `HapticFeedback` | headless Chrome CDP — без исключений |
| **6. Бот + entry point** | `bot/handlers.py`: `/start` с inline-кнопкой «🎧 Открыть плеер» (`WebAppInfo`) и `MenuButtonWebApp`; `main.py`: FastAPI + aiogram в одном event loop, polling фоновой задачей, `uvicorn.run` на 0.0.0.0; README с инструкцией туннелей (cloudflared / ngrok) | 49 тестов + smoke |

**Хотфикс того же дня (реальный поиск):** удалены демо-треки `demo-1…3`; стартовый каталог рендерится из реальных 11-символьных YouTube-ID и заменяется живым `GET /api/search?q=top+hits` (debounce + Enter); состояния загрузки/пусто/ошибка; очередь сохраняет играющий трек; дополнительный лёгкий `GET /health` (без обращения к БД) рядом с полным `/api/health`.

### День 1, вечер — коммит `02e8c2e` «Fix local probe spam and stale tunnel URL handling»

- 🔍 **Новый `diagnose.py`** (≈320 строк) — CLI-диагностика: `.env`, URL, `BOT_TOKEN`, checkout сети.
- Погашен спам access-log от локальных проберов: `_QuietAccessLogFilter` в `main.py` скрывает uvicorn access log для `/health` и `/v1/models`.
- Устаревший URL туннеля: `config.get_webapp_url()` перечитывает `.env` на каждый вызов (адрес tunnel меняется между рестартами — кэш ломал кнопку Mini App); бот-роутер принимает **callable** источник URL и резолвит его на каждый `/start` (`build_player_url` добавляет `/app` и требует `https://`).
- Команда `set_my_commands` перенесена в runtime; тесты `test_bot.py`/`test_config.py` доработаны.

### День 2–3, ночь — коммит `c361620` «Add /api/trending endpoint, spinner-only catalog start, hidden mini-player»

- ➕ **Эндпоинт `GET /api/trending?limit=…`** и вся кормовая база трендов в `audio_service.py` (~300 строк).
- **Переключение трендов на русские харты**: источник №1 — чарт Яндекс Музыки (`music.yandex.ru/handlers/chart.jsx?what=chart&lang=ru`), №2 — топ RU-плейлистов Deezer (мгновенный JSON, без резолвов), №3 —fallback: 20 RU-запросов «один хит — один артист» (`TRENDING_FALLBACK_QUERIES`), резолв каждого в YouTube через yt-dlp.
- **Чистка мусора**: длительности 90–330 c (`TRENDING_MIN/MAX_TRACK_SECONDS`), стоп-регулярка `TITLE_EXCLUDE_RE` для миксов/компиляций/live/countdown/top-чартов — **и на английском, и на русском** («микс», «сборник», «топ 10», «обзор», «подкаст» …), diversity cap «≤ 2 трека на артиста», merge по `track_id`.
- Фронтенд: старт каталога — **spinner-only** с тихим fallback (loading → trend list → search), mini-плеер скрыт до первого воспроизведения.
- +296 строк тестов трендов, +65 в API-тестах.

### День 3 — коммит `79eb95f` «Phase 1 hardening: SWR trending cache, stream retries, volume race fix»

Этап 1 — hardening устойчивости (см. детально в Части II):

- кэш трендов: **single-flight + stale-while-revalidate + cooldown** после пустого refresh;
- **фоновый прогрев** трендов в боевом lifespan (`trending-warmup` task);
- таймауты на все блокирующие yt-dlp/httpx вызовы (30/20/20/30/45 с);
- **ретраи протухших стримов**: свежий ре-resolve на upstream 401/403/410 и на таймауте resolve; 416 — без ретрая;
- **защита от гонки громкости** CloudStorage / localStorage (`setVolume(source)`, `lastUserVolumeChange`);
- поллинг бота — цикл с экспоненциальным backoff 1→2→…→30 c;
- попутно найден и исправлен продуктовый баг `'None'`-артистов, отравлявший diversity cap;
- 7 устаревших тестов трендов переписаны, новые SWR/cooldown регрессии → **67 passed** (было 58 + 7 падавших).

### День 3 — коммит `377a242` «Phase 2: shared httpx stream client + Telegram initData validation» *(текущее состояние)*

- **Общий `httpx.AsyncClient`** для стримов: модульный `_stream_client` + lock, ленивое создание, идемпотентное `close_stream_client()` в обоих lifespan'ах; body-генератор не закрывает клиент → переиспользование TCP/TLS-коннектов и безопасные ретраи.
- **Строгая HMAC-валидация Telegram initData** (новый `backend/telegram_auth.py`, 130 строк): официальный алгоритм — секрет `HMAC_SHA256(bot_token, "WebAppData")`, сортированный check-string из декодированных `parse_qsl`-полей без `hash`, `hmac.compare_digest`, свежесть `auth_date` 24 ч (skew 300 с), результат `InitDataResult(user_id, reason)` — никогда не бросает исключений.
- **API-интеграция**: заголовок `Authorization: tma <initData>`, единый `_resolve_user_id()`: прод (`BOT_TOKEN` + `AUTH_MODE=auto`) → строго 401 без валидной подписи; дев/тесты → fallback на `user_id` (422 при отсутствии).
- Frontend: хелпер `apiHeaders()` во всех favorites-запросах.
- `config.get_bot_token()` / `get_auth_mode()` — перечитывание `.env` (токен ротируется).
- Тестовый каркас: `conftest.py` глушит `get_bot_token` (реальный `.env` не плавит тесты); +12 юнитов telegram_auth, +2 строгих API-сценария, +2 shared-client теста → **82 passed**.

### День 4 — коммит этапов 3–4 «Phase 3: frontend resilience + MenuButton/infra hardening»

Выполнены сразу два этапа бэклога (фронтенд + бот/инфраструктура; продуктовые фичи этапа 5 сознательно не тронуты — остались в `TODO.md`):

- **Этап 3.2 — AbortController для каталога**: один контроллер на активный запрос (`/api/search` ↔ `/api/trending`); новый запрос отменяет предыдущий (быстрый набор и быстрая смена табов больше не оставляют yt-dlp-запросы «висеть» на бэкенде). `AbortError` тихо игнорируется, реальные ошибки сети — как раньше. `restorePopular()` (очистка поля) тоже инвалидирует in-flight ответ, чтобы тот не перезатёр популярное. Тот же механизм добавлен `loadFavorites({force:true})` (своя «свежесть» через requestId остаются).
- **Этап 3.1 — reconciliation favorites**: ответы списка, прилетевшие при in-flight лайках или устаревшей ревизии, больше не выбрасываются навсегда — ставится флаг `favoritesCatchUpPending`, и в `finally` последнего лайка (`reconcileFavorites()`) выполняется тихий догоняющий перезапрос (`quiet`-режим: без спиннера и без мигания ошибок). Правило сохранено: in-flight и довнённые лайки всегда побеждают догоняющий ответ.
- **Этап 3.3 — persist состояния плеера**: ключ `tg-music-player-state`, localStorage + зеркало в Telegram CloudStorage (`version`, `queue` ≤ 200 треков, `currentIndex`, `trackId`, `isShuffle`, `repeatMode`, `position`, `savedAt`). Сохранение: throttle 5 с на `timeupdate` + force на pause/смене трека/queue/shuffle/repeat + flush на `pagehide`/`visibilitychange`. Восстановление «тихое»: на паузе, без `audio.src`; позиция применяется по `loadedmetadata` (`pendingSeekSeconds` с клампом к длительности), резюм привязан к `trackId` (`pendingResume`). Гонку громкости расширили на всё состояние: `lastUserPlaybackAction` и `savedAt`-сравнение — поздний CloudStorage-callback не затирает живую сессию; очередь выживает при первом рендере трендов (`preserveCurrent` с учётом `pendingResume`).
- **Этап 3.4** — аудит `apiHeaders()`: все authenticated-вызовы (favorites GET/POST/DELETE) уже идут через `apiHeaders()`; добавлять нечего.
- **Этап 4.1 — кэш MenuButton**: `_installed_menu_urls` (chat_id → URL); `set_chat_menu_button` вызывается только при первом инсталле или смене URL (переезд туннеля), ошибка установки не кэшируется → «лечебный» ретрай на `/start` сохранён. Повторный `/start` того же URL — ноль API-вызовов.
- **Этап 4.2 — logging**: `print(...)` заменён на `logger.info` в `start_handler` (и убрано формирование `None/…` при пустом WEBAPP_URL — URL логируется только когда кнопка реально установлена).
- **Этап 4.3 — порты/WEBAPP_URL**: дефолт PORT в `config.load_settings()` → **8000** (`DEFAULT_PORT`), синхронизировано с README, `.env.example`, туннельными подсказками и `diagnose.py`; `get_webapp_url()/get_bot_token()/get_auth_mode()` сведены в один `_reread_env_value()` с приоритетом свежего `.env` над «остатком» `load_dotenv` в `os.environ` (удалённый из файла ключ больше не «воскресает» из кэша).
- **Этап 4.4 — косметика `diagnose.py`**: убран задвоенный вывод ошибок (`check_env` больше не печатает — отчёт рендерится один раз в `main`); `BOT_TOKEN` отсутствует → warning, а не FAIL (режим «только API» валиден); сверка PORT ↔ WEBAPP_URL (public-URL с чужим портом → предупреждение, localhost-URL → предупреждение); опечатки («не занять» → «не занят»).
- **Этап 4.5 — мелочи**: `pytest.ini` актуален (asyncio_mode=auto); `start-server` (serveo-хелпер на 8000) решён в пользу `.gitignore` как локальная утилита.
- **Тесты**: +12 (кэш MenuButton: без повторного инсталла/смена URL/per-chat/ретрай после ошибки; конфиг: дефолт 8000, логика свежести `.env`) → **94 passed**.

### День 4–5 — этап 5 «Продуктовые фичи Phase 2: плейлисты + история поиска» *(текущее состояние)*

Все новые маршруты через существующий `_resolve_user_id` (Telegram initData + fallback в dev), все фронтовые вызовы — через `apiHeaders()`. Заглушка `#library-hint` («появятся в следующих обновлениях») удалена.

- **История поиска (backend)**:
  - `/api/search` теперь принимает опциональный `user_id` и **best-effort сохраняет успешные запросы** (`_resolve_optional_user_id` — никогда не роняет публичный поиск; в strict-режиме невалидный initData ⇒ сайд-эффект просто пропускается, в dev — fallback id);
  - дедупликация подряд идущих одинаковых запросов (сравнение с последней записью) + нормализация пробелов; падение шага истории не влияет на ответ поиска;
  - `GET /api/search-history?limit=10..50` — последние запросы (лимит дополнительно клампится `MAX_SEARCH_HISTORY=50`); `DELETE /api/search-history` — очистить всё или одну строку по `query` (`db.delete_search_query`, удаление возвращает счётчик).
- **Плейлисты (backend)**: `POST /api/playlists` (`PlaylistCreate`, title ≤ 100), `GET /api/playlists` (с `track_count`), `DELETE /api/playlists/{id}` (404, если не владелец — треки умирают по FK CASCADE), `GET/POST /api/playlists/{id}/tracks` (`FavoriteCreate` + `_favorite_track` — та же нормализация трека, добавление в конец с авто-`position`, idempotent `added`), `DELETE /api/playlists/{id}/tracks/{track_id}` (404 для чужого плейлиста; `db.playlist_owned` — публичный обёрточный check). Пустой плейлист/чужой/несуществующий ⇒ 404, валидация payload'ов ⇒ 422.
- **Frontend — история поиска**: блок чипсов под поисковой строкой; на фокусе пустого поля — догрузка `/api/search-history` и показ чипсов (клик по чипсу → мгновенный `performSearch`); дедуп на клиенте (первая позиция), кнопка «Очистить» → DELETE; скрытие по blur/вводу/Escape/клику вне.
- **Frontend — плейлисты**: в «Моей музыке» — переключатель «Избранное / Плейлисты» (использует стили `.tabs`), тулбар «+ Создать плейлист» (название через `prompt()` с дефолтом «Новый плейлист»), карточки плейлистов (плей/удаление), отдельный вид треков плейлиста с кнопкой «‹ Назад» и «▶ Весь плейлист» (очередь через `setQueue(tracks, 0)`); кнопка «＋» (`list-plus`) на каждой карточке трека и в шапке полноэкранного плеера (`#full-add-playlist`, дизейблится без играющего трека) открывает **шит «Добавить в плейлист»** со списком плейлистов и «+ Новый плейлист» (создаёт и сразу добавляет трек); статусы на элементе — «Добавлено ✓» / «Уже в плейлисте» / «Ошибка», авто-закрытие 0.7 c; локальные счётчики обновляются оптимистично, при возвращении к списку — тихий refresh. Плейлисты грузятся лениво (первое открытие вкладки/шита).
- **CSS**: `search-history` (чипсы, clear), `.button--compact`, `playlists-list/playlist-card`, `playlist-tracks-header`, `playlist-menu` (backdrop + sheet), `.track-card__add`, `.full-player__actions`; удалён `.library-hint`.
- **Тесты**: +6 в `tests/test_api.py` — автозапись истории (вкл. дедуп, отсутствие записи при 502/анониме), user-scoping и удаление (точечно/целиком), CRUD плейлистов с изоляцией чужих (404 вместо 403 — не раскрываем существование), валидации и «плейлист не найден», strict initData-режим (401 без подписи, изоляция подписанных пользователей), а также проверка новых строк фронтенда (`playlists-list`, `search-history-chips`, `playlist-menu`, `full-add-playlist` в `index.html`; `/api/playlists` и `/api/search-history` в `app.js`; новые классы в `style.css`; `library-hint` удалён) → **100 passed**.

---

### День 5 — этап 6.5 «Комплекс исправлений «Мой волны»» *(текущее состояние)*

Пять продуктовых проблем фичи «Моя волна» исправлены одним пассажем; **тесты: 105 passed** (было 100, +5 новых в `tests/test_api.py`).

1. **Баг 422 «По моим трекам»/истории поиска — `frontend/app.js` + `backend/api.py`**
   `authenticatedUrl(path)` всегда приклеивал `?user_id=…` через знак `?`, поэтому `authenticatedUrl('/api/wave/personalized?limit=18')` и `authenticatedUrl('/api/search-history?limit=10')` строили битые URL вида `?limit=18?user_id=…`; FastAPI парсил `limit="18?user_id=…"` как число ⇒ `422 Unprocessable Content`. Теперь разделитель выбирается по наличию `?` в path (`?` либо `&`). Дополнительно `/api/wave/personalized` сделан мягким на уровне API: `user_id` — строковый Query, парсится новым `_soft_user_id()` (пустой/битый/≤0 ⇒ `None` без 422), резолв через `_resolve_optional_user_id`; при `None` favorites считаются `[]` и запрос никогда не падает (graceful fallback на тренды остался).
2. **«Спокойный» выдавал 1–3 одних и тех же трека — `backend/audio_service.py`**
   Причина: 3 генерические фразы («lofi hip hop chill beats», «ambient relaxing music») водили YouTube на часовые стримы/миксы, которые вычищал `_is_wave_track()` (окно 60–480 c), а пул запросов всегда был одним и тем же. `WAVE_MOOD_QUERIES` расширен с 3 до 27–30 **точечных** запросов (конкретные артисты/сингловые треки) на каждый вайб: calm — Øneheart/Idealism/Kupla/Kudasai/potsu/Nujabes/Petit Biscuit/Tycho/C418/Einaudi/Beach House/Men I Trust/Clairo/Bon Iver…; energetic — Dua Lipa/The Weeknd/Calvin Harris/Zivert/ANNA ASTI/Daft Punk/Fred again/Peggy Gou…; aggressive — Kordhell/DVRST/INTERWORLD/Hensonn/zxcursed/MoonDeity/Slaughter To Prevail…; sad — Три дня дождя/Джизус/Земфира/Молчат Дома/Joji/Billie Eilish/Radiohead/Arctic Monkeys/Lil Peep… `get_wave_by_mood` при каждом вызове перемешивает пул, берёт случайный сэмпл 7 запросов (`WAVE_QUERY_BATCH_SIZE`), shuffle результатов, а при недоборе автоматически добирает следующими случайными пачками по 6 (`WAVE_TOPUP_BATCH_SIZE`) из оставшегося пула до полных `limit`.
3. **Персональная волна — `build_taste_profile` / `get_wave_personalized`**
   `build_taste_profile(..., max_artists=8, sample_size=5)`: топ-8 артистов, при каждом вызове случайно выбирается до 5, суффиксы чередуются («official audio»/«track»/«популярные треки») и итог перемешивается (детерминированный режим сохранён для юнитов). При малом/нулевом числе лайков или короткой выдаче остаток добирается до `limit` случайными выборками запросов из `TRENDING_FALLBACK_QUERIES` + всех пулов `WAVE_MOOD_QUERIES` (bounded `WAVE_TOPUP_MAX_ROUNDS`), с попутным shuffle — «По моим трекам» всегда отдаёт полные 18 неповторяющихся треков.
4. **Автозапуск при клике на чипсы настроения — `frontend/app.js`**
   Клик по `.wave-chip` теперь только меняет `waveVibe`/выделение/`haptic()` (запросов к серверу нет!); сборка волны — только кнопки `#wave-start-mood`/`#wave-start-personal`. `runWave()` блокирует кнопки (disabled + «Собираем волну…») и в `finally` гарантированно возвращает исходное состояние; получив tracks, сразу `setQueue(tracks, 0)` и автостарт с первого трека (`queueSource = "wave"`).
5. **Бесконечная подгрузка вместо зацикливания — `frontend/app.js`**
   Введены `queueSource` ("catalog" — поиск/популярное/библиотека/плейлисты, прежнее поведение `next = 0`; "wave" — «Моя волна») и `waveSource` ("mood"/"personal" для выбора URL догрузки). В `nextTrack()` для волны на границе очереди вместо `next = 0` вызывается `appendMoreWaveTracks()`: фоновой запрос к текущему источнику волны, фильтр треков, уже лежащих в очереди, догрузка в конец (потолок 120 треков, `WAVE_MAX_QUEUE_TRACKS`), дорисовка новых карточек в конец `#wave-list` и обновление счётчика; конкурентные вызовы шарят один in-flight promise (`waveAppending`/`wavePendingAppend`), ошибки — тихо логируются с fallback на цикл. Предвыборка стартует заранее в `playTrack()`, когда включается предпоследний/последний трек волны (`index >= length - 2`).
6. **Тесты**: `tests/test_api.py` +5 — `/api/wave/mood` (передача vibe/limit в сервис, 422 на неизвестный вайб, 502 при AudioServiceError), `/api/wave/personalized` (favorites уходят в сервис 200, пустая персоналка ⇒ fallback трендов, мягкий user_id: None/""/битый/0/-5 никогда не 422/401, strict-initData: unsigned-запрос не роняется), юнит рандомизации (пул ≥ 25 запросов на вайб, ровно 7 запросов за прогон, разные сиды — разные сэмплы, shuffle выдачи). Попутно зафиксирован сорный флейк `test_trending_stage1_maps_yandex_chart_to_youtube_ids` в `tests/test_audio_service.py`: production-шаг «короткий чарт-пул перемешивается» на пуле из 2 кандидатов давал 50/50 порядок; shuffle pinнут к identity внутри теста, чтобы ассерты зависели от логики, а не от RNG. Прогон `pytest tests -q` ×4 → стабильно 105 passed.

---

## 🏗 Часть II. Структурная спецификация (что именно сделано на сегодня)

### 2.1. `main.py` — единая точка входа
- `Settings` из `config.load_settings()`; бот в одном event loop с API (`Dispatcher` + `handle_signals=False`, `close_bot_session=False` — uvicorn владеет сигналами).
- **Боевой lifespan**: `init_db` → прогрев трендов (`get_trending_tracks(20)`, задача `trending-warmup`) → запуск `_run_bot_polling`; корректное завершение в `finally` через `asyncio.gather(..., return_exceptions=True)`: отмена задач, `bot.session.close()`, `close_stream_client()`, `close_db()`.
- `_print_player_url()` — терпимый к cp1251/cp866 вывод локального URL (`http://127.0.0.1:{PORT}/app/`).
- Предупреждения: `WEBAPP_URL` не https → кнопка не будет работать; токена нет → режим «только API».
- Логирование: `logging.basicConfig` + фильтр тихих путей (`/health`, `/v1/models`).

### 2.2. `backend/api.py` — HTTP-слой
| Эндпоинт | Поведение |
|----------|-----------|
| `GET /api/health` | Статус БД |
| `GET /health` | Лёгкий `{"status":"ok"}` (uptime-мониторы) |
| `GET /api/search?q&limit` | Поиск через yt-dlp, `limit` 1–50 |
| `GET /api/trending?limit` | Кэш трендов (SWR, см. 2.3) |
| `GET /api/stream/{id}` | Стрим-прокси с `Range`; ретраи 401/403/410 + таймаут resolve; 416 — сразу |
| `GET/POST/DELETE /api/favorites` | Изоляция по user_id; `_resolve_user_id`: заголовок `Authorization: tma` → 401 без валидной подписи в проде, fallback `user_id` (422) в дев |
| `GET /api/search?…&user_id` | Автоматическое сохранение успешных запросов в историю (best-effort, `_resolve_optional_user_id`) |
| `GET/DELETE /api/search-history` | Последние запросы (≤ `MAX_SEARCH_HISTORY`), очистка всего или по одному `query` |
| `POST/GET/DELETE /api/playlists` | CRUD плейлистов с `track_count`; чужой/несуществующий ⇒ 404 |
| `GET/POST/DELETE /api/playlists/{id}/tracks…` | Треки плейлиста; добавление в конец с `position`, idempotent; strict-initData как везде |
| `/app/*`, `/` | Статика Mini App, redirect `/` → `/app/` |

- `default_lifespan` для standalone/тестов: тоже поднимает БД, прогревает тренды и **закрывает shared-клиент стримов**.

### 2.3. `backend/audio_service.py` — медиа-ядро (крупнейший модуль)
- **Поиск**: `ytsearch:` через yt-dlp в `asyncio.to_thread`, таймаут 30 с, pool 30, фильтры `MIN/MAX_TRACK_SECONDS` (45–900) + `TITLE_EXCLUDE_RE`.
- **Стрим-прокси**: chunk-генератор 64 КБ, нормализация MIME (`video/mp4` → `audio/mp4`), фильтрация `_FORWARDED_HEADERS`, `STREAM_TIMEOUT` (connect 10 / read 60 c).
- **Shared-клиент**: `_stream_client` + `_stream_client_lock`; `_get_stream_client()` ленивый с проверкой `is_closed`; `close_stream_client()` идемпотентный; body-генератор закрывает только upstream response, не клиент.
- **Тренды ( Trophy RU-источник)**: чарт Яндекса → Deezer-плейлисты → 20 fallback-запросов; резолв каждого кандидата в YouTube-ID через семафор (`TRENDING_RESOLVE_POOL_SIZE = 8`), merge по `track_id`, чистка длительностей/заголовков/diversity.
- **Кэш трендов — SWR-архитектура** (вся сеть вне lock'а):
  - свежий кэш (TTL 30 мин) → ответ без сети;
  - stale → мгновенный отдача stale + один фоновый refresh (`TRENDING_REFRESH_TASK`, single-flight под lock'ом);
  - холодный старт → ограниченное ожидание `TRENDING_COLD_WAIT_SECONDS = 15` через `asyncio.shield` (refresh доживает в фоне);
  - пустой refresh → 60-секундный cooldown (`TRENDING_EMPTY_RETRY_COOLDOWN_SECONDS`, поле `empty_at` в `TRENDING_CACHE`).
- **Таймауты** всех этапов: поиска 30 с, резолва стрима 20 с, чарта 20 с, одного трека 30 с, fallback 45 с.

### 2.4. `backend/telegram_auth.py` — валидация initData
- Официальная схема Telegram: секрет `HMAC_SHA256(bot_token, key="WebAppData")`; check-string из декодированных `parse_qsl(init_data, keep_blank_values=True)` полей, отсортированных, присоединённых `\n`; исключается только `hash` (Ed25519 `signature` участвует как обычное поле); сравнение `hmac.compare_digest`.
- Свежесть `auth_date` ≤ 24 ч (`MAX_AUTH_AGE_SECONDS`), clock-skew допуск 300 с (`MAX_CLOCK_SKEW_SECONDS`), дата из будущего дальше 300 с отклоняется.
- `validate_init_data() → InitDataResult(user_id, reason)` (dataclass; `.ok`), никогда не бросает исключений; полезная нагрузка `user.id` парсится строго.

### 2.5. `backend/db.py` — хранилище
- Единственный aiosqlite-коннект + `asyncio.Lock`, `:memory:` поддержан для тестов.
- PRAGMA: `foreign_keys ON`, `busy_timeout 5000`, `journal_mode WAL`.
- Схема (создаётся при каждом старте):
  - `favorites` — используется (UNIQUE `user_id+track_id`, индексы, метаданные трека);
  - `playlists`, `playlist_tracks` (позиция, FK CASCADE) — используются с этапа 5 (CRUD плейлистов, add/list/remove треков, `playlist_owned`);
  - `search_history` — используется с этапа 5 (`MAX_SEARCH_HISTORY = 50`, add/list/clear + decline по одному запросу `delete_search_query`).
- API-функции: `add_favorite/remove_favorite/get_favorites` (+ строгая нормализация трека в `_track_values`), `init_db/close_db`.

### 2.6. `bot/handlers.py` — Telegram-бот
- `/start`: HTML-приветствие с именем пользователя, inline-кнопка «🎧 Открыть плеер» (`WebAppInfo`), `LinkPreviewOptions(is_disabled=True)`.
- `build_player_url()`: добавляет `/app`, отвергает не-HTTPS (Telegram не примет).
- `configure_menu_button()`: установка `MenuButtonWebApp` (чек-кнопка «Плеер» слева от ввода), для чата или глобально; ошибки логируются, никогда не роняют поллинг. **Кэш `_installed_menu_urls` (chat_id → URL)**: API-вызов только при первом инсталле или смене URL; сбой не кэшируется → ретрай на следующем `/start`; `reset_menu_button_cache()` для тестов.
- URL — через callable, перечитывающий `.env` (`_current_webapp_url`); все сообщения — через `logging` (без `print`).

### 2.7. `frontend/` — Mini App (без сборщика)
- `index.html`: 3 зоны (поиск + чипсы истории + вкладки «Поиск»/«Моя музыка»; карточки треков; мини- + полноэкранный плеер); в библиотеке — переключатель «Избранное / Плейлисты», вид треков плейлиста, шит «Добавить в плейлист».
- `style.css`: тема на Telegram-переменных, скрытие mini-плеера до первого play, спиннер каталога, стили чипсов истории/плейлистов/шита.
- `app.js` (≈2900 строк):
  - `tg.ready()/expand()`, стар каталог — spinner, затем тренды;
  - поиск с debounce 450 мс + Enter, состояния loading/пусто/ошибка; **AbortController** — новый запрос (`/api/search`, `/api/trending`) отменяет предыдущий, `AbortError` тихо игнорируется, очистка поля также инвалидирует in-flight ответ;
  - `PlayerState` (queue, currentIndex, shuffle, repeat none/all/one), autoplay-безопасность, `mediaSession`, haptics;
  - **громкость с защитой от гонки**: `setVolume(…, {source})`, `lastUserVolumeChange`, `restoreCloudVolume()` игнорирует in-flight ответ Telegram CloudStorage, если пользователь ужé изменил громкость; persist: `localStorage` (`tg-music-volume`) + Telegram CloudStorage как durable-зеркало;
  - **persist состояния плеера** (`tg-music-player-state`, localStorage + CloudStorage): очередь (≤200), `currentIndex`/`trackId`, позиция (throttle 5 с + pause + pagehide), shuffle/repeat; тихое восстановление на паузе, позиция — по `loadedmetadata`, резюм только для трек-совпадения (`pendingResume`/`pendingSeekSeconds`); гонка гасится `lastUserPlaybackAction` + `savedAt`-сравнением; очередь выживает при первом рендере трендов;
  - лайки с оптимистичным UI и ревизиями (`pendingLikeRequests` / `favoritesRevision`); **reconciliation**: догоняющий перезапрос после опустошения in-flight лайков (`reconcileFavorites()`), quiet-режим без мигания UI;
  - **история поиска** (этап 5): чипсы последних запросов на фокусе пустого поля, клик — мгновенный поиск, «Очистить»; автозапись успешных запросов (`user_id` + `apiHeaders()` в `/api/search`);
  - **плейлисты** (этап 5): вкладка в библиотеке, создание (prompt с дефолтом), удаление, просмотр треков с запуском очереди из плейлиста (`setQueue(tracks, 0)`), шит «Добавить в плейлист» с «＋» на карточках треков и в полноэкранном плеере; ленивая загрузка и тихий refresh счётчиков;
  - `apiHeaders()` — `Authorization: tma <WebApp.initData>` для всех authenticated-вызовов (favorites, search, playlists, search-history).

### 2.8. `config.py` — конфигурация
- `load_settings()` — frozen dataclass: `BOT_TOKEN`, `WEBAPP_URL`, `HOST/PORT` (default **8000** через `DEFAULT_PORT`, диапазон валидируется), `DB_PATH` (по умолчанию `data/music.db`), `LOG_LEVEL`; `bot_enabled` = токен с двоеточием.
- `get_webapp_url()` / `get_bot_token()` / `get_auth_mode()` — сведены в единый `_reread_env_value()`: **перечитывают `.env` на каждый вызов**; свежее значение файла всегда приоритетнее «остатка» `load_dotenv` в `os.environ` (удалённый из `.env` ключ не «воскресает»); `AUTH_MODE`: `auto` (по умолчанию) | `off`.

### 2.9. Тестовый каркас (`tests/`, pytest.ini, 100 passed)
| Файл | Покрытие |
|------|----------|
| `test_db.py` | CRUD favorites, WAL/память, нормализация треков |
| `test_phase1_db_api.py` | Интеграция БД ↔ API |
| `test_audio_service.py` | Поиск, стрим-прокси/Range/shared-клиент, тренды Yandex/Deezer (моки: `FakeChartClient`, `MultiSearchYDL`), SWR/cooldown регрессии |
| `test_api.py` | Search/stream/favorites, строгий initData-режим (401/подделка/изоляция/CRUD), `AUTH_MODE=off`, **плейлисты CRUD + изоляция + strict-режим**, **история поиска (автозапись, дедуп, scoping, удаление)**, структурный smoke фронтенда этапа 5 |
| `test_telegram_auth.py` | 12 юнитов: валидная подпись, tampered, чужой токен, expired/future auth_date, отсутствие hash/user/данных, несортированная check-string → отказ |
| `test_bot.py` | `/app`-URL, inline- и menu-кнопки, callable-URL, **кэш MenuButton** (без повторного инсталла, смена URL, per-chat изоляция, ретрай после сбоя) |
| `test_config.py` | `.env`, порт (**дефолт 8000**), AUTH_MODE, **свежесть `.env` vs `os.environ`** (удалённый ключ не «воскресает») |
| `conftest.py` | Autouse-глушение `get_bot_token` (реальный `.env` не плавит тесты); фикстура сброса shared-клиента; сброс кэша MenuButton — autouse-фикстура `fresh_menu_button_cache` в `test_bot.py` |

### 2.10. Инфраструктура и утилиты
- `requirements.txt` / `requirements-dev.txt` (pytest, aiosqlite-инструменты), `pytest.ini` (asyncio_mode=auto — актуально), `.env.example` (PORT=8000), `.gitignore` (+ `start-server` как локальный serveo-хелпер).
- `diagnose.py` — CLI-диагностика окружения и сети: `.env` (PORT ↔ WEBAPP_URL cross-check), ошибка не задваивается, `BOT_TOKEN` отсутствует → предупреждение (режим «только API» валиден); `print` осознанно остаётся CLI-выводом вне logging-потока.
- Hotfix-паттерн фиксации багов: попутные продуктовые баги исправались за пределами milestone-коммитов (см. Часть III).

---

## 🐞 Часть III. Баги, найденные и исправленные по ходу

1. **Демо-треки в проде** — фейковые `demo-*` ID попали в релиз, удалены хотфиксом (реальные YouTube-ID).
2. **Спам от локальных проберов** — uvicorn access log заливал консоль; фильтр `_QuietAccessLogFilter` на `/health`, `/v1/models`.
3. **Протухший URL туннеля** — `load_dotenv` кэшировал `WEBAPP_URL`; `/start` выдавал битую кнопку после переезда туннеля → перечитывание `.env` на каждом вызове.
4. **Deadlock тренд-кэша** — lock удерживался на всю сетевую цепочку (полсотни секунд) → очередь на `/api/trending` и таймауты туннеля → SWR-рефакторинг.
5. **`str(None)`-артисты** — отсутствующий uploader превращался в строку `'None'`; diversity cap после двух треков молча выбрасывал весь остальной fallback-список → `or ""` + `.strip()`.
6. **Гонка громкости** — асинхронный `CloudStorage.getItem` отрабатывал позже ручного изменения и «стирал» слайдер → `lastUserVolumeChange`-гейт.
7. **Смертельный поллинг бота** — aiogram выходил после сетевого сбоя, бот исчезал навсегда → backoff-цикл.
8. **Незакрытый coroutine** — `close_stream_client()` в `default_lifespan` вызывался без `await` (warning «coroutine was never awaited»).
9. **Блокирующая сеть без таймаутов** — зависший upstream занимал worker thread вечность → таймауты на 5 классов вызовов.

---

## ⚖️ Часть IV. Осознанные компромиссы (не баги)

- Вне Telegram (браузер) все пользователи — общий `FALLBACK_USER_ID = 123456`; прод автоматически строгий при наличии `BOT_TOKEN`.
- `AUTH_MODE`: только `auto | off` (промежуточных градаций нет).
- `initData`-заголовок frontend'ом не отправляется на `/api/stream` и не проверяется backend'ом — `/api/stream` ничего не персистит per-user, подделка не даёт доступа к чужим данным.
- Файл `start-server` (serveo-хелпер) — untracked и намеренно не закоммичен.
- `diagnose.py` использует `print` как CLI-инструмент — осознанно вне logging-потока.

---

## 📌 Актуальный итог

| Метрика | Значение |
|---------|----------|
| Коммитов | 8 (`48cb340` → умная персональная волна) |
| Python-модулей в `backend/` | 4 (+ `telegram_auth.py` в Phase 2) |
| Тестов | 111 passed / 0 failed (~26 c) |
| Строк frontend (`app.js`) | ~2900 |
| Этапов аудита | 5 из 5 завершены |
| API-эндпоинтов | 16 (health×2, search, trending, stream, favorites×3, search-history×2, playlists×6, wave×2) |

## 🧠 Умная волна «По моим трекам» (блок 6.6, `backend/audio_service.py`, тесты 111 passed)
- **Умный парсинг лайков** (`_parse_favorite_item` + `_clean_favorite_text`): настоящий исполнитель извлекается из левой части `"Artist - Song"` (разделители `-`/`–`/`—`) — поле `artist` YouTube часто содержит канал/лейбл («Hajime», «zardy»), который сохраняется как дополнительный; YouTube-мусор (`- Topic`, `VEVO`, `Official Audio/Video/Music Video`, `Lyrics`, `(...)/[...]`, `**`, невидимые символы) вычищается; вайб определяется по стилевым словам исходных `title`/`artist` (slowed/reverb/phonk/drift/trap/… → aggressive; lofi/ambient/chill/piano/… → calm; sad/doomer/пост-панк/… → sad; dance/remix/club/house/… → energetic).
- **Жёсткий фильтр перезаливов** (`_normalize_song_title` + `_merge_wave_results(..., favorite_titles=...)`): нормализатор названия (нижний регистр, без скобок/знаков/пробелов) собирается по всем лайкам, и кандидат с YouTube отбрасывается не только по `track_id`, но и при совпадении/вложении нормализованного названия с лайкнутым — перезаливы с другим ID больше не попадают в волну.
- **Deezer-рекомендации** (`_discover_similar_tracks_sync`, httpx 8 с, `asyncio.to_thread` + внешний таймаут 10 с): для топ-5 любимых артистов — search/artist → топ треков артиста (минус лайкнутые названия, максимум 1–2) → related-артисты (до 3) с их популярными треками; результат в формате `deezer:{id}` «{artist} - {title}». По лайку Miyagi подтягиваются Эндшпиль/Скриптонит/Kizaru и т.п.
- **Сборка `get_wave_personalized`**: (1) профиль вкуса (`build_taste_profile`, sample_size=5) + «{artist} popular track»/«{artist} feat» для топ-3 артистов + стилевые запросы `VIBE_STYLE_QUERIES[detected_vibe]` («best slowed reverb phonk track», «dark trap slowed audio» и т.д.); (2) Deezer-дискавери; (3) добор до `limit` строго из `WAVE_MOOD_QUERIES[detected_vibe]` — при наличии лайков `TRENDING_FALLBACK_QUERIES` не используется вовсе; при пустых лайках — микс всех mood-пулов + тренды. Вайб волны = мажоритарный голос по всем лайкам, ничья решается в пользу самого специфичного вайба (aggressive > sad > calm > energetic). В `_merge_wave_results` сохранён diversity cap ≤ 2 трека на артиста.
- **Тесты**: +4 юнита в `tests/test_api.py` — стилевые aggressive-запросы по phonk-лайкам + запрет попса-добора + лайкнутые ID не возвращаются; перезалив с другим `track_id`, но тем же названием отфильтрован, новый трек проходит; пустые лайки → микс всех mood-пулов и `TRENDING_FALLBACK_QUERIES`; `_discover_similar_tracks_sync` с мок-клиентом httpx (лайкнутое название пропущено, другой топ-трек найден). `pytest tests -q` → **111 passed, 0 failed** (прогон ×2).
