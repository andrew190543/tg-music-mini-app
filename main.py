"""Entry point: run FastAPI and the aiogram bot in one event loop."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramConflictError
from aiogram.types import BotCommand
from fastapi import FastAPI

from backend.api import create_app
from backend.audio_service import close_stream_client, get_trending_tracks
from backend.db import close_db, init_db
from bot.handlers import build_router, configure_menu_button
from config import get_webapp_url, load_settings

settings = load_settings()
logger = logging.getLogger(__name__)


def _build_bot_runtime() -> tuple[Bot, Dispatcher] | None:
    """Create the bot and dispatcher when a usable token is configured."""
    if not settings.bot_enabled:
        return None

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(get_webapp_url))
    return bot, dispatcher


async def _run_bot_polling(bot: Bot, dispatcher: Dispatcher) -> None:
    """Configure the bot and poll Telegram, restarting after failures."""
    delay = 2.0
    while True:
        started = time.monotonic()
        try:
            try:
                await bot.delete_webhook(drop_pending_updates=False)
                await bot.set_my_commands(
                    [BotCommand(command="start", description="Открыть плеер")]
                )
            except Exception:
                logger.warning(
                    "Не удалось обновить команды/webhook бота", exc_info=True
                )

            await configure_menu_button(bot, get_webapp_url())

            await dispatcher.start_polling(
                bot,
                allowed_updates=dispatcher.resolve_used_update_types(),
                handle_signals=False,
                close_bot_session=False,
                polling_timeout=20,
            )
        except asyncio.CancelledError:
            logger.info("Задача поллинга бота остановлена")
            raise
        except TelegramConflictError:
            logger.error(
                "Конфликт токена Telegram (TelegramConflictError): другой "
                "экземпляр бота @srhehjejh_bot уже запущен! Остановите второй "
                "процесс (pkill -f main.py). Повтор через %.0f сек...",
                delay,
            )
        except Exception:
            logger.exception(
                "Polling Telegram прерван; перезапуск через %.0f сек", delay
            )
        else:
            logger.warning(
                "Polling Telegram завершился штатно; перезапуск через %.0f сек",
                delay,
            )

        if time.monotonic() - started >= 60.0:
            delay = 2.0

        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        delay = min(delay * 2.0, 30.0)


def _print_player_url() -> None:
    """Show the local player URL, tolerating consoles without emoji support."""
    url = f"http://127.0.0.1:{settings.port}/app/"
    messages = (
        f"INFO: 🎧 Плеер доступен по ссылке: {url}",
        f"INFO: Плеер доступен по ссылке: {url}",
        f"INFO: Player URL: {url}",
    )

    for message in messages:
        try:
            print(message, flush=True)
            return
        except UnicodeEncodeError:
            continue


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialize the shared database and start Telegram polling in background."""
    database = await init_db(settings.db_path)
    app.state.db = database

    _print_player_url()

    warm_task: asyncio.Task[list[dict]] | None = asyncio.create_task(
        get_trending_tracks(20), name="trending-warmup"
    )

    bot_task: asyncio.Task[None] | None = None
    bot: Bot | None = None
    dispatcher: Dispatcher | None = None

    runtime = _build_bot_runtime()
    if runtime is None:
        logger.warning(
            "BOT_TOKEN не задан — запущен только API. "
            "Укажите токен в .env, чтобы включить Telegram-бота."
        )
    else:
        bot, dispatcher = runtime
        app.state.bot = bot
        bot_task = asyncio.create_task(
            _run_bot_polling(bot, dispatcher), name="telegram-bot"
        )
        app.state.bot_task = bot_task

        if not get_webapp_url().startswith("https://"):
            logger.warning(
                "WEBAPP_URL пуст или не использует HTTPS — кнопка Mini App "
                "не будет показана в боте. Для локальной разработки: "
                "cloudflared tunnel --url http://localhost:8000, затем "
                "укажите полученный https-адрес в WEBAPP_URL."
            )

    try:
        yield
    finally:
        if dispatcher is not None:
            try:
                await dispatcher.stop_polling()
            except Exception:
                pass
        if bot_task is not None:
            bot_task.cancel()
            await asyncio.gather(bot_task, return_exceptions=True)
        if warm_task is not None:
            warm_task.cancel()
            await asyncio.gather(warm_task, return_exceptions=True)
        if bot is not None:
            await bot.session.close()
        await close_stream_client()
        await close_db()


app = create_app(db_path=settings.db_path, lifespan=lifespan)


class _QuietAccessLogFilter(logging.Filter):
    """Hide uvicorn access logs for continuous local probes."""

    QUIET_PATHS = {"/health", "/v1/models"}

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) >= 3:
            path = str(record.args[2]).split("?", 1)[0]
            return path not in self.QUIET_PATHS
        return True


def _configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )
    logging.getLogger("uvicorn.access").addFilter(_QuietAccessLogFilter())
    # httpx logs every upstream request (yt-dlp CDN, Deezer, ...) at INFO
    # with full URLs — that is noise; only real problems are worth showing.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


if __name__ == "__main__":
    _configure_logging()
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
