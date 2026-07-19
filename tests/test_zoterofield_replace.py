"""Tests for in-place ad-hoc-marker -> live Zotero field replacement.

`replace_text_with_zotero_field` strikes the literal marker text and splices a
live ZOTERO_ITEM CSL_CITATION field where the marker was — vs `insert_citation`,
which appends the field and leaves the marker. Verified through the accepted /
rejected views and `scan_citations`.
"""
import pytest

from zoterocite import Docx, read_views, validate, scan_citations, new_doc
from zoterocite.zoterofield import replace_text_with_zotero_field
from zoterocite.ooxml import qn
from zoterocite.docxio import DOCUMENT

MARKER = "(Smith et al., 2020)"
SENTENCE = f"Lesion mapping localizes symptoms {MARKER} to circuits."
URI = "http://zotero.org/groups/2504198/items/SM2020KEY"
ITEMDATA = {
    "id": "2504198/SM2020KEY",
    "type": "article-journal",
    "title": "Mapping symptoms to circuits",
    "container-title": "Brain",
    "issued": {"date-parts": [[2020]]},
}
RENDERED = "(1)"


def _replace(src, *, track):
    doc = Docx(src)
    replace_text_with_zotero_field(
        doc, MARKER, ["SM2020KEY"],
        itemdata=[ITEMDATA], uris=[URI], rendered=RENDERED, track=track,
    )
    return doc


def test_replace_tracked(tmp_path):
    src = tmp_path / "src.docx"
    new_doc(src, [SENTENCE])
    doc = _replace(src, track=True)
    out = tmp_path / "out.docx"
    doc.save(out)

    # round-trips cleanly (no Word repair)
    assert validate(out).ok

    body = Docx(out).raw("word/document.xml").decode()
    assert "ADDIN ZOTERO_ITEM CSL_CITATION" in body
    assert URI in body
    # tracked markup present: the marker was struck and the field inserted
    assert body.count("<w:del ") == 1
    assert body.count("<w:ins ") == 1
    assert "<w:delText" in body

    views = read_views(out)
    # accepted view: marker gone, rendered citation shown where it was
    assert MARKER not in views["accepted"]
    assert RENDERED in views["accepted"]
    assert "Lesion mapping localizes symptoms" in views["accepted"]
    assert "to circuits." in views["accepted"]
    # rejected view: original marker back, field's rendered text absent
    assert MARKER in views["rejected"]
    assert RENDERED not in views["rejected"]

    # a real Zotero field is detectable
    cits = scan_citations(out)
    assert len(cits) == 1
    assert cits[0]["items"][0]["key"] == "SM2020KEY"


def test_replace_untracked(tmp_path):
    src = tmp_path / "src.docx"
    new_doc(src, [SENTENCE])
    doc = _replace(src, track=False)
    out = tmp_path / "out.docx"
    doc.save(out)

    assert validate(out).ok
    body = Docx(out).raw("word/document.xml").decode()
    # no tracked markup at all
    assert "<w:del " not in body
    assert "<w:ins " not in body
    assert "<w:delText" not in body
    assert "ADDIN ZOTERO_ITEM CSL_CITATION" in body

    views = read_views(out)
    # marker gone from every view; field rendered text present
    assert MARKER not in views["accepted"]
    assert MARKER not in views["rejected"]
    assert RENDERED in views["accepted"]
    assert "Lesion mapping localizes symptoms" in views["accepted"]

    cits = scan_citations(out)
    assert len(cits) == 1 and cits[0]["items"][0]["key"] == "SM2020KEY"


def test_replace_field_position_is_inline_not_appended(tmp_path):
    """The field replaces the marker mid-sentence; surrounding text is preserved
    on both sides (i.e. it is NOT appended at the paragraph end)."""
    src = tmp_path / "src.docx"
    new_doc(src, [SENTENCE])
    doc = _replace(src, track=False)
    out = tmp_path / "out.docx"
    doc.save(out)

    accepted = read_views(out)["accepted"]
    # text after the marker survives after the citation -> field was placed inline
    assert accepted.index("symptoms") < accepted.index(RENDERED) < accepted.index("to circuits")


