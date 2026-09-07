"""API keys: generated once, stored hashed, compared by hash.

The raw key is shown to the caller exactly once, at creation. What the
database holds is a SHA-256 of it, so a copy of the database is not a copy of
the keys. Lookup is by hash, and the final comparison is constant-time out of
habit rather than necessity -- the hash is not secret, the key is.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

KEY_PREFIX = "mr-"


def generate_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def key_matches(raw: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_key(raw), stored_hash)


def key_hint(raw: str) -> str:
    """The prefix and last four characters, for logs and listings."""
    return f"{raw[: len(KEY_PREFIX) + 4]}…{raw[-4:]}" if len(raw) > len(KEY_PREFIX) + 8 else "…"
