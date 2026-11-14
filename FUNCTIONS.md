# Описание функций проекта TG Music Mini App

Полный справочник по всем функциям каждого файла проекта: что делает функция, какие параметры принимает и что возвращает.

## Структура проекта

```
main.py                  — точка входа: запуск FastAPI + Telegram-бота
config.py                — загрузка настроек из .env
diagnose.py              — автономная диагностика развёртывания
backend/
  api.py                 — FastAPI-приложение (REST API)
  audio_service.py       — поиск музыки, тренды, «волна», стрим-прокси, кэш
  db.py                  — асинхронная SQLite-база (избранное, история, плейлисты)
  telegram_auth.py       — проверка подписи Telegram initData
bot/
  handlers.py            — хендлеры бота: /start, кнопка Mini App
frontend/
  app.js                 — вся клиентская логика мини-приложения
```

Каждый раздел ниже соответствует одному файлу — заголовок раздела содержит путь к файлу. В конце документа есть **алфавитный указатель**, где сразу видно, в каком файле находится любая функция.

---

## main.py

Точка входа: поднимает FastAPI-сервер и фоновый long-polling Telegram-бота в одном процессе.

| Функция | Описание |
|---|---|
| `create_bot(token)` | Создаёт экземпляр `aiogram.Bot` с режимом разбора сообщений HTML по умолчанию. |
| `create_dispatcher(webapp_url=None)` | Создаёт `Dispatcher` aiogram и подключает к нему роутер `/start` из `bot.handlers`. Если URL не передан, он берётся из `get_webapp_url()`. |
| `sync_menu_button(bot, webapp_url=None)` | Устанавливает глобальную кнопку меню `MenuButtonWebApp` (кнопка «Плеер» рядом с полем ввода) через `configure_menu_button`. |
| `class _QuietProbeFilter` | Фильтр логов: отбрасывает access-логи для частых health-проб (`/health`, `/api/health`), чтобы не засорять журнал. Метод `filter(record)` возвращает `False` для записей о GET/HEAD к этим путям. |
| `_watch_menu_button(bot)` | Бесконечный фоновый цикл: каждые 30 секунд перечитывает `WEBAPP_URL` из `.env` и переустанавливает кнопку меню, если она изменилась (например, туннель переехал). Ошибки подавляются. |
| `_run_bot_polling(settings)` | Запускает polling бота в фоне. Если `BOT_TOKEN` не задан — просто пишет предупреждение и выходит. Иначе: создаёт диспетчер, синхронизирует кнопку меню, удаляет webhook, стартует `start_polling`. При любой ошибке логирует её и повторяет с экспоненциальной задержкой (2 с → 60 с), не роняя FastAPI. В `finally` закрывает watcher меню и HTTP-сессию бота. |
| `build_lifespan(settings)` | Возвращает FastAPI-lifespan: при старте подключает SQLite (`Database.connect`), сохраняет её в `app.state.db`, запускает прогрев трендов (`get_trending_tracks(20)`) и задачу polling бота; при остановке отменяет обе задачи, закрывает стрим-клиент и базу. |
| `create_runtime_app(settings=None)` | Собирает итоговое FastAPI-приложение через `create_app`, подставляя путь к БД и общий lifespan. |
| `main()` | Загружает настройки, настраивает логирование (уровень из `LOG_LEVEL`, формат, фильтр health-проб), создаёт приложение и запускает `uvicorn.run` с параметрами хоста/порта, поддержкой прокси-заголовков и таймаутами keep-alive. |

---

## config.py

Конфигурация из переменных окружения и файла `.env`.

| Функция / класс | Описание |
|---|---|
| `_reread_env_value(key, default="")` | Перечитывает значение `key` из `.env` при каждом вызове (не кэширует), что позволяет менять настройки без перезапуска. Приоритет: значение из `.env` → если ключ объявлен в `.env`, но пуст — `default` → иначе `os.environ`. |
| `class Settings` | Неизменяемый dataclass с полями: `bot_token`, `webapp_url`, `host`, `port`, `db_path`, `log_level`, `proxy_url`. Свойство `bot_enabled` возвращает `True`, только если токен выглядит настоящим (непустой и содержит `:`). |
| `get_webapp_url()` | Возвращает актуальный `WEBAPP_URL`: обрезает пробелы, кавычки и завершающий слэш. |
| `get_bot_token()` | Возвращает актуальный `BOT_TOKEN` (аналогично перечитывается из `.env`). |
| `get_auth_mode()` | Возвращает `AUTH_MODE` как `auto` (по умолчанию) или `off`; любое другое значение приводится к `auto`. |
| `get_proxy_url()` | Возвращает URL прокси из первой непустой переменной: `YTDLP_PROXY`, `PROXY_URL`, `HTTPS_PROXY`, `HTTP_PROXY`. |
| `load_settings()` | Читает все настройки и применяет безопасные значения по умолчанию. Дополнительно: экспортирует прокси в `os.environ["YTDLP_PROXY"]`, проверяет что `PORT` — целое в диапазоне 1–65535 (иначе `ValueError`), преобразует относительный `DB_PATH` в абсолютный относительно корня проекта. |

---

## diagnose.py

Самодиагностика развёртывания (только стандартная библиотека + dotenv). Запускается как `python diagnose.py [--chat-id N]`.

| Функция / класс | Описание |
|---|---|
| `class ActionResult` | Результат одной проверки: имя, списки `errors`/`warnings`/`details`. Методы: `ok(detail)` — добавить деталь успеха; `warn(w)` — добавить предупреждение; `fail(reason)` — добавить ошибку; `render()` — строка вида `[OK] имя` или `[FAIL] имя :: причины`. |
| `_read_env()` | Читает все значения из `.env` в словарь. |
| `check_env()` | Проверка окружения: наличие `.env`, корректность `PORT` (целое 1–65535), наличие `WEBAPP_URL` и что он HTTPS. Возвращает `(порт, url, результат)`. |
| `_check_port_url_consistency(result, raw_url, port)` | Кросс-проверка URL и порта: предупреждение, если `WEBAPP_URL` указывает на localhost (Telegram не откроет Mini App) или его порт не совпадает с настроенным `PORT`. |
| `check_port_owner(port)` | Через `netstat -ano` находит PID-ы, слушающие заданный порт; если ни одного — ошибка «порт не занят». |
| `_read_process_name(pid)` | Возвращает имя исполняемого файла процесса по PID: `tasklist` на Windows, `ps` на Unix; при неудаче — `"Unknown"`. |
| `check_local_server(port)` | Проверяет, что локальный сервер отвечает на `http://127.0.0.1:{port}/app/` (таймаут 5 с). |
| `check_tunnel(webapp_url)` | Проверяет публичный URL туннеля `{WEBAPP_URL}/app/`: HTTP 200 и наличие «TG Music» в теле — успех; 200 без нужного текста — предупреждение; 404/502 — «туннель/адрес битый». |
| `check_telegram_menu(chat_id=None)` | Запрашивает у Telegram `getChatMenuButton`: без токена — предупреждение (режим «только API»); иначе проверяет, что кнопка имеет тип `web_app`, корректный URL и суффикс `/app/`. |
| `_stdout_only(lines)` | Печатает строки отчёта в stdout без лишнего шума. |
| `main()` | Парсит `--chat-id`, последовательно выполняет все пять проверок, собирает отчёт (детали и предупреждения с отступами), печатает его и возвращает 0, если ошибок нет, иначе 1. |

---

## backend/api.py

FastAPI-приложение: системные ручки, поиск, тренды, волна, стриминг, избранное, история поиска, плейлисты.

### Модели и вспомогательные функции