def test_replace_marker_spanning_multiple_runs(tmp_path):
    """The marker split across several runs (each with its own rPr) is still
    located and isolated; only the marker is struck, neighbours preserved."""
    src = tmp_path / "src.docx"
    # Build a paragraph whose marker is split across run boundaries with mixed
    # formatting, so the run-splitter is exercised.
    new_doc(src, [{
        "runs": [
            {"text": "Symptoms localize "},
            {"text": "(Smith ", "italic": True},
            {"text": "et al., ", "bold": True},
            {"text": "2020)"},
            {"text": " to circuits."},
        ],
    }])
    # sanity: the visible text contains the contiguous marker
    assert MARKER in read_views(src)["accepted"]

    doc = _replace(src, track=True)
    out = tmp_path / "out.docx"
    doc.save(out)

    assert validate(out).ok
    views = read_views(out)
    assert MARKER not in views["accepted"]
    assert RENDERED in views["accepted"]
    assert "Symptoms localize" in views["accepted"]
    assert "to circuits." in views["accepted"]
    assert MARKER in views["rejected"]
    assert len(scan_citations(out)) == 1


def test_replace_anchor_not_found_raises(tmp_path):
    """Module convention: an anchor that doesn't match exactly one paragraph
    raises LookupError (same as paras.find_paragraph) — the doc is not mutated."""
    src = tmp_path / "src.docx"
    new_doc(src, [SENTENCE])
    doc = Docx(src)
    with pytest.raises(LookupError):
        replace_text_with_zotero_field(
            doc, "(Jones et al., 1999)", ["SM2020KEY"],
            itemdata=[ITEMDATA], uris=[URI], rendered=RENDERED, track=True,
        )
    # the document tree still carries the original marker, no field, no markup
    root = doc.tree(DOCUMENT)
    visible = "".join(t.text or "" for t in root.iter(qn("w:t")))
    assert MARKER in visible
    assert root.find(".//" + qn("w:ins")) is None
    assert root.find(".//" + qn("w:del")) is None
    # no Zotero field was spliced in
    assert root.find(".//" + qn("w:instrText")) is None


# -- FIX 3: marker straddling an inline-container boundary -------------------
from lxml import etree
from zoterocite.zoterofield import _isolate_marker_runs, _splice_field_at_marker

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _para(inner: str) -> etree._Element:
    return etree.fromstring(
        f'<w:p xmlns:w="{_W}">{inner}</w:p>'.encode()
    )


def _run(text: str) -> str:
    return f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>'


def test_isolate_marker_across_hyperlink_raises_lookuperror():
    """A citation marker that straddles a <w:hyperlink> boundary yields overlapping
    runs under DIFFERENT parents. _wrap_runs_as_del / the no-track splice remove all
    marker runs from a SINGLE parent, so pre-fix this raised an lxml ValueError
    MID-mutation and corrupted the tree. The guard must convert it into a clean,
    pre-mutation LookupError instead — and leave the paragraph untouched."""
    # "(Smith et al., 2020)" spans a run under <w:p> and a run under <w:hyperlink>.
    para = _para(
        _run("Lesion mapping (Smith ")
        + f'<w:hyperlink>{_run("et al., 2020) to circuits.")}</w:hyperlink>'
    )
    before = etree.tostring(para)

    with pytest.raises(LookupError) as ei:
        _isolate_marker_runs(para, MARKER)
    msg = str(ei.value)
    assert "inline container" in msg
    assert "not supported" in msg

    # Not a ValueError, and NOTHING was mutated before the raise (tree byte-identical).
    assert etree.tostring(para) == before

    # The higher-level splice turns this into a skippable no-op (returns False), so a
    # batch converting many markers never crashes on this one. On the LookupError
    # path _splice_field_at_marker returns before touching doc/root/field_xml, so
    # placeholders are safe here.
    para2 = _para(
        _run("Lesion mapping (Smith ")
        + f'<w:hyperlink>{_run("et al., 2020) to circuits.")}</w:hyperlink>'
    )
    assert _splice_field_at_marker(
        None, None, para2, MARKER, "",
        track=True, author="zotero-word-cite", date="2026-01-01T00:00:00Z",
    ) is False


def test_isolate_marker_same_parent_split_still_isolates():
    """Guard must NOT over-fire: a marker split across TWO runs under the SAME parent
    isolates normally (this is the ordinary rPr-preserving split path)."""
    para = _para(_run("Lesion mapping (Smith ") + _run("et al., 2020) to circuits."))
    marker_runs, parent, idx = _isolate_marker_runs(para, MARKER)
    assert marker_runs                      # isolated, no raise
    # every isolated run shares the returned single parent
    assert all(r.getparent() is parent for r in marker_runs)
    assert "".join(
        (t.text or "") for r in marker_runs for t in r.iter(qn("w:t"))
    ) == MARKER
