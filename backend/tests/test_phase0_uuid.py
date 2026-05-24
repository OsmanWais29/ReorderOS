"""Phase 0 — Test 8: uuid6 library produces valid UUIDv7 values.

Pure Python — no database required.
"""

from __future__ import annotations


def test_uuid7_importable_and_produces_valid_uuids():
    """uuid7() must be importable and produce well-formed UUIDv7 values.

    Why: Every Sprint 4 table PK is UUIDv7 (application-generated, not
    gen_random_uuid()). If uuid6 isn't installed or produces malformed
    UUIDs, every INSERT in Phases 1–7 fails on the first call. Catching
    it here costs nothing; catching it mid-Phase 5 during an OAuth
    callback costs debug time.
    """
    from uuid6 import uuid7

    uid = uuid7()
    uid_str = str(uid)

    assert len(uid_str) == 36, (
        f"uuid7() produced wrong length string: '{uid_str}' (len={len(uid_str)})"
    )
    assert uid_str.count("-") == 4, (
        f"uuid7() format wrong — expected 4 hyphens: '{uid_str}'"
    )

    # UUIDv7 version nibble is the 13th character (index 14 in the string).
    # Format: xxxxxxxx-xxxx-7xxx-xxxx-xxxxxxxxxxxx
    version_char = uid_str[14]
    assert version_char == "7", (
        f"uuid7() produced version nibble '{version_char}', expected '7'"
    )


def test_uuid7_values_are_unique():
    """Successive uuid7() calls must produce different values.

    Why: UUIDv7 includes a millisecond timestamp component. Two calls
    within the same millisecond use a random sub-ms sequence to stay
    unique. If the library is broken and returns the same value twice,
    every batch INSERT will conflict on the PK.
    """
    from uuid6 import uuid7

    uids = {str(uuid7()) for _ in range(100)}
    assert len(uids) == 100, (
        f"uuid7() produced duplicates in 100 calls — got only {len(uids)} unique values"
    )


def test_uuid7_is_str_compatible():
    """str(uuid7()) must produce a plain UUID string, not require extra encoding.

    Why: The worker passes str(uuid7()) directly into SQLAlchemy text()
    queries as :id parameters. If the library produces a non-standard
    type that str() doesn't convert cleanly, asyncpg will reject the
    parameter type.
    """
    from uuid6 import uuid7

    uid = uuid7()
    uid_str = str(uid)

    # Must parse back to a standard UUID without error
    import uuid
    parsed = uuid.UUID(uid_str)
    assert str(parsed) == uid_str
