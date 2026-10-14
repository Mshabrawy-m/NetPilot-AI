"""Tests for v2 Slice 2 authentication & RBAC (services.auth + repository).

Uses the minimum allowed PBKDF2 work factor to stay fast while still
exercising the real hashing path.
"""
from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from database import repository
from services import auth

ITERS = 50_000  # lowest allowed work factor - fast but real


# --------------------------------------------------------------------------
# Password hashing
# --------------------------------------------------------------------------
def test_hash_and_verify_roundtrip():
    stored = auth.hash_password("correct horse", iterations=ITERS)
    assert stored.startswith("pbkdf2_sha256$")
    assert auth.verify_password("correct horse", stored) is True
    assert auth.verify_password("wrong horse", stored) is False


def test_unique_salts_produce_different_hashes():
    a = auth.hash_password("same", iterations=ITERS)
    b = auth.hash_password("same", iterations=ITERS)
    assert a != b                      # per-user random salt
    assert auth.verify_password("same", a) and auth.verify_password("same", b)


def test_verify_handles_malformed_stored_safely():
    assert auth.verify_password("x", "not-a-hash") is False
    assert auth.verify_password("x", "bcrypt$1$aa$bb") is False
    assert auth.verify_password("x", "") is False


def test_hash_password_rejects_weak_iterations():
    with pytest.raises(ValueError):
        auth.hash_password("x", iterations=100)


def test_password_policy_length():
    ok, _ = auth.password_policy("short")
    assert ok is False
    ok, _ = auth.password_policy("longenough")
    assert ok is True


# --------------------------------------------------------------------------
# RBAC
# --------------------------------------------------------------------------
def test_role_ordering():
    assert auth.role_at_least("admin", "operator") is True
    assert auth.role_at_least("operator", "admin") is False
    assert auth.role_at_least("viewer", "viewer") is True
    assert auth.role_level("ghost") == -1


def test_validate_role():
    assert auth.validate_role("operator") == "operator"
    with pytest.raises(ValueError):
        auth.validate_role("root")


# --------------------------------------------------------------------------
# Persistence + authentication flow
# --------------------------------------------------------------------------
def test_create_user_validates_role(session):
    with pytest.raises(ValueError):
        repository.create_user(session, username="u", password_hash="h",
                               role="superuser")


def test_username_unique_constraint(session):
    h = auth.hash_password("pw12345678", iterations=ITERS)
    repository.create_user(session, username="dup", password_hash=h,
                           role="viewer")
    session.commit()
    with pytest.raises(IntegrityError):
        repository.create_user(session, username="dup", password_hash=h,
                               role="viewer")
        session.commit()


def test_authenticate_success_sets_last_login(session):
    h = auth.hash_password("sup3rs3cret", iterations=ITERS)
    repository.create_user(session, username="alice", password_hash=h,
                           role="admin")
    session.commit()
    user = auth.authenticate(session, "alice", "sup3rs3cret")
    assert user is not None and user.role == "admin"
    session.commit()
    fetched = repository.get_user_by_name(session, "alice")
    assert fetched.last_login is not None


def test_authenticate_wrong_password_or_unknown_user(session):
    h = auth.hash_password("rightpassword", iterations=ITERS)
    repository.create_user(session, username="bob", password_hash=h)
    session.commit()
    assert auth.authenticate(session, "bob", "wrongpassword") is None
    assert auth.authenticate(session, "nobody", "whatever") is None


def test_authenticate_inactive_user_is_rejected(session):
    h = auth.hash_password("password123", iterations=ITERS)
    user = repository.create_user(session, username="carol",
                                  password_hash=h, is_active=False)
    session.commit()
    assert auth.authenticate(session, "carol", "password123") is None
    repository.set_user_active(session, user, True)
    session.commit()
    assert auth.authenticate(session, "carol", "password123") is not None


def test_stored_hash_is_not_the_plaintext(session):
    pw = "topsecret9"
    repository.create_user(session, username="dave",
                           password_hash=auth.hash_password(pw, iterations=ITERS))
    session.commit()
    stored = repository.get_user_by_name(session, "dave").password_hash
    assert pw not in stored
