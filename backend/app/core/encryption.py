"""Fernet-based token encryption with key-rotation support.

Used to encrypt/decrypt Clover access and refresh tokens at rest.
The primary key encrypts; the previous key is tried on decrypt only.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.core.config import get_settings


class TokenEncryption:
    """Encrypts text to Fernet ciphertext (base64 text, not bytes).

    Key rotation: if TOKEN_ENCRYPTION_KEY_PREVIOUS is set, decrypt will
    try the old key as a fallback.  Encrypt always uses the current key.
    """

    def __init__(self) -> None:
        settings = get_settings()
        if not settings.token_encryption_key:
            raise RuntimeError("TOKEN_ENCRYPTION_KEY is not set")

        current = Fernet(settings.token_encryption_key.encode())
        if settings.token_encryption_key_previous:
            previous = Fernet(settings.token_encryption_key_previous.encode())
            self._fernet = MultiFernet([current, previous])
        else:
            self._fernet = MultiFernet([current])

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc:
            raise ValueError("Token decryption failed — wrong key or corrupted data") from exc
