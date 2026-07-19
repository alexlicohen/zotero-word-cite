"""Tests for zoterocite.comments — authored Word comments."""
from __future__ import annotations

from zoterocite import Docx, new_doc
from zoterocite.comments import add_comment, list_comments
from zoterocite.ooxml import qn

COMMENTS_PART = "word/comments.xml"


def _doc_with_two_comments(tmp_path):
    src = tmp_path / "src.docx"
    new_doc(src, ["First claim here.", "Second claim here."])
    doc = Docx(src)
    add_comment(doc, "First claim", "note one", author="Reviewer")
    add_comment(doc, "Second claim", "note two", author="Reviewer")
    out = tmp_path / "out.docx"
    doc.save(out)
    return out


class TestListComments:
    def test_lists_both(self, tmp_path):
        out = _doc_with_two_comments(tmp_path)
        got = list_comments(Docx(out))
        assert [t for _cid, _a, t in got] == ["note one", "note two"]

    def test_comment_without_w_id_is_skipped(self, tmp_path):
        """A <w:comment> lacking w:id (malformed / foreign-tool output) must be
        skipped, not abort the whole listing with a TypeError from int(None)."""
        out = _doc_with_two_comments(tmp_path)

        # Strip w:id from the FIRST comment element only.
        doc = Docx(out)
        root = doc.tree(COMMENTS_PART)
        comments = root.findall(qn("w:comment"))
        assert len(comments) == 2
        del comments[0].attrib[qn("w:id")]
        assert comments[0].get(qn("w:id")) is None
        broken = tmp_path / "broken.docx"
        doc.save(broken)

        got = list_comments(Docx(broken))  # must not raise
        assert [t for _cid, _a, t in got] == ["note two"]
