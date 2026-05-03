"""DigitalOcean Spaces client wrapper.

Spaces is S3-compatible, so we use boto3 with a Spaces endpoint URL. The client
is created lazily so the app boots even when Spaces credentials are missing
(local dev, CI). Sprint 6 (receipts) is the first real consumer.
"""

from __future__ import annotations

from datetime import timedelta
from functools import lru_cache
from typing import TYPE_CHECKING, Any

import boto3
from botocore.client import Config

from app.core.config import get_settings

if TYPE_CHECKING:
    pass


class SpacesNotConfigured(RuntimeError):
    """Raised when a Spaces operation is attempted without env vars set."""


@lru_cache(maxsize=1)
def get_spaces_client() -> Any:  # boto3 client type is dynamic
    """Return a configured boto3 S3 client pointed at DigitalOcean Spaces."""
    s = get_settings()
    missing = [
        name
        for name, value in (
            ("DO_SPACES_ENDPOINT", s.spaces_endpoint),
            ("DO_SPACES_REGION", s.spaces_region),
            ("DO_SPACES_KEY", s.spaces_key),
            ("DO_SPACES_SECRET", s.spaces_secret),
        )
        if not value
    ]
    if missing:
        raise SpacesNotConfigured(f"Spaces not configured; missing: {', '.join(missing)}")

    return boto3.client(
        "s3",
        endpoint_url=s.spaces_endpoint,
        region_name=s.spaces_region,
        aws_access_key_id=s.spaces_key,
        aws_secret_access_key=s.spaces_secret,
        config=Config(signature_version="s3v4"),
    )


def is_configured() -> bool:
    """Cheap check used by /health/ready and Sprint 6 receipts."""
    s = get_settings()
    return bool(
        s.spaces_endpoint
        and s.spaces_region
        and s.spaces_key
        and s.spaces_secret
        and s.spaces_bucket
    )


def presigned_put_url(
    key: str,
    *,
    content_type: str,
    expires_in: timedelta = timedelta(minutes=15),
) -> str:
    """Pre-signed PUT URL the mobile app uses to upload a receipt photo directly."""
    s = get_settings()
    if not s.spaces_bucket:
        raise SpacesNotConfigured("DO_SPACES_BUCKET is not set")
    client = get_spaces_client()
    url: str = client.generate_presigned_url(
        "put_object",
        Params={"Bucket": s.spaces_bucket, "Key": key, "ContentType": content_type},
        ExpiresIn=int(expires_in.total_seconds()),
        HttpMethod="PUT",
    )
    return url


def presigned_get_url(key: str, *, expires_in: timedelta = timedelta(minutes=15)) -> str:
    """Pre-signed GET URL for retrieving a receipt photo."""
    s = get_settings()
    if not s.spaces_bucket:
        raise SpacesNotConfigured("DO_SPACES_BUCKET is not set")
    client = get_spaces_client()
    url: str = client.generate_presigned_url(
        "get_object",
        Params={"Bucket": s.spaces_bucket, "Key": key},
        ExpiresIn=int(expires_in.total_seconds()),
        HttpMethod="GET",
    )
    return url
