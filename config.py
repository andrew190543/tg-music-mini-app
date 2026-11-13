"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

load_dotenv(ENV_PATH)
_ENV_FILE_KEYS = frozenset(dotenv_values(ENV_PATH))

DEFAULT_DB_PATH = "data/music.db"
DEFAULT_PORT = 8080


def _reread_env_value(key: str, default: str = "") -> str:
    """Return the current value of ``key`` from `.env` or `os.environ`."""
    try:
        values = dotenv_values(ENV_PATH)
    except Exception:
        values = {}
    if key in values and values[key] is not None:
        return str(values[key] or "")
    if key in _ENV_FILE_KEYS:
        return default
    return os.getenv(key, default)


@dataclass(frozen=True)
class Settings:
    """Runtime settings shared by the bot and the API."""

    bot_token: str
    webapp_url: str
    host: str
    port: int
    db_path: Path
    log_level: str
    proxy_url: str = ""

    @property
    def bot_enabled(self) -> bool:
        """The bot is started only when a real-looking token is configured."""
        return bool(self.bot_token) and ":" in self.bot_token


def get_webapp_url() -> str:
    """Return the current ``WEBAPP_URL`` reread from ``.env`` on each call."""
    return _reread_env_value("WEBAPP_URL").strip().strip("\"'").rstrip("/")


def get_bot_token() -> str:
    """Return the current ``BOT_TOKEN`` reread from ``.env`` on each call."""
    return _reread_env_value("BOT_TOKEN").strip().strip("\"'")


def get_auth_mode() -> str:
    """Return ``AUTH_MODE`` as ``auto`` (default) or ``off``."""
    raw = _reread_env_value("AUTH_MODE").strip().lower()
    return raw if raw in {"auto", "off"} else "auto"


def get_proxy_url() -> str:
    """Return explicit proxy URL from ``YTDLP_PROXY`` or ``PROXY_URL``."""
    for key in ("YTDLP_PROXY", "PROXY_URL", "HTTPS_PROXY", "HTTP_PROXY"):
        val = _reread_env_value(key).strip().strip("\"'")
        if val:
            return val
    return ""


def load_settings() -> Settings:
    """Read settings from the environment, applying safe defaults."""
    bot_token = get_bot_token()
    webapp_url = get_webapp_url()
    proxy_url = get_proxy_url()

    if proxy_url and not os.environ.get("YTDLP_PROXY"):
        os.environ["YTDLP_PROXY"] = proxy_url

    raw_port = _reread_env_value("PORT", str(DEFAULT_PORT)).strip() or str(
        DEFAULT_PORT
    )
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ValueError(f"PORT must be an integer, got {raw_port!r}") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"PORT must be between 1 and 65535, got {port}")

    raw_db_path = (
        _reread_env_value("DB_PATH", DEFAULT_DB_PATH).strip() or DEFAULT_DB_PATH
    )
    db_path = Path(raw_db_path)
    if not db_path.is_absolute():
        db_path = BASE_DIR / db_path

    log_level = _reread_env_value("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    host = _reread_env_value("HOST", "0.0.0.0").strip() or "0.0.0.0"

    return Settings(
        bot_token=bot_token,
        webapp_url=webapp_url,
        host=host,
        port=port,
        db_path=db_path,
        log_level=log_level,
        proxy_url=proxy_url,
    )