"""validate: a comment in word/comments.xml with no w:commentReference is an ERROR.

Word never displays an unreferenced comment, so the note is silently lost; this
is the signature of a comment-anchor-loss bug. References are collected from
every story part, so a comment anchored in a footnote is fine.

Synthetic fixtures only (``new_doc`` + ``add_comment``).
"""
from __future__ import annotations

from lxml import etree

from zoterocite import Docx, new_doc
from zoterocite.comments import add_comment
from zoterocite.docxio import DOCUMENT
from zoterocite.ooxml import NS
from zoterocite.validate import validate

W = NS["w"]
AUTHOR = "Reviewer, Example"
REF = f"{{{W}}}commentReference"
FOOTNOTES_CT = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml")


def _orphans(doc):
    from zoterocite.validate import _check_orphan_comments
    return _check_orphan_comments(doc)


def _commented(tmp_path, n=2, name="c.docx"):
    src = tmp_path / name
    new_doc(src, [f"Paragraph number {i} of the synthetic text." for i in range(n)])
    doc = Docx(src)
    for i in range(n):
        add_comment(doc, f"Paragraph number {i}", f"note {i}", author=AUTHOR)
    doc.save(src)
    return src


def _drop_reference(src, cid, out):
    """Remove the run carrying commentReference *cid* (anchor loss)."""
    doc = Docx(src)
    root = doc.tree(DOCUMENT)
    ref = next(r for r in root.iter(REF) if r.get(f"{{{W}}}id") == cid)
    run = ref.getparent()
    run.getparent().remove(run)
    doc.save(out)
    return out


def _orphan_errors(r):
    return [e for e in r.errors if "commentReference" in e]


def test_all_referenced_reports_nothing(tmp_path):
    src = _commented(tmp_path)
    r = validate(src, screen_retractions=False)
    assert "orphan_comments" in r.info["checks"]
    assert _orphan_errors(r) == [] and r.ok


def test_no_comments_part_is_fine(tmp_path):
    src = tmp_path / "plain.docx"
    new_doc(src, ["Just text."])
    assert not Docx(src).has("word/comments.xml")
    r = validate(src, screen_retractions=False)
    assert "orphan_comments" in r.info["checks"]
    assert _orphan_errors(r) == [] and r.ok


def test_unreferenced_comment_is_an_error(tmp_path):
    src = _commented(tmp_path)
    ids = [c.get(f"{{{W}}}id") for c in
           Docx(src).read_tree("word/comments.xml").iter(f"{{{W}}}comment")]
    assert len(ids) == 2
    bad = _drop_reference(src, ids[1], tmp_path / "orphan.docx")
    r = validate(bad, screen_retractions=False)
    errs = _orphan_errors(r)
    assert r.ok is False
    assert len(errs) == 1
    assert "1 comment(s)" in errs[0] and f"id {ids[1]}" in errs[0]
    assert ids[0] not in errs[0].split("id ", 1)[1].split(",")


def test_range_without_reference_is_still_orphan(tmp_path):
    # The commentRangeStart/End survive but the reference run is gone: Word
    # shows nothing, so this is still an orphan.
    src = _commented(tmp_path, n=1)
    cid = next(Docx(src).read_tree(DOCUMENT).iter(REF)).get(f"{{{W}}}id")
    bad = _drop_reference(src, cid, tmp_path / "orphan.docx")
    root = Docx(bad).read_tree(DOCUMENT)
    assert list(root.iter(f"{{{W}}}commentRangeStart"))
    assert _orphans(Docx(bad))


def test_reference_inside_tracked_deletion_counts(tmp_path):
    src = _commented(tmp_path, n=1)
    doc = Docx(src)
    root = doc.tree(DOCUMENT)
    run = next(root.iter(REF)).getparent()
    wrap = etree.Element(f"{{{W}}}del", {f"{{{W}}}id": "900", f"{{{W}}}author": AUTHOR,
                                         f"{{{W}}}date": "2026-01-01T00:00:00Z"})
    run.addprevious(wrap)
    wrap.append(run)
    out = tmp_path / "del.docx"
    doc.save(out)
    assert _orphans(Docx(out)) == []


