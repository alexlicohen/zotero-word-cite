"""Network-bound / fail-closed guards for the Zotero citation engine.

Covers the shared hardening fixes (all offline; the network is monkeypatched):

1. ``zotero._effective_timeout`` clamps to the shared ZOTERO_WORD_CITE_HTTP_TIMEOUT
   ceiling and passes through unchanged when unset.
2. ``zotero.fetch_all`` raises ``LibraryUnavailableError`` (the degraded-read
   signal) when the overall wall-clock budget is exceeded across pages, and emits
   a STDERR progress heartbeat. [SAFETY GUARD — has teeth]
3. ``zotero.create_items(index_degraded=True)`` routes an index-absent item to
   ``skipped_degraded_read`` and creates NOTHING, while an index-present item
   still lands in ``skipped_existing``; the write-gate fail-closed path still
   fills ``failed``. [SAFETY GUARD — has teeth]
"""
import pytest

from zoterocite import zotero, citecheck


# ---------------------------------------------------------------------------
# FIX 1 — reads honour the ZOTERO_WORD_CITE_HTTP_TIMEOUT ceiling
# ---------------------------------------------------------------------------
class TestEffectiveTimeout:
    def test_passthrough_when_unset(self, monkeypatch):
        monkeypatch.delenv("ZOTERO_WORD_CITE_HTTP_TIMEOUT", raising=False)
        assert zotero._effective_timeout(15.0) == 15.0
        assert zotero._effective_timeout(120.0) == 120.0

    def test_clamps_to_ceiling(self, monkeypatch):
        monkeypatch.setenv("ZOTERO_WORD_CITE_HTTP_TIMEOUT", "3")
        # A generous per-call timeout is clamped down to the ceiling.
        assert zotero._effective_timeout(30.0) == 3.0
        assert zotero._effective_timeout(120.0) == 3.0

    def test_ceiling_above_call_timeout_is_noop(self, monkeypatch):
        # A ceiling LOOSER than the caller's timeout never raises the timeout.
        monkeypatch.setenv("ZOTERO_WORD_CITE_HTTP_TIMEOUT", "999")
        assert zotero._effective_timeout(15.0) == 15.0

    def test_malformed_or_nonpositive_ceiling_ignored(self, monkeypatch):
        monkeypatch.setenv("ZOTERO_WORD_CITE_HTTP_TIMEOUT", "not-a-number")
        assert zotero._effective_timeout(15.0) == 15.0
        monkeypatch.setenv("ZOTERO_WORD_CITE_HTTP_TIMEOUT", "0")
        assert zotero._effective_timeout(15.0) == 15.0


# ---------------------------------------------------------------------------
# FIX 2 — fetch_all overall wall-clock budget (SAFETY GUARD)
# ---------------------------------------------------------------------------
class TestFetchAllBudget:
    def _setenv(self, monkeypatch):
        monkeypatch.setenv("ZOTERO_API_KEY", "k")
        monkeypatch.setenv("ZOTERO_GROUP_ID", "42")
        monkeypatch.delenv("ZOTERO_USER_ID", raising=False)
        monkeypatch.delenv("ZOTERO_WORD_CITE_ZOTERO_FETCH_BUDGET", raising=False)  # default 120

    def test_budget_exceeded_raises_and_heartbeats(self, monkeypatch, capsys):
        """A fetch that blows the wall-clock budget mid-pagination raises the
        degraded-read exception (so the index builders fall back to cache / fail
        closed) and has emitted a STDERR heartbeat before doing so.

        TEETH: guarded by the deadline line in fetch_all
        ``if time.time() - deadline_start > budget:`` — replacing it with
        ``if False:`` lets the loop drain all 5 pages and return normally, so
        ``pytest.raises`` fails (RED)."""
        self._setenv(monkeypatch)

        clock = {"t": 1000.0}
        pages = {"n": 0}

        def fake_time():
            return clock["t"]

        def fake_headers(req, timeout=15.0):
            pages["n"] += 1
            clock["t"] += 1000.0  # each page "took" 1000s of wall-clock time
            # Total-Results kept small so that WITHOUT the deadline the loop still
            # terminates quickly (5 pages) instead of hanging — the mutation must
            # RED via "no exception", never via a timeout.
            return ([{"key": f"K{pages['n']}", "data": {}}], {"Total-Results": "5"})

        monkeypatch.setattr(zotero.time, "time", fake_time)
        monkeypatch.setattr(zotero, "_get_json_headers", fake_headers)

        with pytest.raises(zotero.LibraryUnavailableError):
            zotero.fetch_all()

        err = capsys.readouterr().err
        assert "zotero: fetched" in err, "expected a STDERR progress heartbeat"

    def test_within_budget_completes_normally(self, monkeypatch, capsys):
        """With time not advancing past the budget, fetch_all returns all items."""
        self._setenv(monkeypatch)
        clock = {"t": 1000.0}
        pages = {"n": 0}
        page_data = [
            ([{"key": f"K{i}"} for i in range(100)], {"Total-Results": "150"}),
            ([{"key": f"K{i}"} for i in range(100, 150)], {"Total-Results": "150"}),
        ]

        def fake_time():
            return clock["t"]  # never advances → never exceeds budget

        def fake_headers(req, timeout=15.0):
            i = pages["n"]; pages["n"] += 1
            return page_data[i]

        monkeypatch.setattr(zotero.time, "time", fake_time)
        monkeypatch.setattr(zotero, "_get_json_headers", fake_headers)
        out = zotero.fetch_all()
        assert len(out) == 150 and pages["n"] == 2

    def test_fetch_budget_floor_and_default(self, monkeypatch):
        monkeypatch.delenv("ZOTERO_WORD_CITE_ZOTERO_FETCH_BUDGET", raising=False)
        assert zotero._fetch_budget() == zotero._DEFAULT_FETCH_BUDGET
        monkeypatch.setenv("ZOTERO_WORD_CITE_ZOTERO_FETCH_BUDGET", "0.001")
        assert zotero._fetch_budget() == zotero._MIN_FETCH_BUDGET  # floored, never sub-second
        monkeypatch.setenv("ZOTERO_WORD_CITE_ZOTERO_FETCH_BUDGET", "bad")
        assert zotero._fetch_budget() == zotero._DEFAULT_FETCH_BUDGET


