"""Single-command entry point: FastAPI server + Telegram bot polling."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from fastapi import FastAPI

from backend.api import create_app
from backend.audio_service import close_stream_client, get_trending_tracks
from backend.db import Database
from bot.handlers import build_router, configure_menu_button
from config import Settings, get_webapp_url, load_settings

logger = logging.getLogger(__name__)

QUIET_PATHS = ("/health", "/api/health")
POLLING_BACKOFF_MIN_SECONDS = 2.0
POLLING_BACKOFF_MAX_SECONDS = 60.0
MENU_SYNC_INTERVAL_SECONDS = 30.0


def create_bot(token: str) -> Bot:
    """Create an aiogram Bot instance with HTML parse mode."""
    return Bot(
        token=token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def create_dispatcher(webapp_url: str | None = None) -> Dispatcher:
    """Create a Dispatcher wired with the /start router."""
    dispatcher = Dispatcher()
    dispatcher.include_router(
        build_router(webapp_url if webapp_url is not None else get_webapp_url)
    )
    return dispatcher


async def sync_menu_button(bot: Bot, webapp_url: str | None = None) -> bool:
    """Install the global MenuButtonWebApp for the bot."""
    url = webapp_url if webapp_url is not None else get_webapp_url()
    return await configure_menu_button(bot, url)


class _QuietProbeFilter(logging.Filter):
    """Drop access-log records for high-frequency health probes."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not any(
            f'"{method} {path} ' in message
            for method in ("GET", "HEAD")
            for path in QUIET_PATHS
        )


async def _watch_menu_button(bot: Bot) -> None:
    """Periodically re-sync MenuButtonWebApp if WEBAPP_URL changed in .env."""
    while True:
        await asyncio.sleep(MENU_SYNC_INTERVAL_SECONDS)
        with contextlib.suppress(Exception):
            await sync_menu_button(bot)


async def _run_bot_polling(settings: Settings) -> None:
    """Run aiogram polling in the background while the token is configured."""
    if not settings.bot_enabled:
        logger.warning(
            "BOT_TOKEN is not set in .env — FastAPI is running without "
            "Telegram bot polling."
        )
        return

    dispatcher = create_dispatcher()
    delay = POLLING_BACKOFF_MIN_SECONDS

    while True:
        bot = create_bot(settings.bot_token)
        menu_watcher: asyncio.Task[None] | None = None
        try:
            await sync_menu_button(bot)
            await bot.delete_webhook(drop_pending_updates=True)
            delay = POLLING_BACKOFF_MIN_SECONDS
            logger.info(
                "Starting Telegram bot polling (WEBAPP_URL=%s)",
                get_webapp_url() or settings.webapp_url,
            )
            menu_watcher = asyncio.create_task(
                _watch_menu_button(bot), name="menu-button-watcher"
            )
            await dispatcher.start_polling(bot, handle_signals=False)
            return
        except asyncio.CancelledError:
            logger.info("Telegram bot polling stopped")
            raise
        except Exception as exc:
            logger.warning(
                "Telegram bot polling failed (%s: %s); retrying in %.0fs. "
                "FastAPI stays online.",
                type(exc).__name__,
                exc,
                delay,
            )
        finally:
            if menu_watcher is not None:
                menu_watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await menu_watcher
            with contextlib.suppress(Exception):
                await bot.session.close()

        await asyncio.sleep(delay)
        delay = min(delay * 2, POLLING_BACKOFF_MAX_SECONDS)


def build_lifespan(settings: Settings):
    """Create a FastAPI lifespan that owns SQLite and the bot polling task."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = Database(settings.db_path)
        await database.connect()
        app.state.db = database

        warm_task = asyncio.create_task(
            get_trending_tracks(20), name="trending-warmup"
        )
        bot_task = asyncio.create_task(
            _run_bot_polling(settings), name="telegram-bot-polling"
        )
        try:
            yield
        finally:
            warm_task.cancel()
            bot_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await bot_task
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await warm_task
            await close_stream_client()
            await database.close()

    return lifespan


def create_runtime_app(settings: Settings | None = None) -> FastAPI:
    """Create the FastAPI application wired with the shared runtime lifespan."""
    resolved = settings or load_settings()
    return create_app(
        db_path=resolved.db_path,
        lifespan=build_lifespan(resolved),
    )


def main() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logging.getLogger("uvicorn.access").addFilter(_QuietProbeFilter())

    app = create_runtime_app(settings)

    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        proxy_headers=True,
        forwarded_allow_ips="*",
        timeout_keep_alive=75,
        timeout_graceful_shutdown=15,
    )


if __name__ == "__main__":
    main()