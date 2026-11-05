# TG Music — Telegram Mini App

Музыкальный плеер внутри Telegram: FastAPI + aiogram + yt-dlp и ванильный
frontend без сборщика.

## Что умеет

- искать музыку на YouTube через `yt-dlp` и отсеивать live-стримы, Shorts и
  слишком длинные видео;
- стримить аудио через backend-прокси с поддержкой перемотки (`Range`);
- хранить избранное пользователя в SQLite и показывать его во вкладке
  «Моя музыка»;
- работать как Telegram Mini App с авторизацией по `initDataUnsafe.user.id`;
- запускать API отдельно от бота, если `BOT_TOKEN` не задан.

## Локальный запуск

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# заполните BOT_TOKEN и WEBAPP_URL в .env

python main.py
```

После старта:

- API и Mini App: <http://localhost:8000/app/>
- Проверка состояния: <http://localhost:8000/api/health>
- Лёгкий health-check для аптайм-мониторов: <http://localhost:8000/health>

`/health` возвращает `{"status": "ok"}` без обращения к БД. `/api/health`
дополнительно проверяет SQLite.

### Поиск треков

При открытии приложения сразу показываются реальные YouTube-треки (с
11-символьными ID), а затем список заменяется результатами живого поиска
`GET /api/search?q=top+hits`. Ввод в поле поиска отправляет запрос к
`/api/search` и рендерит настоящие `track_id` из yt-dlp. Фейковые ID вида
`demo-1` не используются.

Если `BOT_TOKEN` не задан, запускается только API — это удобно для разработки
интерфейса в обычном браузере. Тестовый `user_id` в этом режиме — `123456`.
Для прямой проверки на порту 8000 можно также использовать:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

## HTTPS-туннель для Telegram

Telegram открывает Mini Apps только по публичному `https://`. Для локальной
разработки поднимите туннель к `http://localhost:8000`.

### Быстрый старт (два терминала)

1. Терминал 1 — запустить приложение:

   ```bash
   python main.py
   ```

2. Терминал 2 — поднять бесплатный туннель без регистрации:

   ```bash
   cloudflared tunnel --url http://localhost:8000
   ```

   Windows: `winget install --id Cloudflare.cloudflared`.

3. Cloudflare напечатает адрес вида
   `https://random-words.trycloudflare.com`. Скопируйте его в `.env`:

   ```env
   WEBAPP_URL=https://random-words.trycloudflare.com
   ```

4. Перезапустите `python main.py` в первом терминале и отправьте боту `/start`.

### Вариант 2: ngrok

```bash
ngrok http 8000
```

Скопируйте `Forwarding`-адрес вида `https://xxxx.ngrok-free.app`.

### Общие правила

В `WEBAPP_URL` указывается только базовый адрес **без** `/app` — бот сам
добавляет путь. После изменения `.env` перезапустите приложение.

При старте бот устанавливает `MenuButtonWebApp` (кнопка «🎧 Плеер» слева от
поля ввода) и присылает inline-кнопку «🎧 Открыть плеер». Обе ведут на
`{WEBAPP_URL}/app`.

Важно:

- туннель должен быть запущен, пока вы тестируете Mini App;
- бесплатные туннели обычно меняют адрес после перезапуска — обновите
  `WEBAPP_URL` и перезапустите бота;
- приложение раздаётся по пути `/app/`, поэтому не добавляйте `/app` в
  `WEBAPP_URL` вручную: бот добавит его сам.

## Проверка

```bash
pytest -q
python -m compileall backend bot tests main.py config.py
```
