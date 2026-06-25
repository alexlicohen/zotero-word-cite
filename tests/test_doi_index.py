"""Tests for zotero.library_doi_index and the unify.py integration.

All network calls are monkeypatched — no real Zotero API is hit.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from zoterocite import zotero as zotero_mod
from zoterocite import unify
from zoterocite import new_doc


# ---------------------------------------------------------------------------
# Helpers: canned library items
# ---------------------------------------------------------------------------

def _make_item(key: str, doi: str | None = None, extra_doi: str | None = None) -> dict:
    """Build a minimal Zotero item dict."""
    data: dict[str, Any] = {"key": key, "title": f"Title for {key}"}
    if doi:
        data["DOI"] = doi
    if extra_doi:
        data["extra"] = f"DOI: {extra_doi}\nSome other extra field"
    return {"key": key, "data": data}


CANNED_ITEMS = [
    _make_item("KEY1", doi="10.1234/alpha"),
    _make_item("KEY2", doi="https://doi.org/10.1234/BETA"),   # URL-prefixed, uppercase
    _make_item("KEY3", extra_doi="10.1234/gamma"),             # DOI in extra field
    _make_item("KEY4"),                                         # no DOI at all
]


# ---------------------------------------------------------------------------
# Fixture: reset the module-level in-memory cache before every test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_doi_index_cache(tmp_path, monkeypatch):
    """Clear in-memory caches and redirect both disk caches to a temp dir."""
    zotero_mod._doi_index_mem = None
    zotero_mod._lib_index_mem = None
    fake_cache = tmp_path / "zotero_doi_index.json"
    fake_lib_cache = tmp_path / "zotero_library_index.json"
    monkeypatch.setattr(zotero_mod, "_DOI_INDEX_CACHE", fake_cache)
    monkeypatch.setattr(zotero_mod, "_LIB_INDEX_CACHE", fake_lib_cache)
    yield
    zotero_mod._doi_index_mem = None
    zotero_mod._lib_index_mem = None


# ---------------------------------------------------------------------------
# Tests for library_doi_index
# ---------------------------------------------------------------------------

class TestLibraryDoiIndex:
    def test_builds_normalized_index(self, monkeypatch):
        """Index maps normalized DOIs (lowercase, prefix stripped) to item keys."""
        monkeypatch.setattr(zotero_mod, "fetch_all", lambda **kw: CANNED_ITEMS)

        idx = zotero_mod.library_doi_index()

        # data.DOI, plain
        assert idx.get("10.1234/alpha") == "KEY1"
        # data.DOI, URL-prefixed and uppercase → normalized
        assert idx.get("10.1234/beta") == "KEY2"
        # extra field DOI
        assert idx.get("10.1234/gamma") == "KEY3"
        # item with no DOI is absent
        assert "KEY4" not in idx.values() or all(
            v != "KEY4" for v in idx.values()
        )

    def test_cache_prevents_second_fetch(self, monkeypatch):
        """A second call within TTL reuses the cache; fetch_all called only once."""
        call_count = {"n": 0}

        def counting_fetch(**kw):
            call_count["n"] += 1
            return CANNED_ITEMS

        monkeypatch.setattr(zotero_mod, "fetch_all", counting_fetch)

        zotero_mod.library_doi_index()
        zotero_mod.library_doi_index()

        assert call_count["n"] == 1, "fetch_all should be called exactly once within TTL"

    def test_refresh_forces_refetch(self, monkeypatch):
        """refresh=True bypasses both caches and re-fetches."""
        call_count = {"n": 0}

        def counting_fetch(**kw):
            call_count["n"] += 1
            return CANNED_ITEMS

        monkeypatch.setattr(zotero_mod, "fetch_all", counting_fetch)

        zotero_mod.library_doi_index()
        zotero_mod.library_doi_index(refresh=True)

        assert call_count["n"] == 2, "refresh=True must trigger a second fetch"

    def test_disk_cache_written_and_reused(self, monkeypatch):
        """Index is written to disk; a fresh process (cleared mem cache) reuses it."""
        monkeypatch.setattr(zotero_mod, "fetch_all", lambda **kw: CANNED_ITEMS)

        # First call — populates disk cache
        idx1 = zotero_mod.library_doi_index()

        # Simulate new process: clear in-memory cache
        zotero_mod._doi_index_mem = None

        call_count = {"n": 0}

        def should_not_fetch(**kw):
            call_count["n"] += 1
            return []

        monkeypatch.setattr(zotero_mod, "fetch_all", should_not_fetch)

        idx2 = zotero_mod.library_doi_index()

        assert call_count["n"] == 0, "disk cache should be used; fetch_all must not be called"
        assert idx1 == idx2

    def test_fetch_failure_no_cache_raises_unavailable(self, monkeypatch):
        """On fetch failure with no disk cache, RAISE LibraryUnavailableError.

        A degraded read must NOT collapse to ``{}`` (an empty library), or a
        write path would treat every work as missing and create duplicates in
        the shared group. Failure and empty are distinct outcomes (F2).
        """
        def boom(**kw):
            raise RuntimeError("network is down")

        monkeypatch.setattr(zotero_mod, "fetch_all", boom)

        with pytest.raises(zotero_mod.LibraryUnavailableError):
            zotero_mod.library_doi_index()

    def test_empty_library_returns_empty_not_raise(self, monkeypatch):
        """A SUCCESSFUL fetch that returns no items is a genuinely empty library:
        return ``{}`` and do NOT raise (creating into it is safe)."""
        monkeypatch.setattr(zotero_mod, "fetch_all", lambda **kw: [])

        result = zotero_mod.library_doi_index()

        assert result == {}, "an empty (but readable) library must return {}"

    def test_fetch_failure_falls_back_to_disk_cache(self, monkeypatch):
        """On fetch failure, an existing disk cache is returned."""
        # Write a stale-ish disk cache (1 hour old, within default 24h TTL)
        stale_index = {"10.9999/stale": "STALEKEY"}
        fetched_at = time.time() - 3600  # 1 hour ago
        cache_path = zotero_mod._DOI_INDEX_CACHE
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({"fetched_at": fetched_at, "index": stale_index}), encoding="utf-8"
        )

        def boom(**kw):
            raise RuntimeError("network is down")

        monkeypatch.setattr(zotero_mod, "fetch_all", boom)

        result = zotero_mod.library_doi_index()

        assert result == stale_index

    def test_expired_disk_cache_triggers_refetch(self, monkeypatch):
        """An on-disk cache older than max_age_hours triggers a fresh fetch."""
        old_index = {"10.9999/old": "OLDKEY"}
        # Write cache that is 25 hours old (past the default 24h TTL)
        fetched_at = time.time() - (25 * 3600)
        cache_path = zotero_mod._DOI_INDEX_CACHE
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({"fetched_at": fetched_at, "index": old_index}), encoding="utf-8"
        )

        call_count = {"n": 0}

        def counting_fetch(**kw):
            call_count["n"] += 1
            return CANNED_ITEMS

        monkeypatch.setattr(zotero_mod, "fetch_all", counting_fetch)

        result = zotero_mod.library_doi_index()

        assert call_count["n"] == 1, "expired cache must trigger a new fetch"
        assert "10.1234/alpha" in result


# ---------------------------------------------------------------------------
# Tests for library_index — the combined {"doi","pmid","title"} index (Bug 1)
# ---------------------------------------------------------------------------

def _make_item_full(key, *, doi=None, pmid_extra=None, pmid_field=None, title=None):
    data: dict[str, Any] = {"key": key, "title": title or f"Title for {key}"}
    if doi:
        data["DOI"] = doi
    extra_lines = []
    if pmid_extra:
        extra_lines.append(f"PMID: {pmid_extra}")
    if extra_lines:
        data["extra"] = "\n".join(extra_lines)
    if pmid_field:
        data["PMID"] = pmid_field
    return {"key": key, "data": data}


LIB_ITEMS = [
    _make_item_full("DK", doi="10.1234/alpha", title="Alpha Paper"),
    _make_item_full("PK", pmid_extra="31234567", title="PMID-only Paper"),  # PMID in extra, no DOI
    _make_item_full("PF", pmid_field="40000001", title="PMID-field Paper"),  # PMID data key
    _make_item_full("TK", title="A Title Only Paper"),                       # title only
]


class TestLibraryIndex:
    def test_builds_all_three_in_one_fetch(self, monkeypatch):
        """One fetch_all builds DOI, PMID, and title maps."""
        fetch_calls = {"n": 0}

        def counting_fetch(**kw):
            fetch_calls["n"] += 1
            return LIB_ITEMS

        monkeypatch.setattr(zotero_mod, "fetch_all", counting_fetch)

        idx = zotero_mod.library_index()

        assert fetch_calls["n"] == 1, "library_index must fetch the library exactly once"
        assert idx["doi"].get("10.1234/alpha") == "DK"
        assert idx["pmid"].get("31234567") == "PK"      # parsed from extra "PMID: N"
        assert idx["pmid"].get("40000001") == "PF"      # from PMID data key
        assert idx["title"].get(zotero_mod._normalize_title("A Title Only Paper")) == "TK"
        # The DOI item is also title-indexed (every item with a title is).
        assert idx["title"].get(zotero_mod._normalize_title("Alpha Paper")) == "DK"

    def test_cache_prevents_second_fetch(self, monkeypatch):
        calls = {"n": 0}

        def counting(**kw):
            calls["n"] += 1
            return LIB_ITEMS

        monkeypatch.setattr(zotero_mod, "fetch_all", counting)
        zotero_mod.library_index()
        zotero_mod.library_index()
        assert calls["n"] == 1, "second call within TTL must reuse the cache"

    def test_refresh_forces_refetch(self, monkeypatch):
        calls = {"n": 0}

        def counting(**kw):
            calls["n"] += 1
            return LIB_ITEMS

        monkeypatch.setattr(zotero_mod, "fetch_all", counting)
        zotero_mod.library_index()
        zotero_mod.library_index(refresh=True)
        assert calls["n"] == 2

    def test_disk_cache_written_and_reused(self, monkeypatch):
        monkeypatch.setattr(zotero_mod, "fetch_all", lambda **kw: LIB_ITEMS)
        idx1 = zotero_mod.library_index()

        zotero_mod._lib_index_mem = None  # simulate fresh process

        def should_not_fetch(**kw):
            raise AssertionError("disk cache should serve; fetch_all must not run")

        monkeypatch.setattr(zotero_mod, "fetch_all", should_not_fetch)
        idx2 = zotero_mod.library_index()
        assert idx1 == idx2

    def test_empty_library_returns_three_empty_maps(self, monkeypatch):
        monkeypatch.setattr(zotero_mod, "fetch_all", lambda **kw: [])
        idx = zotero_mod.library_index()
        assert idx == {"doi": {}, "pmid": {}, "title": {}}

    def test_fetch_failure_no_cache_raises_unavailable(self, monkeypatch):
        """A degraded read with no usable cache RAISES (never a silent empty
        library) so the write path fails closed and cannot mass-duplicate."""
        def boom(**kw):
            raise RuntimeError("network is down")

        monkeypatch.setattr(zotero_mod, "fetch_all", boom)
        with pytest.raises(zotero_mod.LibraryUnavailableError):
            zotero_mod.library_index()

    def test_fetch_failure_falls_back_to_disk_cache(self, monkeypatch):
        stale = {"doi": {"10.9/stale": "SK"}, "pmid": {"1": "SK"}, "title": {"t": "SK"}}
        fetched_at = time.time() - 3600
        cache_path = zotero_mod._LIB_INDEX_CACHE
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({"fetched_at": fetched_at, "index": stale}), encoding="utf-8"
        )

        def boom(**kw):
            raise RuntimeError("network is down")

        monkeypatch.setattr(zotero_mod, "fetch_all", boom)
        result = zotero_mod.library_index()
        assert result == stale

    def test_pmid_extraction_helper(self):
        """_extract_pmid_from_item reads the data key first, then extra."""
        assert zotero_mod._extract_pmid_from_item(
            {"data": {"extra": "PMID: 12345\nfoo"}}) == "12345"
        assert zotero_mod._extract_pmid_from_item(
            {"data": {"PMID": "67890"}}) == "67890"
        assert zotero_mod._extract_pmid_from_item({"data": {}}) is None


class TestLibraryIndexStrictLenient:
    """The strict/lenient split: writes fail closed; reads degrade to the
    library_doi_index disk cache (DOI-only) instead of going empty."""

    @staticmethod
    def _boom(**kw):
        raise RuntimeError("network is down")

    @staticmethod
    def _write_doi_cache(map_: dict[str, str]) -> None:
        """Seed the legacy DOI-only disk cache (zotero_doi_index.json)."""
        p = zotero_mod._DOI_INDEX_CACHE
        p.parent.mkdir(parents=True, exist_ok=True)
        # Stale on purpose — the lenient fallback ignores TTL on a failed read.
        p.write_text(
            json.dumps({"fetched_at": time.time() - 10 * 86400, "index": map_}),
            encoding="utf-8",
        )

    def test_lenient_falls_back_to_doi_cache(self, monkeypatch):
        """strict=False + failed fetch + cold combined cache + present DOI cache
        → DOI-only index, no raise; status reports degraded/doi_only."""
        doi_map = {"10.1234/alpha": "AKEY", "10.1234/beta": "BKEY"}
        self._write_doi_cache(doi_map)
        monkeypatch.setattr(zotero_mod, "fetch_all", self._boom)

        idx, status = zotero_mod.library_index_status(strict=False)
        assert idx == {"doi": doi_map, "pmid": {}, "title": {}}
        assert status == {"degraded": True, "doi_only": True}
        # The plain back-compat accessor returns the same dict (no raise).
        zotero_mod._lib_index_mem = None
        assert zotero_mod.library_index(strict=False) == {
            "doi": doi_map, "pmid": {}, "title": {},
        }

    def test_strict_failed_fetch_cold_cache_raises(self, monkeypatch):
        """strict=True (default) + failed fetch + cold combined cache → RAISE,
        even when a stale DOI cache exists (fail-closed: never serve it to a write)."""
        # A stale DOI cache is present, but strict must NOT use it.
        self._write_doi_cache({"10.1234/alpha": "AKEY"})
        monkeypatch.setattr(zotero_mod, "fetch_all", self._boom)

        with pytest.raises(zotero_mod.LibraryUnavailableError):
            zotero_mod.library_index()           # default strict=True
        with pytest.raises(zotero_mod.LibraryUnavailableError):
            zotero_mod.library_index(strict=True)

    def test_lenient_neither_cache_raises(self, monkeypatch):
        """strict=False but NEITHER cache loadable → still raises (nothing to serve)."""
        # No combined cache (tmp), no DOI cache written.
        assert not zotero_mod._DOI_INDEX_CACHE.exists()
        assert not zotero_mod._LIB_INDEX_CACHE.exists()
        monkeypatch.setattr(zotero_mod, "fetch_all", self._boom)

        with pytest.raises(zotero_mod.LibraryUnavailableError):
            zotero_mod.library_index(strict=False)

    def test_lenient_prefers_fresh_combined_cache_not_degraded(self, monkeypatch):
        """A usable combined cache is served full (DOI+PMID+title), NOT degraded,
        even with strict=False — the DOI-only fallback is the LAST resort."""
        combined = {"doi": {"10.1/x": "XK"}, "pmid": {"99": "XK"}, "title": {"t": "XK"}}
        lp = zotero_mod._LIB_INDEX_CACHE
        lp.parent.mkdir(parents=True, exist_ok=True)
        lp.write_text(
            json.dumps({"fetched_at": time.time() - 3600, "index": combined}),
            encoding="utf-8",
        )
        # DOI cache also present but must be ignored in favour of the richer cache.
        self._write_doi_cache({"10.1/x": "XK"})

        def should_not_fetch(**kw):
            raise AssertionError("fresh combined cache should serve; no fetch")

        monkeypatch.setattr(zotero_mod, "fetch_all", should_not_fetch)
        idx, status = zotero_mod.library_index_status(strict=False)
        assert idx == combined
        assert status == {"degraded": False, "doi_only": False}

    def test_status_clean_on_normal_fetch(self, monkeypatch):
        """A successful live fetch reports non-degraded status."""
        monkeypatch.setattr(zotero_mod, "fetch_all", lambda **kw: LIB_ITEMS)
        idx, status = zotero_mod.library_index_status(strict=False)
        assert idx["doi"].get("10.1234/alpha") == "DK"
        assert status == {"degraded": False, "doi_only": False}

    def test_strict_stale_combined_cache_raises(self, monkeypatch):
        """H1: strict=True + failed fetch + a TTL-EXPIRED combined cache must RAISE,
        not silently serve the stale index. A stale combined cache can miss
        recently-added group items → duplicate creation on the WRITE path.
        'Complete' is not 'fresh'. (Before the fix this returned the stale dict as
        degraded=False and apply_unification created against it.)"""
        stale = {"doi": {"10.1/x": "XK"}, "pmid": {}, "title": {}}
        lp = zotero_mod._LIB_INDEX_CACHE
        lp.parent.mkdir(parents=True, exist_ok=True)
        lp.write_text(
            json.dumps({"fetched_at": time.time() - 25 * 3600, "index": stale}),
            encoding="utf-8",
        )
        monkeypatch.setattr(zotero_mod, "fetch_all", self._boom)
        with pytest.raises(zotero_mod.LibraryUnavailableError):
            zotero_mod.library_index()  # strict=True default — fail closed

    def test_lenient_stale_combined_cache_served_degraded(self, monkeypatch):
        """H1: strict=False + failed fetch + a TTL-expired combined cache serves the
        (richer) combined index but flags it DEGRADED (not doi_only), so coverage
        consumers surface a 'provisional' note rather than trusting it blindly."""
        stale = {"doi": {"10.1/x": "XK"}, "pmid": {"9": "XK"}, "title": {"t": "XK"}}
        lp = zotero_mod._LIB_INDEX_CACHE
        lp.parent.mkdir(parents=True, exist_ok=True)
        lp.write_text(
            json.dumps({"fetched_at": time.time() - 25 * 3600, "index": stale}),
            encoding="utf-8",
        )
        monkeypatch.setattr(zotero_mod, "fetch_all", self._boom)
        idx, status = zotero_mod.library_index_status(strict=False)
        assert idx == stale
        assert status == {"degraded": True, "doi_only": False}

    def test_strict_fresh_combined_cache_on_refresh_serves(self, monkeypatch):
        """A FRESH (within-TTL) combined cache IS still served to strict on a
        fetch failure (refresh=True path) — only TTL-expired caches fail closed."""
        fresh = {"doi": {"10.1/x": "XK"}, "pmid": {}, "title": {}}
        lp = zotero_mod._LIB_INDEX_CACHE
        lp.parent.mkdir(parents=True, exist_ok=True)
        lp.write_text(
            json.dumps({"fetched_at": time.time() - 3600, "index": fresh}),
            encoding="utf-8",
        )
        monkeypatch.setattr(zotero_mod, "fetch_all", self._boom)
        assert zotero_mod.library_index(refresh=True) == fresh  # fresh → served


# ---------------------------------------------------------------------------
# Tests for unify.py integration
# ---------------------------------------------------------------------------

def _meta(doi, title, family="Author", year="2020", journal="J", typ="article-journal"):
    return {
        "doi": doi,
        "title": title,
        "authors": [{"family": family, "given": "A"}],
        "year": year,
        "journal": journal,
        "type": typ,
    }


WORK_A = _meta("10.5/aaa", "Work A")
WORK_B = _meta("10.5/bbb", "Work B")
WORK_C = _meta("10.5/ccc", "Work C")

# Library has Work A (key KEYA); B and C are missing.
FAKE_DOI_INDEX = {
    "10.5/aaa": "KEYA",
}


def _fake_resolve_many(text, *, fetch=True):
    """Resolve three works by substring of their text."""
    t = text.lower()
    if "work a" in t:
        return {
            "input": text, "metadata": dict(WORK_A), "confidence": "high",
            "source": "crossref",
            "candidates": [{**WORK_A, "score": 90.0}],
            "identifiers": {"doi": None, "pmid": None, "arxiv": None, "isbn": None},
        }
    if "work b" in t:
        return {
            "input": text, "metadata": dict(WORK_B), "confidence": "high",
            "source": "crossref",
            "candidates": [{**WORK_B, "score": 90.0}],
            "identifiers": {"doi": None, "pmid": None, "arxiv": None, "isbn": None},
        }
    if "work c" in t:
        return {
            "input": text, "metadata": dict(WORK_C), "confidence": "high",
            "source": "crossref",
            "candidates": [{**WORK_C, "score": 90.0}],
            "identifiers": {"doi": None, "pmid": None, "arxiv": None, "isbn": None},
        }
    return {
        "input": text, "metadata": None, "confidence": "low",
        "source": "crossref",
        "candidates": [],
        "identifiers": {"doi": None, "pmid": None, "arxiv": None, "isbn": None},
    }


class TestUnifyUsesIndex:
    @pytest.fixture()
    def three_ref_doc(self, tmp_path):
        """A doc with three reflist entries (Work A, B, C) — no placeholders."""
        p = tmp_path / "three.docx"
        new_doc(p, [
            "Text citing work a [1], work b [2], work c [3].",
            "References",
            "1. Work A. J. 2020.",
            "2. Work B. J. 2020.",
            "3. Work C. J. 2020.",
        ])
        return p

    def test_plan_calls_library_doi_index_once(self, three_ref_doc, monkeypatch):
        """plan_unification calls library_index exactly once, regardless of N refs."""
        call_count = {"n": 0}

        def counting_index(**kw):
            call_count["n"] += 1
            return {"doi": dict(FAKE_DOI_INDEX), "pmid": {}, "title": {}}

        monkeypatch.setattr(unify.zotero, "library_index", counting_index)
        monkeypatch.setattr(unify.refresolve, "resolve_reference", _fake_resolve_many)
        monkeypatch.setattr(unify.citecheck, "ensure_retraction_db", lambda **kw: (None, None))

        plan = unify.plan_unification(three_ref_doc)

        assert call_count["n"] == 1, (
            f"library_index must be called exactly once; got {call_count['n']}"
        )

    def test_plan_get_item_by_doi_not_called(self, three_ref_doc, monkeypatch):
        """get_item_by_doi must NOT be called per-reference in plan_unification."""
        get_item_calls = {"n": 0}

        def should_not_call(doi):
            get_item_calls["n"] += 1
            return None

        monkeypatch.setattr(unify.zotero, "library_index",
                            lambda **kw: {"doi": dict(FAKE_DOI_INDEX), "pmid": {}, "title": {}})
        monkeypatch.setattr(unify.zotero, "get_item_by_doi", should_not_call)
        monkeypatch.setattr(unify.refresolve, "resolve_reference", _fake_resolve_many)
        monkeypatch.setattr(unify.citecheck, "ensure_retraction_db", lambda **kw: (None, None))

        unify.plan_unification(three_ref_doc)

        assert get_item_calls["n"] == 0, (
            "get_item_by_doi must not be called per-reference; use library_index"
        )

    def test_plan_doi_match_via_index(self, three_ref_doc, monkeypatch):
        """Work A (in index) is flagged in_library=True; B and C are not."""
        monkeypatch.setattr(unify.zotero, "library_index",
                            lambda **kw: {"doi": dict(FAKE_DOI_INDEX), "pmid": {}, "title": {}})
        monkeypatch.setattr(unify.refresolve, "resolve_reference", _fake_resolve_many)
        monkeypatch.setattr(unify.citecheck, "ensure_retraction_db", lambda **kw: (None, None))

        plan = unify.plan_unification(three_ref_doc)
        refs = plan["references"]

        in_lib = {r["metadata"]["doi"]: r["in_library"] for r in refs if r.get("metadata")}
        keys   = {r["metadata"]["doi"]: r["existing_key"] for r in refs if r.get("metadata")}

        assert in_lib.get("10.5/aaa") is True
        assert keys.get("10.5/aaa") == "KEYA"
        assert in_lib.get("10.5/bbb") is False
        assert in_lib.get("10.5/ccc") is False

    def test_apply_calls_library_doi_index_once(self, three_ref_doc, monkeypatch):
        """apply_unification calls library_index exactly once for placeholder lookups."""
        call_count = {"n": 0}

        def counting_index(**kw):
            call_count["n"] += 1
            return {"doi": dict(FAKE_DOI_INDEX), "pmid": {}, "title": {}}

        monkeypatch.setattr(unify.zotero, "library_index", counting_index)
        monkeypatch.setattr(unify.refresolve, "resolve_reference", _fake_resolve_many)
        monkeypatch.setattr(unify.citecheck, "ensure_retraction_db", lambda **kw: (None, None))

        # Build plan first (this call is separate, counted separately)
        plan = unify.plan_unification(three_ref_doc)

        # Reset counter before apply
        call_count["n"] = 0

        monkeypatch.setattr(unify.zotero, "key_can_write", lambda: False)
        monkeypatch.setattr(unify.zotero, "key_can_write_status", lambda: False)
        monkeypatch.setattr(unify.zotero, "item_uri",
                            lambda key: "http://zotero.org/groups/x/items/" + key)
        monkeypatch.setattr(unify.zoterofield, "insert_citation", lambda doc, *a, **k: doc)

        out = three_ref_doc.parent / "out.docx"
        unify.apply_unification(three_ref_doc, plan, {"accept": [0, 1, 2]}, out=out)

        assert call_count["n"] == 1, (
            f"apply_unification must call library_index exactly once; got {call_count['n']}"
        )
