"""Phase 0 — Tests 1–4: TokenEncryption class.

Tests 1-4 are pure Python — no database required.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet


# ── Test 1: Round-trip ────────────────────────────────────────────────────────


def test_encrypt_decrypt_roundtrip():
    """Encrypt a realistic token string, decrypt it, compare.

    Why: Fernet produces base64 ciphertext. The Sprint 4 schema stores
    this as text (not bytea). If encrypt() returns bytes but the DB
    column expects str, or if decrypt() expects bytes but gets str back
    from the DB, the round-trip breaks silently — you'd store a token
    you can never read back.
    """
    from app.core.encryption import TokenEncryption

    enc = TokenEncryption()
    plaintext = "clover_access_token_abc123_live"

    ciphertext = enc.encrypt(plaintext)

    # Must be a str — matches the text column in tenant_pos_connections
    assert isinstance(ciphertext, str), (
        f"encrypt() should return str for text column, got {type(ciphertext)}"
    )

    # Must differ from plaintext — encryption actually happened
    assert ciphertext != plaintext, "encrypt() returned plaintext unchanged"

    # Round-trip must recover the original value exactly
    decrypted = enc.decrypt(ciphertext)
    assert decrypted == plaintext, (
        f"Round-trip failed: expected '{plaintext}', got '{decrypted}'"
    )


# ── Test 2: Garbage rejection ─────────────────────────────────────────────────


def test_decrypt_rejects_garbage():
    """Garbage input must raise, not silently return corrupted plaintext.

    Why: If decrypt() fails silently on bad input, the worker sends a
    garbage Bearer token to Clover, gets 401, retries 5 times, and
    dead-letters the event — with no indication the root cause was a
    corrupted stored token. A loud exception lets mark_failed write a
    clear error message immediately.
    """
    from app.core.encryption import TokenEncryption

    enc = TokenEncryption()

    with pytest.raises(Exception):
        enc.decrypt("this-is-not-valid-fernet-ciphertext")


# ── Test 3: Missing key fails at construction time ────────────────────────────


def test_missing_encryption_key_raises_at_construction(monkeypatch):
    """Missing TOKEN_ENCRYPTION_KEY must crash at construction, not at first use.

    Why: If the class silently initialises without a key, the first
    encrypt() call fails deep inside an OAuth callback — hard to debug
    in production. Failing at construction means the FastAPI lifespan
    raises immediately, DO App Platform marks the deploy as failed, and
    you get an alert before any user hits the broken path.
    """
    from app.core.config import get_settings

    # Set to empty string — pydantic-settings gives env vars precedence over
    # the .env file, so this overrides the file value. Empty string is falsy,
    # which triggers the "not set" guard in TokenEncryption.__init__.
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "")
    monkeypatch.delenv("TOKEN_ENCRYPTION_KEY_PREVIOUS", raising=False)
    get_settings.cache_clear()

    try:
        from app.core.encryption import TokenEncryption

        with pytest.raises((RuntimeError, ValueError, KeyError, TypeError)):
            TokenEncryption()
    finally:
        # Restore cache so subsequent tests use the real settings
        get_settings.cache_clear()


# ── Test 4: Key rotation ──────────────────────────────────────────────────────


def test_key_rotation_decrypts_tokens_encrypted_with_old_key(monkeypatch):
    """Tokens encrypted with the previous key must still decrypt after rotation.

    Why: In production, key rotation means:
      1. TOKEN_ENCRYPTION_KEY_PREVIOUS = old key
      2. TOKEN_ENCRYPTION_KEY = new key
      3. Redeploy

    After step 3, every existing token in tenant_pos_connections was
    encrypted with the old key. If MultiFernet doesn't fall back to the
    previous key, every merchant's connection breaks simultaneously —
    they'd all need to re-authorize via OAuth.
    """
    from app.core.config import get_settings
    from app.core.encryption import TokenEncryption

    key_a = Fernet.generate_key().decode()
    key_b = Fernet.generate_key().decode()

    # Encrypt with key A as current
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", key_a)
    monkeypatch.delenv("TOKEN_ENCRYPTION_KEY_PREVIOUS", raising=False)
    get_settings.cache_clear()

    try:
        enc_old = TokenEncryption()
        ciphertext = enc_old.encrypt("clover_refresh_token_xyz")

        # Rotate: key B is current, key A is previous
        monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", key_b)
        monkeypatch.setenv("TOKEN_ENCRYPTION_KEY_PREVIOUS", key_a)
        get_settings.cache_clear()

        enc_new = TokenEncryption()

        # Must still decrypt the token encrypted with the old key
        assert enc_new.decrypt(ciphertext) == "clover_refresh_token_xyz"
    finally:
        get_settings.cache_clear()
