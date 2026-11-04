"""OFFLINE tests for tools/provision_neon_prod.py - never connect or touch WCM.

They prove the helper's security contract without any credential exposure or
network: the SCRAM-SHA-256 derivation matches the published RFC 7677 vector, a
plaintext password can never be emitted, the full owner -> admin -> validation
sequence runs in the reviewed order, every credential is kept out of stdout and
the parent environment, and every refusal gate fails closed. Real WCM access is
replaced by a fake store and the connection by an injected recorder.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
from pathlib import Path

import pytest

from database import roles as rolemod
from tools import provision_neon_prod as prov

ROLE_DIR = Path("database/sql/roles")


def _sql(name: str) -> str:
    return (ROLE_DIR / name).read_text(encoding="utf-8")


PROD_OWNER_DSN = ("postgresql://neondb_owner:OWN3rPw@ep-prod-neon.neon.tech"
                  ":5432/neondb?sslmode=require")


# --------------------------------------------------------------- fake plumbing
class _FakeCursor:
    def __init__(self, recorder, error):
        self._rec = recorder
        self._err = error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql):
        self._rec.append(sql)
        if self._err is not None:
            raise self._err


class _FakeConn:
    def __init__(self, recorder, error=None):
        self.autocommit = False
        self.closed = False
        self._rec = recorder
        self._err = error

    def cursor(self):
        return _FakeCursor(self._rec, self._err)

    def close(self):
        self.closed = True


class _FakeWcm:
    def __init__(self, seed=None):
        self.store = dict(seed if seed is not None else {prov.OWNER_TARGET: PROD_OWNER_DSN})

    def read(self, target):
        return self.store.get(target)

    def write(self, target, secret, *, username=""):
        self.store[target] = secret
        return True


def _make_connect(recorder, error=None):
    def _connect(dsn, **kwargs):
        return _FakeConn(recorder, error)
    return _connect


def _success_run(monkeypatch=None, connect=None, wcm=None, child_calls=None):
    """Run a full happy-path provisioning and return (recorder, child, wcm)."""
    recorder = []
    wcm = wcm or _FakeWcm()
    child = child_calls if child_calls is not None else []

    def _child(args, env_extra):
        child.append((list(args), dict(env_extra)))
        return 0

    code = prov.provision(
        confirm_production=True,
        connect=connect or _make_connect(recorder),
        wcm_read=wcm.read, wcm_write=wcm.write, child=_child)
    return recorder, child, wcm, code


# --------------------------------------------------------- SCRAM correctness
def test_scram_derivation_matches_rfc7677_vector():
    """The verifier's StoredKey/ServerKey reproduce RFC 7677's signed values."""
    salt = base64.b64decode("W22ZaJ0SNY7soEsUEjb6gQ==")
    client_key, stored_key, server_key = prov._derive_keys("pencil", salt, 4096)
    auth_msg = (
        "n=user,r=rOprNGfwEbeRWgbNEkqO"
        ",r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0,"
        "s=W22ZaJ0SNY7soEsUEjb6gQ==,i=4096,"
        "c=biws,r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0")
    # RFC 5802: ClientSignature = HMAC(StoredKey, AuthMessage);
    #            ClientProof    = ClientKey XOR ClientSignature.
    client_sig = hmac.new(stored_key, auth_msg.encode("utf-8"), hashlib.sha256).digest()
    proof = bytes(a ^ b for a, b in zip(client_key, client_sig))
    sig = hmac.new(server_key, auth_msg.encode("utf-8"), hashlib.sha256).digest()
    b64 = lambda raw: base64.b64encode(raw).decode("ascii")
    assert b64(proof) == "dHzbZapWIk4jUhN+Ute9ytag9zjfMHgsqmmiz7AndVQ="
    assert b64(sig) == "6rriTRBi23WpRR/wtup+mMhUZUn/dB5nLTJRsjl95G4="
    # And the helper stores exactly these keys in the pg_authid verifier format.
    verifier = prov.build_scram_secret("pencil", iterations=4096, salt=salt)
    _, _, tail = verifier.split("$", 2)
    stored_b64, server_b64 = tail.split(":")
    assert stored_b64 == b64(stored_key) and server_b64 == b64(server_key)


def test_scram_selfcheck_helper_true():
    assert prov._scram_matches_rfc7677() is True


def test_build_scram_secret_random_salt_differs_deterministic_stable():
    a = prov.build_scram_secret("pw")
    b = prov.build_scram_secret("pw")
    assert a != b  # random salt
    fixed = prov.build_scram_secret("pw", salt=b"0123456789abcdef")
    assert fixed == prov.build_scram_secret("pw", salt=b"0123456789abcdef")
    assert fixed.startswith("SCRAM-SHA-256$4096:")


@pytest.mark.parametrize("value,expected", [
    (("SCRAM-SHA-256$4096:c2FsdA==="
      "$hV2Qz9K1n3m4p5q6r7s8t9u0v1w2x3y4z5a6b7c8d9e=:YWJjZGVmZ2hpamtsbW5vcA=="), True),
    ("md50123456789abcdef0123456789abcdef", False),   # MD5, not SCRAM
    ("hunter2", False),                                # plaintext
    ("SCRAM-SHA-256$1:salt$stored:server", False),     # iterations too low / bad
])
def test_is_scram_verifier(value, expected):
    assert prov.is_scram_verifier(value) is expected


def test_generate_password_entropy_uniqueness_charset():
    pw = prov.generate_password()
    assert len(pw) >= 42 and not re.search(r"\s", pw) and pw != prov.generate_password()
    with pytest.raises(prov.ProvisionError):
        prov.generate_password(nbytes=8)


# ------------------------------------------------------- no-plaintext guarantee
def test_alter_statement_rejects_plaintext_password():
    with pytest.raises(prov.ProvisionError):
        prov._alter_password_statement(rolemod.ROLE_APP, "plaintext-secret")


def test_alter_statement_rejects_unknown_role():
    with pytest.raises(prov.ProvisionError):
        prov._alter_password_statement("evil_role",
                                       prov.build_scram_secret("pw", salt=b"0123456789abcdef"))


def test_alter_statement_emits_only_the_verifier():
    verifier = prov.build_scram_secret("p@ss w0rd", salt=b"0123456789abcdef")
    stmt = prov._alter_password_statement(rolemod.ROLE_APP, verifier)
    assert stmt == f"ALTER ROLE netpilot_app WITH PASSWORD '{verifier}'"
    # the plaintext never appears; only the SCRAM verifier does
    assert "p@ss w0rd" not in stmt


# ------------------------------------------------------------- redaction
def test_redact_scrubs_dsn_password_literal_and_verifier():
    text = ("postgresql://bob:Sup3rPw@ep-x.neon.tech:5432/neondb "
            "ALTER ROLE x WITH PASSWORD 'SCRAM-SHA-256$4096:sal$t:k:v' "
            "SCRAM-SHA-256$4096:salt$stored:server")
    out = prov.redact(text)
    assert "Sup3rPw" not in out
    assert "dHzbZapWIk" not in out
    assert "4096:salt$stored:server" not in out
    assert "SCRAM-SHA-256$***" in out


# -------------------------------------------------------- happy-path sequence
def test_provision_runs_the_reviewed_sequence_in_order():
    recorder, _child, _wcm, code = _success_run()
    assert code == 0
    alter_idx = [i for i, s in enumerate(recorder)
                 if "ALTER ROLE" in s and " WITH PASSWORD 'SCRAM-SHA-256$" in s]
    assert len(alter_idx) == len(prov.PROVISION_ROLES) == 5
    assert recorder[0] == _sql("10_create_roles.sql")
    # ownership transfers only after every role has a password installed
    assert recorder[alter_idx[-1] + 1] == _sql("20_ownership.sql")
    # admin-side privileges/init then grants, in order
    tail = recorder[alter_idx[-1] + 2:]
    assert tail == [_sql("21_schema_privileges.sql"), _sql("40_default_privileges.sql"),
                    _sql("30_grant_collector.sql"), _sql("31_grant_app.sql"),
                    _sql("32_grant_retention_rollup.sql")]
    # no statement ever carries a plaintext password
    for s in recorder:
        for lit in re.findall(r"(?i)PASSWORD\s+'([^']*)'", s):
            assert lit.startswith("SCRAM-SHA-256$")


def test_provision_stores_five_credentials_and_validates_all_five():
    _recorder, child, wcm, _code = _success_run()
    for role in prov.PROVISION_ROLES:
        assert wcm.read(prov.wcm_target(role)) is not None
        assert wcm.read(prov.wcm_target(role)).startswith(f"postgresql://{role}:")
    # init-schema child (admin process only, manage_schema true)
    init_args, init_env = child[0]
    assert init_args == ["tools/run_collector.py", "init-schema"]
    assert init_env[rolemod.ROLE_URL_ENV[rolemod.ROLE_ADMIN]]
    assert init_env["NETPILOT_MANAGE_SCHEMA"] == "true"
    assert init_env["NETPILOT_DEMO_MODE"] == "false"
    # validator child sees exactly the five role URLs, never the admin DSN absent
    val_args, val_env = child[1]
    assert val_args == ["tools/validate_pg_roles_prod.py", "validate",
                        "--confirm-production"]
    assert val_env["NETPILOT_DEMO_MODE"] == "false"
    for role in prov.PROVISION_ROLES:
        assert val_env[rolemod.ROLE_URL_ENV[role]]


def test_provision_never_mutates_parent_environment():
    before = set(os.environ)
    _success_run()
    after = set(os.environ)
    assert before == after
    for var in rolemod.ROLE_URL_ENV.values():
        assert var not in after


def test_provision_output_contains_no_secret(capsys):
    _success_run()
    out = capsys.readouterr().out
    assert "OWN3rPw" not in out              # owner password never printed
    assert "SCRAM-SHA-256$" not in out       # verifier never printed
    assert "postgresql://" not in out        # no DSN printed
    assert "netpilot_admin" in out           # role names are fine to show


# ------------------------------------------------------------- error handling
def test_error_path_redacts_verifier_and_dsn(capsys):
    boom = RuntimeError(
        "duplicate key " + PROD_OWNER_DSN + " PASSWORD '"
        + prov.build_scram_secret("pencil", salt=b"0123456789abcdef") + "'")
    recorder = []
    code = prov.provision(confirm_production=True,
                          connect=_make_connect(recorder, error=boom),
                          wcm_read=_FakeWcm().read, wcm_write=_FakeWcm().write,
                          child=lambda a, e: 0)
    out = capsys.readouterr().out
    assert code == 1
    # the secret parts are scrubbed; the non-secret scheme://***@host may remain
    assert "OWN3rPw" not in out and "SCRAM-SHA-256$" not in out
    assert "neondb_owner:OWN3rPw" not in out


def test_wcm_write_failure_aborts_before_ownership():
    recorder = []

    class _FailingWcm(_FakeWcm):
        def write(self, target, secret, *, username=""):
            self.store[target] = secret
            return False  # simulate a store failure on the first role

    child = []
    code = prov.provision(
        confirm_production=True, connect=_make_connect(recorder),
        wcm_read=_FailingWcm().read, wcm_write=_FailingWcm().write,
        child=lambda a, e: child.append(a) or 0)
    assert code == 2
    # the run stopped mid role-creation: 20_ownership was never executed
    assert _sql("20_ownership.sql") not in recorder
    assert child == []


# ------------------------------------------------------------------ refusals
def test_refuses_without_confirm_production(capsys):
    calls = []
    code = prov.provision(
        confirm_production=False,
        connect=lambda *a, **k: calls.append(1) or _FakeConn([]),
        wcm_read=_FakeWcm().read, wcm_write=_FakeWcm().write,
        child=lambda a, e: calls.append(1))
    assert code == 2
    assert calls == []
    assert "REFUSED" in capsys.readouterr().out


def test_refuses_when_owner_credential_missing(capsys):
    calls = []
    empty = _FakeWcm(seed={})
    code = prov.provision(
        confirm_production=True,
        connect=lambda *a, **k: calls.append(1) or _FakeConn([]),
        wcm_read=empty.read, wcm_write=empty.write,
        child=lambda a, e: calls.append(1))
    assert code == 2
    assert calls == []
    assert prov.OWNER_TARGET in capsys.readouterr().out


@pytest.mark.parametrize("marker", prov.KNOWN_DISPOSABLE_MARKERS)
def test_refuses_disposable_m5_host(marker, capsys):
    disposable = ("postgresql://neondb_owner:pw@" + marker + ".neon.tech:5432/neondb"
                  "?sslmode=require")
    wcm = _FakeWcm(seed={prov.OWNER_TARGET: disposable})
    calls = []
    code = prov.provision(
        confirm_production=True,
        connect=lambda *a, **k: calls.append(1) or _FakeConn([]),
        wcm_read=wcm.read, wcm_write=wcm.write,
        child=lambda a, e: calls.append(1))
    assert code == 2
    assert calls == []                                   # refuses before connecting
    assert "disposable" in capsys.readouterr().out.lower()


def test_refuses_non_windows_credential_manager(capsys):
    def _raise_read(target):
        raise prov.WcmUnavailableError("not windows")
    code = prov.provision(
        confirm_production=True, connect=_make_connect([]),
        wcm_read=_raise_read, wcm_write=lambda *a, **k: True,
        child=lambda a, e: 0)
    assert code == 2
    assert "REFUSED" in capsys.readouterr().out


# --------------------------------------------------------------------- CLI
def test_cli_selfcheck_subcommand_runs_offline(capsys):
    code = prov.main(["selfcheck"])
    assert code == 0
    assert "selfcheck:" in capsys.readouterr().out


def test_cli_provision_requires_confirm_flag_present():
    args = prov.build_parser().parse_args(["provision", "--confirm-production"])
    assert args.command == "provision" and args.confirm_production is True
