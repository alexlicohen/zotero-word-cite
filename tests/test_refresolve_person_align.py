"""Teeth tests for refresolve._author_family_compare person-alignment.

Each test pins a specific behavior of the author cross-check and would FAIL
against the pre-fix raw-index walk, where a leading collective the authoritative
source omits shifted every later position by one and false-flagged the last real
author as fabricated.
"""
from __future__ import annotations

import zoterocite.refresolve as rr


def _fam(*xs):
    return [{"family": f, "given": g} for f, g in xs]


def test_author_compare_leading_collective_no_false_mismatch():
    """A cited list that prepends a collective the authoritative source omits must
    NOT report the trailing real author as fabricated."""
    cited = [{"family": "GBD 2019 Collaborators", "given": ""},
             {"family": "Murray", "given": "C"}]
    actual = [{"family": "Murray", "given": "C"}]
    r = rr._author_family_compare(cited, actual)
    assert r["mismatch"] is False, r["details"]


def test_author_compare_still_flags_fabricated_extra_author():
    cited = _fam(("Smith", "J"), ("Jones", "A"), ("Ghost", "Z"))
    actual = _fam(("Smith", "J"), ("Jones", "A"))
    r = rr._author_family_compare(cited, actual)
    assert r["mismatch"] is True and any("Ghost" in d for d in r["details"])


def test_author_compare_etal_truncation_not_flagged():
    cited = _fam(("Smith", "J"))
    actual = _fam(("Smith", "J"), ("Jones", "A"), ("Lee", "K"))
    assert rr._author_family_compare(cited, actual)["mismatch"] is False


def test_author_compare_positional_surname_mismatch_caught():
    cited = _fam(("Smith", "J"), ("Wrong", "A"))
    actual = _fam(("Smith", "J"), ("Jones", "A"))
    r = rr._author_family_compare(cited, actual)
    assert r["mismatch"] is True and any("#2" in d for d in r["details"])
