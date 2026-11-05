from types import SimpleNamespace

import pytest
from aiogram.enums import ParseMode
from aiogram.types import MenuButtonWebApp

from bot.handlers import (
    build_menu_button,
    build_player_keyboard,
    build_player_url,
    configure_menu_button,
    send_welcome,
)


class FakeMessage:
    def __init__(self, first_name: str = "Аня") -> None:
        self.from_user = SimpleNamespace(first_name=first_name)
        self.answer_args = None

    async def answer(self, text: str, **kwargs):
        self.answer_args = (text, kwargs)


class FakeBot:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.menu_button = None

    async def set_chat_menu_button(self, menu_button):
        if self.error is not None:
            raise self.error
        self.menu_button = menu_button
        return True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://example.com", "https://example.com/app"),
        ("https://example.com/", "https://example.com/app"),
        ("https://example.com/app", "https://example.com/app"),
        ("https://example.com/app/", "https://example.com/app"),
        ("https://example.com/base", "https://example.com/base/app"),
        ("http://localhost:8000", None),
        ("", None),
    ],
)
def test_player_url_appends_app_suffix(raw, expected):
    assert build_player_url(raw) == expected


def test_player_keyboard_requires_https():
    assert build_player_keyboard("") is None
    assert build_player_keyboard("http://localhost:8080") is None

    keyboard = build_player_keyboard("https://example.com")
    assert keyboard is not None
    button = keyboard.inline_keyboard[0][0]
    assert button.text == "🎧 Открыть плеер"
    assert button.web_app.url == "https://example.com/app"

    keyboard = build_player_keyboard("https://example.com/app")
    assert keyboard.inline_keyboard[0][0].web_app.url == "https://example.com/app"


def test_menu_button_uses_webapp_url():
    assert build_menu_button("") is None
    assert build_menu_button("http://localhost") is None

    menu_button = build_menu_button("https://example.com")
    assert isinstance(menu_button, MenuButtonWebApp)
    assert menu_button.text == "Плеер"
    assert menu_button.web_app.url == "https://example.com/app"


@pytest.mark.asyncio
async def test_configure_menu_button_installs_webapp_button():
    bot = FakeBot()

    installed = await configure_menu_button(bot, "https://example.com")

    assert installed is True
    assert isinstance(bot.menu_button, MenuButtonWebApp)
    assert bot.menu_button.web_app.url == "https://example.com/app"


@pytest.mark.asyncio
async def test_configure_menu_button_skips_invalid_url():
    bot = FakeBot()

    installed = await configure_menu_button(bot, "http://localhost:8000")

    assert installed is False
    assert bot.menu_button is None


@pytest.mark.asyncio
async def test_configure_menu_button_survives_api_error():
    bot = FakeBot(error=RuntimeError("temporary Telegram error"))

    installed = await configure_menu_button(bot, "https://example.com")

    assert installed is False
    assert bot.menu_button is None


@pytest.mark.asyncio
async def test_welcome_message_contains_player_button():
    message = FakeMessage("<Аня>")

    await send_welcome(message, "https://example.com/app")

    text, kwargs = message.answer_args
    assert "&lt;Аня&gt;" in text
    assert kwargs["parse_mode"] == ParseMode.HTML
    assert kwargs["reply_markup"] is not None
    assert kwargs["link_preview_options"].is_disabled is True


@pytest.mark.asyncio
async def test_welcome_message_explains_missing_webapp_url():
    message = FakeMessage()

    await send_welcome(message, "")

    text, kwargs = message.answer_args
    assert "WEBAPP_URL" in text
    assert kwargs["reply_markup"] is None