# ---------------------------------------------------------------------------
# FIX 4b — create_items(index_degraded=True) fails closed (SAFETY GUARD)
# ---------------------------------------------------------------------------
class TestCreateItemsIndexDegraded:
    def _creds(self, monkeypatch):
        monkeypatch.setenv("ZOTERO_API_KEY", "k")
        monkeypatch.setenv("ZOTERO_GROUP_ID", "42")
        monkeypatch.delenv("ZOTERO_USER_ID", raising=False)

    def test_degraded_absent_refused_present_skipped(self, monkeypatch):
        """On a degraded index read: an index-ABSENT item is refused (routed to
        ``skipped_degraded_read``, never POSTed) while an index-PRESENT item is
        recorded ``skipped_existing``. Nothing is created.

        TEETH: guarded by the create_items line
        ``if existing_key is None and index_degraded:`` — replacing it with
        ``if False:`` (or ``and not index_degraded``) lets the absent item fall
        through to creation, so ``created`` becomes non-empty and ``_post_json``
        is called (RED on both assertions)."""
        self._creds(monkeypatch)
        norm_present = citecheck._normalise_doi("10.1/present")
        doi_index = {norm_present: "PKEY"}

        post_calls = []

        def fake_post_json(path, body, *, extra_headers=None, timeout=15.0):
            post_calls.append((path, list(body)))
            return {"successful": {str(i): {"key": f"NEW{i}"} for i in range(len(body))},
                    "unchanged": {}, "failed": {}}

        monkeypatch.setattr(zotero, "key_can_write_status", lambda: True)
        monkeypatch.setattr(zotero, "_title_exists_in_library", lambda t: None)
        monkeypatch.setattr(zotero, "_post_json", fake_post_json)

        metas = [
            {"title": "Present", "doi": "10.1/present"},
            {"title": "Absent", "doi": "10.1/absent"},
        ]
        result = zotero.create_items(
            metas, dedup=True, index_degraded=True,
            doi_index=doi_index, pmid_index={},
        )

        assert result["created"] == [], "degraded read must create NOTHING"
        assert post_calls == [], "no POST may be issued when the absent item is refused"
        assert [d["title"] for d in result["skipped_degraded_read"]] == ["Absent"]
        assert len(result["skipped_existing"]) == 1
        assert result["skipped_existing"][0]["existing_key"] == "PKEY"

    def test_write_gate_failclosed_still_fills_failed(self, monkeypatch):
        """The write-gate fail-closed path is unchanged: a non-writable key routes
        every item to ``failed`` (with a truthful reason) and creates nothing, and
        the new ``skipped_degraded_read`` key is present (empty)."""
        self._creds(monkeypatch)
        monkeypatch.setattr(zotero, "key_can_write_status", lambda: False)

        def boom(*a, **k):
            raise AssertionError("no POST when the write gate refuses")

        monkeypatch.setattr(zotero, "_post_json", boom)

        metas = [{"title": "A", "doi": "10.1/a"}, {"title": "B", "doi": "10.1/b"}]
        result = zotero.create_items(metas, dedup=True, index_degraded=True)
        assert result["created"] == []
        assert len(result["failed"]) == 2
        assert result["skipped_degraded_read"] == []
        assert all("write access" in f["reason"] for f in result["failed"])

    def test_non_degraded_default_unchanged(self, monkeypatch):
        """With index_degraded=False (default), an index-absent item still flows to
        creation as before (no skipped_degraded_read routing)."""
        self._creds(monkeypatch)
        post_calls = []

        def fake_post_json(path, body, *, extra_headers=None, timeout=15.0):
            post_calls.append((path, list(body)))
            return {"successful": {str(i): {"key": f"NEW{i}"} for i in range(len(body))},
                    "unchanged": {}, "failed": {}}

        monkeypatch.setattr(zotero, "key_can_write_status", lambda: True)
        monkeypatch.setattr(zotero, "_title_exists_in_library", lambda t: None)
        monkeypatch.setattr(zotero, "_post_json", fake_post_json)

        result = zotero.create_items(
            [{"title": "Absent", "doi": "10.1/absent"}],
            dedup=True, doi_index={}, pmid_index={},
        )
        assert len(result["created"]) == 1
        assert result["skipped_degraded_read"] == []
        assert post_calls, "the absent item is created on the normal (non-degraded) path"
