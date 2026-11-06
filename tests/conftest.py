"""Shared pytest fixtures.

The repository ships with an optional local ``.env`` (gitignored) that can
contain a real ``BOT_TOKEN``. With a token configured, the favorites
endpoints switch to strict initData validation and every API test that posts
plain ``user_id`` payloads would fail with 401. The autouse fixture below
forces the unauthenticated fallback for all tests unless a test explicitly
re-enables the token (auth tests patch it themselves).
"""

from __future__ import annotations

import pytest

from backend import api as api_module


@pytest.fixture(autouse=True)
def force_fallback_auth(monkeypatch):
    """Make the API run in fallback mode (no bot token) for every test."""
    monkeypatch.setattr(api_module, "get_bot_token", lambda: "")
