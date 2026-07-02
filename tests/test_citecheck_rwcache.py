"""Retraction-Watch parse cache: load_retraction_db memoizes the ~62 MB CSV
parse per (path, mtime, size) so the several features that screen retractions in
one run don't each re-parse it — and a refresh (new file) is never served stale.

Synthetic fixtures only.
"""
from __future__ import annotations

import os
import time

from zoterocite import citecheck as C

_CSV = (
    "RetractionDOI,OriginalPaperDOI,RetractionNature,RetractionDate,Title\n"
    "10.1/r,10.1000/aaa,Retraction,2020-01-01,Bad paper\n"
)


def _write(p, body):
    p.write_text(body, encoding="utf-8")


def test_same_file_is_cached(tmp_path):
    """Two loads of the same unchanged file return the SAME object — proof the
    parse was reused (RED if the cache is removed: each call would build a new
    dict, so `is` would be False)."""
    C._reset_retraction_cache()
    p = tmp_path / "rw.csv"
    _write(p, _CSV)
    a = C.load_retraction_db(p)
    b = C.load_retraction_db(p)
    assert a is b
    assert "10.1000/aaa" in a


def test_refresh_invalidates_no_stale_serve(tmp_path):
    """A rewritten CSV (a Retraction-Watch refresh) invalidates the cache — the
    next load reparses and sees the new content; the old snapshot is untouched."""
    C._reset_retraction_cache()
    p = tmp_path / "rw.csv"
    _write(p, _CSV)
    a = C.load_retraction_db(p)
    # Add a row (size changes -> key changes even if mtime resolution is coarse);
    # bump mtime too as belt-and-suspenders.
    _write(p, _CSV + "10.2/r,10.1000/bbb,Retraction,2021-02-02,Another\n")
    os.utime(p, (time.time() + 2, time.time() + 2))
    b = C.load_retraction_db(p)
    assert b is not a                 # reparsed
    assert "10.1000/bbb" in b         # sees new content — never served stale
    assert "10.1000/bbb" not in a     # prior snapshot unchanged


def test_distinct_paths_not_conflated(tmp_path):
    """Two different CSV paths get independent cache entries."""
    C._reset_retraction_cache()
    p1 = tmp_path / "a.csv"
    _write(p1, _CSV)
    p2 = tmp_path / "b.csv"
    _write(p2, _CSV.replace("10.1000/aaa", "10.1000/ccc"))
    assert "10.1000/aaa" in C.load_retraction_db(p1)
    assert "10.1000/ccc" in C.load_retraction_db(p2)


def test_reset_clears(tmp_path):
    C._reset_retraction_cache()
    p = tmp_path / "rw.csv"
    _write(p, _CSV)
    a = C.load_retraction_db(p)
    C._reset_retraction_cache()
    assert C.load_retraction_db(p) is not a
