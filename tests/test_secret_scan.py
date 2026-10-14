"""The CI secret scanner must be clean on the real tree and catch planted
secrets (Slice 8). Test-first guard so the hygiene gate cannot silently rot."""
from tools import secret_scan_check as scan


def test_live_tree_is_clean():
    # The pipeline's single source of truth: a green result here is exactly
    # what the 'secret-scan' CI job asserts.
    assert scan.main() == 0


def test_patterns_flag_real_looking_secrets():
    assert scan._GROQ.search("k = 'gsk_" + "A" * 30 + "'")
    assert scan._ASSIGNED.search('api_key = "' + "B" * 40 + '"')
    assert scan._ASSIGNED.search("token = '" + "C" * 35 + "'")


def test_patterns_ignore_placeholders_and_fakes():
    assert not scan._GROQ.search("GROQ_API_KEY=")          # empty template
    # obviously-fake sentinel (underscore breaks the 20+ alnum run)
    assert not scan._GROQ.search("gsk_SENTINEL_SHOULD_NEVER")
    assert not scan._ASSIGNED.search("password = ''")       # short/empty


def test_scan_detects_planted_secret(tmp_path, monkeypatch):
    (tmp_path / "leak.py").write_text(
        'api_key = "' + "Z" * 40 + '"\n', encoding="utf-8")
    monkeypatch.setattr(scan, "_ROOT", str(tmp_path))
    hits = scan._scan()
    assert hits and hits[0][0] == "leak.py"
