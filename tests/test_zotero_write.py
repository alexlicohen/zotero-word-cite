"""Tests for the Zotero write capability in zoterocite/zotero.py.

All HTTP calls are monkeypatched — no real network, no real writes.
Group library ID 2504198 is referenced in canned responses but never contacted.
"""
from __future__ import annotations

import io
import json
import secrets
from typing import Any
from unittest.mock import MagicMock, patch
import urllib.error
import urllib.parse

import pytest

from zoterocite import zotero


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_response(body: Any, headers: dict | None = None, status: int = 200):
    """Return a context-manager mock that mimics ``urllib.request.urlopen``."""
    raw = json.dumps(body).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = raw
    mock_resp.headers = headers or {}
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def _make_urlopen_side_effect(*responses):
    """Return a side_effect callable that yields successive canned responses.

    Each element of ``responses`` is either a response mock (returned directly)
    or a callable (invoked with the request, returns a mock).
    Wraps each in a context manager.
    """
    it = iter(responses)

    def side_effect(req, timeout=30.0):
        resp = next(it)
        if callable(resp) and not isinstance(resp, MagicMock):
            resp = resp(req)
        return resp

    return side_effect


# ---------------------------------------------------------------------------
# csljson_to_zotero_item
# ---------------------------------------------------------------------------

class TestCsljsonToZoteroItem:
    SAMPLE = {
        "doi": "10.1000/xyz123",
        "title": "A Great Paper",
        "authors": [
            {"family": "Smith", "given": "John A"},
            {"family": "Jones", "given": "Mary"},
        ],
        "year": "2023",
        "journal": "Nature Neuroscience",
        "volume": "26",
        "issue": "4",
        "pages": "512-520",
        "type": "article-journal",
    }

    def test_basic_mapping(self):
        item = zotero.csljson_to_zotero_item(self.SAMPLE)
        assert item["itemType"] == "journalArticle"
        assert item["title"] == "A Great Paper"
        assert item["DOI"] == "10.1000/xyz123"
        assert item["publicationTitle"] == "Nature Neuroscience"
        assert item["date"] == "2023"
        assert item["volume"] == "26"
        assert item["issue"] == "4"
        assert item["pages"] == "512-520"

    def test_authors_mapped_to_creators(self):
        item = zotero.csljson_to_zotero_item(self.SAMPLE)
        creators = item["creators"]
        assert len(creators) == 2
        assert creators[0] == {"creatorType": "author", "firstName": "John A", "lastName": "Smith"}
        assert creators[1] == {"creatorType": "author", "firstName": "Mary", "lastName": "Jones"}

    def test_csl_type_mapping(self):
        for csl_type, expected in [
            ("article-journal", "journalArticle"),
            ("paper-conference", "conferencePaper"),
            ("book", "book"),
            ("chapter", "bookSection"),
        ]:
            meta = {**self.SAMPLE, "type": csl_type}
            item = zotero.csljson_to_zotero_item(meta)
            assert item["itemType"] == expected, f"Expected {expected} for {csl_type}"

    def test_unknown_csl_type_falls_back_to_default(self):
        meta = {**self.SAMPLE, "type": "weird-unknown-type"}
        item = zotero.csljson_to_zotero_item(meta, item_type="journalArticle")
        assert item["itemType"] == "journalArticle"

    def test_item_type_override_arg(self):
        meta = {**self.SAMPLE, "type": ""}  # no CSL type
        item = zotero.csljson_to_zotero_item(meta, item_type="conferencePaper")
        assert item["itemType"] == "conferencePaper"

    def test_collections_attached(self):
        item = zotero.csljson_to_zotero_item(self.SAMPLE, collections=["CKEY1", "CKEY2"])
        assert item["collections"] == ["CKEY1", "CKEY2"]

    def test_tags_attached(self):
        item = zotero.csljson_to_zotero_item(self.SAMPLE, tags=["grant", "r01"])
        assert item["tags"] == [{"tag": "grant"}, {"tag": "r01"}]

    def test_empty_collections_and_tags_default(self):
        item = zotero.csljson_to_zotero_item(self.SAMPLE)
        assert item["collections"] == []
        assert item["tags"] == []

    def test_missing_fields_handled_gracefully(self):
        item = zotero.csljson_to_zotero_item({"title": "Minimal"})
        assert item["title"] == "Minimal"
        assert item["creators"] == []
        assert item["DOI"] == ""
        assert item["date"] == ""


# ---------------------------------------------------------------------------
# key_can_write
# ---------------------------------------------------------------------------

class TestKeyCanWrite:
    def test_returns_true_when_groups_all_write(self, monkeypatch):
        monkeypatch.setenv("ZOTERO_API_KEY", "testkey")
        monkeypatch.setenv("ZOTERO_GROUP_ID", "2504198")

        payload = {"access": {"groups": {"all": {"write": True, "read": True}}}}
        mock_resp = _fake_response(payload)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            assert zotero.key_can_write() is True

    def test_returns_true_when_per_group_write(self, monkeypatch):
        monkeypatch.setenv("ZOTERO_API_KEY", "testkey")
        monkeypatch.setenv("ZOTERO_GROUP_ID", "2504198")

        payload = {"access": {"groups": {"2504198": {"write": True}}}}
        mock_resp = _fake_response(payload)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            assert zotero.key_can_write() is True

    def test_returns_false_when_write_absent(self, monkeypatch):
        monkeypatch.setenv("ZOTERO_API_KEY", "testkey")
        monkeypatch.setenv("ZOTERO_GROUP_ID", "2504198")

        payload = {"access": {"groups": {"all": {"write": False, "read": True}}}}
        mock_resp = _fake_response(payload)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            assert zotero.key_can_write() is False

    def test_returns_false_on_network_error(self, monkeypatch):
        monkeypatch.setenv("ZOTERO_API_KEY", "testkey")
        monkeypatch.setenv("ZOTERO_GROUP_ID", "2504198")

        with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
            assert zotero.key_can_write() is False

    def test_returns_false_when_no_config(self, monkeypatch):
        monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
        monkeypatch.delenv("ZOTERO_GROUP_ID", raising=False)
        monkeypatch.delenv("ZOTERO_USER_ID", raising=False)
        assert zotero.key_can_write() is False

    def test_api_key_not_in_library_urls(self, monkeypatch):
        """The API key must not appear in library item/collection request URLs.

        Note: ``key_can_write`` calls ``GET /keys/<key>`` (the key IS the resource
        path — unavoidable by Zotero API design). We verify that build_request and
        _post_json, which construct library-scoped URLs, never embed the key there,
        and that key_can_write does not return or raise with the key value.
        """
        monkeypatch.setenv("ZOTERO_API_KEY", "SECRETKEY999")
        monkeypatch.setenv("ZOTERO_GROUP_ID", "2504198")

        # build_request produces library-scoped URLs — key must not appear
        req = zotero.build_request("items", {"q": "test"})
        assert "SECRETKEY999" not in req.full_url, (
            f"API key leaked into build_request URL: {req.full_url}"
        )
        # The key IS in the header
        assert req.get_header("Zotero-api-key") == "SECRETKEY999"

        # key_can_write return value must not contain the secret
        with patch("urllib.request.urlopen", return_value=_fake_response(
            {"access": {"groups": {"all": {"write": True}}}}
        )):
            result = zotero.key_can_write()
        assert result is True  # bool, not the key string