| Функция / класс | Описание |
|---|---|
| `class FavoriteCreate` | Pydantic-модель запроса добавления трека: `user_id` (опц.), `track_id`, `title` (обязательные), `artist`, `duration`, `thumbnail`, `url` с ограничениями длины. |
| `class PlaylistCreate` | Pydantic-модель создания плейлиста: `user_id` (опц.), `title`. |
| `_init_data_from_request(request)` | Извлекает «сырой» initData из заголовка `Authorization: tma <initData>`; возвращает пустую строку, если заголовка нет. |
| `_resolve_user_id(request, fallback_user_id)` | Обязательная авторизация: если настроен `BOT_TOKEN` и `AUTH_MODE != off` — проверяет initData через `validate_init_data`, при ошибке 401, и возвращает Telegram user id. Иначе использует `fallback_user_id` (422, если его нет). |
| `_resolve_optional_user_id(request, fallback_user_id)` | Необязательная авторизация: то же самое, но при отсутствии/некорректном initData возвращает `None`, не выбрасывая ошибку. |
| `_soft_user_id(raw_user_id)` | Мягко парсит `user_id` из query-строки: не-число или неположительное число → `None` без HTTP 422. |
| `_is_allowed_thumb_url(raw_url)` | Проверяет, что URL обложки ведёт на разрешённый CDN (`ytimg.com`, `dzcdn.net`, `deezer.com`, `ggpht.com`, `googleusercontent.com`) и имеет схему http/https — защита от SSRF. |
| `_favorite_track(payload)` | Нормализует payload избранного; если `url` не задан, строит ссылку на YouTube (`https://www.youtube.com/watch?v=...`) из `track_id`. |
| `_store_search_query(database, user_id, query)` | Сохраняет успешный поисковый запрос в историю, пропуская сразу повторяющийся (сравнивает с последней записью). |
| `_serve_audio_stream(track_id)` | Скачивает/берёт трек из дискового кэша (`get_cached_audio_file`) и отдаёт его через `FileResponse` с `Cache-Control: private, max-age=3600`; ошибки превращаются в HTTP 502. |

### `create_app(...)` — фабрика приложения и её эндпоинты

`create_app(db_path=None, frontend_dir=None, lifespan=None)` строит приложение, монтирует CORS, статику `/app` и регистрирует эндпоинты:

| Эндпоинт / обработчик | Описание |
|---|---|
| `default_lifespan(app)` | Встроенный lifespan: подключение БД, прогрев трендов, отмена prefetch-задач и закрытие ресурсов при остановке. |
| `GET /api/health` → `health(request)` | Проверка работоспособности: пингует БД; статус `ok`/`error`, версия, состояние БД. При недоступности БД — HTTP 503. |
| `GET /health` → `root_health()` | Минимальный ответ `{"status": "ok"}` для внешних мониторингов. |
| `GET /api/search` → `search(request, q, limit, user_id)` | Поиск треков через `search_tracks`; при ошибке — 502. Успешный запрос сохраняется в историю (если удалось определить пользователя). |
| `GET /api/trending` → `trending(limit)` | Возвращает список популярных треков из кэша/фонового обновления (`get_trending_tracks`). |
| `GET /api/thumb` → `thumb(u)` | Прокси обложек через бэкенд (чтобы обходить блокировки и общий прокси). Недопустимый хост — 400; ошибка загрузки — редирект 302 на оригинальный URL. Ответ кэшируется на сутки. |
| `GET /api/wave/mood` → `wave_mood(vibe, limit)` | «Волна» по настроению (`calm/energetic/aggressive/sad`) через `get_wave_by_mood`; при сбое откатывается на тренды, и только если и их нет — 502. |
| `GET /api/wave/personalized` → `wave_personalized(request, limit, user_id)` | Персональная волна: берёт избранное пользователя из БД, строит подборку `get_wave_personalized`; при пустом результате — тренды. |
| `GET /api/prefetch` → `prefetch(track_id)` | Запускает фоновую загрузку следующего трека в дисковый кэш (не более 4 одновременных задач). Клиент вызывает её, пока играет текущий трек — чтобы переключение не зависало при замороженном WebView. Возвращает `{"started": bool}`. |
| `GET /api/stream` → `stream_query(track_id)` | Отдаёт аудиофайл трека (query-параметром — безопасно для URL и двоеточий). |
| `GET /api/stream/{track_id:path}` → `stream_path(track_id)` | Обратная совместимость: тот же стрим, но путь-параметром. |
| `GET /api/favorites` → `list_favorites(request, user_id)` | Список избранного пользователя (с обязательной авторизацией). |
| `POST /api/favorites` → `add_favorite(payload, request)` | Добавляет трек в избранное; возвращает `{"added": bool, "track": ...}`. |
| `DELETE /api/favorites` → `remove_favorite_query(...)` и `DELETE /api/favorites/{track_id:path}` → `remove_favorite_path(...)` | Удаление трека из избранного (query- и path-варианты), ответ `{"removed": bool}`. |
| `GET /api/search-history` → `get_search_history(request, user_id, limit)` | Последние поисковые запросы пользователя (до 10). |
| `DELETE /api/search-history` → `delete_search_history(request, user_id, query)` | Удаляет один запрос (если `query` передан) или всю историю. |
| `GET /api/playlists` → `list_playlists(...)` | Список плейлистов пользователя с количеством треков. |
| `POST /api/playlists` → `create_playlist(payload, request)` | Создаёт плейлист; в ответ добавляет `track_count: 0`. |
| `DELETE /api/playlists/{playlist_id}` → `delete_playlist(...)` | Удаляет плейлист; 404, если не найден или не принадлежит пользователю. |
| `GET /api/playlists/{playlist_id}/tracks` → `list_playlist_tracks(...)` | Треки плейлиста по порядку позиций; 404 при отсутствии прав. |
| `POST /api/playlists/{playlist_id}/tracks` → `add_playlist_track(...)` | Добавляет трек в плейлист; 404, если плейлист чужой/несуществует. |
| `DELETE /api/playlists/{playlist_id}/tracks` → `remove_playlist_track_query(...)` и `.../tracks/{track_id:path}` → `remove_playlist_track_path(...)` | Удаление трека из плейлиста (query- и path-варианты) с проверкой владельца. |
| `GET /` и `GET /app` → `root()`, `app_slash_redirect()` | Редирект 302 на `/app/` (мини-приложение). |
| `app.mount("/app", StaticFiles(...))` | Раздача статики фронтенда (`frontend/index.html` и др.). |

---

## backend/audio_service.py

Ядро музыки: обёртка над yt-dlp, поиск, тренды, «волна», резолв прямых ссылок, дисковый кэш аудио и HTTP-прокси стримов.

### Прокси и HTTP-клиент

| Функция / класс | Описание |
|---|---|
| `_is_local_port_open(host, port)` | Проверяет, слушается ли локальный порт (например, входы Xray/Happ 10808/10809) — TCP-подключение с таймаутом 0.25 с. |
| `_detect_proxy_url()` | Определяет прокси: сначала явный из env (`YTDLP_PROXY`, `HTTPS_PROXY`, `HTTP_PROXY`, `ALL_PROXY`; значения `none/off/direct` отключают), иначе автодетект локальных портов 10808/10809/1080/2080. Результат кэшируется на 30 с. |
| `_resolve_httpx_proxy_url()` | Выбирает прокси, совместимый с httpx: предпочитает HTTP-вход 10809 (для socks5 требуется пакет `socksio`), заменяет `socks5h://` на `socks5://`. |
| `_get_stream_client()` | Общий ленивый `httpx.AsyncClient` для всех исходящих стримов/загрузок: редиректы, таймауты, прокси; при отсутствии `socksio` — фолбэк на HTTP-прокси или прямое соединение. |
| `close_stream_client()` | Закрывает общий клиент (идемпотентно), вызывается при остановке приложения. |
| `fetch_thumbnail_bytes(url)` | Скачивает обложку через общий (проксируемый) клиент с LRU-кэшем на 256 элементов; при 404/403 для `maxresdefault` пробует `hqdefault`. Возвращает `(bytes, media_type)`. |
| `_yt_dlp_base_params()` | Базовые опции yt-dlp: quiet, без плейлистов, без скачивания, таймауты и ретраи, прокси при наличии. |
| `_is_transport_error(exc)` | Определяет по тексту исключения сетевой характер ошибки (timeout, connection reset, DNS и т.п.). |
| `_ydl_extract_info(url, params)` | Вызывает `yt_dlp.extract_info` с одной повторной попыткой при транспортных ошибках, при этом сбрасывает и переопределяет детект прокси. |

### Поиск

