"""Unit tests for the staging cert harness's target-identity guard.

These prove the guard cannot be pointed at the wrong database: it proceeds ONLY
for the known staging identity, and refuses for missing-flag, production, and
unknown identities. Pure — no DB or app dependency.
"""

from __future__ import annotations

from scripts.staging_cert.guard import (
    STAGING_DB_FINGERPRINT,
    db_fingerprint,
    guard_decision,
)

_STAGING_URL = "postgresql://u:p@staging-db.example.ondigitalocean.com:25060/defaultdb"


def test_db_fingerprint_is_stable_and_credential_free() -> None:
    a = db_fingerprint(_STAGING_URL)
    b = db_fingerprint(
        "postgresql+asyncpg://u:p@staging-db.example.ondigitalocean.com:25060/defaultdb"
    )
    # deterministic + independent of the driver prefix and (host/port/db only) creds
    assert a == b
    assert len(a) == 16 and all(c in "0123456789abcdef" for c in a)
    # different host or db → different fingerprint
    assert (
        db_fingerprint("postgresql://u:p@prod-db.example.ondigitalocean.com:25060/defaultdb") != a
    )
    assert db_fingerprint("postgresql://u:p@staging-db.example.ondigitalocean.com:25060/other") != a


def test_guard_missing_flag_refuses() -> None:
    ok, why = guard_decision(allow_flag=None, fingerprint=STAGING_DB_FINGERPRINT)
    assert ok is False and "ALLOW_STAGING_CERT" in why
    ok2, _ = guard_decision(allow_flag="0", fingerprint=STAGING_DB_FINGERPRINT)
    assert ok2 is False


def test_guard_staging_identity_passes() -> None:
    ok, why = guard_decision(allow_flag="1", fingerprint=STAGING_DB_FINGERPRINT)
    assert ok is True and why == "ok"


def test_guard_production_identity_refuses() -> None:
    # A production fingerprint is simply any fingerprint not on the allowlist.
    prod_fp = db_fingerprint("postgresql://u:p@prod-db.example.ondigitalocean.com:25060/defaultdb")
    assert prod_fp != STAGING_DB_FINGERPRINT
    ok, why = guard_decision(allow_flag="1", fingerprint=prod_fp)
    assert ok is False and "not the allowed staging identity" in why


def test_guard_unknown_identity_refuses() -> None:
    ok, why = guard_decision(allow_flag="1", fingerprint="deadbeefdeadbeef")
    assert ok is False and "not the allowed staging identity" in why


def test_guard_allowlist_is_staging_only() -> None:
    from scripts.staging_cert.guard import ALLOWED_FINGERPRINTS

    assert ALLOWED_FINGERPRINTS == frozenset({STAGING_DB_FINGERPRINT})