# ---------------------------------------------------------------------------
# ensure_collection
# ---------------------------------------------------------------------------

class TestEnsureCollection:
    def test_returns_existing_key(self, monkeypatch):
        monkeypatch.setenv("ZOTERO_API_KEY", "testkey")
        monkeypatch.setenv("ZOTERO_GROUP_ID", "2504198")

        coll_data = [
            {"key": "COLL001", "data": {"name": "My Grant"}},
            {"key": "COLL002", "data": {"name": "Other"}},
        ]
        headers = {"Total-Results": "2"}
        mock_resp = _fake_response(coll_data, headers=headers)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            key = zotero.ensure_collection("My Grant")

        assert key == "COLL001"

    def test_case_insensitive_match(self, monkeypatch):
        monkeypatch.setenv("ZOTERO_API_KEY", "testkey")
        monkeypatch.setenv("ZOTERO_GROUP_ID", "2504198")

        coll_data = [{"key": "COLL003", "data": {"name": "tsc r01"}}]
        headers = {"Total-Results": "1"}
        mock_resp = _fake_response(coll_data, headers=headers)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            key = zotero.ensure_collection("TSC R01")

        assert key == "COLL003"

    def test_creates_when_absent(self, monkeypatch):
        monkeypatch.setenv("ZOTERO_API_KEY", "testkey")
        monkeypatch.setenv("ZOTERO_GROUP_ID", "2504198")

        # GET collections → empty list
        get_resp = _fake_response([], headers={"Total-Results": "0"})
        # POST create → successful
        post_resp = _fake_response({"successful": {"0": {"key": "NEWCOLL"}}, "failed": {}})

        responses = iter([get_resp, post_resp])

        def fake_urlopen(req, timeout=30.0):
            return next(responses)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            key = zotero.ensure_collection("Brand New Collection")

        assert key == "NEWCOLL"


# ---------------------------------------------------------------------------
# create_items — dedup, write token, collection, tags
# ---------------------------------------------------------------------------

META_NEW = {
    "doi": "10.1016/new.2024",
    "title": "Brand New Article",
    "authors": [{"family": "Cohen", "given": "Alex"}],
    "year": "2024",
    "journal": "Epilepsia",
    "volume": "65",
    "issue": "3",
    "pages": "100-110",
    "type": "article-journal",
}

META_EXISTING = {
    "doi": "10.1016/exist.2020",
    "title": "Already In Library",
    "authors": [{"family": "Smith", "given": "Jane"}],
    "year": "2020",
    "journal": "Brain",
    "volume": "143",
    "issue": "1",
    "pages": "1-10",
    "type": "article-journal",
}


def _build_create_side_effect(
    *,
    has_write: bool = True,
    existing_doi: str | None = None,
    collection_key: str | None = None,
    collection_name: str | None = None,
    created_keys: dict[str, str] | None = None,  # doi → new key
):
    """Build a ``urlopen`` side_effect that handles the full create_items flow.

    Handles in order:
      1. GET /keys/<key>  (key_can_write)
      2. [optional] GET library items search for existing DOI
      3. [optional] GET library all items (fallback DOI scan)
      4. [optional] GET collections (ensure_collection GET)
      5. [optional] POST collections (ensure_collection create)
      6. GET library items search (new DOI check)
      7. GET library all items (new DOI fallback)
      8. POST /items (create)
    """
    call_log: list[dict] = []

    def side_effect(req, timeout=30.0):
        method = getattr(req, "method", "GET")
        url = req.full_url
        call_log.append({"method": method, "url": url, "req": req})

        # key_can_write → GET /keys/...
        if "/keys/" in url:
            if has_write:
                return _fake_response({
                    "access": {"groups": {"all": {"write": True, "read": True}}}
                })
            else:
                return _fake_response({
                    "access": {"groups": {"all": {"write": False, "read": True}}}
                })

        # GET collections
        if "/collections" in url and method == "GET":
            if collection_key and collection_name:
                data = [{"key": collection_key, "data": {"name": collection_name}}]
                return _fake_response(data, headers={"Total-Results": "1"})
            return _fake_response([], headers={"Total-Results": "0"})

        # POST collections (create)
        if "/collections" in url and method == "POST":
            return _fake_response({"successful": {"0": {"key": collection_key or "AUTOCOLL"}}, "failed": {}})

        # GET items (search or fetch_all for dedup)
        if "/items" in url and method == "GET":
            decoded_url = urllib.parse.unquote(url)
            # If we're looking for the existing DOI, return a hit
            if existing_doi and existing_doi.lower() in decoded_url.lower():
                return _fake_response(
                    [{"key": "EXISTKEY", "data": {"DOI": existing_doi, "title": "Already In Library"}}],
                    headers={"Total-Results": "1"},
                )
            # fetch_all (no q param) — return empty for new items
            if "q=" not in url:
                return _fake_response([], headers={"Total-Results": "0"})
            # search for new DOI — not found
            return _fake_response([], headers={"Total-Results": "0"})

        # POST items (create)
        if "/items" in url and method == "POST":
            body = json.loads(req.data.decode("utf-8"))
            successful = {}
            for i, itm in enumerate(body):
                doi = itm.get("DOI", "")
                new_key = (created_keys or {}).get(doi, f"NEWKEY{i}")
                successful[str(i)] = {"key": new_key, "data": itm}
            return _fake_response({"successful": successful, "unchanged": {}, "failed": {}})

        # Fallback — empty items list
        return _fake_response([], headers={"Total-Results": "0"})

    return side_effect, call_log


