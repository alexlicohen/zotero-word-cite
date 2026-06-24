"""GF-8: textbox text inside mc:AlternateContent must be read EXACTLY once.

A modern DrawingML textbox is stored as an ``mc:AlternateContent`` block holding the
SAME content twice — once under ``mc:Choice`` (the DrawingML ``<wps:wsp>`` /
``<w:txbxContent>``) and once under ``mc:Fallback`` (a legacy VML ``<v:textbox>`` /
``<w:txbxContent>``). Per OOXML markup-compatibility a consumer uses the Choice if it
understands the namespace, else the Fallback — NEVER both. A naive text walk over all
``<w:t>`` descendants read BOTH copies, doubling the textbox text (and inflating NIH
word counts and the changes-summary section context).

These tests pin the single-read behavior at the two shared text owners:
:func:`zoterocite.paras.paragraph_text` and :func:`zoterocite.views._extract`
(via :func:`zoterocite.views.read_views`). They FAIL on baseline 7efa1ed (text twice)
and PASS after the mc:Fallback-skip fix.
"""
import io
import zipfile

from lxml import etree

from zoterocite.docxio import DOCUMENT, Docx
from zoterocite.ooxml import qn
from zoterocite.paras import find_paragraph, iter_paragraphs, paragraph_text
from zoterocite.sections import get_paragraphs_ooxml
from zoterocite.views import read_views

# Namespace URIs used to assemble the raw fixture.
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
WPS = "http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing"
V = "urn:schemas-microsoft-com:vml"

# A paragraph carrying an mc:AlternateContent textbox. The Choice (modern DrawingML)
# and the Fallback (legacy VML) BOTH wrap a <w:txbxContent> whose paragraph reads
# "Widget Diagram"; the surrounding body text reads "Intro" so we can also confirm
# ordinary runs are untouched.
_DOCUMENT_XML = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document
    xmlns:w="{W}" xmlns:mc="{MC}" xmlns:wps="{WPS}" xmlns:v="{V}">
  <w:body>
    <w:p>
      <w:r><w:t>Intro </w:t></w:r>
      <w:r>
        <mc:AlternateContent>
          <mc:Choice Requires="wps">
            <w:drawing>
              <wps:wsp>
                <wps:txbx>
                  <w:txbxContent>
                    <w:p><w:r><w:t>Widget Diagram</w:t></w:r></w:p>
                  </w:txbxContent>
                </wps:txbx>
              </wps:wsp>
            </w:drawing>
          </mc:Choice>
          <mc:Fallback>
            <w:pict>
              <v:shape>
                <v:textbox>
                  <w:txbxContent>
                    <w:p><w:r><w:t>Widget Diagram</w:t></w:r></w:p>
                  </w:txbxContent>
                </v:textbox>
              </v:shape>
            </w:pict>
          </mc:Fallback>
        </mc:AlternateContent>
      </w:r>
    </w:p>
  </w:body>
</w:document>"""

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml"
    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""

_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
    Target="word/document.xml"/>
</Relationships>"""


def _make_docx(tmp_path):
    out = tmp_path / "alternate_content.docx"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("word/document.xml", _DOCUMENT_XML)
    out.write_bytes(buf.getvalue())
    return out


def test_paragraph_text_reads_textbox_once(tmp_path):
    root = Docx(str(_make_docx(tmp_path))).read_tree(DOCUMENT)
    # The body-level <w:p> (not the inner txbxContent paragraph) is the one carrying
    # "Intro"; its paragraph_text must include "Widget Diagram" exactly once.
    paras = iter_paragraphs(root)
    body_p = next(p for p in paras if "Intro" in paragraph_text(p))
    text = paragraph_text(body_p)
    assert text.count("Widget Diagram") == 1, repr(text)
    assert "Intro" in text


def test_read_views_counts_textbox_once(tmp_path):
    views = read_views(str(_make_docx(tmp_path)))
    for mode in ("raw", "accepted", "rejected"):
        assert views[mode].count("Widget Diagram") == 1, (mode, views[mode])
        # The duplicated VML copy must not survive as an extra word, either.
        assert views[mode].split().count("Widget") == 1, (mode, views[mode])


# GF-8 follow-up: iter_paragraphs (the per-paragraph accessor used by anchoring and
# every index-aligned consumer) must also yield each visible paragraph once. A
# textbox's mc:AlternateContent block holds the caption under BOTH an mc:Choice and
# an mc:Fallback, each wrapping a w:txbxContent > w:p; root.iter(w:p) therefore
# yielded body + Choice-inner + Fallback-inner = 3 paragraphs for one textbox,
# triple-counting the caption and hard-crashing find_paragraph. These FAIL on
# baseline e11dafc (3x / LookupError) and PASS after the w:txbxContent-ancestor
# exclusion.


def test_iter_paragraphs_yields_textbox_text_once(tmp_path):
    root = Docx(str(_make_docx(tmp_path))).read_tree(DOCUMENT)
    paras = iter_paragraphs(root)
    # Across the WHOLE paragraph list, the caption appears in exactly one paragraph
    # (the body w:p that wraps the textbox), never as standalone inner paragraphs.
    with_widget = [p for p in paras if "Widget Diagram" in paragraph_text(p)]
    assert len(with_widget) == 1, [paragraph_text(p) for p in paras]


def test_find_paragraph_resolves_textbox_anchor(tmp_path):
    root = Docx(str(_make_docx(tmp_path))).read_tree(DOCUMENT)
    # Baseline raised LookupError("matched 3 paragraphs"); now it resolves to one.
    p = find_paragraph(root, "Widget Diagram")
    assert "Widget Diagram" in paragraph_text(p)


def test_ooxml_and_views_word_counts_agree_on_textbox(tmp_path):
    docx = _make_docx(tmp_path)
    # The raw-OOXML per-paragraph path and the read_views accepted path are two
    # independent extractors; after the fix both read the caption exactly once.
    ooxml_text = " ".join(get_paragraphs_ooxml(str(docx)))
    accepted = read_views(str(docx))["accepted"]
    assert ooxml_text.split().count("Widget") == 1, ooxml_text
    assert ooxml_text.split().count("Widget") == accepted.split().count("Widget")
