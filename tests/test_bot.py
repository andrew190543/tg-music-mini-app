from types import SimpleNamespace

import pytest
from aiogram.enums import ParseMode
from aiogram.types import MenuButtonWebApp

from bot.handlers import (
    build_menu_button,
    build_player_keyboard,
    build_player_url,
    configure_menu_button,
    reset_menu_button_cache,
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
        self.calls: list[tuple[MenuButtonWebApp | None, int | None]] = []

    async def set_chat_menu_button(self, menu_button=None, chat_id=None):
        self.calls.append((menu_button, chat_id))
        if self.error is not None:
            raise self.error
        self.menu_button = menu_button
        return True


@pytest.fixture(autouse=True)
def fresh_menu_button_cache():
    """The module-level cache is process-wide: isolate every test."""
    reset_menu_button_cache()
    yield
    reset_menu_button_cache()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://example.com", "https://example.com/app/"),
        ("https://example.com/", "https://example.com/app/"),
        ("https://example.com/app", "https://example.com/app/"),
        ("https://example.com/app/", "https://example.com/app/"),
        ("https://example.com/base", "https://example.com/base/app/"),
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
    assert button.web_app.url == "https://example.com/app/"

    keyboard = build_player_keyboard("https://example.com/app")
    assert keyboard.inline_keyboard[0][0].web_app.url == "https://example.com/app/"


def test_menu_button_uses_webapp_url():
    assert build_menu_button("") is None
    assert build_menu_button("http://localhost") is None

    menu_button = build_menu_button("https://example.com")
    assert isinstance(menu_button, MenuButtonWebApp)
    assert menu_button.text == "Плеер"
    assert menu_button.web_app.url == "https://example.com/app/"


@pytest.mark.asyncio
async def test_configure_menu_button_installs_webapp_button():
    bot = FakeBot()

    installed = await configure_menu_button(bot, "https://example.com")

    assert installed is True
    assert isinstance(bot.menu_button, MenuButtonWebApp)
    assert bot.menu_button.web_app.url == "https://example.com/app/"


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
async def test_same_url_is_installed_only_once():
    """Repeated /start with an unchanged URL must not re-call the API."""
    bot = FakeBot()

    first = await configure_menu_button(bot, "https://example.com")
    second = await configure_menu_button(bot, "https://example.com")

    assert first is True
    assert second is True
    assert len(bot.calls) == 1


@pytest.mark.asyncio
async def test_changed_url_is_installed_again():
    """A moved tunnel (new WEBAPP_URL) must reinstall the menu button."""
    bot = FakeBot()

    await configure_menu_button(bot, "https://old.example")
    await configure_menu_button(bot, "https://new.example")

    assert len(bot.calls) == 2
    assert bot.menu_button.web_app.url == "https://new.example/app/"


@pytest.mark.asyncio
async def test_failed_install_is_not_cached():
    """The healing behaviour stays intact: failures retry on next /start."""
    bot = FakeBot(error=RuntimeError("temporary Telegram error"))

    assert await configure_menu_button(bot, "https://example.com") is False
    bot.error = None
    assert await configure_menu_button(bot, "https://example.com") is True
    assert len(bot.calls) == 2


@pytest.mark.asyncio
async def test_cache_is_per_chat_scope():
    """The default (all chats) and per-chat scopes are cached separately."""
    bot = FakeBot()

    await configure_menu_button(bot, "https://example.com")
    await configure_menu_button(bot, "https://example.com", chat_id=42)
    await configure_menu_button(bot, "https://example.com", chat_id=42)

    assert len(bot.calls) == 2
    assert bot.calls[1] == (bot.calls[1][0], 42)
    assert bot.menu_button.web_app.url == "https://example.com/app/"


@pytest.mark.asyncio
async def test_nothing_logged_when_url_invalid():
    """Empty/HTTP WEBAPP_URL must never produce a `None/…` URL anywhere."""
    bot = FakeBot()

    installed = await configure_menu_button(bot, "")

    assert installed is False
    assert bot.calls == []
    assert build_player_url("") is None


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