class TestCreateItems:
    def test_skips_existing_doi(self, monkeypatch):
        monkeypatch.setenv("ZOTERO_API_KEY", "testkey")
        monkeypatch.setenv("ZOTERO_GROUP_ID", "2504198")

        side_effect, _ = _build_create_side_effect(
            existing_doi="10.1016/exist.2020",
        )

        with patch("urllib.request.urlopen", side_effect=side_effect):
            result = zotero.create_items([META_EXISTING], dedup=True)

        assert result["created"] == []
        assert len(result["skipped_existing"]) == 1
        assert result["skipped_existing"][0]["existing_key"] == "EXISTKEY"
        assert result["failed"] == []

    def test_creates_new_item(self, monkeypatch):
        monkeypatch.setenv("ZOTERO_API_KEY", "testkey")
        monkeypatch.setenv("ZOTERO_GROUP_ID", "2504198")

        side_effect, call_log = _build_create_side_effect(
            created_keys={"10.1016/new.2024": "BRANDNEW"},
        )

        with patch("urllib.request.urlopen", side_effect=side_effect):
            result = zotero.create_items([META_NEW], dedup=True)

        assert result["failed"] == []
        assert result["skipped_existing"] == []
        assert len(result["created"]) == 1
        assert result["created"][0]["key"] == "BRANDNEW"
        assert result["created"][0]["doi"] == "10.1016/new.2024"

        # Verify a POST to /items was made
        posts = [c for c in call_log if c["method"] == "POST" and "/items" in c["url"]]
        assert posts, "Expected at least one POST to /items"

    def test_collection_ensured_and_attached(self, monkeypatch):
        monkeypatch.setenv("ZOTERO_API_KEY", "testkey")
        monkeypatch.setenv("ZOTERO_GROUP_ID", "2504198")

        side_effect, call_log = _build_create_side_effect(
            collection_key="COLL_TSC",
            collection_name="TSC R01",
            created_keys={"10.1016/new.2024": "NEWKEY"},
        )

        with patch("urllib.request.urlopen", side_effect=side_effect):
            result = zotero.create_items([META_NEW], collection="TSC R01", dedup=True)

        assert result["created"][0]["key"] == "NEWKEY"

        # The POST body must contain the collection key
        posts = [c for c in call_log if c["method"] == "POST" and "/items" in c["url"]]
        assert posts
        body = json.loads(posts[0]["req"].data.decode("utf-8"))
        assert "COLL_TSC" in body[0]["collections"]

    def test_tag_attached_to_created_item(self, monkeypatch):
        monkeypatch.setenv("ZOTERO_API_KEY", "testkey")
        monkeypatch.setenv("ZOTERO_GROUP_ID", "2504198")

        side_effect, call_log = _build_create_side_effect(
            created_keys={"10.1016/new.2024": "TAGGEDKEY"},
        )

        with patch("urllib.request.urlopen", side_effect=side_effect):
            result = zotero.create_items([META_NEW], tags=["r01", "tsc"], dedup=True)

        assert result["created"][0]["key"] == "TAGGEDKEY"

        posts = [c for c in call_log if c["method"] == "POST" and "/items" in c["url"]]
        body = json.loads(posts[0]["req"].data.decode("utf-8"))
        tag_strings = [t["tag"] for t in body[0]["tags"]]
        assert "r01" in tag_strings
        assert "tsc" in tag_strings

    def test_write_token_present_and_unique_across_batches(self, monkeypatch):
        """Each POST /items must carry a Zotero-Write-Token, unique per request."""
        monkeypatch.setenv("ZOTERO_API_KEY", "testkey")
        monkeypatch.setenv("ZOTERO_GROUP_ID", "2504198")

        write_tokens: list[str] = []

        def capture_side_effect(req, timeout=30.0):
            method = getattr(req, "method", "GET")
            url = req.full_url

            if "/keys/" in url:
                return _fake_response({"access": {"groups": {"all": {"write": True}}}})
            if "/collections" in url and method == "GET":
                return _fake_response([], headers={"Total-Results": "0"})
            if "/items" in url and method == "GET":
                return _fake_response([], headers={"Total-Results": "0"})
            if "/items" in url and method == "POST":
                token = req.get_header("Zotero-write-token")
                if token:
                    write_tokens.append(token)
                body = json.loads(req.data.decode("utf-8"))
                successful = {str(i): {"key": f"K{i}", "data": b} for i, b in enumerate(body)}
                return _fake_response({"successful": successful, "unchanged": {}, "failed": {}})
            return _fake_response([], headers={"Total-Results": "0"})

        # Two items with distinct DOIs — each create_items call issues its own POST
        meta1 = {**META_NEW, "doi": "10.1/aaa", "title": "Item A"}
        meta2 = {**META_NEW, "doi": "10.1/bbb", "title": "Item B"}

        with patch("urllib.request.urlopen", side_effect=capture_side_effect):
            result1 = zotero.create_items([meta1], dedup=False)
            result2 = zotero.create_items([meta2], dedup=False)

        # Each create_items call issues a unique Zotero-Write-Token per POST
        assert len(write_tokens) == 2, f"Expected 2 write tokens, got: {write_tokens}"
        assert write_tokens[0] != write_tokens[1], "Write tokens must be unique per request"

    def test_readonly_key_returns_failed_no_post(self, monkeypatch):
        monkeypatch.setenv("ZOTERO_API_KEY", "testkey")
        monkeypatch.setenv("ZOTERO_GROUP_ID", "2504198")

        post_called = []

        def side_effect(req, timeout=30.0):
            method = getattr(req, "method", "GET")
            url = req.full_url
            if "/keys/" in url:
                return _fake_response({"access": {"groups": {"all": {"write": False, "read": True}}}})
            if method == "POST":
                post_called.append(url)
            return _fake_response([], headers={"Total-Results": "0"})

        with patch("urllib.request.urlopen", side_effect=side_effect):
            result = zotero.create_items([META_NEW], dedup=True)

        assert post_called == [], "POST must NOT be called when key lacks write access"
        assert result["created"] == []
        assert result["skipped_existing"] == []
        assert len(result["failed"]) == 1
        assert "write access" in result["failed"][0]["reason"].lower()

    def test_dedup_false_skips_existence_check(self, monkeypatch):
        monkeypatch.setenv("ZOTERO_API_KEY", "testkey")
        monkeypatch.setenv("ZOTERO_GROUP_ID", "2504198")

        search_calls = []

        def side_effect(req, timeout=30.0):
            method = getattr(req, "method", "GET")
            url = req.full_url
            if "/keys/" in url:
                return _fake_response({"access": {"groups": {"all": {"write": True}}}})
            if "/items" in url and method == "GET":
                search_calls.append(url)
                return _fake_response([], headers={"Total-Results": "0"})
            if "/items" in url and method == "POST":
                body = json.loads(req.data.decode("utf-8"))
                successful = {str(i): {"key": f"K{i}", "data": b} for i, b in enumerate(body)}
                return _fake_response({"successful": successful, "unchanged": {}, "failed": {}})
            return _fake_response([], headers={"Total-Results": "0"})

        with patch("urllib.request.urlopen", side_effect=side_effect):
            result = zotero.create_items([META_EXISTING], dedup=False)

        # No search calls when dedup=False
        assert search_calls == []
        assert len(result["created"]) == 1
        assert result["skipped_existing"] == []

    def test_api_key_never_in_result_or_exception(self, monkeypatch):
        """API key must not appear in any returned dict value or exception string."""
        secret = "SUPERSECRETAPIKEY123"
        monkeypatch.setenv("ZOTERO_API_KEY", secret)
        monkeypatch.setenv("ZOTERO_GROUP_ID", "2504198")

        def side_effect(req, timeout=30.0):
            method = getattr(req, "method", "GET")
            url = req.full_url
            if "/keys/" in url:
                return _fake_response({"access": {"groups": {"all": {"write": True}}}})
            if "/items" in url and method == "GET":
                return _fake_response([], headers={"Total-Results": "0"})
            if "/items" in url and method == "POST":
                successful = {"0": {"key": "NKEY", "data": {}}}
                return _fake_response({"successful": successful, "unchanged": {}, "failed": {}})
            return _fake_response([], headers={"Total-Results": "0"})

        with patch("urllib.request.urlopen", side_effect=side_effect):
            result = zotero.create_items([META_NEW], dedup=False)

        # Recursively check no value in result contains the secret
        def _check_no_secret(obj):
            if isinstance(obj, str):
                assert secret not in obj, f"API key leaked into result string: {obj!r}"
            elif isinstance(obj, dict):
                for v in obj.values():
                    _check_no_secret(v)
            elif isinstance(obj, list):
                for v in obj:
                    _check_no_secret(v)

        _check_no_secret(result)

    def test_empty_input_returns_empty_result(self, monkeypatch):
        monkeypatch.setenv("ZOTERO_API_KEY", "testkey")
        monkeypatch.setenv("ZOTERO_GROUP_ID", "2504198")

        with patch("urllib.request.urlopen") as mock_urlopen:
            result = zotero.create_items([], dedup=True)

        mock_urlopen.assert_not_called()
        assert result == {"created": [], "skipped_existing": [], "failed": [], "skipped_degraded_read": []}

    def test_failed_item_recorded_not_raised(self, monkeypatch):
        """A POST failure for one item should be recorded, not raised."""
        monkeypatch.setenv("ZOTERO_API_KEY", "testkey")
        monkeypatch.setenv("ZOTERO_GROUP_ID", "2504198")

        def side_effect(req, timeout=30.0):
            method = getattr(req, "method", "GET")
            url = req.full_url
            if "/keys/" in url:
                return _fake_response({"access": {"groups": {"all": {"write": True}}}})
            if "/items" in url and method == "GET":
                return _fake_response([], headers={"Total-Results": "0"})
            if "/items" in url and method == "POST":
                failed = {"0": {"code": 400, "message": "Invalid item type"}}
                return _fake_response({"successful": {}, "unchanged": {}, "failed": failed})
            return _fake_response([], headers={"Total-Results": "0"})

        with patch("urllib.request.urlopen", side_effect=side_effect):
            result = zotero.create_items([META_NEW], dedup=False)

        assert result["created"] == []
        assert len(result["failed"]) == 1
        assert "Invalid item type" in result["failed"][0]["reason"]


