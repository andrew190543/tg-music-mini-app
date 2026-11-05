"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

DEFAULT_DB_PATH = "data/music.db"


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


def load_settings() -> Settings:
    """Read settings from the environment, applying safe defaults."""
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    webapp_url = os.getenv("WEBAPP_URL", "").strip().rstrip("/")

    raw_port = os.getenv("PORT", "8080").strip() or "8080"
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
