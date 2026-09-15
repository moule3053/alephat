"""Secret encryption helpers (Fernet / MultiFernet)."""

import os

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class EncryptionKeyMissingError(ValueError):
    """Raised when TOKEN_ENCRYPTION_KEY is unset."""


def _parse_keys(raw: str) -> list[bytes]:
    keys: list[bytes] = []
    for part in raw.replace("\n", ",").split(","):
        stripped = part.strip()
        if stripped:
            keys.append(stripped.encode())
    return keys


def _fernet_from_env() -> MultiFernet:
    raw = os.environ.get("TOKEN_ENCRYPTION_KEY")
    if not raw:
        raise EncryptionKeyMissingError("TOKEN_ENCRYPTION_KEY is required")
    keys = _parse_keys(raw)
    if not keys:
        raise EncryptionKeyMissingError("TOKEN_ENCRYPTION_KEY is empty")
    return MultiFernet([Fernet(k) for k in keys])


def encrypt_secret(value: str) -> str:
    if not value:
        return ""
    return _fernet_from_env().encrypt(value.encode()).decode()


def decrypt_secret(value: str) -> str:
    if not value:
        return ""
    try:
        return _fernet_from_env().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Failed to decrypt secret") from exc


def try_encrypt(value: str | None) -> str | None:
    if value is None:
        return None
    return encrypt_secret(value)


def try_decrypt(value: str | None) -> str | None:
    if value is None:
        return None
    if value.startswith("plaintext:"):
        return value.removeprefix("plaintext:")
    try:
        return decrypt_secret(value)
    except EncryptionKeyMissingError:
        return value