# ---------------------------------------------------------------------------
# F3: doi_index threading (no per-item fetch_all scan) + dedup-error isolation
# ---------------------------------------------------------------------------

class TestCreateItemsDoiIndex:
    def test_supplied_index_skips_per_item_fetch_all(self, monkeypatch):
        """When a doi_index is supplied, create_items must dedup against it in
        O(1)/item — NEVER the full-library fetch_all scan per DOI (F3 O(N·M))."""
        monkeypatch.setenv("ZOTERO_API_KEY", "testkey")
        monkeypatch.setenv("ZOTERO_GROUP_ID", "2504198")

        # Index says META_EXISTING is present (key EXISTKEY); META_NEW is absent.
        index = {"10.1016/exist.2020": "EXISTKEY"}

        # fetch_all is the expensive full-library scan get_item_by_doi falls back
        # to without an index — it must NOT be called when the index answers.
        fetch_all_calls = {"n": 0}

        def boom_fetch_all(*a, **k):
            fetch_all_calls["n"] += 1
            raise AssertionError("fetch_all must not run when a doi_index is supplied")

        monkeypatch.setattr(zotero, "fetch_all", boom_fetch_all)
        # The title fallback only runs for an index MISS; record it so we can
        # confirm a hit short-circuits before any title search.
        title_calls = []
        monkeypatch.setattr(
            zotero, "_title_exists_in_library",
            lambda title: title_calls.append(title) or None,
        )

        posts = []

        def side_effect(req, timeout=30.0):
            method = getattr(req, "method", "GET")
            url = req.full_url
            if "/keys/" in url:
                return _fake_response({"access": {"groups": {"all": {"write": True}}}})
            if "/items" in url and method == "POST":
                body = json.loads(req.data.decode("utf-8"))
                posts.append(body)
                successful = {str(i): {"key": f"NEW{i}", "data": b} for i, b in enumerate(body)}
                return _fake_response({"successful": successful, "unchanged": {}, "failed": {}})
            return _fake_response([], headers={"Total-Results": "0"})

        with patch("urllib.request.urlopen", side_effect=side_effect):
            result = zotero.create_items(
                [META_EXISTING, META_NEW], dedup=True, doi_index=index,
            )

        assert fetch_all_calls["n"] == 0, "fetch_all must never run with a doi_index"
        # EXISTING was found in the index → skipped via the index (no title search for it).
        assert title_calls == ["Brand New Article"], (
            "only the index-MISS item should reach the title fallback"
        )
        assert len(result["skipped_existing"]) == 1
        assert result["skipped_existing"][0]["existing_key"] == "EXISTKEY"
        # Only the genuinely-new item was POSTed.
        assert len(result["created"]) == 1
        assert result["created"][0]["title"] == "Brand New Article"
        assert len(posts) == 1 and len(posts[0]) == 1

    def test_dedup_error_degrades_one_item_batch_continues(self, monkeypatch):
        """A transient read error during ONE item's dedup degrades THAT item to
        'failed' — the batch continues and items already written aren't dropped
        or duplicated (F3: dedup lookups guarded by the per-item try)."""
        monkeypatch.setenv("ZOTERO_API_KEY", "testkey")
        monkeypatch.setenv("ZOTERO_GROUP_ID", "2504198")

        meta_ok = {**META_NEW, "doi": "10.1/ok", "title": "Good Item"}
        meta_boom = {**META_NEW, "doi": "10.1/boom", "title": "Transient Failure Item"}
        meta_ok2 = {**META_NEW, "doi": "10.1/ok2", "title": "Another Good Item"}

        # Empty index → every DOI is an index MISS, so each item reaches the
        # title fallback. Make the title search raise for ONE item only.
        def flaky_title(title):
            if title == "Transient Failure Item":
                raise urllib.error.HTTPError(
                    "https://api.zotero.org/groups/2504198/items", 503,
                    "Service Unavailable", {}, None,
                )
            return None

        monkeypatch.setattr(zotero, "_title_exists_in_library", flaky_title)
        monkeypatch.setattr(
            zotero, "fetch_all",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("no fetch_all")),
        )

        posted_titles = []

        def side_effect(req, timeout=30.0):
            method = getattr(req, "method", "GET")
            url = req.full_url
            if "/keys/" in url:
                return _fake_response({"access": {"groups": {"all": {"write": True}}}})
            if "/items" in url and method == "POST":
                body = json.loads(req.data.decode("utf-8"))
                for b in body:
                    posted_titles.append(b.get("title"))
                successful = {str(i): {"key": f"NEW{i}", "data": b} for i, b in enumerate(body)}
                return _fake_response({"successful": successful, "unchanged": {}, "failed": {}})
            return _fake_response([], headers={"Total-Results": "0"})

        with patch("urllib.request.urlopen", side_effect=side_effect):
            result = zotero.create_items(
                [meta_ok, meta_boom, meta_ok2], dedup=True, doi_index={},
            )

        # The flaky item is recorded as failed, NOT raised.
        failed_titles = {f["title"] for f in result["failed"]}
        assert "Transient Failure Item" in failed_titles
        # The other two were created — the batch continued past the failure.
        created_titles = {c["title"] for c in result["created"]}
        assert created_titles == {"Good Item", "Another Good Item"}
        # The failed item was NEVER posted (no blind create), and nothing duplicated.
        assert "Transient Failure Item" not in posted_titles
        assert sorted(posted_titles) == ["Another Good Item", "Good Item"]

    def test_none_metadata_skipped_not_crashed(self, monkeypatch):
        """Bug 2: a None entry in ``metas`` must be skipped (not crash on
        ``meta.get``); valid metas are still processed. A None metadata can't
        become an item — skip it, don't AttributeError."""
        monkeypatch.setenv("ZOTERO_API_KEY", "testkey")
        monkeypatch.setenv("ZOTERO_GROUP_ID", "2504198")

        # No network for dedup: empty index + stubbed title search (None = absent).
        monkeypatch.setattr(zotero, "_title_exists_in_library", lambda title: None)
        monkeypatch.setattr(
            zotero, "fetch_all",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("no fetch_all")),
        )

        posted_titles = []

        def side_effect(req, timeout=30.0):
            method = getattr(req, "method", "GET")
            url = req.full_url
            if "/keys/" in url:
                return _fake_response({"access": {"groups": {"all": {"write": True}}}})
            if "/items" in url and method == "POST":
                body = json.loads(req.data.decode("utf-8"))
                for b in body:
                    posted_titles.append(b.get("title"))
                successful = {str(i): {"key": f"NEW{i}", "data": b} for i, b in enumerate(body)}
                return _fake_response({"successful": successful, "unchanged": {}, "failed": {}})
            return _fake_response([], headers={"Total-Results": "0"})

        with patch("urllib.request.urlopen", side_effect=side_effect):
            # None entry is interleaved with a valid one — must NOT crash.
            result = zotero.create_items([None, META_NEW], dedup=True, doi_index={})

        # The None was silently dropped; the valid meta was created.
        assert len(result["created"]) == 1
        assert result["created"][0]["title"] == "Brand New Article"
        assert result["failed"] == []
        assert posted_titles == ["Brand New Article"]

    def test_all_none_metadata_returns_empty_no_write(self, monkeypatch):
        """A metas list of only None entries returns the empty result WITHOUT a
        write-permission probe or any POST (degenerate after the None-skip)."""
        monkeypatch.setenv("ZOTERO_API_KEY", "testkey")
        monkeypatch.setenv("ZOTERO_GROUP_ID", "2504198")

        def boom(req, timeout=30.0):
            raise AssertionError("no network for an all-None metas list")

        with patch("urllib.request.urlopen", side_effect=boom):
            result = zotero.create_items([None, None], dedup=True)

        assert result == {"created": [], "skipped_existing": [], "failed": [], "skipped_degraded_read": []}

    def test_pmid_index_dedups_present_by_pmid(self, monkeypatch):
        """Bug 2 defense-in-depth: a meta carrying a PMID present in ``pmid_index``
        is recorded ``skipped_existing`` (never created) even when its DOI/title
        do NOT match — catches a ref upstream false-flagged 'missing'."""
        monkeypatch.setenv("ZOTERO_API_KEY", "testkey")
        monkeypatch.setenv("ZOTERO_GROUP_ID", "2504198")

        # The PMID-present meta has NO doi and a title the library does not know,
        # so ONLY the PMID path can catch it.
        meta_pmid_present = {
            "doi": "",
            "pmid": "31234567",
            "title": "A Paper Present Only By PMID",
            "authors": [{"family": "Pino", "given": "M"}],
            "year": "2018",
            "journal": "J",
            "type": "article-journal",
        }

        # Title search must say "absent" (proves the PMID path, not title, deduped).
        title_calls = []
        monkeypatch.setattr(
            zotero, "_title_exists_in_library",
            lambda title: title_calls.append(title) or None,
        )
        monkeypatch.setattr(
            zotero, "fetch_all",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("no fetch_all")),
        )

        posts = []

        def side_effect(req, timeout=30.0):
            method = getattr(req, "method", "GET")
            url = req.full_url
            if "/keys/" in url:
                return _fake_response({"access": {"groups": {"all": {"write": True}}}})
            if "/items" in url and method == "POST":
                body = json.loads(req.data.decode("utf-8"))
                posts.append(body)
                successful = {str(i): {"key": f"NEW{i}", "data": b} for i, b in enumerate(body)}
                return _fake_response({"successful": successful, "unchanged": {}, "failed": {}})
            return _fake_response([], headers={"Total-Results": "0"})

        with patch("urllib.request.urlopen", side_effect=side_effect):
            result = zotero.create_items(
                [meta_pmid_present],
                dedup=True,
                doi_index={},
                pmid_index={"31234567": "PMIDKEY"},
            )

        # Recorded as existing via the PMID index — NOT created, NEVER posted.
        assert result["created"] == []
        assert len(result["skipped_existing"]) == 1
        assert result["skipped_existing"][0]["existing_key"] == "PMIDKEY"
        assert posts == [], "a present-by-PMID item must never be POSTed (no duplicate)"

    def test_genuinely_new_meta_created_despite_pmid_index(self, monkeypatch):
        """Control: a meta whose PMID is NOT in ``pmid_index`` (and absent by DOI
        and title) is still created — the PMID dedup doesn't over-block."""
        monkeypatch.setenv("ZOTERO_API_KEY", "testkey")
        monkeypatch.setenv("ZOTERO_GROUP_ID", "2504198")

        meta_new_pmid = {**META_NEW, "pmid": "99999999"}

        monkeypatch.setattr(zotero, "_title_exists_in_library", lambda title: None)
        monkeypatch.setattr(
            zotero, "fetch_all",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("no fetch_all")),
        )

        posted = []

        def side_effect(req, timeout=30.0):
            method = getattr(req, "method", "GET")
            url = req.full_url
            if "/keys/" in url:
                return _fake_response({"access": {"groups": {"all": {"write": True}}}})
            if "/items" in url and method == "POST":
                body = json.loads(req.data.decode("utf-8"))
                posted.extend(b.get("title") for b in body)
                successful = {str(i): {"key": f"NEW{i}", "data": b} for i, b in enumerate(body)}
                return _fake_response({"successful": successful, "unchanged": {}, "failed": {}})
            return _fake_response([], headers={"Total-Results": "0"})

        with patch("urllib.request.urlopen", side_effect=side_effect):
            result = zotero.create_items(
                [meta_new_pmid],
                dedup=True,
                doi_index={},
                pmid_index={"31234567": "OTHERKEY"},  # different PMID
            )

        assert len(result["created"]) == 1
        assert result["created"][0]["title"] == "Brand New Article"
        assert posted == ["Brand New Article"]


