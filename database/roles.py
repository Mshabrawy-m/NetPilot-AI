"""Database role -> connection resolution (Stage A6b-1 M4).

M3 produced the least-privilege PostgreSQL roles (``netpilot_admin``,
``netpilot_collector``, ``netpilot_app``, ``netpilot_retention``,
``netpilot_rollup``) and their GRANT scripts. M4 wires those roles into the
runtime so that **each process connects with its own, explicitly configured
role URL** and never borrows a more privileged one.

Design rules encoded here

* **Separate URL per role.** Each role reads its own environment variable
  (see :data:`ROLE_URL_ENV`). ``DATABASE_URL`` remains the single-connection
  fallback used only when *no* role separation has been adopted, so existing
  local/demo/legacy deployments are unaffected.
* **Role separation is opt-in.** It becomes active the moment any one of the
  ``NETPILOT_DB_URL_*`` variables is set (:func:`role_separation_enabled`).
  Once active, a role whose own URL is missing **fails closed** - it does not
  silently fall back to ``DATABASE_URL`` and, critically, never to the
  administrator URL.
* **Administrator is explicit and separate.** Schema initialization runs as
  ``netpilot_admin`` using ``NETPILOT_DB_URL_ADMIN``; no runtime role ever
  resolves to it, and a runtime role is never granted (or asked to run) DDL.
* **Demo mode stays isolated.** A demo run always uses its own SQLite store;
  role URLs are never consulted, so demo data can never be routed through an
  external PostgreSQL connection.
* **Credential-safe.** Every failure names only the role and the environment
  variable - never a DSN, host, user or password.

This module deliberately imports nothing from :mod:`database.connection` to
avoid a cycle; it is the leaf that the connection layer and the runners call.
"""
from __future__ import annotations

import os

from config import get_settings

#: The five logical roles M3 created and M4 wires up.
ROLE_ADMIN = "netpilot_admin"
ROLE_COLLECTOR = "netpilot_collector"
ROLE_APP = "netpilot_app"
ROLE_RETENTION = "netpilot_retention"
ROLE_ROLLUP = "netpilot_rollup"

#: Roles a running process uses for ordinary work. They hold only DML (and the
#: retention role only DELETE on ``metric_samples``); none has schema DDL.
RUNTIME_ROLES = (ROLE_COLLECTOR, ROLE_APP, ROLE_RETENTION, ROLE_ROLLUP)

#: Every recognised role.
ALL_ROLES = (ROLE_ADMIN,) + RUNTIME_ROLES

#: One dedicated connection-string variable per role. Keeping them separate is
#: what lets each process use only its own credentials/role.
ROLE_URL_ENV: dict[str, str] = {
    ROLE_APP: "NETPILOT_DB_URL_APP",
    ROLE_COLLECTOR: "NETPILOT_DB_URL_COLLECTOR",
    ROLE_RETENTION: "NETPILOT_DB_URL_RETENTION",
    ROLE_ROLLUP: "NETPILOT_DB_URL_ROLLUP",
    ROLE_ADMIN: "NETPILOT_DB_URL_ADMIN",
}

#: Short, human label for each role used in operator-facing messages.
ROLE_LABEL: dict[str, str] = {
    ROLE_APP: "Streamlit application reader",
    ROLE_COLLECTOR: "standalone collector",
    ROLE_RETENTION: "retention runner",
    ROLE_ROLLUP: "daily-rollup runner",
    ROLE_ADMIN: "schema administrator",
}


class RoleConfigError(ValueError):
    """A required role connection is missing or a role is not recognised.

    The message is always credential-safe: it names only the role and the
    environment variable, never a DSN. It subclasses ``ValueError`` so callers
    that already treat a configuration problem as a value error keep working.
    """


def validate_role(role: str) -> None:
    """Raise :class:`RoleConfigError` for an unrecognised role name."""
    if role not in ROLE_URL_ENV:
        raise RoleConfigError(
            f"unknown database role: {role!r}; expected one of: "
            + ", ".join(sorted(ALL_ROLES)) + ".")


def role_is_runtime(role: str) -> bool:
    """True for the DML-only roles that must never run DDL or admin actions."""
    validate_role(role)
    return role in RUNTIME_ROLES


def role_separation_enabled(env=None) -> bool:
    """True once the operator has adopted M3 roles (any role URL is set).

    Detection uses only the *presence* of a value - the contents are never
    inspected or echoed - so no credential leaks from this predicate.
    """
    env = os.environ if env is None else env
    return any((env.get(var, "") or "").strip() for var in ROLE_URL_ENV.values())


def _missing_url_message(role: str, env_var: str) -> str:
    label = ROLE_LABEL.get(role, role)
    if role == ROLE_ADMIN:
        return (
            f"{env_var} is required for explicit schema initialization "
            f"(role {role!r}, the {label}). Schema administration is "
            "configured separately from every runtime role and is never "
            "granted to one. Set " + env_var + " to the administrator "
            "PostgreSQL connection string.")
    return (
        f"{env_var} is required for the {role!r} role (the {label}), but "
        "database role separation is enabled and it is not configured. A "
        "runtime role never falls back to another process's connection - in "
        "particular not the administrator URL. Set " + env_var + " to this "
        "process's own PostgreSQL connection string.")


def resolve_role_url(role: str, settings=None, env=None) -> str:
    """Return the connection URL this process must use for ``role``.

    Order of decision (each branch is credential-safe):

    1. ``role`` is validated.
    2. **Demo mode** always returns the isolated SQLite ``database_url`` and no
       role URL is consulted, so demo data cannot reach an external database.
    3. Otherwise the role's own ``NETPILOT_DB_URL_*`` value wins if present.
    4. If role separation is active but *this* role's URL is missing, fail
       closed with :class:`RoleConfigError` - never fall back to another role.
    5. With separation inactive (legacy single-connection deployment), fall
       back to ``database_url`` so existing behaviour is preserved.
    """
    validate_role(role)
    settings = settings if settings is not None else get_settings()
    env = os.environ if env is None else env

    if getattr(settings, "demo_mode", False):
        # Demo is a local sandbox; roles are meaningless against its SQLite
        # store and must never pull in a PostgreSQL connection string.
        return settings.database_url

    env_var = ROLE_URL_ENV[role]
    role_url = (env.get(env_var, "") or "").strip()
    if role_url:
        return role_url

    if role_separation_enabled(env):
        raise RoleConfigError(_missing_url_message(role, env_var))

    # Separation not adopted: single-connection local/legacy path.
    return settings.database_url