| Функция | Описание |
|---|---|
| `_coerce_duration(value)` | Приводит длительность к неотрицательному int секунд (0 при ошибке). |
| `_best_thumbnail(entry)` | Выбирает лучший URL обложки: поле `thumbnail` или последний (наибольший) элемент списка `thumbnails`. |
| `_refine_search_query(query)` | Добавляет слово «audio» к запросу, если там нет «audio»/«lyrics» — для попадания в аудиоверсии. |
| `_entry_duration(entry)` | Длительность записи как положительный int или `None`. |
| `_is_music_track(entry)` | Фильтр «музыкальности»: не эфир, название не матчится на `TITLE_EXCLUDE_RE` (чарты, миксы, подкасты, «топ N» и т.д.), длительность 45–900 с. |
| `_deezer_search_tracks_sync(query, limit)` | Резервный поиск через публичный API Deezer (убирает «audio» из запроса), возвращает нормализованные треки с `track_id = deezer:<id>`. |
| `_search_tracks_sync(query, limit, pool_size)` | Синхронный поиск: прямой URL или `ytsearch{pool}:query` через yt-dlp; фильтрует записи `_is_music_track`, собирает метаданные. При пустом результате и не-URL — фолбэк на Deezer; при полной неудаче — `AudioServiceError`. |
| `search_tracks(query, limit, pool_size)` | Асинхронная обёртка: чистит запрос, ограничивает `limit` (1–50), выполняет поиск в отдельном потоке с таймаутом 40 с. |

### Тренды

| Функция | Описание |
|---|---|
| `_is_trending_track(entry)` | Как `_is_music_track`, но с окном длительности 90–360 с (для трендов). |
| `_entry_to_track(entry)` | Преобразует запись yt-dlp в словарь трека. |
| `_chart_track_from_entry(item, entry)` | Сопоставляет позицию чарта (Deezer) с найденной в YouTube записью и строит трек. |
| `_chart_item_to_deezer_track(item)` | Конвертирует элемент чарта в трек `deezer:<id>` (фолбэк, когда YouTube-резолв не удался). |
| `_fetch_deezer_ru_playlist_sync(limit)` | Берёт русский плейлист из Deezer (поиск по двум запросам, затем треки плейлиста), оставляет треки с кириллицей, возвращает `{artist, title, deezer_id, duration, thumbnail}`. |
| `_get_chart_items_sync(limit)` | Дедуплицирует (по исполнителю+названию) результат `_fetch_deezer_ru_playlist_sync`. |
| `_yt_search_entries_sync(query, pool)` | Плоский YouTube-поиск через yt-dlp; ошибки — `AudioServiceError`. |
| `_resolve_chart_track_sync(item, query)` | Резолвит один элемент чарта: YouTube-поиск → Deezer-конвертация → Deezer-поиск. |
| `_resolve_chart_items(chart_items, limit)` | Параллельно (семофор на 6) резолвит элементы чарта с таймаутом 25 с; дедуп по track_id и не более 2 треков на исполнителя. |
| `_search_single_fallback_query_sync(query)` | Резолвит один запасной запрос (YouTube → Deezer), не прерывая батч при ошибке. |
| `_get_trending_from_search_sync(limit)` | Параллельный пул потоков для запросов `TRENDING_FALLBACK_QUERIES` (ротация списка каждые 30 минут); собирает треки с теми же лимитами дедупа, обрезает по таймауту 45 с. |
| `_merge_by_track_id(tracks, extra, limit)` | Объединяет два списка треков без дубликатов по `track_id`, до `limit`. |
| `_refresh_trending_cache(limit)` | Полное обновление кэша трендов: чарт Deezer → резолв → при нехватке — фолбэк-поиск → при полной пустоте — конвертация чарта в Deezer-треки. Обновляет `TRENDING_CACHE` под локом или фиксирует `empty_at` для cooldown. |
| `_start_trending_refresh_locked(limit)` | Создаёт и сохраняет глобальную задачу `_refresh_trending_cache`. |
| `_rotate_cached_tracks(tracks)` | Детерминированно перемешивает кэш (зерно = номер 30-минутного интервала) — список меняется без новых запросов. |
| `get_trending_tracks(limit)` | Public API трендов по схеме stale-while-revalidate: свежий кэш — вернуть сразу; иначе запустить фоновое обновление. При холодном старте ждёт до 25 с (`TRENDING_COLD_WAIT_SECONDS`), после — отдаёт пустой/старый список, а задача продолжает работать в фоне. Есть cooldown 15 с после пустого результата. |

### Волна (My Wave)

| Функция | Описание |
|---|---|
| `_clean_favorite_text(value)` | Чистит текст трека: убирает невидимые символы, суффиксы «- Topic», «VEVO», «Official Audio/Video», «Lyrics», скобки-ремарки. |
| `_parse_favorite_item(fav)` | Разбирает избранное на `{artist, title, vibe}`: вытаскивает исполнителя из «Артист - Трек», эвристически определяет вайб по ключевым словам (aggressive/calm/sad/energetic, по умолчанию energetic). |
| `_normalize_song_title(title)` | Нормализует название для сравнения дубликатов: нижний регистр, только буквы/цифры. |
| `build_taste_profile(favorites, max_artists, sample_size)` | Строит профиль вкуса: топ артистов по частоте в избранном → поисковые запросы вида `<артист> <суффикс>` (`official audio`, `track`, `популярные треки`), при заданном `sample_size` — случайная выборка и перемешивание. |
| `_is_wave_track(track)` | Фильтр треков волны: название не из «мусорных» категорий, длительность 60–480 с. |
| `_merge_wave_results(results, limit, favorite_ids, favorite_titles)` | Сливает результаты нескольких поисков: исключает уже прослушанное/избранное (по id и нормализованному названию), лимит 2 трека на исполнителя. |
| `_wave_search_queries(queries, per_query)` | Параллельный (семофор 4) поиск по набору запросов с общим таймаутом 45 с; исключения отдельных запросов не роняют батч. |
| `_wave_per_query_limit(limit, query_count)` | Сколько треков искать на каждый запрос, чтобы набрать `limit`. |
| `get_wave_by_mood(vibe, limit)` | Волна по настроению: случайная выборка 7 запросов из пула настроения, затем догоняющие батчи по 5, пока не набрано `limit` треков; перемешивает результат. Неизвестный вайб или пустой результат — `AudioServiceError`. |
| `_discover_similar_queries_sync(artists, favorite_titles, limit)` | Через Deezer `search/artist` → `related` → `top` строит запросы по похожим исполнителям, исключая уже любимые треки. |
| `get_wave_personalized(favorites, limit)` | Персональная волна: разбирает избранное, определяет доминирующий вайб, составляет запросы (профиль вкуса + «артист popular/feat» + запросы стиля вайба), ищет параллельно; при нехватке — похожие артисты через Deezer и догоняющие раунды из пулов настроений/трендов (до 6 раундов). Исключает уже находящиеся в избранном треки. |

### Резолв прямого аудио

| Функция | Описание |
|---|---|
| `_track_webpage_url(track_id)` | Преобразует `track_id` в URL страницы (прямой URL остаётся как есть, иначе `youtube.com/watch?v=...`). |
| `_resolve_deezer_to_youtube_id_sync(deezer_id)` | Запрашивает метаданные трека Deezer API, ищет подходящую запись на YouTube; возвращает `(yt_id, preview_url)`. Preview (30-секундный mp3) — запасной вариант. |
| `_extract_yt_stream_info_sync(track_id)` | Через yt-dlp получает прямую ссылку на аудио (приоритет m4a → webm → mp4) и HTTP-заголовки сессии: сначала поле `url`, затем `requested_downloads`, затем перебор `formats` с аудиокодеком. |
| `_resolve_stream_info_sync(track_id)` | Маршрутизатор: `deezer:<id>` → резолв в YouTube (или preview), иначе — прямая экстракция YouTube. Возвращает `ResolvedStreamInfo` (url + headers + yt_id). |
| `_get_audio_stream_url_sync(track_id)` | Сокращение: только URL потока. |
| `get_audio_stream_url(track_id)` | Асинхронная обёртка с таймаутом 45 с. |

### Дисковый кэш аудио

