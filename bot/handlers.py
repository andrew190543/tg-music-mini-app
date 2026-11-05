"""Telegram bot handlers: /start, the Mini App button and the chat menu button."""

from __future__ import annotations

import html
import logging
from collections.abc import Callable

from aiogram import Bot, Router
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)

logger = logging.getLogger(__name__)

PLAYER_BUTTON_TEXT = "🎧 Открыть плеер"
MENU_BUTTON_TEXT = "Плеер"

WebAppUrlSource = str | Callable[[], str]


def _current_webapp_url(source: WebAppUrlSource) -> str:
    """Resolve either a constant URL or a callable rereading ``.env``."""
    return (source() if callable(source) else source).strip().rstrip("/")


def build_player_url(webapp_url: str) -> str | None:
    """Return the public Mini App URL, adding the ``/app`` suffix when needed.

    ``WEBAPP_URL`` is documented as a base HTTPS URL. Telegram only accepts
    HTTPS URLs for WebApp buttons, so plain-HTTP values are rejected.
    """
    url = (webapp_url or "").strip().rstrip("/")
    if not url.lower().startswith("https://"):
        return None

    if url.lower().endswith("/app"):
        return url
    return f"{url}/app"


def build_player_keyboard(webapp_url: str) -> InlineKeyboardMarkup | None:
    """Create a WebApp keyboard when a public HTTPS URL is configured."""
    url = build_player_url(webapp_url)
    if url is None:
        return None

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=PLAYER_BUTTON_TEXT,
                    web_app=WebAppInfo(url=url),
                )
            ]
        ]
    )


def build_menu_button(webapp_url: str) -> MenuButtonWebApp | None:
    """Create the persistent chat menu button shown next to the input field."""
    url = build_player_url(webapp_url)
    if url is None:
        return None

    return MenuButtonWebApp(
        text=MENU_BUTTON_TEXT,
        web_app=WebAppInfo(url=url),
    )


async def configure_menu_button(
    bot: Bot,
    webapp_url: str,
    chat_id: int | None = None,
) -> bool:
    """Install ``MenuButtonWebApp`` for the requested chat (or all chats).

    Returns ``True`` when Telegram acknowledged the update. Configuration
    failures are logged but never stop the API or the polling loop.
    """
    menu_button = build_menu_button(webapp_url)
    if menu_button is None:
        logger.warning(
            "WEBAPP_URL не задан или не использует HTTPS — "
            "MenuButtonWebApp не установлена"
        )
        return False

    try:
        if chat_id is None:
            await bot.set_chat_menu_button(menu_button=menu_button)
        else:
            await bot.set_chat_menu_button(
                chat_id=chat_id,
                menu_button=menu_button,
            )
        return True
    except Exception:
        logger.warning(
            "Не удалось установить MenuButtonWebApp", exc_info=True
        )
        return False


async def send_welcome(message: Message, webapp_url: str) -> None:
    """Send the welcome message and, when possible, the player button."""
    first_name = getattr(message.from_user, "first_name", None) or "друг"
    safe_name = html.escape(first_name)
    keyboard = build_player_keyboard(webapp_url)

    if keyboard is None:
        text = (
            f"🎧 <b>Привет, {safe_name}!</b>\n\n"
            "Плеер ещё не настроен: администратору нужно указать публичный "
            "HTTPS-адрес в <code>WEBAPP_URL</code> и перезапустить бота."
        )
    else:
        text = (
            f"🎧 <b>Привет, {safe_name}!</b>\n\n"
            "Здесь можно искать музыку, слушать её прямо в Telegram "
            "и сохранять любимые треки.\n\n"
            "Нажми кнопку ниже, чтобы открыть плеер."
        )

    await message.answer(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


def build_router(webapp_url: WebAppUrlSource) -> Router:
    """Create the bot router that rereads the Mini App URL on every ``/start``."""
    router = Router(name="start")

    @router.message(CommandStart())
    async def start_handler(message: Message) -> None:
        # Re-apply the menu button on demand: this heals setups where the
        # startup API call failed (for example after a temporary network error).
        url = _current_webapp_url(webapp_url)
        bot = getattr(message, "bot", None)
        if bot is not None:
            installed = await configure_menu_button(
                bot, url, chat_id=message.chat.id
            )
            if installed:
                print(
                    "INFO: Отправлена кнопка с актуальным URL: "
                    f"{build_player_url(url)}/",
                    flush=True,
                )
        await send_welcome(message, url)

    return router
