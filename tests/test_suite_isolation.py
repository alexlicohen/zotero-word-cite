"""conftest's ``_isolate_retraction_feed`` holds for every test: the Retraction
Watch cache is a per-test tmp path and real network calls fail, so no test
downloads the ~65 MB feed into the package's data/ cache."""
from __future__ import annotations

import urllib.error
import urllib.request

import pytest

import zoterocite.citecheck as cc


def test_rw_cache_is_tmp(tmp_path):
    assert tmp_path in cc.default_rw_path().parents


def test_network_is_blocked():
    # The block's own message: a DNS failure on a real attempt would not match.
    with pytest.raises(urllib.error.URLError, match="network disabled"):
        urllib.request.urlopen("https://example.invalid/", timeout=1)
