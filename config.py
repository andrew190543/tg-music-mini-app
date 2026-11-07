"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

# Keys present in the .env file when the process started. ``load_dotenv``
# copies .env-only values verbatim into ``os.environ`` and never refreshes
# them: the getters below must be able to tell such stale copies apart from
# genuinely OS-provided variables.
load_dotenv(ENV_PATH)
_ENV_FILE_KEYS = frozenset(dotenv_values(ENV_PATH))

DEFAULT_DB_PATH = "data/music.db"
DEFAULT_PORT = 8000


def _reread_env_value(key: str) -> str:
    """Return the current value of ``key`` from the live configuration.

    Reading order (first match wins):

    1. a fresh re-read of the ``.env`` file — it is the deployment
       switchboard and can change at runtime (e.g. the tunnel address moves);
    2. ``os.environ`` — real OS-level variables, values injected after the
       process start (tests) and the copy ``load_dotenv()`` made at startup;
    3. the ``default``.
    """
    values = dotenv_values(ENV_PATH)
    if key in values:
        return str(values[key] or "")
    if key in _ENV_FILE_KEYS:
        # The key used to be in .env, so the ``os.environ`` copy was made by
        # load_dotenv at startup. Now the key is gone from the file and the
        # identical live edit is honoured: the stale cache must not win.
        return ""
    return os.getenv(key, "")


@dataclass(frozen=True)
class Settings:
    """Runtime settings shared by the bot and the API."""

    bot_token: str
    webapp_url: str
    host: str
    port: int
    db_path: Path
    log_level: str

    @property
    def bot_enabled(self) -> bool:
        """The bot is started only when a real-looking token is configured."""
        return bool(self.bot_token) and ":" in self.bot_token


def get_webapp_url() -> str:
    """Return the current ``WEBAPP_URL`` reread from ``.env`` on each call.

    The tunnel address can change during the bot lifetime, so it is important
    that ``/start`` does not use the stale value cached by ``load_dotenv``.
    """
    return _reread_env_value("WEBAPP_URL").strip().rstrip("/")


def get_bot_token() -> str:
    """Return the current ``BOT_TOKEN`` reread from ``.env`` on each call.

    Like :func:`get_webapp_url`, the token must never be cached: it can be
    rotated while the process is running and the API rechecks it on every
    authenticated request.
    """
    return _reread_env_value("BOT_TOKEN").strip()


def get_auth_mode() -> str:
    """Return ``AUTH_MODE`` as ``auto`` (default) or ``off``.

    * ``auto`` - when a bot token is configured, favorites endpoints require
      valid signed initData; without a token they fall back to the explicit
      ``user_id`` (local development and tests).
    * ``off``  - always use the fallback ``user_id``, even with a token.
    """
    raw = _reread_env_value("AUTH_MODE").strip().lower()
    return raw if raw in {"auto", "off"} else "auto"


def load_settings() -> Settings:
    """Read settings from the environment, applying safe defaults."""
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    webapp_url = os.getenv("WEBAPP_URL", "").strip().rstrip("/")

    raw_port = os.getenv("PORT", str(DEFAULT_PORT)).strip() or str(
        DEFAULT_PORT
    )
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ValueError(f"PORT must be an integer, got {raw_port!r}") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"PORT must be between 1 and 65535, got {port}")

    raw_db_path = os.getenv("DB_PATH", DEFAULT_DB_PATH).strip() or DEFAULT_DB_PATH
    db_path = Path(raw_db_path)
    if not db_path.is_absolute():
        db_path = BASE_DIR / db_path

    log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"

    return Settings(
        bot_token=bot_token,
        webapp_url=webapp_url,
        host=os.getenv("HOST", "0.0.0.0").strip() or "0.0.0.0",
        port=port,
        db_path=db_path,
        log_level=log_level,
    )
