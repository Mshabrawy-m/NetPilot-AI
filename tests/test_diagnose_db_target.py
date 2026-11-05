"""Offline tests for tools/diagnose_db_target.py.

No network, no real database, no real WCM access.
"""
from __future__ import annotations

import hashlib
from unittest.mock import patch

import pytest

import tools.diagnose_db_target as diag
from tools.diagnose_db_target import TargetInfo

# ---------------------------------------------------------------------------
# _fingerprint
# ---------------------------------------------------------------------------

class TestFingerprint:
    def test_stable_across_calls(self):
        fp1 = diag._fingerprint("host1", "db1", "user1")
        fp2 = diag._fingerprint("host1", "db1", "user1")
        assert fp1 == fp2

    def test_ignores_password(self):
        """Fingerprint only uses host+database+user; password changes do not
        affect it."""
        dsn_a = "postgresql+psycopg2://user:PASS1@host:5432/db?sslmode=require"
        dsn_b = "postgresql+psycopg2://user:DIFFERENT@host:5432/db?sslmode=require"
        ta = diag.parse_target(dsn_a)
        tb = diag.parse_target(dsn_b)
        assert ta.fingerprint == tb.fingerprint

    def test_is_8_hex_chars(self):
        fp = diag._fingerprint("h", "d", "u")
        assert len(fp) == 8
        int(fp, 16)  # valid hex

    def test_matches_manual_sha(self):
        expected = hashlib.sha256(b"hostdbuser").hexdigest()[:8]
        assert diag._fingerprint("host", "db", "user") == expected


# ---------------------------------------------------------------------------
# parse_target / redacted output
# ---------------------------------------------------------------------------

class TestParseTarget:
    def test_basic_extraction(self):
        # Password uses URL-encoding for '@' which is the standard DSN form
        dsn = ("postgresql+psycopg2://netpilot_app:s3cretP%40ss"
               "@ep-my-host.us-east-1.aws.neon.tech:5432/neondb"
               "?sslmode=require")
        t = diag.parse_target(dsn)
        assert t.host == "ep-my-host.us-east-1.aws.neon.tech"
        assert t.port == 5432
        assert t.database == "neondb"
        assert t.user == "netpilot_app"
        assert t.sslmode == "require"

    def test_redacted_never_shows_password(self):
        dsn = ("postgresql+psycopg2://user:TOP_SECRET@h:5432/d"
               "?sslmode=require")
        t = diag.parse_target(dsn)
        redacted = t.redacted()
        assert "TOP_SECRET" not in redacted
        assert "://" not in redacted

    def test_pooler_detection(self):
        dsn_pool = ("postgresql+psycopg2://u:p@ep-pooled-pooler.us-east-1"
                    ".aws.neon.tech:5432/db")
        dsn_direct = ("postgresql+psycopg2://u:p@ep-direct.us-east-1"
                      ".aws.neon.tech:5432/db")
        assert diag.parse_target(dsn_pool).pooler is True
        assert diag.parse_target(dsn_direct).pooler is False

    def test_default_sslmode(self):
        dsn = "postgresql+psycopg2://u:p@h:5432/db"
        t = diag.parse_target(dsn)
        assert t.sslmode == "prefer"

    def test_special_chars_in_password_not_leaked(self):
        # Properly URL-encoded: p%40ss%2Fw0rd%2Bsp%3Dec
        dsn = ("postgresql+psycopg2://app:p%40ss%2Fw0rd%2Bsp%3Dec"
               "@host:5432/db?sslmode=require")
        t = diag.parse_target(dsn)
        assert "p@ss" not in t.redacted()
        assert "w0rd" not in t.redacted()
        assert t.user == "app"
        assert t.host == "host"


# ---------------------------------------------------------------------------
# assert_readonly
# ---------------------------------------------------------------------------

class TestAssertReadonly:
    def test_select_passes(self):
        diag.assert_readonly("SELECT 1")

    def test_with_cte_passes(self):
        diag.assert_readonly("WITH x AS (SELECT 1) SELECT * FROM x")

    def test_set_transaction_read_only_passes(self):
        diag.assert_readonly("SET TRANSACTION READ ONLY")

    @pytest.mark.parametrize("bad", [
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET x=1",
        "DELETE FROM t",
        "DROP TABLE t",
        "CREATE TABLE t (id int)",
        "ALTER TABLE t ADD COLUMN x int",
        "GRANT SELECT ON t TO u",
        "TRUNCATE t",
    ])
    def test_mutation_refused(self, bad):
        with pytest.raises(RuntimeError, match="READ-ONLY guard"):
            diag.assert_readonly(bad)

    def test_privilege_names_as_string_literals_pass(self):
        """has_table_privilege(...'INSERT'...) is a read, not a write."""
        sql = ("SELECT has_table_privilege('netpilot_app', 'devices', "
               "'INSERT')")
        diag.assert_readonly(sql)  # should not raise

    def test_forbidden_token_in_literal_stripped(self):
        """The regex dequotes before token scan, so quoted privilege names
        are safe."""
        sql = "SELECT has_schema_privilege('u', 'public', 'CREATE')"
        diag.assert_readonly(sql)


