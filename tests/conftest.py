import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))          # make `zoterocite` importable
FIXTURES = ROOT / "fixtures"


@pytest.fixture
def fixtures():
    return FIXTURES


@pytest.fixture(autouse=True)
def _isolate_http_cache(tmp_path, monkeypatch):
    """Isolate the opt-in ``_http`` response cache for EVERY test.

    ``_http.http_get(..., cache_ttl=...)`` (used by the EFetch/iCite/Crossref
    resolution reads) writes a disk-backed cache under ``data/``. Without
    isolation a test that patches ``urlopen`` would (a) write the real
    ``data/http_response_cache.json`` and (b) serve another test's cached body,
    making the suite order-dependent. Redirect the cache file to a per-test tmp
    path and clear the in-memory layer so each test starts cold and touches no
    shared state. Tests that specifically exercise the cache redirect the path
    themselves (their fixture runs after this one, so it wins)."""
    try:
        import zoterocite._http as _h
        monkeypatch.setattr(
            _h, "_HTTP_CACHE_PATH", tmp_path / "http_response_cache.json",
            raising=False,
        )
        _h._reset_http_cache()
    except Exception:  # noqa: BLE001 — isolation is best-effort; never break a test
        pass


@pytest.fixture(autouse=True)
def _isolate_retraction_feed(tmp_path, monkeypatch):
    """No test reads or downloads the live Retraction Watch feed (~65 MB).
    ``cite_check`` and unify/endnote's ``load_retraction_map(allow_network=True)``
    refresh a missing or stale feed into the package-relative ``data/`` cache, so
    the default path points at a per-test tmp file and every real ``urlopen``
    fails. Tests that fake ``urlopen`` or the path patch after this fixture runs,
    so theirs wins."""
    import urllib.error
    import urllib.request
    import zoterocite.citecheck as cc

    def _offline(*_a, **_k):
        raise urllib.error.URLError("network disabled in the test suite")

    monkeypatch.setattr(cc, "default_rw_path",
                        lambda: tmp_path / "rw_cache" / "retraction_watch.csv")
    monkeypatch.setattr(urllib.request, "urlopen", _offline)