# ---------------------------------------------------------------------------
# F8 — transient-failure retry in the zotero GET/POST primitives.
#
# Zotero documents Backoff / Retry-After; a 429 or transient 5xx mid-pagination
# must NOT raise an unhandled traceback. The retry logic is local to zotero (it
# carries the API key + write token in its own request path, kept separate from
# _http) with an injectable sleep so tests never really sleep.
# ---------------------------------------------------------------------------

def _zotero_http_error(code: int, retry_after=None, backoff=None):
    """Build an HTTPError carrying Retry-After / Backoff headers (like Zotero)."""
    import email.message
    hdrs = email.message.Message()
    if retry_after is not None:
        hdrs["Retry-After"] = str(retry_after)
    if backoff is not None:
        hdrs["Backoff"] = str(backoff)
    return urllib.error.HTTPError(
        url="https://api.zotero.org/x", code=code, msg="err", hdrs=hdrs, fp=None
    )


class TestZoteroRetry:
    """The GET/POST primitives retry ONCE on a transient 429/5xx, never sleep
    for real in tests, and a 429 mid-fetch_all does not raise."""

    def test_get_json_headers_retries_once_on_429_then_succeeds(self, monkeypatch):
        # First attempt 429 (with Retry-After), second attempt succeeds.
        slept = []
        monkeypatch.setattr(zotero, "_retry_sleep", lambda s: slept.append(s))
        monkeypatch.setenv("ZOTERO_API_KEY", "k")
        monkeypatch.setenv("ZOTERO_GROUP_ID", "2504198")

        seq = iter([
            _zotero_http_error(429, retry_after=2),
            _fake_response([{"key": "A"}], headers={"Total-Results": "1"}),
        ])

        def fake(req, timeout=30.0):
            nxt = next(seq)
            if isinstance(nxt, BaseException):
                raise nxt
            return nxt

        with patch("urllib.request.urlopen", side_effect=fake):
            data, headers = zotero._get_json_headers(zotero.build_request("items/top"))
        assert data == [{"key": "A"}]
        assert headers.get("Total-Results") == "1"
        # backed off once, honoring Retry-After (clamped), and never slept for real.
        assert slept == [2.0]

    def test_fetch_all_survives_transient_429_midpagination(self, monkeypatch):
        # Page 1 OK (2 of 3 items), page 2 hits a 429 once then succeeds with the
        # final item. The 429 must NOT raise — fetch_all returns all 3 items.
        slept = []
        monkeypatch.setattr(zotero, "_retry_sleep", lambda s: slept.append(s))
        monkeypatch.setenv("ZOTERO_API_KEY", "k")
        monkeypatch.setenv("ZOTERO_GROUP_ID", "2504198")

        page1 = _fake_response(
            [{"key": "A"}, {"key": "B"}], headers={"Total-Results": "3"})
        err = _zotero_http_error(503, retry_after=1)
        page2 = _fake_response([{"key": "C"}], headers={"Total-Results": "3"})
        seq = iter([page1, err, page2])

        def fake(req, timeout=30.0):
            nxt = next(seq)
            if isinstance(nxt, BaseException):
                raise nxt
            return nxt

        with patch("urllib.request.urlopen", side_effect=fake):
            items = zotero.fetch_all()
        assert [it["key"] for it in items] == ["A", "B", "C"]
        assert slept == [1.0]

    def test_persistent_429_eventually_raises_not_infinite(self, monkeypatch):
        # Bounded: a persistent 429 raises after the single retry (caller's own
        # try/except — e.g. library_doi_index — turns this into a fail-closed).
        monkeypatch.setattr(zotero, "_retry_sleep", lambda s: None)
        monkeypatch.setenv("ZOTERO_API_KEY", "k")
        monkeypatch.setenv("ZOTERO_GROUP_ID", "2504198")
        with patch("urllib.request.urlopen",
                   side_effect=lambda req, timeout=30.0: (_ for _ in ()).throw(
                       _zotero_http_error(429, retry_after=0))):
            with pytest.raises(urllib.error.HTTPError):
                zotero._get_json(zotero.build_request("items/X"))

    def test_404_propagates_unchanged_for_get_item(self, monkeypatch):
        # A 404 is NOT a retry status — it must pass straight through so get_item
        # can catch it and return None (deleted item), not be swallowed/retried.
        calls = {"n": 0}
        monkeypatch.setattr(zotero, "_retry_sleep", lambda s: None)
        monkeypatch.setenv("ZOTERO_API_KEY", "k")
        monkeypatch.setenv("ZOTERO_GROUP_ID", "2504198")

        def fake(req, timeout=30.0):
            calls["n"] += 1
            raise _zotero_http_error(404)

        with patch("urllib.request.urlopen", side_effect=fake):
            assert zotero.get_item("GONEKEY") is None
        assert calls["n"] == 1  # no retry on a 404

    def test_post_json_retries_once_on_5xx(self, monkeypatch):
        slept = []
        monkeypatch.setattr(zotero, "_retry_sleep", lambda s: slept.append(s))
        monkeypatch.setenv("ZOTERO_API_KEY", "k")
        monkeypatch.setenv("ZOTERO_GROUP_ID", "2504198")
        seq = iter([
            _zotero_http_error(502, backoff=1),
            _fake_response({"successful": {"0": {"key": "NEW"}}, "failed": {}}),
        ])

        def fake(req, timeout=30.0):
            nxt = next(seq)
            if isinstance(nxt, BaseException):
                raise nxt
            return nxt

        with patch("urllib.request.urlopen", side_effect=fake):
            resp = zotero._post_json("items", [{"itemType": "journalArticle"}])
        assert resp["successful"]["0"]["key"] == "NEW"
        assert slept == [1.0]  # honored the Backoff header


