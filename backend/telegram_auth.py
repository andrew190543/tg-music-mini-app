"""Validation of Telegram Mini App ``initData`` per the official HMAC scheme.

Implements the verification described in
https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

The Mini App sends its raw ``initData`` in the ``Authorization`` header with
the ``tma`` scheme; the backend re-computes ``hash`` from the sorted
``data_check_string`` using ``HMAC(bot_secret_key)`` as the key and checks the
freshness of ``auth_date``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl

# Data older than this cannot be trusted: one replay window per day.
MAX_AUTH_AGE_SECONDS = 24 * 60 * 60
# ``auth_date`` is generated on the client \u2014 allowance for a small clock skew.
MAX_CLOCK_SKEW_SECONDS = 300


@dataclass(frozen=True, slots=True)
class InitDataResult:
    """Outcome of ``initData`` validation.

    On success ``user_id`` is set and ``reason`` is empty; on failure
    both fields describe why the data was rejected (``user_id`` stays None).
    """

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
    # Bot secret key = HMAC_SHA256(bot_token, key="WebAppData").
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
    """Validate signed WebApp ``initData`` and extract the user id.

    The check string is built from **decoded** query parts (``parse_qsl``),
    sorted lexicographically, with only ``hash`` excluded \u2014 other signature
    fields (e.g. Ed25519 ``signature``) take part as regular strings.

    Returns a failed :class:`InitDataResult` (never raises) whenever the
    signature, freshness or user payload does not check out.
    """
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
