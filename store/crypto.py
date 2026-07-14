"""Optional at-rest encryption for API key secrets stored in PostgreSQL/JSON.

Set GROK2API_SECRET_KEY to a passphrase (or 32+ byte raw key). When unset,
secrets are stored plaintext (same as historical keys.json).
"""

from __future__ import annotations

import base64
import hashlib
import os
from functools import lru_cache
from typing import Any


def _passphrase() -> str:
    return (
        os.getenv("GROK2API_SECRET_KEY")
        or os.getenv("GROK2API_FERNET_KEY")
        or ""
    ).strip()


def encryption_enabled() -> bool:
    return bool(_passphrase())


@lru_cache(maxsize=1)
def _fernet():
    phrase = _passphrase()
    if not phrase:
        return None
    try:
        from cryptography.fernet import Fernet  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "GROK2API_SECRET_KEY set but cryptography not installed; "
            "pip install cryptography"
        ) from e
    # Derive a url-safe 32-byte key from passphrase
    dig = hashlib.sha256(phrase.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(dig)
    return Fernet(key)


def encrypt_secret(plain: str | None) -> str | None:
    if plain is None:
        return None
    if not plain:
        return plain
    if not encryption_enabled():
        return plain
    f = _fernet()
    if f is None:
        return plain
    token = f.encrypt(plain.encode("utf-8")).decode("ascii")
    return "enc:v1:" + token


def decrypt_secret(value: str | None) -> str | None:
    if value is None:
        return None
    if not value.startswith("enc:v1:"):
        return value
    if not encryption_enabled():
        # Cannot decrypt without key — return None rather than leak ciphertext
        return None
    f = _fernet()
    if f is None:
        return None
    raw = value[len("enc:v1:") :]
    try:
        return f.decrypt(raw.encode("ascii")).decode("utf-8")
    except Exception:
        return None


def maybe_encrypt_key_record(rec: dict[str, Any]) -> dict[str, Any]:
    out = dict(rec)
    if out.get("secret"):
        out["secret"] = encrypt_secret(str(out["secret"]))
    return out


def maybe_decrypt_key_record(rec: dict[str, Any]) -> dict[str, Any]:
    out = dict(rec)
    if out.get("secret"):
        dec = decrypt_secret(str(out["secret"]))
        if dec is not None:
            out["secret"] = dec
    return out
