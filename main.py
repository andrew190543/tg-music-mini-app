"""Entry point: run FastAPI and the aiogram bot in one event loop."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand
from fastapi import FastAPI

from backend.api import create_app
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
    """Configure the bot and poll Telegram without taking down the API."""
    try:
        try:
            await bot.set_my_commands(
                [BotCommand(command="start", description="Открыть плеер")]
            )
        except Exception:
            logger.warning(
                "Не удалось обновить команды бота", exc_info=True
            )

        await configure_menu_button(bot, get_webapp_url())

        # uvicorn owns process signals in embedded mode; the bot must not
        # install its own SIGINT/SIGTERM handlers or close the shared session.
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
            handle_signals=False,
            close_bot_session=False,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Telegram-бот остановлен; API продолжает работу")


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
            # Windows consoles may use cp1251/cp866 and cannot encode emoji.
            continue


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialize the shared database and start Telegram polling in background."""
    database = await init_db(settings.db_path)
    # The API routes read request.app.state.db, so both parts share one
    # connection and one SQLite WAL session.
    app.state.db = database

    _print_player_url()

    bot_task: asyncio.Task[None] | None = None
    bot: Bot | None = None

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
        if bot_task is not None:
            bot_task.cancel()
            await asyncio.gather(bot_task, return_exceptions=True)
        if bot is not None:
            await bot.session.close()
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
    # Local probes (health, OpenAI-style model discovery) must not flood the
    # console. Keep the access log for real traffic only.
    logging.getLogger("uvicorn.access").addFilter(_QuietAccessLogFilter())


if __name__ == "__main__":
    _configure_logging()
    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )
