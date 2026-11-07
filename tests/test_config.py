import pytest

import config
from config import DEFAULT_PORT, BASE_DIR, load_settings


def test_music_default_port_is_8000():
    """README, tunnels and diagnose.py all target 8000 — so must config."""
    assert config.DEFAULT_PORT == 8000
    assert DEFAULT_PORT == 8000


def test_default_port_is_used_when_port_unset(monkeypatch):
    monkeypatch.delenv("PORT", raising=False)

    settings = load_settings()

    assert settings.port == 8000


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


def test_get_webapp_url_rereads_env_file(tmp_path, monkeypatch):
    env_path = tmp_path / "custom.env"
    monkeypatch.setattr(config, "ENV_PATH", env_path)

    env_path.write_text("WEBAPP_URL=https://old.example\n", encoding="utf-8")
    assert config.get_webapp_url() == "https://old.example"

    env_path.write_text("WEBAPP_URL=https://new.example\n", encoding="utf-8")
    assert config.get_webapp_url() == "https://new.example"


def test_get_webapp_url_without_trailing_slash(tmp_path, monkeypatch):
    env_path = tmp_path / "custom.env"
    monkeypatch.setattr(config, "ENV_PATH", env_path)

    env_path.write_text(
        "WEBAPP_URL=https://example.com/app/\n", encoding="utf-8"
    )
    assert config.get_webapp_url() == "https://example.com/app"


def test_get_webapp_url_prefers_env_file_over_stale_os_environ(
    tmp_path, monkeypatch
):
    """The .env-file value wins: it is re-read to catch tunnel moves."""
    env_path = tmp_path / "custom.env"
    monkeypatch.setattr(config, "ENV_PATH", env_path)
    monkeypatch.setenv("WEBAPP_URL", "https://stale.example")

    env_path.write_text(
        "WEBAPP_URL=https://fresh-tunnel.example\n", encoding="utf-8"
    )
    assert config.get_webapp_url() == "https://fresh-tunnel.example"


def test_removed_webapp_url_key_ignores_stale_cached_value(
    tmp_path, monkeypatch
):
    """A key removed from .env must not be resurrected from os.environ.

    ``load_dotenv`` copies .env-only keys into the process environment once
    and never refreshes them; if the line is later deleted from the file,
    the stale copy would otherwise keep an old tunnel URL alive forever.
    """
    env_path = tmp_path / "custom.env"
    monkeypatch.setattr(config, "ENV_PATH", env_path)
    monkeypatch.setattr(config, "_ENV_FILE_KEYS", frozenset({"WEBAPP_URL"}))
    monkeypatch.setenv("WEBAPP_URL", "https://stale-tunnel.example")

    # The key is gone from the file now:
    env_path.write_text("BOT_TOKEN=123456:test\n", encoding="utf-8")
    assert config.get_webapp_url() == ""


def test_removed_bot_token_key_is_also_reset(tmp_path, monkeypatch):
    env_path = tmp_path / "custom.env"
    monkeypatch.setattr(config, "ENV_PATH", env_path)
    monkeypatch.setattr(config, "_ENV_FILE_KEYS", frozenset({"BOT_TOKEN"}))
    monkeypatch.setenv("BOT_TOKEN", "123456:stale-token")

    env_path.write_text("WEBAPP_URL=https://example.com\n", encoding="utf-8")
    assert config.get_bot_token() == ""


def test_get_bot_token_reads_env_file_value(tmp_path, monkeypatch):
    env_path = tmp_path / "custom.env"
    monkeypatch.setattr(config, "ENV_PATH", env_path)

    env_path.write_text("BOT_TOKEN=123456:abc\n", encoding="utf-8")
    assert config.get_bot_token() == "123456:abc"

    env_path.write_text("BOT_TOKEN=654321:xyz\n", encoding="utf-8")
    assert config.get_bot_token() == "654321:xyz"


def test_auth_mode_falls_back_to_auto(tmp_path, monkeypatch):
    env_path = tmp_path / "custom.env"
    monkeypatch.setattr(config, "ENV_PATH", env_path)
    monkeypatch.delenv("AUTH_MODE", raising=False)

    env_path.write_text("", encoding="utf-8")
    assert config.get_auth_mode() == "auto"

    env_path.write_text("AUTH_MODE=off\n", encoding="utf-8")
    assert config.get_auth_mode() == "off"

    env_path.write_text("AUTH_MODE=bogus\n", encoding="utf-8")
    assert config.get_auth_mode() == "auto"