def test_reference_in_footnote_is_not_orphan(tmp_path):
    # Move the reference run out of the body into a footnote story.
    src = _commented(tmp_path, n=1)
    doc = Docx(src)
    root = doc.tree(DOCUMENT)
    run = next(root.iter(REF)).getparent()
    run.getparent().remove(run)
    fn = etree.fromstring(
        f'<w:footnotes xmlns:w="{W}"><w:footnote w:id="1"><w:p>'
        '<w:r><w:t>A synthetic footnote.</w:t></w:r></w:p></w:footnote></w:footnotes>')
    fn.find(f".//{{{W}}}p").append(run)
    doc.add_part("word/footnotes.xml", etree.tostring(fn), FOOTNOTES_CT)
    out = tmp_path / "fn.docx"
    doc.save(out)
    assert not list(Docx(out).read_tree(DOCUMENT).iter(REF))
    r = validate(out, screen_retractions=False)
    assert "orphan_comments" in r.info["checks"]
    assert _orphan_errors(r) == []


def test_reference_only_in_glossary_does_not_count(tmp_path):
    # The glossary document has its own comments part; its references never
    # anchor a main-document comment.
    src = _commented(tmp_path, n=1)
    doc = Docx(src)
    root = doc.tree(DOCUMENT)
    run = next(root.iter(REF)).getparent()
    run.getparent().remove(run)
    g = etree.fromstring(f'<w:document xmlns:w="{W}"><w:body><w:p/></w:body></w:document>')
    g.find(f".//{{{W}}}p").append(run)
    doc.add_part("word/glossary/document.xml", etree.tostring(g),
                 "application/vnd.openxmlformats-officedocument."
                 "wordprocessingml.document.glossary+xml")
    out = tmp_path / "gl.docx"
    doc.save(out)
    assert _orphans(Docx(out))


def test_malformed_comments_part_is_reported_once(tmp_path):
    # A broken comments.xml is the well-formedness check's finding, not ours.
    src = _commented(tmp_path, n=1)
    doc = Docx(src)
    doc.set_part_bytes("word/comments.xml", b"<w:comments><broken>")
    out = tmp_path / "bad.docx"
    doc.save(out)
    r = validate(out, screen_retractions=False)
    assert r.ok is False
    assert "orphan_comments" in r.info["checks"]
    assert _orphan_errors(r) == []
    assert any("malformed XML in part word/comments.xml" in e for e in r.errors)


def test_reference_in_non_story_part_does_not_count(tmp_path):
    # Only word/*.xml story parts anchor a comment; a stray commentReference in a
    # customXml item (or any non-word/ part) must not mask an orphan.
    src = _commented(tmp_path, n=1)
    doc = Docx(src)
    root = doc.tree(DOCUMENT)
    run = next(root.iter(REF)).getparent()
    run.getparent().remove(run)
    holder = etree.fromstring(f'<w:document xmlns:w="{W}"><w:body><w:p/></w:body></w:document>')
    holder.find(f".//{{{W}}}p").append(run)
    doc.add_part("customXml/item9.xml", etree.tostring(holder), "application/xml")
    out = tmp_path / "cx.docx"
    doc.save(out)
    assert _orphans(Docx(out))


def test_reference_inside_comments_part_does_not_count(tmp_path):
    # A comment part is not a story part: a commentReference that survives only
    # inside word/comments.xml itself must not mask the orphan.
    src = _commented(tmp_path, n=1)
    doc = Docx(src)
    root = doc.tree(DOCUMENT)
    run = next(root.iter(REF)).getparent()
    run.getparent().remove(run)
    croot = doc.tree("word/comments.xml")
    next(croot.iter(f"{{{W}}}p")).append(run)
    out = tmp_path / "cm.docx"
    doc.save(out)
    assert _orphans(Docx(out))
