"""Tests for backend.telegram_auth (initData HMAC validation)."""

from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import urlencode

from backend.telegram_auth import validate_init_data

BOT_TOKEN = "7000000000:AAExampleTokenForTests"


def sign_init_data(
    pairs: list[tuple[str, str]],
    bot_token: str,
    *,
    unsorted: bool = False,
) -> str:
    """Compute the official initData hash for the given pairs.

    The check string follows the contract: fields except ``hash``, decoded,
    joined with ``\\n``, sorted lexicographically. ``unsorted=True`` signs a
    deliberately unsorted check string to verify that the backend rejects it.
    """
    fields = [f"{key}={value}" for key, value in pairs if key != "hash"]
    if unsorted:
        # Deliberately break the contract: fields are joined in the raw
        # order instead of the lexicographically sorted one.
        check_string = "\n".join(reversed(fields))
    else:
        check_string = "\n".join(sorted(fields))
    secret = hmac.new(
        b"WebAppData",
        bot_token.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    computed = hmac.new(
        secret, check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{urlencode(pairs, safe='{}')}&hash={computed}"


def make_init_data(
    bot_token: str = BOT_TOKEN,
    user_id: int = 9001,
    auth_date: int | None = None,
    *,
    unsorted: bool = False,
    with_user: bool = True,
) -> str:
    """Build a fully signed initData string as the Mini App would send it."""
    if auth_date is None:
        auth_date = int(time.time())
    pairs: list[tuple[str, str]] = [("auth_date", str(auth_date))]
    if with_user:
        pairs.append(("user", f'{{"id": {user_id}, "first_name": "Test"}}'))
    return sign_init_data(pairs, bot_token, unsorted=unsorted)


def test_valid_init_data_extracts_user_id():
    result = validate_init_data(make_init_data(), BOT_TOKEN)

    assert result.ok is True
    assert result.user_id == 9001
    assert result.reason == ""


def test_tampered_payload_rejected():
    init_data = make_init_data(user_id=9001)
    tampered = init_data.replace("Test", "Hacker")

    result = validate_init_data(tampered, BOT_TOKEN)

    assert result.ok is False
    assert result.user_id is None
    assert result.reason == "signature mismatch"


def test_wrong_bot_token_rejected():
    result = validate_init_data(make_init_data(), "11:OtherToken")

    assert result.ok is False
    assert result.reason == "signature mismatch"


def test_expired_auth_date_rejected():
    expired = int(time.time()) - (24 * 60 * 60) - 3600

    result = validate_init_data(make_init_data(auth_date=expired), BOT_TOKEN)

    assert result.ok is False
    assert result.reason == "auth_date is expired"


def test_future_auth_date_beyond_skew_rejected():
    future = int(time.time()) + 3600

    result = validate_init_data(make_init_data(auth_date=future), BOT_TOKEN)

    assert result.ok is False
    assert result.reason == "auth_date is in the future"


def test_fresh_auth_date_is_accepted():
    fresh = int(time.time()) - 60

    result = validate_init_data(make_init_data(auth_date=fresh), BOT_TOKEN)

    assert result.ok is True


def test_missing_hash_rejected():
    init_data = make_init_data()
    without_hash = "&".join(
        part for part in init_data.split("&") if not part.startswith("hash=")
    )

    result = validate_init_data(without_hash, BOT_TOKEN)

    assert result.ok is False
    assert result.reason == "hash field is missing"


def test_unsorted_check_string_rejected():
    result = validate_init_data(
        make_init_data(unsorted=True), BOT_TOKEN
    )

    assert result.ok is False
    assert result.reason == "signature mismatch"


def test_missing_user_rejected():
    # The payload is signed correctly but contains no user field at all.
    init_data = make_init_data(with_user=False)

    result = validate_init_data(init_data, BOT_TOKEN)

    assert result.ok is False
    assert result.user_id is None
    assert result.reason == "user id is missing"


def test_empty_init_data_rejected():
    result = validate_init_data("", BOT_TOKEN)

    assert result.ok is False
    assert result.reason == "initData is empty"


def test_blank_bot_token_reported():
    result = validate_init_data(make_init_data(), "")

    assert result.ok is False
    assert result.reason == "bot token is not configured"