# ---------------------------------------------------------------------------
# F6 — tri-state key_can_write_status: True / False / WRITE_ACCESS_UNKNOWN.
#
# A transient failure must yield UNKNOWN (not a misleading definitive "no write
# access"). UNKNOWN is falsy so the boolean gate still fails closed, but callers
# can detect it to say "retry — could not verify" rather than asserting no-write.
# ---------------------------------------------------------------------------

class TestKeyCanWriteStatus:
    def test_definitive_yes(self, monkeypatch):
        monkeypatch.setenv("ZOTERO_API_KEY", "k")
        monkeypatch.setenv("ZOTERO_GROUP_ID", "2504198")
        resp = _fake_response({"access": {"groups": {"all": {"write": True}}}})
        with patch("urllib.request.urlopen", return_value=resp):
            assert zotero.key_can_write_status() is True
            assert zotero.key_can_write() is True

    def test_definitive_no(self, monkeypatch):
        monkeypatch.setenv("ZOTERO_API_KEY", "k")
        monkeypatch.setenv("ZOTERO_GROUP_ID", "2504198")
        resp = _fake_response({"access": {"groups": {"all": {"write": False}}}})
        with patch("urllib.request.urlopen", return_value=resp):
            assert zotero.key_can_write_status() is False
            assert zotero.key_can_write() is False

    def test_network_error_is_unknown_not_false(self, monkeypatch):
        monkeypatch.setattr(zotero, "_retry_sleep", lambda s: None)
        monkeypatch.setenv("ZOTERO_API_KEY", "k")
        monkeypatch.setenv("ZOTERO_GROUP_ID", "2504198")
        with patch("urllib.request.urlopen",
                   side_effect=OSError("connection refused")):
            status = zotero.key_can_write_status()
        assert status is zotero.WRITE_ACCESS_UNKNOWN
        assert not status                      # falsy → fail-closed at any gate
        # back-compat boolean wrapper still returns False on the unknown.
        with patch("urllib.request.urlopen",
                   side_effect=OSError("connection refused")):
            assert zotero.key_can_write() is False

    def test_persistent_5xx_is_unknown(self, monkeypatch):
        # A transient status that never recovers → could-not-verify → UNKNOWN
        # (not a wrong "no write access").
        monkeypatch.setattr(zotero, "_retry_sleep", lambda s: None)
        monkeypatch.setenv("ZOTERO_API_KEY", "k")
        monkeypatch.setenv("ZOTERO_GROUP_ID", "2504198")
        with patch("urllib.request.urlopen",
                   side_effect=lambda req, timeout=15.0: (_ for _ in ()).throw(
                       _zotero_http_error(503, retry_after=0))):
            assert zotero.key_can_write_status() is zotero.WRITE_ACCESS_UNKNOWN

    def test_no_config_is_definitive_false_not_unknown(self, monkeypatch):
        monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
        monkeypatch.delenv("ZOTERO_GROUP_ID", raising=False)
        monkeypatch.delenv("ZOTERO_USER_ID", raising=False)
        # Nothing to write WITH is a definitive no, not "could not verify".
        assert zotero.key_can_write_status() is False

    def test_create_items_unknown_records_truthful_retry_reason(self, monkeypatch):
        # When write-access cannot be verified, create_items fails closed AND the
        # per-item reason tells the user to retry, not that the key lacks access.
        monkeypatch.setattr(zotero, "_retry_sleep", lambda s: None)
        monkeypatch.setenv("ZOTERO_API_KEY", "k")
        monkeypatch.setenv("ZOTERO_GROUP_ID", "2504198")
        with patch("urllib.request.urlopen",
                   side_effect=OSError("offline")):
            result = zotero.create_items(
                [{"title": "T", "doi": "10.1/x"}], dedup=True, doi_index={})
        assert result["created"] == []
        assert len(result["failed"]) == 1
        reason = result["failed"][0]["reason"].lower()
        assert "verify" in reason and "retry" in reason
        # must NOT assert a definitive "does not have write access".
        assert "does not have write access" not in reason


