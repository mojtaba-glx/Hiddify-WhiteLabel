"""Token encryption behind a testable interface.

Production cipher is Fernet (key lives outside the DB). The DB keeps only
ciphertext + SHA-256 fingerprint + last-4 tail for human identification.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from abc import ABC, abstractmethod
from typing import Optional, Union

try:
    from cryptography.fernet import Fernet, InvalidToken as FernetInvalidToken
except Exception:  # pragma: no cover - import error surfaces at construction
    Fernet = None  # type: ignore[assignment]
    FernetInvalidToken = Exception  # type: ignore[assignment]


class TokenCipherError(RuntimeError):
    """Raised for any encrypt/decrypt failure (message never holds secrets)."""


class TokenCipher(ABC):
    """Encryption seam for bot tokens (replaceable, e.g. KMS later)."""

    @abstractmethod
    def encrypt(self, plaintext: str) -> str:
        """Encrypt a plain bot token, return opaque ciphertext string."""

    @abstractmethod
    def decrypt(self, ciphertext: str) -> str:
        """Decrypt ciphertext back to the plain bot token."""

    def encrypt_secret(self, plaintext: str) -> str:
        """Encrypt an integration secret without applying bot-token rules."""
        raise TokenCipherError("generic secret encryption is unavailable")

    def decrypt_secret(self, ciphertext: str) -> str:
        """Decrypt an integration secret without applying bot-token rules."""
        raise TokenCipherError("generic secret decryption is unavailable")


class FernetTokenCipher(TokenCipher):
    """Fernet implementation of :class:`TokenCipher`."""

    def __init__(self, key: Union[str, bytes]) -> None:
        if Fernet is None:
            raise TokenCipherError("cryptography package is not available")
        try:
            raw = key.encode("utf-8") if isinstance(key, str) else bytes(key)
            self._fernet = Fernet(raw)
        except Exception as exc:
            raise TokenCipherError("invalid TOKEN_ENCRYPTION_KEY") from exc

    def encrypt(self, plaintext: str) -> str:
        if not plaintext or ":" not in plaintext:
            raise TokenCipherError("invalid token shape")
        try:
            return self._fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")
        except Exception as exc:
            raise TokenCipherError("encrypt failed") from exc

    def decrypt(self, ciphertext: str) -> str:
        if not ciphertext:
            raise TokenCipherError("empty ciphertext")
        try:
            return self._fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
        except Exception as exc:
            raise TokenCipherError("decrypt failed") from exc

    def encrypt_secret(self, plaintext: str) -> str:
        value = str(plaintext or "")
        if not value.strip() or len(value) > 4096:
            raise TokenCipherError("invalid integration secret")
        try:
            return self._fernet.encrypt(value.encode("utf-8")).decode("utf-8")
        except Exception as exc:
            raise TokenCipherError("secret encryption failed") from exc

    def decrypt_secret(self, ciphertext: str) -> str:
        if not ciphertext:
            raise TokenCipherError("empty secret ciphertext")
        try:
            return self._fernet.decrypt(str(ciphertext).encode("utf-8")).decode("utf-8")
        except Exception as exc:
            raise TokenCipherError("secret decryption failed") from exc

    def __repr__(self) -> str:  # never leak key material
        return "FernetTokenCipher(key=[REDACTED])"


def generate_key() -> str:
    """Generate a fresh Fernet key (URL-safe base64 string)."""
    if Fernet is None:
        raise TokenCipherError("cryptography package is not available")
    return Fernet.generate_key().decode("utf-8")


def fingerprint_token(token: str) -> str:
    """SHA-256 hex fingerprint of a bot token (UNIQUE column, non-reversible)."""
    if not token:
        raise TokenCipherError("empty token")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_tail(token: str, keep: int = 4) -> str:
    """Last N chars of a token for human disambiguation (safe to display)."""
    text = str(token or "")
    if len(text) <= keep:
        return "****"
    return text[-keep:]


def generate_public_id(nbytes: int = 16) -> str:
    """Non-guessable public tenant id (URL-safe). Must be used by caller, no manual input."""
    return secrets.token_urlsafe(nbytes)


HASH_LENGTH = 64
TAIL_LENGTH = 4
TAIL_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{4}$")
HASH_PATTERN = re.compile(r"^[A-Fa-f0-9]{64}$")


def validate_fingerprint(value: str) -> str:
    """Accept only exactly 64 lowercase hex characters."""
    if not value or not HASH_PATTERN.match(value):
        raise TokenCipherError("invalid fingerprint: must be 64 hex chars")
    return value.lower()


def validate_tail(value: str) -> str:
    """Accept only exactly 4 URL-safe-ish characters and no colon / full token."""
    if not value or len(value) != TAIL_LENGTH:
        raise TokenCipherError("invalid token_tail: must be exactly 4 characters")
    if ":" in value:
        raise TokenCipherError("invalid token_tail: must not contain colon")
    if not TAIL_PATTERN.match(value):
        raise TokenCipherError("invalid token_tail: invalid characters")
    return value


def validate_hash(value: Optional[str]) -> Optional[str]:
    """Accept only 64 hex chars or None."""
    if value is None:
        return None
    if not value or not HASH_PATTERN.match(value):
        raise TokenCipherError("invalid hash: must be 64 hex chars or None")
    return value.lower()


def validate_encrypted_token(value: str) -> str:
    """Reject plain tokens and non-Fernet shapes. Accepts gAAAA... strings."""
    if not value or not str(value).strip():
        raise TokenCipherError("encrypted_token is required")
    clean = str(value).strip()
    if not clean.startswith("gAAAA"):
        raise TokenCipherError("encrypted_token must be a Fernet ciphertext (gAAAA...)")
    if len(clean) < 60:
        raise TokenCipherError("encrypted_token too short")
    return clean


def build_bot_credential(cipher: TokenCipher, plain_token: str) -> dict[str, str]:
    """Return a consistent dict of *encrypted_token*, *token_fingerprint*,
    *token_tail* for a given raw token. Callers MUST use this instead of
    manually constructing the three fields — it guarantees consistency."""
    return {
        "encrypted_token": cipher.encrypt(plain_token),
        "token_fingerprint": fingerprint_token(plain_token),
        "token_tail": token_tail(plain_token),
    }


def rotate_bot_credential(cipher: TokenCipher, new_plain_token: str) -> dict[str, str]:
    """Same as :func:`build_bot_credential` (separate function for clarity
    when rotating an existing bot's token)."""
    return build_bot_credential(cipher, new_plain_token)


def generate_webhook_secret(nbytes: int = 32) -> str:
    """Per-bot webhook secret (URL-safe). Never persisted raw (see below)."""
    return secrets.token_urlsafe(nbytes)


def hash_webhook_secret(secret: str) -> str:
    """SHA-256 hex digest of a webhook secret — the only form ever stored."""
    if not str(secret or "").strip():
        raise TokenCipherError("empty webhook secret")
    return hashlib.sha256(str(secret).encode("utf-8")).hexdigest()


def verify_webhook_secret(secret: str, digest: str) -> bool:
    """Constant-time comparison of a presented secret against a stored digest."""
    try:
        expected = str(digest or "").strip().lower()
        actual = hashlib.sha256(str(secret or "").encode("utf-8")).hexdigest()
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False