| Функция | Описание |
|---|---|
| `_audio_cache_dir()` | Каталог кэша `<tempdir>/tg-music-audio-cache` (создаёт при необходимости). |
| `_audio_cache_key(track_id)` | Безопасное имя файла: заменяет недопустимые символы, максимум 120 символов. |
| `audio_media_type_for(track_or_path)` | Определяет MIME по магическим байтам файла (ftyp → audio/mp4, ID3/MPEG sync → audio/mpeg, OggS → audio/ogg, EBML → audio/webm), иначе по расширению; для deezer-треков — `audio/mpeg`. |
| `_audio_cache_prune()` | Если суммарный размер кэша > 256 МБ, удаляет самые старые файлы (по mtime), синхронизируя индекс и локи. |
| `_download_via_ytdlp_sync(track_id, destination, resolved_yt_id)` | Надёжное скачивание через yt-dlp (`download=True`, чанки 512 КБ, ретраи фрагментов), затем переименовывает готовый файл в целевой; для Deezer без YouTube-совпадения — скачивание preview. |
| `_download_audio_to_cache(track_id, stream_info, destination)` | Основная загрузка: качает по 1 МБ Range-чанками через общий клиент (обход троттлинга googlevideo), отслеживает redirect и Content-Range; ограничение 64 МБ на файл; при любом сбое — фолбэк на `_download_via_ytdlp_sync` (таймаут 45 с). |
| `get_cached_audio_file(track_id)` | Публичное API кэша: пер-трековый `asyncio.Lock`; возвращает существующий файл (обновляя mtime) или скачивает новый, добавляет в индекс и prune. |

### Проксирование стрима

| Функция | Описание |
|---|---|
| `_request_headers(range_header)` | Заголовки для upstream-запроса: UA браузера, `Accept-Encoding: identity`, прокидывает `Range`. |
| `_response_headers(response)` | Пробрасывает нужные заголовки ответа (Content-Type/Length/Range, Accept-Ranges, ETag, Last-Modified) наружу. |
| `_media_type(response)` | MIME ответа; `video/mp4` нормализуется в `audio/mp4`. |
| `start_audio_stream(url, range_header, client, chunk_size)` | Открывает потоковый GET к upstream с одной повторной попыткой при транспортной ошибке; возвращает `AudioStream` (статус, заголовки, тип, ленивый генератор тела с корректным закрытием). |
| `proxy_audio_stream(url, range_header, ...)` | Асинхронный генератор, отдающий тело `start_audio_stream` чанками (использовался как стрим-прокси до перехода на файловый кэш). |

---

## backend/db.py

Асинхронный репозиторий aiosqlite: избранное, история поиска, плейлисты.

### Модульные утилиты

| Функция | Описание |
|---|---|
| `_normalize_duration(value)` | Длительность → неотрицательный int (0 при ошибке). |
| `_normalize_query(query)` | Нормализация поискового запроса (сжатие пробелов). |
| `_normalize_playlist_title(title)` | Очистка названия плейлиста, обрезка до 60 символов. |

### Класс `Database`

| Метод | Описание |
|---|---|
| `__init__(path)` | Сохраняет путь, создаёт лок для сериализации операций. |
| `connection` (property) | Активное соединение; `RuntimeError`, если не подключено. |
| `connect()` | Открывает aiosqlite (Row-фабрика), включает WAL и foreign_keys, применяет схему (таблицы `favorites`, `search_history`, `playlists`, `playlist_tracks` + индексы). Повторный вызов безопасен. |
| `close()` | Закрывает соединение (идемпотентно). |
| `ping()` | `SELECT 1` — признак живой базы. |
| `add_favorite(user_id, track)` | Добавляет/обновляет трек в избранном (UPSERT по `(user_id, track_id)`); обязательны `track_id` и `title`, иначе `ValueError`. Возвращает `True`, если трек добавлен впервые. |
| `remove_favorite(user_id, track_id)` | Удаляет трек из избранного; `True`, если что-то удалено. |
| `list_favorites(user_id)` | Список избранного, новые сверху (`added_at DESC`). |
| `add_search_history(user_id, query)` | UPSERT запроса в историю, затем подрезка до 10 последних записей на пользователя. |
| `list_search_history(user_id, limit)` | Последние запросы (лимит обрезается 1..10). |
| `delete_search_query(user_id, query)` | Удаляет один запрос из истории. |
| `clear_search_history(user_id)` | Очищает всю историю пользователя; возвращает число удалённых. |
| `create_playlist(user_id, title)` | Создаёт плейлист и возвращает его запись (`id`, `user_id`, `title`, `created_at`). |
| `list_playlists(user_id)` | Плейлисты пользователя с `track_count` (LEFT JOIN по трекам), новые сверху. |
| `playlist_owned(user_id, playlist_id)` | Проверка владения плейлистом. |
| `delete_playlist(user_id, playlist_id)` | Удаляет плейлист (треки каскадно через FK); `True` при успехе. |
| `list_playlist_tracks(user_id, playlist_id)` | Треки плейлиста по позициям; `None`, если плейлист чужой/несуществует. |
| `add_track_to_playlist(user_id, playlist_id, track)` | Добавляет трек в конец плейлиста (позиция = MAX+1). Возвращает `None` (не владелец), `False` (уже есть) или `True`. |
| `remove_track_from_playlist(user_id, playlist_id, track_id)` | Удаляет трек только при подтверждённом владении плейлистом (подзапрос EXISTS). |

---

## backend/telegram_auth.py

Проверка `initData` Telegram Mini App по официальной HMAC-схеме.

| Функция / класс | Описание |
|---|---|
| `class InitDataResult` | Результат проверки: `user_id`, `reason`; свойство `ok` = `user_id is not None`. |
| `_build_data_check_string(pairs)` | Формирует строку проверки: все поля кроме `hash`, отсортированные, через `\n`. |
| `_signature_secret(bot_token)` | Вычисляет ключ подписи: `HMAC-SHA256(key="WebAppData", msg=bot_token)`. |
| `_user_id_from_pairs(pairs)` | Извлекает `user.id` из JSON-поля `user`. |
| `_auth_date_from_pairs(pairs)` | Извлекает `auth_date` как int. |
| `validate_init_data(init_data, bot_token)` | Полная проверка: непустые токен и initData → наличие `hash` → вычисление и безопасное сравнение HMAC → проверка `auth_date` (не в будущем, не старше 24 ч с учётом skew 5 мин) → извлечение `user_id > 0`. Возвращает `InitDataResult` с причиной отказа при любой неудаче. |

---

## bot/handlers.py

Хендлеры Telegram-бота: команда `/start`, инлайн-кнопка плеера, кнопка меню чата.

| Функция | Описание |
|---|---|
| `reset_menu_button_cache()` | Сбрасывает кэш установленных кнопок меню (для тестов и диагностики). |
| `_current_webapp_url(source)` | Резолвит источник URL: строка или функция (перечитывающая `.env`); обрезает слэш. |
| `build_player_url(webapp_url)` | Строит публичный URL плеера: только HTTPS (иначе `None`), всегда с завершающим слэшем `/app/` (обход проблемы с редиректом devtunnel). |
| `build_player_keyboard(webapp_url)` | Инлайн-клавиатура с кнопкой «🎧 Открыть плеер» (`WebAppInfo`); `None`, если URL не настроен. |
| `build_menu_button(webapp_url)` | Объект `MenuButtonWebApp` («Плеер» рядом с полем ввода); `None` без корректного URL. |
| `configure_menu_button(bot, webapp_url, chat_id=None)` | Устанавливает кнопку меню для конкретного чата или глобально. Кэширует URL по scope чата: повторный вызов с тем же URL не тратит запрос к API. Ошибки логируются, не прерывают работу. Возвращает `True` при успехе/пропуске. |
| `send_welcome(message, webapp_url)` | Приветствие по `/start`: персональное (HTML-экранированное имя), с клавиатурой плеера, либо сообщение о ненастроенном `WEBAPP_URL`. Превью ссылок отключены. |
| `build_router(webapp_url)` | Собирает `Router` с хендлером `CommandStart`: при каждом `/start` перечитывает URL, заново применяет кнопку меню (самолечение после сетевых сбоев) и отправляет приветствие. |

---

## frontend/app.js