# ---------------------------------------------------------------------------
# _reorder_by_keys — CRIT-3 mis-binding guard
# ---------------------------------------------------------------------------

class TestReorderByKeys:
    """Binding each requested key to the returned item whose own key matches it,
    and refusing to bind a keyless item to a key it may not belong to."""

    def test_partial_unkeyed_response_does_not_misbind(self):
        # Request A,B,C; response has A (keyed) + one keyless item. B must NOT be
        # bound to the keyless item (it could be a child note/attachment) — it
        # fails safe to None instead of embedding the wrong work's metadata.
        import warnings
        items = [{"id": "2504198/A", "title": "Paper A"},
                 {"title": "MYSTERY note, no key"}]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            out = zotero._reorder_by_keys(["A", "B", "C"], items, what="test")
        assert out[0]["title"] == "Paper A"
        assert out[1] is None          # B: keyless item NOT bound here
        assert out[2] is None          # C: genuinely absent
        # the keyless item is never attributed to any requested key
        assert all(o is None or o.get("title") != "MYSTERY note, no key" for o in out)

    def test_fully_keyless_response_keeps_positional_legacy(self):
        # When NO key is recoverable at all (legacy/stub shape), response order is
        # the only signal and positional binding is preserved.
        items = [{"title": "first"}, {"title": "second"}]
        out = zotero._reorder_by_keys(["A", "B"], items, what="test")
        assert [o["title"] for o in out] == ["first", "second"]

    def test_keyed_response_reordered_to_requested_order(self):
        # Zotero serves in library order; we must reorder to the requested order.
        items = [{"id": "g/B"}, {"id": "g/A"}]      # library order B, A
        out = zotero._reorder_by_keys(["A", "B"], items, what="test")
        assert [o["id"] for o in out] == ["g/A", "g/B"]


class TestYearFromDate:
    """``_year_from_date`` must return a 4-digit year or ``None`` — never leak a
    non-numeric date string ("in press", "n.d.", "forthcoming") or a stray short
    run ("21") as the "year". A leaked string corrupts the rendered reference
    (e.g. ``in press;12(3):1-9``) and the author-year sort key. Sibling extractors
    ``cite._year_from`` / ``zoterolocal._year_from_date`` already do this."""

    def test_extracts_four_digit_year(self):
        assert zotero._year_from_date("2021-12") == "2021"
        assert zotero._year_from_date("2021") == "2021"
        assert zotero._year_from_date("May 2021") == "2021"
        assert zotero._year_from_date("2021-12-01 1") == "2021"

    def test_non_numeric_date_yields_none_not_the_string(self):
        for bad in ("in press", "n.d.", "forthcoming", "submitted"):
            assert zotero._year_from_date(bad) is None, (
                f"{bad!r} must not leak as the year"
            )

    def test_short_numeric_run_is_not_a_year(self):
        # Fewer than 4 digits is not a plausible year and must not leak.
        assert zotero._year_from_date("21") is None
        assert zotero._year_from_date("199") is None

    def test_empty_and_none_yield_none(self):
        assert zotero._year_from_date("") is None
        assert zotero._year_from_date(None) is None


