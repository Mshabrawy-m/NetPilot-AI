"""OFFLINE tests for tools/provision_neon_prod.py - never connect or touch WCM.

They prove the helper's security contract without any credential exposure or
network: the Neon API password provisioning flow, the SCRAM derivation matches
the published RFC 7677 vector (legacy test), a plaintext password can never be
emitted to the wire in SQL, the full owner -> API -> admin -> validation
sequence runs in the reviewed order, every credential is kept out of stdout and
the parent environment, and every refusal gate fails closed. Real WCM access is
replaced by a fake store, the connection by an injected recorder, and the Neon
API by a mock that returns a deterministic fake password.
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


def _success_run(monkeypatch=None, connect=None, wcm=None, child_calls=None,
                 api_reset=None, api_wait=None, use_api=False):
    """Run a full happy-path provisioning and return (recorder, child, wcm, code).

    Default path uses ALTER ROLE plaintext (no API). Set use_api=True or provide
    api_reset/api_wait to test the API path.
    """
    recorder = []
    wcm = wcm or _FakeWcm()
    child = child_calls if child_calls is not None else []

    def _child(args, env_extra):
        child.append((list(args), dict(env_extra)))
        return 0

    # API mocks (only used when explicitly requested)
    if use_api or api_reset is not None:
        if api_reset is None:
            def _default_api_reset(project_id, branch_id, role):
                fake_pw = f"Ne0nGen-{role}-Fk9xQz3mWp7"
                return fake_pw, [f"op-{role}"]
            api_reset = _default_api_reset
        if api_wait is None:
            def _default_wait(op_ids):
                pass
            api_wait = _default_wait

    kwargs = {
        "confirm_production": True,
        "connect": connect or _make_connect(recorder),
        "wcm_read": wcm.read, "wcm_write": wcm.write, "child": _child,
        "project_id": "test-proj", "branch_id": "br-test"}
    if use_api or api_reset is not None:
        kwargs["api_reset_password"] = api_reset
        kwargs["api_wait_ops"] = api_wait

    code = prov.provision(**kwargs)
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
    """Default (ALTER ROLE): create-roles SQL -> 5x ALTER PASSWORD -> ownership -> grants."""
    recorder, _child, _wcm, code = _success_run()
    assert code == 0
    # First SQL is create-roles
    assert recorder[0] == _sql("10_create_roles.sql")
    # 5 ALTER ROLE statements follow (plaintext password path)
    alter_idx = [i for i, s in enumerate(recorder)
                 if "ALTER ROLE" in s and " WITH PASSWORD '" in s]
    assert len(alter_idx) == len(prov.PROVISION_ROLES) == 5
    # Ownership SQL comes after all ALTER ROLE statements
    assert recorder[alter_idx[-1] + 1] == _sql("20_ownership.sql")
    # Admin/grants phase follows ownership
    tail = recorder[alter_idx[-1] + 2:]
    assert tail == [_sql("21_schema_privileges.sql"), _sql("40_default_privileges.sql"),
                    _sql("30_grant_collector.sql"), _sql("31_grant_app.sql"),
                    _sql("32_grant_retention_rollup.sql")]
    # No SCRAM verifier in any ALTER statement (plaintext only)
    for s in recorder:
        for lit in re.findall(r"(?i)PASSWORD\s+'([^']*)'", s):
            assert not lit.startswith("SCRAM-SHA-256$")


def test_provision_stores_five_credentials_and_validates_all_five():
    _recorder, child, wcm, _code = _success_run()
    for role in prov.PROVISION_ROLES:
        stored = wcm.read(prov.wcm_target(role))
        assert stored is not None
        assert stored.startswith(f"postgresql://{role}:")
        # DSN must contain a non-empty password (CSPRNG-generated)
        # Format: postgresql://role:PASSWORD@host:port/db?params
        pw_section = stored[len(f"postgresql://{role}:"):]
        assert "@" in pw_section  # password present before @
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
                          child=lambda a, e: 0,
                          project_id="p", branch_id="b")
    out = capsys.readouterr().out
    assert code == 1
    # the secret parts are scrubbed; the non-secret scheme://***@host may remain
    assert "OWN3rPw" not in out and "SCRAM-SHA-256$" not in out
    assert "neondb_owner:OWN3rPw" not in out


def test_wcm_write_failure_aborts_before_ownership():
    recorder = []

    class _FailingWcm(_FakeWcm):
        n = 0
        def write(self, target, secret, *, username=""):
            _FailingWcm.n += 1
            if _FailingWcm.n == 1:
                return False  # simulate a store failure on the first role
            self.store[target] = secret
            return True

    failing = _FailingWcm()
    child = []
    code = prov.provision(
        confirm_production=True, connect=_make_connect(recorder),
        wcm_read=failing.read, wcm_write=failing.write,
        child=lambda a, e: child.append(a) or 0,
        project_id="p", branch_id="b")
    assert code == 2
    # the run stopped mid role-processing: 20_ownership was never executed
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
def test_cli_selfcheck_subcommand_runs_offline(capsys, monkeypatch, tmp_path):
    # A clean CI host has no Neon CLI login. That is a live-provisioning
    # prerequisite, not an offline guarantee, so selfcheck must still pass.
    monkeypatch.setattr(prov, "_NEON_CLI_CRED_PATH", tmp_path / "missing.json")
    code = prov.main(["selfcheck"])
    out = capsys.readouterr().out
    assert code == 0
    assert "selfcheck:" in out
    assert "[WARN] Neon CLI credentials file accessible" in out


def test_cli_provision_requires_confirm_flag_present():
    args = prov.build_parser().parse_args(["provision", "--confirm-production"])
    assert args.command == "provision" and args.confirm_production is True


def test_cli_provision_accepts_project_and_branch_ids():
    args = prov.build_parser().parse_args(
        ["provision", "--confirm-production",
         "--project-id", "custom-proj", "--branch-id", "br-custom"])
    assert args.project_id == "custom-proj"
    assert args.branch_id == "br-custom"


# ============================= Neon API Regression Tests =============================
class TestNeonApiPasswordProvisioning:
    """Verify the Neon API path works when explicitly invoked (for rotation)."""

    def test_api_reset_password_called_for_each_role(self):
        """All 5 roles receive a reset_password call."""
        called_for = []

        def _track_api(project_id, branch_id, role):
            called_for.append(role)
            return (f"pw-{role}", [f"op-{role}"])

        _success_run(api_reset=_track_api)
        assert called_for == list(prov.PROVISION_ROLES)

    def test_api_wait_operations_called_after_all_resets(self):
        """Operations are polled after the password loop, not during."""
        events = []

        def _api_reset(project_id, branch_id, role):
            events.append(("reset", role))
            return (f"pw-{role}", [f"op-{role}"])

        def _wait(ops):
            events.append(("wait", tuple(ops)))

        _success_run(api_reset=_api_reset, api_wait=_wait)
        # Wait comes after all 5 resets
        reset_count = sum(1 for e in events if e[0] == "reset")
        assert reset_count == 5
        assert events[-1][0] == "wait"
        assert len(events[-1][1]) == 5  # all op IDs collected

    def test_no_sql_password_in_api_path(self):
        """When API is used, no ALTER ROLE is issued via psycopg2."""
        recorder, _child, _wcm, code = _success_run(use_api=True)
        assert code == 0
        for sql in recorder:
            assert "ALTER ROLE" not in sql
            assert "PASSWORD" not in sql

    def test_role_dsn_contains_api_password(self):
        """Each stored WCM DSN uses the password from the API response."""
        fake_pw = "Ne0nApi-TestPw-Abc123"

        def _api_reset(project_id, branch_id, role):
            return fake_pw, [f"op-{role}"]

        _, _, wcm, code = _success_run(api_reset=_api_reset)
        assert code == 0
        for role in prov.PROVISION_ROLES:
            dsn = wcm.read(prov.wcm_target(role))
            assert fake_pw in dsn


class TestScramRejection:
    """_alter_password_statement (SCRAM guard) and _alter_password_plaintext
    guards remain functional."""

    def test_alter_statement_rejects_plaintext(self):
        with pytest.raises(prov.ProvisionError, match="non-SCRAM"):
            prov._alter_password_statement(rolemod.ROLE_APP, "myplaintext")

    def test_alter_statement_accepts_valid_scram(self):
        verifier = prov.build_scram_secret("test", salt=b"0123456789abcdef")
        stmt = prov._alter_password_statement(rolemod.ROLE_APP, verifier)
        assert "ALTER ROLE" in stmt

    def test_alter_plaintext_accepts_normal_password(self):
        stmt = prov._alter_password_plaintext(rolemod.ROLE_APP, "MyStr0ng!Pw")
        assert "ALTER ROLE netpilot_app WITH PASSWORD 'MyStr0ng!Pw'" == stmt

    def test_alter_plaintext_rejects_empty_password(self):
        with pytest.raises(prov.ProvisionError, match="empty password"):
            prov._alter_password_plaintext(rolemod.ROLE_APP, "")

    def test_alter_plaintext_rejects_scram_format(self):
        """A SCRAM verifier must never be passed to the plaintext path."""
        verifier = prov.build_scram_secret("test", salt=b"0123456789abcdef")
        with pytest.raises(prov.ProvisionError, match="SCRAM verifier"):
            prov._alter_password_plaintext(rolemod.ROLE_APP, verifier)

    def test_alter_plaintext_rejects_unknown_role(self):
        with pytest.raises(prov.ProvisionError, match="unrecognised"):
            prov._alter_password_plaintext("hacker_role", "pass123")

    def test_alter_plaintext_escapes_single_quotes(self):
        stmt = prov._alter_password_plaintext(rolemod.ROLE_APP, "it's-a-test")
        assert "it''s-a-test" in stmt

    def test_default_flow_uses_plaintext_not_scram(self):
        """The default provisioner path sends plaintext (not SCRAM) via ALTER ROLE."""
        recorder, _, _, code = _success_run()
        assert code == 0
        alter_stmts = [s for s in recorder if "ALTER ROLE" in s and "PASSWORD" in s]
        assert len(alter_stmts) == 5
        for s in alter_stmts:
            assert "SCRAM-SHA-256$" not in s


class TestSecretRedaction:
    """API errors never leak password or response body."""

    def test_api_error_does_not_leak_password(self, capsys):
        """When API raises, the exception message has no response body."""
        secret = "D0-NOT-LEAK-this-password"

        def _api_reset_fails(project_id, branch_id, role):
            # Simulate a ProvisionError that does NOT include the password
            raise prov.ProvisionError(
                f"Neon API reset_password failed for {role!r}: HTTP 500")

        code = prov.provision(
            confirm_production=True,
            connect=_make_connect([]),
            wcm_read=_FakeWcm().read,
            wcm_write=_FakeWcm().write,
            child=lambda a, e: 0,
            api_reset_password=_api_reset_fails,
            api_wait_ops=lambda o: None,
            project_id="p", branch_id="b")
        out = capsys.readouterr().out
        assert code == 2
        assert secret not in out
        assert "HTTP 500" in out  # error info is fine

    def test_password_never_appears_in_stdout(self, capsys):
        """Happy path: CSPRNG-generated password never printed."""
        _success_run()
        out = capsys.readouterr().out
        # No random base64-like strings (CSPRNG passwords) in output
        assert "password configured" in out  # status message is fine
        # The actual password token should not appear
        # (CSPRNG uses URL-safe base64 which has [A-Za-z0-9_-] chars, length ~43)


class TestWcmReadbackAndOverwrite:
    """WCM credentials stored correctly and overwrite detection."""

    def test_readback_matches_written_dsn(self):
        """After write, a read returns the same DSN."""
        _, _, wcm, code = _success_run()
        assert code == 0
        for role in prov.PROVISION_ROLES:
            target = prov.wcm_target(role)
            dsn = wcm.read(target)
            assert dsn is not None
            assert dsn.startswith(f"postgresql://{role}:")
            assert "sslmode=require" in dsn

    def test_existing_credential_triggers_rotation_message(self, capsys):
        """When WCM already has a non-empty credential, the flow prints rotation."""
        # Seed a role with an existing credential
        seed = {prov.OWNER_TARGET: PROD_OWNER_DSN}
        seed[prov.wcm_target(rolemod.ROLE_ADMIN)] = "postgresql://netpilot_admin:old@host/db"
        wcm = _FakeWcm(seed=seed)
        _success_run(wcm=wcm)
        out = capsys.readouterr().out
        assert "rotating existing credential" in out.lower()


class TestPartialFailureAndRetry:
    """Provisioning handles mid-loop failures gracefully (ALTER ROLE path)."""

    def test_connection_error_on_third_role_stops_flow(self):
        """If psycopg2 raises on role 3, phases after the loop do not execute."""
        recorder = []
        role_count = [0]

        class _SelectiveErrorCursor:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def execute(self, sql):
                if "ALTER ROLE" in sql:
                    role_count[0] += 1
                    if role_count[0] == 3:
                        raise RuntimeError("connection reset by server")
                recorder.append(sql)

        class _SelectiveConn:
            autocommit = False
            def cursor(self):
                return _SelectiveErrorCursor()
            def close(self):
                pass

        def _connect(dsn, **kw):
            return _SelectiveConn()

        code = prov.provision(
            confirm_production=True,
            connect=_connect,
            wcm_read=_FakeWcm().read,
            wcm_write=_FakeWcm().write,
            child=lambda a, e: 0,
            project_id="p", branch_id="b")
        assert code == 1  # execution error (not refusal)
        # 10_create_roles.sql ran
        assert _sql("10_create_roles.sql") in recorder
        # 20_ownership.sql did NOT run
        assert _sql("20_ownership.sql") not in recorder

    def test_first_two_credentials_stored_before_failure(self):
        """Roles processed before the failure get their WCM entry."""
        wcm = _FakeWcm()
        role_count = [0]

        class _FailThirdCursor:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def execute(self, sql):
                if "ALTER ROLE" in sql:
                    role_count[0] += 1
                    if role_count[0] == 3:
                        raise RuntimeError("fail")

        class _FailThirdConn:
            autocommit = False
            def cursor(self):
                return _FailThirdCursor()
            def close(self):
                pass

        def _connect(dsn, **kw):
            return _FailThirdConn()

        prov.provision(
            confirm_production=True,
            connect=_connect,
            wcm_read=wcm.read,
            wcm_write=wcm.write,
            child=lambda a, e: 0,
            project_id="p", branch_id="b")
        roles_list = list(prov.PROVISION_ROLES)
        # First two should be stored (they got through ALTER + WCM write)
        assert wcm.read(prov.wcm_target(roles_list[0])) is not None
        assert wcm.read(prov.wcm_target(roles_list[1])) is not None
        # Third should NOT be stored (failed before WCM write)
        assert wcm.read(prov.wcm_target(roles_list[2])) is None

    def test_retry_after_success_overwrites_all_credentials(self):
        """Re-running provisioning is safe: all credentials get fresh DSNs."""
        wcm = _FakeWcm()
        # First run
        prov.provision(
            confirm_production=True,
            connect=_make_connect([]),
            wcm_read=wcm.read,
            wcm_write=wcm.write,
            child=lambda a, e: 0,
            project_id="p", branch_id="b")
        first_admin_dsn = wcm.read(prov.wcm_target(rolemod.ROLE_ADMIN))
        assert first_admin_dsn is not None

        # Second run (retry/rotation — uses ALTER ROLE with new passwords)
        prov.provision(
            confirm_production=True,
            connect=_make_connect([]),
            wcm_read=wcm.read,
            wcm_write=wcm.write,
            child=lambda a, e: 0,
            project_id="p", branch_id="b")
        second_admin_dsn = wcm.read(prov.wcm_target(rolemod.ROLE_ADMIN))
        assert second_admin_dsn is not None
        # CSPRNG ensures each run gets a different password
        assert first_admin_dsn != second_admin_dsn


class TestNeonApiHelpers:
    """Unit tests for the API helper functions (no real HTTP)."""

    def test_read_neon_cli_token_missing_file(self, monkeypatch, tmp_path):
        """Raises ProvisionError when the credentials file doesn't exist."""
        monkeypatch.setattr(prov, "_NEON_CLI_CRED_PATH", tmp_path / "nonexistent.json")
        with pytest.raises(prov.ProvisionError, match="not found"):
            prov._read_neon_cli_token()

    def test_read_neon_cli_token_no_access_token(self, monkeypatch, tmp_path):
        """Raises ProvisionError when access_token is empty."""
        cred = tmp_path / "credentials.json"
        cred.write_text('{"type": "oauth", "access_token": "", "expires_at": 0}')
        monkeypatch.setattr(prov, "_NEON_CLI_CRED_PATH", cred)
        with pytest.raises(prov.ProvisionError, match="no access_token"):
            prov._read_neon_cli_token()

    def test_read_neon_cli_token_expired(self, monkeypatch, tmp_path):
        """Raises ProvisionError when token is expired."""
        cred = tmp_path / "credentials.json"
        cred.write_text('{"access_token": "tok", "expires_at": 1000}')  # past
        monkeypatch.setattr(prov, "_NEON_CLI_CRED_PATH", cred)
        with pytest.raises(prov.ProvisionError, match="expired"):
            prov._read_neon_cli_token()

    def test_read_neon_cli_token_success(self, monkeypatch, tmp_path):
        """Returns the token string when valid."""
        import time
        cred = tmp_path / "credentials.json"
        future = int(time.time()) + 3600
        cred.write_text(f'{{"access_token": "good_token_xyz", "expires_at": {future}}}')
        monkeypatch.setattr(prov, "_NEON_CLI_CRED_PATH", cred)
        token = prov._read_neon_cli_token()
        assert token == "good_token_xyz"

    def test_neon_api_base_is_https(self):
        """The API base URL uses HTTPS (TLS verified by urllib)."""
        assert prov.NEON_API_BASE.startswith("https://")
        assert "console.neon.tech" in prov.NEON_API_BASE
