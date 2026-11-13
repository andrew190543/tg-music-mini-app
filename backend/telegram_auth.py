"""Validation of Telegram Mini App ``initData`` per the official HMAC scheme."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl

MAX_AUTH_AGE_SECONDS = 24 * 60 * 60
MAX_CLOCK_SKEW_SECONDS = 300


@dataclass(frozen=True, slots=True)
class InitDataResult:
    """Outcome of ``initData`` validation."""

    user_id: int | None
    reason: str

    @property
    def ok(self) -> bool:
        return self.user_id is not None


def _build_data_check_string(pairs: list[tuple[str, str]]) -> str:
    """Join every field except ``hash`` into the sorted check string."""
    relevant = sorted(
        f"{key}={value}" for key, value in pairs if key != "hash"
    )
    return "\n".join(relevant)


def _signature_secret(bot_token: str) -> bytes:
    return hmac.new(
        b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256
    ).digest()


def _user_id_from_pairs(pairs: list[tuple[str, str]]) -> int | None:
    raw_user = dict(pairs).get("user")
    if not raw_user:
        return None
    try:
        user: Any = json.loads(raw_user)
    except (ValueError, TypeError):
        return None
    if not isinstance(user, dict):
        return None
    try:
        return int(user.get("id"))
    except (TypeError, ValueError):
        return None


def _auth_date_from_pairs(pairs: list[tuple[str, str]]) -> int | None:
    raw = dict(pairs).get("auth_date")
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def validate_init_data(init_data: str, bot_token: str) -> InitDataResult:
    """Validate signed WebApp ``initData`` and extract the user id."""
    if not bot_token:
        return InitDataResult(user_id=None, reason="bot token is not configured")

    raw = (init_data or "").strip()
    if not raw:
        return InitDataResult(user_id=None, reason="initData is empty")

    pairs = parse_qsl(raw, keep_blank_values=True)
    provided_hash = dict(pairs).get("hash", "")
    if not provided_hash:
        return InitDataResult(user_id=None, reason="hash field is missing")

    data_check_string = _build_data_check_string(pairs)
    computed_hash = hmac.new(
        _signature_secret(bot_token),
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(computed_hash, provided_hash):
        return InitDataResult(user_id=None, reason="signature mismatch")

    auth_date = _auth_date_from_pairs(pairs)
    if auth_date is None:
        return InitDataResult(user_id=None, reason="auth_date is missing")

    now = int(time.time())
    if auth_date > now + MAX_CLOCK_SKEW_SECONDS:
        return InitDataResult(user_id=None, reason="auth_date is in the future")
    if now - auth_date > MAX_AUTH_AGE_SECONDS + MAX_CLOCK_SKEW_SECONDS:
        return InitDataResult(user_id=None, reason="auth_date is expired")

    user_id = _user_id_from_pairs(pairs)
    if user_id is None or user_id <= 0:
        return InitDataResult(user_id=None, reason="user id is missing")

    return InitDataResult(user_id=user_id, reason="")