# ---------------------------------------------------------------------------
# fetch_all — completeness / fail-CLOSED on a missing Total-Results header
# ---------------------------------------------------------------------------
# A missing (proxy-stripped) or truncated ``Total-Results`` must NOT be treated
# as "the whole library" — a partial index returned as complete makes the strict
# write path (library_index → create_items) see un-fetched items as absent and
# RE-CREATE them, mass-duplicating the shared group. fetch_all fails CLOSED:
# raising LibraryUnavailableError (the degraded-read signal) instead.
class TestFetchAllCompleteness:
    def _setenv(self, monkeypatch):
        monkeypatch.setenv("ZOTERO_API_KEY", "k")
        monkeypatch.setenv("ZOTERO_GROUP_ID", "2504198")
        monkeypatch.delenv("ZOTERO_USER_ID", raising=False)
        monkeypatch.delenv("ZOTERO_WORD_CITE_ZOTERO_FETCH_BUDGET", raising=False)

    def test_missing_total_results_header_fails_closed(self, monkeypatch):
        """TEETH: first page is a FULL page (100 items) with NO Total-Results
        header. Before the fix ``total`` defaulted to len(out) → loop broke →
        the first 100 items were returned AS the complete library (silent
        truncation of a e.g. 3000-item group). After: it raises
        LibraryUnavailableError so a strict write cannot proceed on a partial
        index.

        Mutation check: restoring ``total = headers.get('Total-Results', len(out))``
        + ``if start >= int(total): break`` makes this return 100 items (no raise)
        → RED."""
        self._setenv(monkeypatch)

        def fake_headers(req, timeout=15.0):
            # A brimming page with the completeness header STRIPPED.
            return ([{"key": f"K{i}", "data": {}} for i in range(100)], {})

        monkeypatch.setattr(zotero, "_get_json_headers", fake_headers)

        with pytest.raises(zotero.LibraryUnavailableError):
            zotero.fetch_all()

    def test_empty_page_without_header_fails_closed(self, monkeypatch):
        """An empty first page with NO Total-Results is a degraded read, not an
        empty library (an empty library reports ``Total-Results: 0``). Fail
        closed rather than returning [] as 'the whole (empty) library'."""
        self._setenv(monkeypatch)
        monkeypatch.setattr(
            zotero, "_get_json_headers",
            lambda req, timeout=15.0: ([], {}),
        )
        with pytest.raises(zotero.LibraryUnavailableError):
            zotero.fetch_all()

    def test_truncated_mid_pagination_fails_closed(self, monkeypatch):
        """Total-Results says 150 but page 2 comes back empty (proxy hiccup):
        we have only 100 of 150. Fail closed instead of returning a partial
        library as complete."""
        self._setenv(monkeypatch)
        pages = {"n": 0}
        seq = [
            ([{"key": f"K{i}", "data": {}} for i in range(100)], {"Total-Results": "150"}),
            ([], {"Total-Results": "150"}),  # short/empty page mid-pagination
        ]

        def fake_headers(req, timeout=15.0):
            i = pages["n"]; pages["n"] += 1
            return seq[i]

        monkeypatch.setattr(zotero, "_get_json_headers", fake_headers)
        with pytest.raises(zotero.LibraryUnavailableError):
            zotero.fetch_all()

    def test_complete_paginated_fetch_still_succeeds(self, monkeypatch):
        """The happy path is unchanged: a normal paginated fetch whose pages all
        carry a consistent Total-Results returns the whole library."""
        self._setenv(monkeypatch)
        pages = {"n": 0}
        seq = [
            ([{"key": f"K{i}", "data": {}} for i in range(100)], {"Total-Results": "150"}),
            ([{"key": f"K{i}", "data": {}} for i in range(100, 150)], {"Total-Results": "150"}),
        ]

        def fake_headers(req, timeout=15.0):
            i = pages["n"]; pages["n"] += 1
            return seq[i]

        monkeypatch.setattr(zotero, "_get_json_headers", fake_headers)
        out = zotero.fetch_all()
        assert len(out) == 150 and pages["n"] == 2

    def test_empty_library_with_header_returns_empty(self, monkeypatch):
        """A genuinely empty library (``Total-Results: 0``) still returns [] —
        the fail-closed path must not misfire on a real empty library (callers
        may safely create)."""
        self._setenv(monkeypatch)
        monkeypatch.setattr(
            zotero, "_get_json_headers",
            lambda req, timeout=15.0: ([], {"Total-Results": "0"}),
        )
        assert zotero.fetch_all() == []

    def test_bounded_max_items_returns_partial_without_completeness_claim(self, monkeypatch):
        """A bounded ``max_items`` read is a deliberate partial fetch, not a
        whole-library claim — it returns the cap even without a completeness
        cross-check (and even if the header were absent)."""
        self._setenv(monkeypatch)
        monkeypatch.setattr(
            zotero, "_get_json_headers",
            lambda req, timeout=15.0: (
                [{"key": f"K{i}", "data": {}} for i in range(100)], {},
            ),
        )
        out = zotero.fetch_all(max_items=10)
        assert len(out) == 10


# ---------------------------------------------------------------------------
# count_items — fail-CLOSED on a missing Total-Results header
# ---------------------------------------------------------------------------
class TestCountItemsFailClosed:
    def _setenv(self, monkeypatch):
        monkeypatch.setenv("ZOTERO_API_KEY", "k")
        monkeypatch.setenv("ZOTERO_GROUP_ID", "2504198")
        monkeypatch.delenv("ZOTERO_USER_ID", raising=False)

    def test_missing_header_raises_not_zero(self, monkeypatch):
        """Before: a missing Total-Results defaulted to 0 (fail-open: 'no items'
        for an unreadable count). After: raises the degraded-read signal."""
        self._setenv(monkeypatch)
        monkeypatch.setattr(
            zotero, "_get_json_headers",
            lambda req, timeout=15.0: ([], {}),
        )
        with pytest.raises(zotero.LibraryUnavailableError):
            zotero.count_items()

    def test_present_header_still_read(self, monkeypatch):
        self._setenv(monkeypatch)
        monkeypatch.setattr(
            zotero, "_get_json_headers",
            lambda req, timeout=15.0: ([], {"Total-Results": "137"}),
        )
        assert zotero.count_items() == 137