Клиентская логика мини-приложения (IIFE). Константы: `userId` (из Telegram initDataUnsafe или фолбэк), ключи localStorage, таймауты, лимиты поиска/волны.

### Сеть и авторизация

| Функция | Описание |
|---|---|
| `apiHeaders(extra)` | Базовые заголовки запросов + `Authorization: tma <initData>` для серверной проверки подписи. |
| `isTransientStatus(status)` | Признак «временной» ошибки (408/429/502/503/504) — такие статусы стоит повторить. |
| `waitMs(ms, signal)` | Promise-пауза с поддержкой отмены через `AbortSignal`. |
| `fetchJson(url, options)` | Универсальный `fetch` с JSON-ответом: таймаут, повторные попытки (с нарастающей задержкой 450 мс) для временных статусов/сбоев сети, проброс внешнего `signal`. |

### Мелкие утилиты

| Функция | Описание |
|---|---|
| `clamp(value, min, max)` | Ограничение числа диапазоном. |
| `haptic(type)` | Тактильный отклик через Telegram `HapticFeedback` (безопасно вне Telegram). |
| `initIcons()` | Перерисовка иконок Lucide. |
| `setIcon(button, name)` | Заменяет иконку кнопки, если она изменилась. |
| `formatTime(totalSeconds)` | Секунды → `мм:сс` или `ч:мм:сс`. |
| `parseDuration(value)` | Длительность из числа или строки `ч:мм:сс` → секунды (int ≥ 0). |
| `paintRange(input)` | Заливка прогресса у `input[type=range]` через CSS-переменную `--value`. |
| `readStoredVolume()` / `saveVolume(value)` | Громкость из/в localStorage и Telegram CloudStorage. |
| `restoreCloudVolume()` | Подтягивает громкость из CloudStorage, если пользователь не менял её локально позже. |
| `markUserPlaybackAction()` | Фиксирует время последнего действия пользователя (чтобы облачное состояние не перетирало локальное). |

### Сохранение состояния плеера

| Функция | Описание |
|---|---|
| `capturePlayerState(position)` | Снимок состояния: очередь (до 200 треков), индекс, shuffle, repeat, позиция, метка времени. |
| `savePlayerState({force, position})` | Сохраняет снимок в localStorage и CloudStorage не чаще раза в 5 с (или принудительно). |
| `readPlayerStateFromStorage(storage)` | Читает и валидирует сохранённое состояние. |
| `applyRestoredState(data, savedAt)` | Восстанавливает очередь/режимы, показывает док, подготавливает возобновление (`pendingResume`) с сохранённой позицией. |
| `restoreSavedPlayerState()` | Восстановление из localStorage, затем из CloudStorage — если облачная копия новее и пользователь ещё ничего не нажимал. |

### Telegram и вкладки

| Функция | Описание |
|---|---|
| `applyTheme()` | Тема из `tg.colorScheme` (light/dark) + цвета шапки клиента. |
| `initTelegram()` | `tg.ready()`, `tg.expand()`, тема, подписка на `themeChanged`. |
| `activateTab(name)` | Переключение вкладок Поиск/Волна/Моя музыка: скрытие панели истории, фильтр библиотеки, восстановление «Популярного» или повтор поиска, загрузка избранного/плейлистов. |
| `readCachedTrending()` / `writeCachedTrending(tracks)` | Оффлайн-кэш «Популярного» в localStorage (30 минут). |
| `setSearchPlaceholder(name)` | Плейсхолдер поиска в зависимости от вкладки. |
| `stopActiveListRequest()` / `forgetActiveListRequest(controller)` | Управление текущим AbortController списков треков. |
| `syncClearButton()` | Показ/скрытие кнопки «очистить поиск». |
| `setSearchHeading(title)` / `setSearchCount(text)` / `pluralizeTracks(count)` | Заголовок, счётчик и русская плюрализация («1 трек / 2 трека / 5 треков»). |
| `hideSearchEmpty()` / `showSearchEmpty(title, text, onRetry)` | Состояния «ничего не найдено»/«ошибка» с кнопкой «Повторить». |
| `showCatalogSpinner(text)` | Каталожный спиннер загрузки списка. |
| `performSearch(rawQuery)` | Онлайн-поиск: запрос `/api/search`, рендер, синхронизация очереди, сохранение запроса в историю; при ошибке — экран повтора. Защита от гонок через `searchRequestId`. |
| `loadTrending({background})` | Загрузка «Популярного» (`/api/trending`), запись в кэш, фоновый режим без спиннера. |
| `restorePopular()` | Возврат к «Популярному»: из памяти или загрузка заново. |
| `scheduleSearch()` | Дебаунс ввода (450 мс); пустой ввод → «Популярное» через 150 мс. |

### История поиска

| Функция | Описание |
|---|---|
| `loadSearchHistory({clearLoaded})` | Загружает последние 10 запросов с сервера. |
| `rememberSearchQuery(query)` | Локально поднимает запрос наверх списка (без обращения к серверу). |
| `renderSearchHistoryChips()` | Рисует чипы истории. |
| `updateSearchHistoryVisibility()` / `showSearchHistory()` / `hideSearchHistory()` / `scheduleHideSearchHistory(delay)` | Показ панели истории при фокусе в пустом поле, скрытие с задержкой (чтобы клик по чипу успел сработать). |
| (обработчики) | Клик по чипу → выполнить поиск; «очистить» → `DELETE /api/search-history`. |

### Волна (My Wave)

| Функция | Описание |
|---|---|
| `setWaveMode(mode)` | Переключение режимов «по настроению» / «персональная». |
| `renderWaveChips()` | Чипы настроений (Спокойный/Бодрый/Агрессивный/Грустный). |
| `showWaveSpinner(text)` / `hideWaveEmpty()` / `showWaveEmpty(title, text)` / `setWaveCount(text)` | UI-состояния панели волны. |
| `waveLaunchButtons()` / `setWaveButtonsBusy(busy)` | Кнопки запуска: блокировка и подпись «Собираем волну…» на время генерации. |
| `runWave(url, source)` | Запрос волны (таймаут 50 с), рендер списка, установка очереди (`queueSource = "wave"`); защита от гонок, экран ошибки с повтором. |
| `loadWaveMood()` / `loadWavePersonal()` | Запуск волны по выбранному вайбу или персональной. |
| `currentWaveUrl()` / `waveTrackIds()` | URL для догрузки и множество уже добавленных id. |
| `appendWaveCards(tracks)` | Дописывает карточки в конец списка волны. |
| `appendMoreWaveTracks()` | «Бесконечная» волна: догружает следующую партию, исключая дубли, до 120 треков в очереди; параллельные вызовы схлопываются в один Promise. |

### Очередь и треки

