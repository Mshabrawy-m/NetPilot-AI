"""Authentication & RBAC primitives (v2 Slice 2).

Pure, dependency-free crypto and role logic - **no SQL, no Streamlit**.
Persistence of users lives in ``database.repository``; the orchestrating
``authenticate`` here only ties a stored user to the repository read.

Passwords are hashed with PBKDF2-HMAC-SHA256 and a per-user random salt
(240k iterations by default), compared in constant time. The plaintext
password is never stored or logged.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

from database.models import ROLE_LEVELS, USER_ROLES

_ALGO = "pbkdf2_sha256"
_SALT_BYTES = 16
DEFAULT_ITERATIONS = 240_000
MIN_PASSWORD_LENGTH = 8


# --------------------------------------------------------------------------
# Password hashing
# --------------------------------------------------------------------------
def hash_password(password: str,
                  iterations: int = DEFAULT_ITERATIONS) -> str:
    """Return 'pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>'."""
    if iterations < 50_000:
        raise ValueError("iterations below the accepted work factor")
    salt = secrets.token_bytes(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             salt, iterations)
    return f"{_ALGO}${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verification of ``password`` against ``stored`` hash.

    Malformed / foreign-format hashes simply return False (never raise), so
    callers can pass untrusted stored values safely.
    """
    try:
        algo, its, salt_hex, hash_hex = stored.split("$")
        if algo != _ALGO:
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt_hex), int(its))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), hash_hex)


def password_policy(password: str) -> tuple[bool, str]:
    """Minimal strength check used by the sign-up / bootstrap flow."""
    if password is None or len(password) < MIN_PASSWORD_LENGTH:
        return False, f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    return True, "ok"


# --------------------------------------------------------------------------
# Role-based access control
# --------------------------------------------------------------------------
def validate_role(role: str) -> str:
    if role not in USER_ROLES:
        raise ValueError(f"role must be one of {USER_ROLES}")
    return role


def role_level(role: str) -> int:
    return ROLE_LEVELS.get(role, -1)


def role_at_least(role: str, minimum: str) -> bool:
    """True if ``role`` is at least as privileged as ``minimum``."""
    return role_level(role) >= role_level(minimum)


# --------------------------------------------------------------------------
# Orchestration (repository-backed, but this function holds no SQL)
# --------------------------------------------------------------------------
def authenticate(session, username: str, password: str):
    """Return the active ``User`` on valid credentials, else ``None``.

    Never reveals whether the username or the password was the failure.
    """
    from database import repository
    user = repository.get_user_by_name(session, username)
    if user is None or not user.is_active:
        # Still run a hash to reduce username-enumeration timing signal.
        verify_password(password, f"{_ALGO}$1$00$00")
        return None
    if not verify_password(password, user.password_hash):
        return None
    repository.set_last_login(session, user, repository.db_utcnow())
    return user