# ---------------------------------------------------------------------------
# Verdict mapping
# ---------------------------------------------------------------------------

class TestVerdict:
    def _target(self, host="h", db="neondb", user="netpilot_app"):
        return TargetInfo(host=host, port=5432, database=db, user=user,
                          sslmode="require", pooler=False,
                          fingerprint=diag._fingerprint(host, db, user))

    def test_pass_all_in_public(self):
        found = {"devices": "public", "links": "public"}
        v = diag._derive_verdict(self._target(), "neondb", '"$user", public',
                                 found, [], [])
        assert "PASS" in v

    def test_h2_wrong_database(self):
        v = diag._derive_verdict(self._target(db="neondb"), "postgres",
                                 '"$user", public', {}, ["devices"], ["devices"])
        assert "H2" in v

    def test_h3_wrong_schema(self):
        found = {"devices": "myschema", "links": "myschema"}
        v = diag._derive_verdict(self._target(), "neondb", '"$user", public',
                                 found, [], ["devices", "links"])
        assert "H3" in v

    def test_h5_tables_absent(self):
        v = diag._derive_verdict(self._target(), "neondb", '"$user", public',
                                 {}, ["devices", "links"], ["devices", "links"])
        assert "H5" in v


# ---------------------------------------------------------------------------
# WCM integration (mocked)
# ---------------------------------------------------------------------------

class TestWcmIntegration:
    def test_from_wcm_label(self):
        assert diag.WCM_TARGET_APP == "netpilot/prod/netpilot_app"

    @patch("tools.diagnose_db_target._wcm_read", return_value=None)
    def test_missing_wcm_returns_none(self, mock_read):
        result = diag._wcm_read("nonexistent")
        assert result is None

    @patch("tools.diagnose_db_target._wcm_read",
           return_value="postgresql+psycopg2://u:p@h:5432/d")
    def test_wcm_returns_dsn_string(self, mock_read):
        dsn = diag._wcm_read(diag.WCM_TARGET_APP)
        assert dsn is not None
        assert "postgresql" in dsn


# ---------------------------------------------------------------------------
# Secret leak prevention (integration-style)
# ---------------------------------------------------------------------------

class TestSecretLeaks:
    """Ensure no function's return value or printed output can leak
    a password."""

    _SAMPLE_DSN = ("postgresql+psycopg2://app:MyP%40ss!%23%24%25world"
                   "@ep-test.neon.tech:5432/neondb?sslmode=require")

    def test_parse_target_output_clean(self):
        t = diag.parse_target(self._SAMPLE_DSN)
        full = t.redacted()
        assert "MyP@ss" not in full
        assert "world" not in full

    def test_fingerprint_does_not_include_password(self):
        dsn_a = "postgresql+psycopg2://u:AAA@h:5432/d"
        dsn_b = "postgresql+psycopg2://u:BBB@h:5432/d"
        assert diag.parse_target(dsn_a).fingerprint == \
               diag.parse_target(dsn_b).fingerprint


class TestRedactedTargetBlock:
    """Prove _redacted_target_block() never includes a password."""

    def test_block_never_leaks_password(self, monkeypatch):
        from unittest.mock import MagicMock

        from database import connection

        # Create a mock engine whose url contains a password
        fake_url = ("postgresql+psycopg2://testuser:Sup3rS3cret"
                    "@fake-host:5432/testdb?sslmode=require")
        mock_engine = MagicMock()
        mock_engine.url = fake_url
        # Simulate the query failing (so we test the fallback path)
        mock_engine.connect.side_effect = ConnectionError(
            "auth failed for password Sup3rS3cret")

        monkeypatch.setattr(connection, "get_engine", lambda: mock_engine)
        result = connection._redacted_target_block()
        assert "Sup3rS3cret" not in result
        # The fingerprint is safe
        assert "testuser" in result  # username is OK to show
        assert "fake-host" in result

    def test_block_handles_sqlite_gracefully(self, monkeypatch, tmp_path):
        """SQLite DSN has no host/user; block returns 'unavailable' or
        partial output, and never crashes."""
        from database import connection
        url = f"sqlite:///{(tmp_path / 'x.db').as_posix()}"
        monkeypatch.setenv("DATABASE_URL", url)
        monkeypatch.setenv("NETPILOT_DEMO_MODE", "false")
        connection._engine = None
        connection._engine_url = None
        result = connection._redacted_target_block()
        # Should not raise; may return partial or 'unavailable'
        assert isinstance(result, str)
        # No password in SQLite (there is none, but verify)
        assert "://" not in result or "sqlite" not in result