| Функция | Описание |
|---|---|
| `setupMarquee()` | Включает бегущую строку, если название не влезает (на resize и после загрузки шрифтов). |
| `getTrackFromCard(card)` | Собирает объект трека из data-атрибутов карточки. |
| `normalizeTrack(track)` | Приводит произвольный объект API к `{track_id, title, artist, duration, thumbnail}`. |
| `isFavorite(trackId)` / `storeFavorite(track)` | Проверка/добавление в локальную карту избранного. |
| `syncFullLike(track)` / `syncLikeUI()` | Синхронизация состояния кнопок «сердечко» (полный плеер + все карточки). |
| `setLibraryLoading(loading)` / `libraryFilterQuery()` / `matchesLibraryFilter(track)` / `applyLibraryFilter()` | Загрузочное состояние и локальный фильтр библиотеки (по активному виду: избранное/плейлисты/треки плейлиста). |
| `renderLibrary()` | Рендер избранного в «Моей музыке» с фильтром. |
| `pluralizePlaylists(count)` / `setPlaylistsCount()` | Русская плюрализация плейлистов. |
| `authenticatedUrl(path)` | Добавляет `user_id` в query (для режима без Telegram-авторизации). |
| `activateLibraryView(name)` | Переключение «избранное / плейлисты / треки плейлиста». |
| `loadPlaylists({quiet})` | Загрузка плейлистов с сервера. |
| `renderPlaylists(list)` | Карточки плейлистов (играть/удалить/открыть). |
| `createPlaylistRequest(title)` / `createPlaylist()` / `askPlaylistTitle()` | Создание плейлиста (prompt-диалог) и обновление списка. |
| `deletePlaylist(playlist)` | Удаление плейлиста. |
| `renderPlaylistTracksView(title, tracks, isError)` / `fetchPlaylistTracks(id)` / `openPlaylist(playlist)` | Экран треков плейлиста и его загрузка. |
| `playPlaylist(playlist)` | Воспроизведение плейлиста с начала. |
| `addTrackToPlaylistRequest(playlistId, track)` / `updatePlaylistCount(id, delta)` | Добавление трека в плейлист и коррекция счётчика. |
| `renderPlaylistMenu()` / `openPlaylistMenu(track)` / `closePlaylistMenu()` | Bottom-sheet выбора плейлиста для «добавить в плейлист», статусы «Добавлено ✓ / Уже в плейлисте / Ошибка». |
| `loadFavorites({force, quiet})` | Загрузка избранного с сервера (защита от гонок + `favoritesRevision`); обновляет карту и UI. |
| `forgetFavoritesAbort(controller)` / `reconcileFavorites()` | Управление отменой; догрузка избранного после завершения отложенных лайков. |
| `favoritePayload(track)` | Тело POST-запроса избранного. |
| `toggleFavorite(track, button)` | Оптимистичное переключение лайка: сразу меняет UI, шлёт POST/DELETE, при ошибке откатывает состояние. |
| `markCurrentCard()` | Подсветка текущей карточки (и состояния «загрузка»). |
| `syncQueueFromCards({preserveCurrent})` | Синхронизирует очередь из видимых карточек; при `preserveCurrent` сохраняет текущий трек (вставляя его в список, если исчез). |
| `setQueue(tracks, startIndex)` | Устанавливает новую очередь и при `startIndex >= 0` начинает воспроизведение. |

### Отрисовка треков

| Функция | Описание |
|---|---|
| `thumbUrl(thumbnail)` | Превращает URL обложки разрешённого CDN в ссылку на прокси `/api/thumb?u=...`. |
| `applyCover(element, thumbnail)` | Устанавливает фон обложки после успешной предзагрузки (`Image` probe), иначе убирает. |
| `showPlayerDock()` | Показ мини-плеера. |
| `buildTrackCard(track, index)` | Создаёт DOM-карточку трека: обложка, название, исполнитель, длительность, кнопки «лайк» и «в плейлист». |
| `renderTrackList(container, tracks)` / `renderTracks(tracks)` | Полная перерисовка списка карточек. |
| `setPlayerStatus(text)` / `updateTrackUI(track)` | Обновление полного плеера: заголовок, артист, бегущие строки, обложки, `document.title`, прогресс, лайк. |

### Воспроизведение

| Функция | Описание |
|---|---|
| `buildStreamUrl(trackId, attempt)` | URL стрима `/api/stream?track_id=...` (с cache-buster `_r` при повторе). |
| `getEffectiveDuration()` | Длительность: из `<audio>` или метаданных трека. |
| `renderPlayState()` | Иконки play/pause у кнопок. |
| `setBuffering(buffering)` | Состояние буферизации + статус «Загрузка потока…». |
| `handlePlaybackFailure(error)` | Обработка неудачного `audio.play()`. |
| `prefetchTrack(trackId)` | Запрос `/api/prefetch` — фоновая загрузка трека в кэш бэкенда (важно для игры с выключенным экраном). |
| `scheduleNextPrefetch()` | Предзагрузка следующего (или случайного при shuffle) трека после события `canplay`/`playing`. |
| `playTrack(index, {resumePosition, retryAttempt})` | Главная функция воспроизведения: ставит UI, при «волне» догружает хвост очереди, «прогревает» стрим обычным fetch (Range 0–1023, таймаут 60 с — чтобы HTML5 audio в WebView не отвалился), затем назначает `audio.src`, включает Media Session и запускает play. |
| `replayCurrent()` | Повтор текущего трека с нуля. |
| `togglePlay()` | Play/pause c учётом отсутствия `src` и возобновлением сохранённой позиции. |
| `pickRandomIndex()` | Случайный индекс ≠ текущему. |
| `nextTrack({fromEnded})` | Следующий трек: повтор одного трека, shuffle, зацикливание каталога, для волны — догрузка партии. |
| `prevTrack()` | Предыдущий трек (учёт repeat all). |
| `handleEnded()` | Действие по окончании трека (repeat one → повтор, иначе next). |
| `handleAudioError()` | Ошибка `<audio>`: одна повторная загрузка трека (cache-buster), затем статус ошибки и авто-переход к следующему треку (до 3 подряд). |
| `selectCard(card)` | Клик по карточке: определить/синхронизировать очередь, повторный клик по текущему — пауза, иначе играть. |

### Таймлайн, громкость, Media Session

| Функция | Описание |
|---|---|
| `updateTimeline()` | Прогресс-бар, времена, мини-прогресс, `setPositionState` для ОС. |
| `handleMetadata()` | Обработка `loadedmetadata`: применяет отложенный seek (`pendingSeekSeconds`) при восстановлении позиции. |
| `syncVolumeUI()` / `setVolume(value, {persist, source})` | UI громкости и установка громкости с сохранением. |
| `setMediaPlaybackState(state)` / `updateMediaSession(track)` / `updatePositionState(duration, position, force)` | Интеграция с Media Session API: статус воспроизведения, метаданные (название/артист/обложка) для системного плеера, позиция (не чаще раза в 900 мс). |
| `seekBy(delta)` | Перемотка вперёд/назад на N секунд. |
| `setupMediaSession()` | Регистрация обработчиков аппаратных/системных кнопок: play, pause, stop, next, prev, seekforward/backward, seekto. |
| `beginSeek()` / `applySeek()` / `endSeek()` | Ручное перетаскивание ползунка прогресса. |

### Обработчики событий `<audio>` и UI

Слушатели: `loadstart/waiting` → буферизация, `canplay/playing` → сброс ошибок + `scheduleNextPrefetch`, `play/pause` → состояние и сохранение, `timeupdate` → таймлайн + автосохранение позиции, `loadedmetadata/durationchange/ended/error` → хендлеры выше, `volumechange` → синхронизация.

| Функция | Описание |
|---|---|
| `openPlayer()` / `closePlayer()` | Открытие/закрытие полноэкранного плеера (с `inert` для фона). |
| `flushPlayerState()` | Принудительное сохранение состояния на `pagehide`/`visibilitychange` (закрытие/сворачивание мини-приложения). |
| `setupTrackListInteractions(list)` | Делегирование кликов/Enter по карточкам (кроме кнопок лайка и «в плейлист») → `selectCard`. |
| (обработчики кликов) | Лайк/добавление в плейлист на карточках и в полном плеере; кнопки play/next/prev/shuffle/repeat; ползунки seek и громкости; mute (запоминает последнюю слышимую громкость); Escape закрывает меню/историю/плеер. |

### Инициализация

В конце скрипта: `fullPlayer.inert = true`, `initTelegram`, иконки, marquee, Media Session, режим волны и чипы, восстановление состояния плеера и громкости, синхронизация UI громкости, загрузка избранного, обновление таймлайна, отрисовка «Популярного» из кэша (или загрузка с фоновым обновлением устаревшего кэша). Наружу экспортируется отладочный объект `window.TGMusicApp` со всеми ключевыми функциями.

---

## Алфавитный указатель: где находится функция

Все функции проекта в алфавитном порядке с указанием файла. Внутри backend/api.py функции-эндпоинты определены внутри `create_app`; методы классов — внутри соответствующего класса.

