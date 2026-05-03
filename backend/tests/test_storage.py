"""Storage / Spaces wrapper tests. Pure-config; no network."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.core import storage
from app.core.config import get_settings


def _reset_caches() -> None:
    get_settings.cache_clear()
    storage.get_spaces_client.cache_clear()


def test_not_configured_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in (
        "DO_SPACES_ENDPOINT",
        "DO_SPACES_REGION",
        "DO_SPACES_BUCKET",
        "DO_SPACES_KEY",
        "DO_SPACES_SECRET",
    ):
        monkeypatch.delenv(k, raising=False)
    _reset_caches()
    assert storage.is_configured() is False


def test_presigned_put_raises_when_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in (
        "DO_SPACES_ENDPOINT",
        "DO_SPACES_REGION",
        "DO_SPACES_BUCKET",
        "DO_SPACES_KEY",
        "DO_SPACES_SECRET",
    ):
        monkeypatch.delenv(k, raising=False)
    _reset_caches()
    with pytest.raises(storage.SpacesNotConfigured):
        storage.presigned_put_url("receipts/x.jpg", content_type="image/jpeg")


def test_presigned_put_returns_url_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DO_SPACES_ENDPOINT", "https://nyc3.digitaloceanspaces.com")
    monkeypatch.setenv("DO_SPACES_REGION", "nyc3")
    monkeypatch.setenv("DO_SPACES_BUCKET", "reorderos-receipts")
    monkeypatch.setenv("DO_SPACES_KEY", "AKIAIOSFODNN7EXAMPLE")
    monkeypatch.setenv("DO_SPACES_SECRET", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
    _reset_caches()
    assert storage.is_configured() is True
    url = storage.presigned_put_url(
        "receipts/abc.jpg",
        content_type="image/jpeg",
        expires_in=timedelta(minutes=5),
    )
    # boto3 defaults to path-style: <endpoint>/<bucket>/<key>?<sig...>
    assert url.startswith("https://nyc3.digitaloceanspaces.com/reorderos-receipts/receipts/abc.jpg")
    assert "X-Amz-Expires=300" in url
    assert "X-Amz-Signature=" in url
