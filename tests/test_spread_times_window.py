"""spread_timestamps refuses a window whose start is after its end.

The old code silently SWAPPED such a window, so a ``start`` given as true UTC
(hours after the local-wall-clock-with-Z "now") wrote marks after the session end.
``spread_timestamps`` now raises ``ValueError`` before touching any mark. No
timezone conversion anywhere.
"""
from __future__ import annotations

import pytest

from zoterocite import Docx, add_comment, new_doc, spread_timestamps
from zoterocite.ooxml import qn

AC = "Reviewer, Example"


def _seed(tmp_path, date="2026-06-26T12:00:00Z"):
    src = tmp_path / "w.docx"
    new_doc(src, ["Alpha anchor here.", "Beta anchor here."])
    doc = Docx(src)
    add_comment(doc, "Alpha", "note one", author=AC, date=date)
    add_comment(doc, "Beta", "note two", author=AC, date=date)
    doc.save(src)
    return src


def _comment_dates(doc):
    return [c.get(qn("w:date")) for c in doc.tree("word/comments.xml").iter(qn("w:comment"))]


def test_start_after_explicit_end_raises_and_writes_nothing(tmp_path):
    doc = Docx(_seed(tmp_path))
    before = _comment_dates(doc)
    with pytest.raises(ValueError, match="after its end"):
        spread_timestamps(doc, AC, start="2026-06-26T15:00:00Z", end="2026-06-26T14:00:00Z")
    assert _comment_dates(doc) == before


def test_start_after_defaulted_now_raises(tmp_path):
    """The UTC-for-local mistake: end defaults to now, start lands after it."""
    doc = Docx(_seed(tmp_path))
    with pytest.raises(ValueError, match="end defaulted to now"):
        spread_timestamps(doc, AC, start="2999-01-01T00:00:00Z")


def test_message_states_the_wall_clock_convention(tmp_path):
    doc = Docx(_seed(tmp_path))
    with pytest.raises(ValueError, match="LOCAL wall-clock with a Z suffix"):
        spread_timestamps(doc, AC, start="2026-06-26T15:00:00Z", end="2026-06-26T14:00:00Z")


def test_ordered_and_equal_windows_still_work(tmp_path):
    doc = Docx(_seed(tmp_path))
    assert spread_timestamps(doc, AC, start="2026-06-26T14:00:00Z",
                             end="2026-06-26T14:30:00Z") == 2
    ds = _comment_dates(doc)
    assert all("2026-06-26T14:00:00Z" <= d <= "2026-06-26T14:30:00Z" for d in ds), ds
    # start == end is not "after": the min-minutes rule widens it backwards
    assert spread_timestamps(doc, AC, start="2026-06-26T15:00:00Z",
                             end="2026-06-26T15:00:00Z") == 2
    ds = _comment_dates(doc)
    assert all("2026-06-26T14:45:00Z" <= d <= "2026-06-26T15:00:00Z" for d in ds), ds


def test_inverted_window_raises_even_with_no_author_marks(tmp_path):
    doc = Docx(_seed(tmp_path))
    with pytest.raises(ValueError, match="after its end"):
        spread_timestamps(doc, "Nobody, Else", start="2026-06-26T18:00:00Z",
                          end="2026-06-26T17:00:00Z")