| Функция | Файл |
|---|---|
| `activateLibraryView` | frontend/app.js |
| `activateTab` | frontend/app.js |
| `add_favorite` (метод Database) | backend/db.py |
| `add_favorite` (эндпоинт POST /api/favorites) | backend/api.py |
| `add_playlist_track` (эндпоинт POST /api/playlists/{id}/tracks) | backend/api.py |
| `add_search_history` (метод Database) | backend/db.py |
| `addTrackToPlaylistRequest` | frontend/app.js |
| `appendMoreWaveTracks` | frontend/app.js |
| `appendWaveCards` | frontend/app.js |
| `applyCover` | frontend/app.js |
| `applyLibraryFilter` | frontend/app.js |
| `applyRestoredState` | frontend/app.js |
| `applySeek` | frontend/app.js |
| `applyTheme` | frontend/app.js |
| `app_slash_redirect` (эндпоинт GET /app) | backend/api.py |
| `askPlaylistTitle` | frontend/app.js |
| `authenticatedUrl` | frontend/app.js |
| `beginSeek` | frontend/app.js |
| `build_dispatcher` | main.py |
| `build_lifespan` | main.py |
| `build_menu_button` | bot/handlers.py |
| `build_player_keyboard` | bot/handlers.py |
| `build_player_url` | bot/handlers.py |
| `build_router` | bot/handlers.py |
| `buildStreamUrl` | frontend/app.js |
| `buildTrackCard` | frontend/app.js |
| `build_taste_profile` | backend/audio_service.py |
| `capturePlayerState` | frontend/app.js |
| `check_env` | diagnose.py |
| `check_local_server` | diagnose.py |
| `check_port_owner` | diagnose.py |
| `check_telegram_menu` | diagnose.py |
| `check_tunnel` | diagnose.py |
| `clamp` | frontend/app.js |
| `close()` (метод Database) | backend/db.py |
| `closePlaylistMenu` | frontend/app.js |
| `closePlayer` | frontend/app.js |
| `close_stream_client` | backend/audio_service.py |
| `connect()` (метод Database) | backend/db.py |
| `configure_menu_button` | bot/handlers.py |
| `connection` (property Database) | backend/db.py |
| `create_app` (фабрика FastAPI) | backend/api.py |
| `create_bot` | main.py |
| `create_playlist` (метод Database) | backend/db.py |
| `create_playlist` (эндпоинт POST /api/playlists) | backend/api.py |
| `create_runtime_app` | main.py |
| `createPlaylist` | frontend/app.js |
| `createPlaylistRequest` | frontend/app.js |
| `currentWaveUrl` | frontend/app.js |
| `delete_playlist` (метод Database) | backend/db.py |
| `delete_playlist` (эндпоинт DELETE /api/playlists/{id}) | backend/api.py |
| `delete_search_history` (эндпоинт DELETE /api/search-history) | backend/api.py |
| `delete_search_query` (метод Database) | backend/db.py |
| `deletePlaylist` | frontend/app.js |
| `_audio_cache_dir` | backend/audio_service.py |
| `_audio_cache_key` | backend/audio_service.py |
| `_audio_cache_prune` | backend/audio_service.py |
| `_auth_date_from_pairs` | backend/telegram_auth.py |
| `_best_thumbnail` | backend/audio_service.py |
| `_build_data_check_string` | backend/telegram_auth.py |
| `_chart_item_to_deezer_track` | backend/audio_service.py |
| `_chart_track_from_entry` | backend/audio_service.py |
| `_check_port_url_consistency` | diagnose.py |
| `_clean_favorite_text` | backend/audio_service.py |
| `_coerce_duration` | backend/audio_service.py |
| `_current_webapp_url` | bot/handlers.py |
| `_deezer_search_tracks_sync` | backend/audio_service.py |
| `_detect_proxy_url` | backend/audio_service.py |
| `_discover_similar_queries_sync` | backend/audio_service.py |
| `_download_audio_to_cache` | backend/audio_service.py |
| `_download_via_ytdlp_sync` | backend/audio_service.py |
| `_entry_duration` | backend/audio_service.py |
| `_entry_to_track` | backend/audio_service.py |
| `_extract_yt_stream_info_sync` | backend/audio_service.py |
| `_favorite_track` | backend/api.py |
| `_fetch_deezer_ru_playlist_sync` | backend/audio_service.py |
| `_get_audio_stream_url_sync` | backend/audio_service.py |
| `_get_chart_items_sync` | backend/audio_service.py |
| `_get_stream_client` | backend/audio_service.py |
| `_get_trending_from_search_sync` | backend/audio_service.py |
| `_init_data_from_request` | backend/api.py |
| `_is_allowed_thumb_url` | backend/api.py |
| `_is_local_port_open` | backend/audio_service.py |
| `_is_music_track` | backend/audio_service.py |
| `_is_transport_error` | backend/audio_service.py |
| `_is_trending_track` | backend/audio_service.py |
| `_is_wave_track` | backend/audio_service.py |
| `_log_failure` (внутренний колбэк prefetch) | backend/api.py |
| `_media_type` | backend/audio_service.py |
| `_merge_by_track_id` | backend/audio_service.py |
| `_merge_wave_results` | backend/audio_service.py |
| `_normalize_duration` | backend/db.py |
| `_normalize_playlist_title` | backend/db.py |
| `_normalize_query` | backend/db.py |
| `_normalize_song_title` | backend/audio_service.py |
| `_parse_favorite_item` | backend/audio_service.py |
| `_QuietProbeFilter` (класс, метод `filter`) | main.py |
| `_read_env` | diagnose.py |
| `_read_process_name` | diagnose.py |
| `_refresh_trending_cache` | backend/audio_service.py |
| `_request_headers` | backend/audio_service.py |
| `_resolve_chart_items` | backend/audio_service.py |
| `_resolve_chart_track_sync` | backend/audio_service.py |
| `_resolve_deezer_to_youtube_id_sync` | backend/audio_service.py |
| `_resolve_httpx_proxy_url` | backend/audio_service.py |
| `_resolve_optional_user_id` | backend/api.py |
| `_resolve_stream_info_sync` | backend/audio_service.py |
| `_resolve_user_id` | backend/api.py |
| `_response_headers` | backend/audio_service.py |
| `_reread_env_value` | config.py |
| `_rotate_cached_tracks` | backend/audio_service.py |
| `_search_single_fallback_query_sync` | backend/audio_service.py |
| `_search_tracks_sync` | backend/audio_service.py |
| `_serve_audio_stream` | backend/api.py |
| `_signature_secret` | backend/telegram_auth.py |
| `_soft_user_id` | backend/api.py |
| `_start_trending_refresh_locked` | backend/audio_service.py |
| `_stdout_only` | diagnose.py |
| `_store_search_query` | backend/api.py |
| `_track_webpage_url` | backend/audio_service.py |
| `_user_id_from_pairs` | backend/telegram_auth.py |
| `_watch_menu_button` | main.py |
| `_wave_per_query_limit` | backend/audio_service.py |
| `_wave_search_queries` | backend/audio_service.py |
| `_ydl_extract_info` | backend/audio_service.py |
| `_yt_dlp_base_params` | backend/audio_service.py |
| `_yt_search_entries_sync` | backend/audio_service.py |
| `endSeek` | frontend/app.js |
| `fetchJson` | frontend/app.js |
| `fetchPlaylistTracks` | frontend/app.js |
| `fetch_thumbnail_bytes` | backend/audio_service.py |
| `flushPlayerState` | frontend/app.js |
| `formatTime` | frontend/app.js |
| `forgetActiveListRequest` | frontend/app.js |
| `forgetFavoritesAbort` | frontend/app.js |
| `get_audio_stream_url` | backend/audio_service.py |
| `get_auth_mode` | config.py |
| `get_bot_token` | config.py |
| `get_cached_audio_file` | backend/audio_service.py |
| `get_proxy_url` | config.py |
| `get_search_history` (эндпоинт GET /api/search-history) | backend/api.py |
| `get_trending_tracks` | backend/audio_service.py |
| `get_wave_by_mood` | backend/audio_service.py |
| `get_wave_personalized` | backend/audio_service.py |
| `get_webapp_url` | config.py |
| `getEffectiveDuration` | frontend/app.js |
| `handleAudioError` | frontend/app.js |
| `handleEnded` | frontend/app.js |
| `handleMetadata` | frontend/app.js |
| `handlePlaybackFailure` | frontend/app.js |
| `haptic` | frontend/app.js |
| `health` (эндпоинт GET /api/health) | backend/api.py |
| `hideSearchEmpty` | frontend/app.js |
| `hideSearchHistory` | frontend/app.js |
| `hideWaveEmpty` | frontend/app.js |
| `initIcons` | frontend/app.js |
| `initTelegram` | frontend/app.js |
| `isFavorite` | frontend/app.js |
| `isTransientStatus` | frontend/app.js |
| `libraryFilterQuery` | frontend/app.js |
| `list_favorites` (метод Database) | backend/db.py |
| `list_favorites` (эндпоинт GET /api/favorites) | backend/api.py |
| `list_playlist_tracks` (метод Database) | backend/db.py |
| `list_playlist_tracks` (эндпоинт GET /api/playlists/{id}/tracks) | backend/api.py |
| `list_playlists` (метод Database) | backend/db.py |
| `list_playlists` (эндпоинт GET /api/playlists) | backend/api.py |
| `list_search_history` (метод Database) | backend/db.py |
| `loadFavorites` | frontend/app.js |
| `loadPlaylists` | frontend/app.js |
| `loadSearchHistory` | frontend/app.js |
| `loadTrending` | frontend/app.js |
| `loadWaveMood` | frontend/app.js |
| `loadWavePersonal` | frontend/app.js |
| `main` (запуск сервера) | main.py |
| `main` (запуск диагностики) | diagnose.py |
| `markCurrentCard` | frontend/app.js |
| `markUserPlaybackAction` | frontend/app.js |
| `matchesLibraryFilter` | frontend/app.js |
| `nextTrack` | frontend/app.js |
| `normalizeTrack` | frontend/app.js |
| `openPlaylist` | frontend/app.js |
| `openPlaylistMenu` | frontend/app.js |
| `openPlayer` | frontend/app.js |
| `paintRange` | frontend/app.js |
| `parseDuration` | frontend/app.js |
| `performSearch` | frontend/app.js |
| `ping()` (метод Database) | backend/db.py |
| `pickRandomIndex` | frontend/app.js |
| `playPlaylist` | frontend/app.js |
| `playTrack` | frontend/app.js |
| `playlist_owned` (метод Database) | backend/db.py |
| `prefetch` (эндпоинт GET /api/prefetch) | backend/api.py |
| `prefetchTrack` | frontend/app.js |
| `proxy_audio_stream` | backend/audio_service.py |
| `readCachedTrending` | frontend/app.js |
| `readPlayerStateFromStorage` | frontend/app.js |
| `readStoredVolume` | frontend/app.js |
| `reconcileFavorites` | frontend/app.js |
| `rememberSearchQuery` | frontend/app.js |
| `remove_favorite` (метод Database) | backend/db.py |
| `remove_favorite_path` (эндпоинт DELETE /api/favorites/{track_id:path}) | backend/api.py |
| `remove_favorite_query` (эндпоинт DELETE /api/favorites) | backend/api.py |
| `remove_playlist_track_path` (эндпоинт) | backend/api.py |
| `remove_playlist_track_query` (эндпоинт) | backend/api.py |
| `remove_track_from_playlist` (метод Database) | backend/db.py |
| `renderLibrary` | frontend/app.js |
| `renderPlayState` | frontend/app.js |
| `renderPlaylists` | frontend/app.js |
| `renderPlaylistMenu` | frontend/app.js |
| `renderPlaylistTracksView` | frontend/app.js |
| `renderSearchHistoryChips` | frontend/app.js |
| `renderTrackList` | frontend/app.js |
| `renderTracks` | frontend/app.js |
| `renderWaveChips` | frontend/app.js |
| `replayCurrent` | frontend/app.js |
| `reset_menu_button_cache` | bot/handlers.py |
| `restoreCloudVolume` | frontend/app.js |
| `restorePopular` | frontend/app.js |
| `restoreSavedPlayerState` | frontend/app.js |
| `root` (эндпоинт GET /) | backend/api.py |
| `root_health` (эндпоинт GET /health) | backend/api.py |
| `runWave` | frontend/app.js |
| `savePlayerState` | frontend/app.js |
| `saveVolume` | frontend/app.js |
| `scheduleHideSearchHistory` | frontend/app.js |
| `scheduleNextPrefetch` | frontend/app.js |
| `scheduleSearch` | frontend/app.js |
| `search` (эндпоинт GET /api/search) | backend/api.py |
| `search_tracks` | backend/audio_service.py |
| `selectCard` | frontend/app.js |
| `send_welcome` | bot/handlers.py |
| `setBuffering` | frontend/app.js |
| `setIcon` | frontend/app.js |
| `setLibraryLoading` | frontend/app.js |
| `setMediaPlaybackState` | frontend/app.js |
| `setPlayerStatus` | frontend/app.js |
| `setQueue` | frontend/app.js |
| `setSearchCount` | frontend/app.js |
| `setSearchHeading` | frontend/app.js |
| `setSearchPlaceholder` | frontend/app.js |
| `setVolume` | frontend/app.js |
| `setWaveButtonsBusy` | frontend/app.js |
| `setWaveCount` | frontend/app.js |
| `setWaveMode` | frontend/app.js |
| `setupMarquee` | frontend/app.js |
| `setupMediaSession` | frontend/app.js |
| `setupTrackListInteractions` | frontend/app.js |
| `seekBy` | frontend/app.js |
| `showCatalogSpinner` | frontend/app.js |
| `showPlayerDock` | frontend/app.js |
| `showSearchEmpty` | frontend/app.js |
| `showSearchHistory` | frontend/app.js |
| `showWaveEmpty` | frontend/app.js |
| `showWaveSpinner` | frontend/app.js |
| `start_audio_stream` | backend/audio_service.py |
| `stopActiveListRequest` | frontend/app.js |
| `stream_path` (эндпоинт GET /api/stream/{track_id:path}) | backend/api.py |
| `stream_query` (эндпоинт GET /api/stream) | backend/api.py |
| `syncClearButton` | frontend/app.js |
| `syncFullLike` | frontend/app.js |
| `syncLikeUI` | frontend/app.js |
| `sync_menu_button` | main.py |
| `syncQueueFromCards` | frontend/app.js |
| `syncVolumeUI` | frontend/app.js |
| `thumb` (эндпоинт GET /api/thumb) | backend/api.py |
| `thumbUrl` | frontend/app.js |
| `toggleFavorite` | frontend/app.js |
| `togglePlay` | frontend/app.js |
| `updateMediaSession` | frontend/app.js |
| `updatePlaylistCount` | frontend/app.js |
| `updatePositionState` | frontend/app.js |
| `updateTimeline` | frontend/app.js |
| `updateTrackUI` | frontend/app.js |
| `validate_init_data` | backend/telegram_auth.py |
| `waitMs` | frontend/app.js |
| `wave_mood` (эндпоинт GET /api/wave/mood) | backend/api.py |
| `wave_personalized` (эндпоинт GET /api/wave/personalized) | backend/api.py |
| `waveLaunchButtons` | frontend/app.js |
| `waveTrackIds` | frontend/app.js |
| `writeCachedTrending` | frontend/app.js |

