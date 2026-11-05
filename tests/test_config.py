import pytest

from config import BASE_DIR, load_settings


def test_settings_are_loaded_from_environment(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("WEBAPP_URL", "https://example.com/app/")
    monkeypatch.setenv("HOST", "127.0.0.1")
    monkeypatch.setenv("PORT", "9090")
    monkeypatch.setenv("DB_PATH", "tmp/test.db")
    monkeypatch.setenv("LOG_LEVEL", "debug")

    settings = load_settings()

    assert settings.bot_token == "123456:test-token"
    assert settings.webapp_url == "https://example.com/app"
    assert settings.host == "127.0.0.1"
    assert settings.port == 9090
    assert settings.db_path == BASE_DIR / "tmp" / "test.db"
    assert settings.log_level == "DEBUG"
    assert settings.bot_enabled is True


def test_bot_is_disabled_without_token(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "")

    settings = load_settings()

    assert settings.bot_enabled is False


def test_invalid_port_is_rejected(monkeypatch):
    monkeypatch.setenv("PORT", "not-a-number")

    with pytest.raises(ValueError, match="PORT"):
        load_settings()


def test_out_of_range_port_is_rejected(monkeypatch):
    monkeypatch.setenv("PORT", "70000")

    with pytest.raises(ValueError, match="between 1 and 65535"):
        load_settings()
