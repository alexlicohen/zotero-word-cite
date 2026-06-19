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
    """Clear in-memory cache and redirect the disk cache to a temp dir."""
    zotero_mod._doi_index_mem = None
    fake_cache = tmp_path / "zotero_doi_index.json"
    monkeypatch.setattr(zotero_mod, "_DOI_INDEX_CACHE", fake_cache)
    yield
    zotero_mod._doi_index_mem = None


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
        """plan_unification calls library_doi_index exactly once, regardless of N refs."""
        call_count = {"n": 0}

        def counting_index(**kw):
            call_count["n"] += 1
            return dict(FAKE_DOI_INDEX)

        monkeypatch.setattr(unify.zotero, "library_doi_index", counting_index)
        monkeypatch.setattr(unify.refresolve, "resolve_reference", _fake_resolve_many)
        monkeypatch.setattr(unify.citecheck, "ensure_retraction_db", lambda **kw: (None, None))

        plan = unify.plan_unification(three_ref_doc)

        assert call_count["n"] == 1, (
            f"library_doi_index must be called exactly once; got {call_count['n']}"
        )

    def test_plan_get_item_by_doi_not_called(self, three_ref_doc, monkeypatch):
        """get_item_by_doi must NOT be called per-reference in plan_unification."""
        get_item_calls = {"n": 0}

        def should_not_call(doi):
            get_item_calls["n"] += 1
            return None

        monkeypatch.setattr(unify.zotero, "library_doi_index", lambda **kw: dict(FAKE_DOI_INDEX))
        monkeypatch.setattr(unify.zotero, "get_item_by_doi", should_not_call)
        monkeypatch.setattr(unify.refresolve, "resolve_reference", _fake_resolve_many)
        monkeypatch.setattr(unify.citecheck, "ensure_retraction_db", lambda **kw: (None, None))

        unify.plan_unification(three_ref_doc)

        assert get_item_calls["n"] == 0, (
            "get_item_by_doi must not be called per-reference; use library_doi_index"
        )

    def test_plan_doi_match_via_index(self, three_ref_doc, monkeypatch):
        """Work A (in index) is flagged in_library=True; B and C are not."""
        monkeypatch.setattr(unify.zotero, "library_doi_index", lambda **kw: dict(FAKE_DOI_INDEX))
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
        """apply_unification calls library_doi_index exactly once for placeholder lookups."""
        call_count = {"n": 0}

        def counting_index(**kw):
            call_count["n"] += 1
            return dict(FAKE_DOI_INDEX)

        monkeypatch.setattr(unify.zotero, "library_doi_index", counting_index)
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
            f"apply_unification must call library_doi_index exactly once; got {call_count['n']}"
        )