### Классы и их методы

| Класс | Файл | Методы |
|---|---|---|
| `Settings` (+ свойство `bot_enabled`) | config.py | — |
| `ActionResult` | diagnose.py | `ok`, `warn`, `fail`, `render` |
| `FavoriteCreate`, `PlaylistCreate` | backend/api.py | — (Pydantic-модели) |
| `ResolvedStreamInfo`, `AudioStream` | backend/audio_service.py | — (dataclass-контейнеры) |
| `AudioServiceError` | backend/audio_service.py | — (исключение) |
| `Database` | backend/db.py | `__init__`, `connection`, `connect`, `close`, `ping`, `add_favorite`, `remove_favorite`, `list_favorites`, `add_search_history`, `list_search_history`, `delete_search_query`, `clear_search_history`, `create_playlist`, `list_playlists`, `playlist_owned`, `delete_playlist`, `list_playlist_tracks`, `add_track_to_playlist`, `remove_track_from_playlist` |
| `InitDataResult` (+ свойство `ok`) | backend/telegram_auth.py | — |
| `_QuietProbeFilter` | main.py | `filter` |
| `PlayerState` (объект состояния) | frontend/app.js | — |

---

*Документ сгенерирован по состоянию кода на ноябрь 2026 г. При изменении функций обновляйте соответствующие разделы.*
