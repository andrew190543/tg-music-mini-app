# Техническое задание и пошаговый план разработки: Telegram Mini App (TMA) Music Player

## 1. Стек технологий и архитектура
- **Язык и рантайм:** Python 3.13+
- **Telegram-бот:** `aiogram 3.x` (асинхронный, точка входа и выдача кнопки запуска WebApp).
- **Backend API и сервер статики:** `FastAPI` + `uvicorn` (работает в одном event loop с ботом или параллельным сервисом).
- **Поиск и аудио-поток:** `yt-dlp` (извлечение метаданных и прямых аудио-потоков `m4a`/`opus` из YouTube/SoundCloud без полного скачивания на диск, либо с кэшированием через `ffmpeg`).
- **База данных:** `SQLite` + `aiosqlite` (хранение лайков, плейлистов пользователя и истории поиска по `telegram_user_id`).
- **Frontend (Mini App):** Vanilla HTML5 / CSS3 / JS (ES6+) без тяжелых сборщиков (для простоты деплоя и скорости), с подключением `telegram-web-app.js`. Аудио-движок на базе стандартного `HTML5 Audio API`.

## 2. Целевая структура проекта
```text
tg_music_app/
├── bot/
│   ├── __init__.py
│   └── handlers.py          # Обработчик /start и кнопка WebAppInfo
├── backend/
│   ├── __init__.py
│   ├── api.py               # Маршруты FastAPI (/search, /stream, /favorites)
│   ├── audio_service.py     # Обертка над yt-dlp для поиска и стриминга
│   └── db.py                # Асинхронная работа с SQLite (избранное, плейлисты)
├── frontend/
│   ├── index.html           # Разметка Mini App
│   ├── style.css            # Стилизация с привязкой к переменным темы Telegram
│   └── app.js               # Логика плеера, поиск, громкость, запросы к API
├── .env                     # BOT_TOKEN, WEBAPP_URL, PORT
├── main.py                  # Точка входа: запуск FastAPI + раздача frontend + старт бота
└── requirements.txt
```

Дополнительно на Фазе 1 добавлены: `config.py`, `requirements-dev.txt`,
`.env.example`, `.gitignore`, `pytest.ini` и каталог `tests/`.

## 3. План разработки

### Фаза 1. Каркас проекта и база данных

- [x] Шаг 1.1: Создать структуру папок и файлов согласно разделу 2.
- [x] Шаг 1.2: Сформировать requirements.txt (aiogram, fastapi, uvicorn, yt-dlp, aiosqlite, python-dotenv, httpx).
- [x] Шаг 1.3: Написать модуль backend/db.py на базе aiosqlite. Создать таблицу favorites:
  - id (INTEGER PRIMARY KEY AUTOINCREMENT)
  - user_id (INTEGER, индекс)
  - track_id (TEXT — ID видео/трека из источника)
  - title (TEXT)
  - artist (TEXT)
  - duration (INTEGER — в секундах)
  - thumbnail (TEXT — URL обложки)
- [x] Шаг 1.4: Реализовать функции init_db(), add_favorite(), remove_favorite(), get_favorites(user_id).

**Статус Фазы 1:** завершена. Проверка: `pytest -q` — 19 тестов пройдено;
smoke-тест `main.py` — `/api/health` и статика отвечают кодом 200.

### Фаза 2. Аудио-сервис и прокси-стриминг

- [x] Шаг 2.1: Реализовать асинхронную функцию поиска search_tracks(query: str, limit: int = 15). Использовать yt-dlp в asyncio.to_thread с параметрами extract_flat=True, quiet=True и префиксом ytsearch15:{query}. Возвращать список словарей: track_id, title, artist (uploader/channel), duration, thumbnail.
- [x] Шаг 2.2: Реализовать функцию получения прямой ссылки на аудиопоток get_audio_stream_url(track_id: str). Настройки yt-dlp: format='bestaudio[ext=m4a]/bestaudio', quiet=True, noplaylist=True.
- [x] Шаг 2.3: (Важно для обхода CORS и региональных блоков IP клиента) Реализовать генератор проксирования потока через httpx.AsyncClient с поддержкой HTTP-заголовка Range (чтобы в плеере работала перемотка ползунком).

**Статус Фазы 2:** завершена. Проверка: `pytest -q` — 31 тест пройдено
(включая 12 тестов `tests/test_audio_service.py`).

### Фаза 3. Backend API на FastAPI

- [x] Шаг 3.1: Настроить экземпляр FastAPI, подключить CORSMiddleware и смонтировать папку frontend/ как статику на корневой маршрут /app.
- [x] Шаг 3.2: Создать эндпоинт GET /api/search?q={query} — валидирует строку, вызывает search_tracks, возвращает JSON со списком треков.
- [x] Шаг 3.3: Создать эндпоинт GET /api/stream/{track_id} — получает прямой URL через get_audio_stream_url и отдает StreamingResponse (или делает редирект/проксирование с поддержкой Accept-Ranges: bytes и Content-Type: audio/mp4).
- [x] Шаг 3.4: Создать эндпоинты для избранного:
  - GET /api/favorites?user_id={id} — список сохраненных треков.
  - POST /api/favorites — добавление трека в избранное.
  - DELETE /api/favorites/{track_id}?user_id={id} — удаление из избранного.

**Статус Фазы 3:** завершена. Проверка: `pytest -q` — 38 тестов пройдено
(включая 8 тестов `tests/test_api.py`); `compileall` — OK.

### Фаза 4. Frontend — вёрстка и дизайн Telegram Mini App

