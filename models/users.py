"""In-memory user models.

Per product requirements, credentials continue to live in an in-memory
structure (populated from the hardcoded roster in game.py) until a database
layer is introduced in a future phase. What changed here is *how* the
password is stored inside that structure: previously it was kept as plain
text, which meant that a memory dump, a debugger, or an accidental
`print(player.__dict__)` would leak every player's and admin's real
password. Passwords are now salted+hashed (PBKDF2-HMAC-SHA256, stdlib only,
no new dependency) the moment the object is created, and compared using a
constant-time check so login attempts cannot be used for a timing attack.
"""
from __future__ import annotations

import hashlib
import hmac
import os

_HASH_ALGO = "sha256"
_ITERATIONS = 200_000


def hash_password(password: str, salt: bytes | None = None) -> str:
    """Return a self-describing 'algo$iterations$salt$digest' hash string."""
    if salt is None:
        salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(_HASH_ALGO, password.encode("utf-8"), salt, _ITERATIONS)
    return f"pbkdf2_{_HASH_ALGO}${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Constant-time comparison of a candidate password against a stored hash."""
    if not password or not stored_hash:
        return False
    try:
        algo, iterations_s, salt_hex, digest_hex = stored_hash.split("$")
        algo_name = algo.replace("pbkdf2_", "", 1)
        iterations = int(iterations_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (ValueError, AttributeError):
        return False

    candidate = hashlib.pbkdf2_hmac(algo_name, password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(candidate, expected)


class Player:
    def __init__(self, username, id, password):
        self.username = username
        self.id = id
        self.team_id = None
        # `password` arrives as plain text from the hardcoded roster (game.py).
        # It is hashed immediately and never stored in plain text past __init__.
        self._password_hash = hash_password(password)

    def verify_password(self, candidate: str) -> bool:
        return verify_password(candidate, self._password_hash)


class Admin:
    def __init__(self, username, id, password):
        self.username = username
        self.id = id
        self._password_hash = hash_password(password)

    def verify_password(self, candidate: str) -> bool:
        return verify_password(candidate, self._password_hash)