- [x] Шаг 4.1: Подключить `telegram-web-app.js` и Lucide, разделить экран на 3 зоны:
  - верхняя панель: поле поиска с кнопкой очистки и вкладки «Поиск» / «Моя музыка»;
  - центральная скроллируемая зона: карточки треков (обложка, название, автор, длительность, лайк);
  - нижняя фиксированная панель: мини-плеер (обложка, бегущая строка, Play/Pause, Next) и развёрнутый полноэкранный плеер (большая обложка, таймлайн с `<input type="range">`, текущее/общее время, Shuffle, Prev, Play/Pause, Next, Repeat off/all/one, громкость 0–100% с Mute/Unmute).
- [x] Шаг 4.2: Использовать нативные переменные Telegram (`--tg-theme-bg-color`, `--tg-theme-text-color`, `--tg-theme-button-color`, `--tg-theme-secondary-bg-color`) для автоматической светлой/тёмной темы; стилизовать `<input type="range">` для таймлайна и громкости.

**Статус Фазы 4:** завершена. Проверка: `pytest -q` — 38 тестов пройдено;
`compileall` — OK; статика `/app/`, `/app/style.css`, `/app/app.js` отдаётся с кодом 200.

### Фаза 5. Frontend — логика плеера и интеграция с Telegram

- [x] Шаг 5.1: Инициализировать `window.Telegram.WebApp` (`tg.ready()`, `tg.expand()`); получить `user_id` из `tg.initDataUnsafe.user.id` с фоллбэком на тестовый ID `123456` в обычном браузере.
- [x] Шаг 5.2: Создать `PlayerState`: `audio = new Audio()`, `queue`, `currentIndex`, `isPlaying`, `isShuffle`, `repeatMode` (`none` / `all` / `one`), `volume` (по умолчанию `1.0`).
- [x] Шаг 5.3: Реализовать `playTrack(index)` (`/api/stream/{track_id}`, обновление обложки и текста), `togglePlay()`, `nextTrack()` / `prevTrack()` с учётом shuffle и repeat; автопереход по `audio.onended`.
- [x] Шаг 5.4: Связать ползунок громкости с `audio.volume` (0.0–1.0), сохранять уровень в `localStorage`, добавить Mute/Unmute; обновлять прогресс по `audio.ontimeupdate` и перематывать через `audio.currentTime` при перетаскивании.
- [x] Шаг 5.5: Подключить `navigator.mediaSession`: метаданные трека, обложка и обработчики play/pause/prev/next/seek для экрана блокировки и системных медиакнопок.
- [x] Шаг 5.6: Добавить `tg.HapticFeedback.impactOccurred('light')` на Play/Pause, лайк и переключение треков.

**Статус Фазы 5:** завершена. Проверка: `pytest -q` — 38 тестов пройдено;
`node --check frontend/app.js` — OK; `compileall` — OK. Headless Chrome (CDP):
инициализация (`user_id` 123456, очередь из 3 треков), play/pause, next/prev,
громкость 45% + localStorage, Mute/Unmute, цикл Repeat, Shuffle, лайк, перемотка,
автопереход `ended` во всех режимах — без JS-исключений.

### Фаза 6. Telegram-бот и единая точка входа

- [x] Шаг 6.1: В `bot/handlers.py` создать роутер aiogram: по `/start` отправлять приветствие с inline-кнопкой «🎧 Открыть плеер» (`WebAppInfo(url="{WEBAPP_URL}/app")`) и устанавливать `MenuButtonWebApp` (кнопка «🎧 Плеер» слева от поля ввода).
- [x] Шаг 6.2: В `main.py` объединить FastAPI и aiogram: в lifespan вызывать `init_db()`, запускать `dp.start_polling(bot)` фоновой задачей `asyncio.create_task()`; запуск через `uvicorn.run("main:app", host="0.0.0.0", port=8000)`.
- [x] Шаг 6.3: Обновить `.env.example` и создать `README.md` с инструкцией по HTTPS-туннелю через cloudflared или ngrok (Telegram открывает Mini Apps только по `https://`).

**Статус Фазы 6:** завершена. Проверка: `pytest -q` — 49 тестов пройдено
(включая новые тесты `tests/test_bot.py` для `/app`-URL, inline- и menu-кнопок);
`compileall` — OK. Smoke-тест `python main.py`: lifespan поднял БД,
`/api/health` → `database: ok`, `/app/` → 200, `/` → 307 на `/app/`.
При заданном `BOT_TOKEN` бот настраивает команды и `MenuButtonWebApp`,
затем запускает polling в фоне, не блокируя API.

### Хотфикс: реальный поиск и `/health`

- [x] Удалены демо-треки `demo-1`…`demo-3`; стартовый список рендерится в JS из реальных 11-символьных YouTube ID и заменяется результатами `GET /api/search?q=top+hits`.
- [x] Поле поиска вызывает `GET /api/search?q=...&limit=15` с debounce/Enter и рендерит настоящие `track_id` из yt-dlp; добавлены состояния загрузки, пустой выдачи и ошибки.
- [x] Добавлен `GET /health` → `{"status": "ok"}` (помимо `/api/health` с проверкой БД).
- [x] Очередь сохраняет текущий играющий трек при замене результатов поиска.

**Статус хотфикса:** `pytest -q` — 49 тестов пройдено; `node --check frontend/app.js` — OK; `compileall` — OK. Headless Chrome (CDP, мок `/api/search`): стартовые треки, поиск «daft punk», пустая выдача, возврат к популярному и сохранение текущего трека — без JS-исключений и без `demo-*`.