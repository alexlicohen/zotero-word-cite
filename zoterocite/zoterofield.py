"""Insert *live* Zotero citation fields into a .docx.

These are not static text: each citation is a Word complex field whose code is
``ADDIN ZOTERO_ITEM CSL_CITATION {…}`` carrying the item's CSL-JSON and its
**group-library URI**. A document-level ``ZOTERO_PREF`` field pins the CSL style
(Vancouver by default). When the file is opened in Word/LibreOffice with the
Zotero plugin and refreshed, Zotero recognizes the fields as its own, formats
them in the chosen style, and builds the bibliography from your group library —
nothing generated externally.

NOTE: the field schema must match what Zotero expects; this is written to
Zotero's documented format but cannot be exercised against the plugin headlessly,
so validate with one "Refresh" in Word.
"""
from __future__ import annotations

import copy
import hashlib
import html
import json
import re
import unicodedata
import warnings
from pathlib import Path
from typing import List, Optional

from lxml import etree

from . import csldb
from .docxio import DOCUMENT, Docx
from .ooxml import NS, now_iso, qn
from .paras import (ParaIndex, find_paragraph, find_paragraphs, get_body,
                    iter_paragraphs, paragraph_text)

W = NS["w"]
CSL_SCHEMA = "https://github.com/citation-style-language/schema/raw/master/csl-citation.json"
DEFAULT_STYLE = "vancouver-superscript"   # NIH/Vancouver with superscript numbers
STYLE_URLS = {
    "vancouver-superscript": "http://www.zotero.org/styles/vancouver-superscript",
    "vancouver": "http://www.zotero.org/styles/vancouver",
    "apa": "http://www.zotero.org/styles/apa",
    "nature": "http://www.zotero.org/styles/nature",
}
_PREF_DATA = (
    '<data data-version="3" zotero-version="6.0.27">'
    '<session id="{sid}"/>'
    '<style id="{style}" locale="en-US" hasBibliography="1" bibliographyStyleHasBeenSet="1"/>'
    '<prefs><pref name="fieldType" value="Field"/>'
    '<pref name="automaticJournalAbbreviations" value="true"/>'
    '<pref name="noteType" value="0"/></prefs></data>'
)

# Zotero stores document prefs as Word CUSTOM DOCUMENT PROPERTIES (not a field):
# ZOTERO_PREF_1, _2, ... each holding a <=255-char chunk of the <data> XML.
_CUSTOM_PART = "docProps/custom.xml"
_CUSTOM_NS = "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"
_VT_NS = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"
_CUSTOM_CT = "application/vnd.openxmlformats-officedocument.custom-properties+xml"
_CUSTOM_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/custom-properties"
_FMTID = "{D5CDD505-2E9C-101B-9397-08002B2CF9AE}"
_ROOT_RELS = "_rels/.rels"


def _chunks(s: str, n: int = 255) -> List[str]:
    return [s[i:i + n] for i in range(0, len(s), n)] or [""]


def _ensure_root_rel(doc: Docx, rel_type: str, target: str) -> None:
    rels = doc.tree(_ROOT_RELS)
    for rel in rels:
        if rel.get("Target") == target and rel.get("Type") == rel_type:
            return
    used = {r.get("Id") for r in rels}
    i = 1
    while f"rId{i}" in used:
        i += 1
    el = etree.SubElement(rels, qn("pr:Relationship"))
    el.set("Id", f"rId{i}"); el.set("Type", rel_type); el.set("Target", target)


def set_document_prefs(doc: Docx, data_xml: str) -> None:
    """Store the Zotero ``<data>`` prefs blob as ZOTERO_PREF_* custom properties
    (chunked at 255 chars), creating/registering docProps/custom.xml as needed."""
    if not doc.has(_CUSTOM_PART):
        root = etree.Element("{%s}Properties" % _CUSTOM_NS, nsmap={None: _CUSTOM_NS, "vt": _VT_NS})
        doc.add_part(_CUSTOM_PART, etree.tostring(root, xml_declaration=True, encoding="UTF-8",
                                                  standalone=True), content_type=_CUSTOM_CT)
        _ensure_root_rel(doc, _CUSTOM_REL, "docProps/custom.xml")
    root = doc.tree(_CUSTOM_PART)
    pids = []
    for prop in list(root):
        pids.append(int(prop.get("pid", "1")))
        if (prop.get("name") or "").startswith("ZOTERO_PREF"):
            root.remove(prop)
    pid = max(pids or [1]) + 1
    for i, chunk in enumerate(_chunks(data_xml), start=1):
        prop = etree.SubElement(root, "{%s}property" % _CUSTOM_NS)
        prop.set("fmtid", _FMTID); prop.set("pid", str(pid)); prop.set("name", f"ZOTERO_PREF_{i}")
        etree.SubElement(prop, "{%s}lpwstr" % _VT_NS).text = chunk
        pid += 1


def _esc(s: str) -> str:
    return html.escape(s, quote=False)


_FLD_BEGIN = '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
_FLD_SEPARATE = '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
_FLD_END = '<w:r><w:fldChar w:fldCharType="end"/></w:r>'


def _field_code(instr: str) -> str:
    return f'<w:r><w:instrText xml:space="preserve"> {_esc(instr)} </w:instrText></w:r>'


def _field_runs(instr: str, result_xml: Optional[str]) -> str:
    """XML run string for a Word complex field. ``result_xml`` is the visible
    result runs (Zotero overwrites on refresh); ``None`` => code-only."""
    s = _FLD_BEGIN + _field_code(instr)
    if result_xml is not None:
        s += _FLD_SEPARATE + result_xml
    s += _FLD_END
    return s


_HTML_TAG = re.compile(r"<(/?)(sup|sub|i|b|em|strong)\b[^>]*>", re.IGNORECASE)


def _strip_html(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def _plain_run(text: str) -> str:
    return f'<w:r><w:t xml:space="preserve">{_esc(text)}</w:t></w:r>'


def _runs_from_html(htmlstr: str) -> str:
    """Convert Zotero's rendered-citation HTML (``<sup>``/``<i>``/``<b>``) into
    formatted Word runs, so the citation *displays* correctly (e.g. superscript)
    on first open — before any Zotero refresh."""
    state = {"sup": 0, "sub": 0, "i": 0, "b": 0}
    out: List[str] = []

    def emit(chunk: str) -> None:
        text = html.unescape(re.sub(r"<[^>]+>", "", chunk))
        if not text:
            return
        rpr = ""
        if state["sup"] > 0:
            rpr += '<w:vertAlign w:val="superscript"/>'
        elif state["sub"] > 0:
            rpr += '<w:vertAlign w:val="subscript"/>'
        if state["i"] > 0:
            rpr += "<w:i/>"
        if state["b"] > 0:
            rpr += "<w:b/>"
        rprx = f"<w:rPr>{rpr}</w:rPr>" if rpr else ""
        out.append(f'<w:r>{rprx}<w:t xml:space="preserve">{_esc(text)}</w:t></w:r>')

    pos = 0
    for m in _HTML_TAG.finditer(htmlstr or ""):
        emit((htmlstr or "")[pos:m.start()])
        tag = m.group(2).lower()
        key = {"em": "i", "strong": "b"}.get(tag, tag)
        state[key] += -1 if m.group(1) else 1
        pos = m.end()
    emit((htmlstr or "")[pos:])
    return "".join(out)


def _runs(xml: str) -> List[etree._Element]:
    return list(etree.fromstring(f'<w:w xmlns:w="{W}">{xml}</w:w>'))


def _paragraph(xml: str) -> etree._Element:
    return etree.fromstring(f'<w:p xmlns:w="{W}">{xml}</w:p>')


def _short(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:10].upper()


def _resolve_style_url(style: str, *, validate_online: bool = False) -> str:
    """Map a requested CSL ``style`` to the Zotero style URL written into the
    document pref, validating it first.

    A style is accepted if it is a known catalog id *or* a syntactically-
    plausible CSL slug (and, when ``validate_online``, one the CSL repo confirms
    exists). An unrecognized value (a URL, a typo, an empty string) — or, under
    ``validate_online``, a plausible slug the repo reports as **absent** (HTTP
    404) — raises ``ValueError`` with the nearest catalog matches and how to add
    one, instead of silently writing a broken pref from an unknown string.

    Network policy (F11) — the default ``validate_online=False`` validates
    against the LOCAL ``csldb`` catalog ONLY and makes **no network call**. This
    is the deterministic field-write / doc-edit path (every ``insert_citation`` /
    ``cite_into`` / ``replace_*`` calls ``ensure_pref`` → here), which must never
    reach the network on a citation insert. Online existence checking is an
    OPT-IN, deferred to the explicit ``zoterocite csldb --check <id> --online``
    step (``cli.py``). With ``validate_online=True`` an uncataloged-but-plausible
    slug is additionally confirmed against the CSL repo and a 404 is rejected; an
    unreachable network fails *open* to the offline rule (we never hard-fail
    offline — see :func:`zoterocite.csldb.is_valid_style`).

    Catalog ids and the 4 incumbents short-circuit with no network in either mode
    (a known id is always valid; the 4 are returned from ``STYLE_URLS`` before any
    check). The 4 historically-mapped styles keep their explicit Zotero URLs; any
    other valid id is turned into the canonical ``zotero.org/styles/<id>`` URL.
    """
    if style in STYLE_URLS:
        return STYLE_URLS[style]
    if csldb.is_valid_style(style, online=validate_online):
        return f"http://www.zotero.org/styles/{style}"
    nearest = csldb.nearest_styles(style, n=3)
    hint = ", ".join(f"{s.name!r} ({s.csl_id})" for s in nearest) or "(none)"
    raise ValueError(
        f"unrecognized CSL style {style!r}: not a known catalog id and not a "
        f"plausible CSL slug (lowercase letters/digits joined by single "
        f"hyphens, e.g. 'the-lancet-neurology'). Nearest catalog matches: "
        f"{hint}. To add a new journal, find its id at "
        f"github.com/citation-style-language/styles and add it to "
        f"zoterocite/csldb.py, or pass that id directly."
    )


def ensure_pref(doc: Docx, style: str = DEFAULT_STYLE, *,
                validate_online: bool = False) -> None:
    """Pin the CSL ``style`` via Zotero document prefs (idempotent). Stored as
    Word custom document properties, which is where Zotero looks for them.

    Raises ``ValueError`` on an unrecognized style (see :func:`_resolve_style_url`)
    rather than silently writing a broken pref.

    ``validate_online`` defaults to ``False`` so the common citation-insert path
    validates the style against the LOCAL catalog only and never touches the
    network (F11). Pass ``True`` only from an explicit, opt-in validation step
    (e.g. ``zoterocite csldb --check --online``) to additionally confirm an
    uncataloged-but-plausible slug exists in the CSL repo."""
    style_url = _resolve_style_url(style, validate_online=validate_online)
    data = _PREF_DATA.format(sid=_short("session", style_url), style=style_url)
    set_document_prefs(doc, data)


def _build_zotero_field_xml(
    citation_id_seed: List[str],
    keys: List[str],
    itemdata: List[dict],
    uris: List[str],
    *,
    rendered: str = "(citation)",
    rendered_html: Optional[str] = None,
    extras: Optional[List[dict]] = None,
) -> str:
    """Build the XML run string for ONE ``ZOTERO_ITEM CSL_CITATION`` field
    grouping ``keys``. Shared by :func:`insert_citation` (append) and
    :func:`replace_field_with_zotero` (swap-in).

    ``citation_id_seed`` seeds a stable citationID (e.g. an anchor + keys, or a
    located foreign field's id). Returns the run XML (begin/instr/sep/result/end).
    """
    extras = extras or [{} for _ in keys]
    # zip() would SILENTLY truncate to the shortest list, dropping or mis-pairing
    # citation items. Fail loudly instead: a field must carry exactly one
    # itemData/uri/extras per key. (cite_into guards this too; this is the shared
    # builder's own backstop for every other entry point.)
    if not (len(keys) == len(itemdata) == len(uris) == len(extras)):
        raise ValueError(
            "zotero field lists are not aligned with keys "
            f"(keys={len(keys)}, itemdata={len(itemdata)}, uris={len(uris)}, "
            f"extras={len(extras)}); refusing to build a field that would silently "
            "drop or mis-pair citation items"
        )
    citation_items = []
    for key, idata, uri, ex in zip(keys, itemdata, uris, extras):
        ci = {"id": idata.get("id", key), "uris": [uri], "itemData": idata}
        if ex.get("locator"):
            ci["locator"] = str(ex["locator"])
            ci["label"] = ex.get("label", "page")
        for k in ("prefix", "suffix"):
            if ex.get(k):
                ci[k] = ex[k]
        if _suppresses_author(ex):
            # CSL citation-item flag: the author is printed as plain text before
            # the field ("Smith et al. (2019)"), so the field renders the year only.
            ci["suppress-author"] = True
        citation_items.append(ci)
    if rendered_html:
        plain = _strip_html(rendered_html) or "(citation)"
        result_xml = _runs_from_html(rendered_html) or _plain_run(plain)
        formatted = rendered_html
    else:
        plain = rendered
        result_xml = _plain_run(rendered)
        formatted = rendered
    citation = {
        "citationID": _short("cite", *citation_id_seed, *keys),
        "properties": {"formattedCitation": formatted, "plainCitation": plain, "noteIndex": 0},
        "citationItems": citation_items,
        "schema": CSL_SCHEMA,
    }
    instr = "ADDIN ZOTERO_ITEM CSL_CITATION " + json.dumps(citation, ensure_ascii=False)
    return _field_runs(instr, result_xml)


def _suppresses_author(extra: Optional[dict]) -> bool:
    """True when a citation item's extras ask for CSL ``suppress-author`` (the
    JSON spelling ``"suppress-author"`` or the Python-friendly ``suppress_author``)."""
    return bool(extra) and bool(extra.get("suppress-author") or extra.get("suppress_author"))


# -- grouped / narrative pre-render (shown until the first Zotero Refresh) -----
PLACEHOLDER = "(citation)"
# An author-date in-text render always carries a year-like token; a numeric one
# ("(1)", "[3]", superscript "1") never does. Grouping by "; " is only what an
# author-date style prints, so a render without one keeps the placeholder.
_YEARISH = re.compile(r"\b\d{4}[a-z]?\b|\bn\.\s?d\.|\bin press\b|\bforthcoming\b", re.IGNORECASE)
_LETTER = re.compile(r"[^\W\d_]")
_TAG_SPLIT = re.compile(r"(<[^>]+>)")


def _fold(s: str) -> str:
    """Accent-folded, case-folded sort key (``Ó`` sorts with ``O``), the order an
    author-date style uses to sort the items of one citation."""
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().casefold()


def _strip_outer_parens_html(h: str) -> Optional[str]:
    """Remove the outer ``(`` and ``)`` from a rendered citation's HTML, leaving
    tags (``<i>``) intact. ``None`` when the visible text is not parenthesised."""
    plain = _strip_html(h)
    if not (plain.startswith("(") and plain.endswith(")")):
        return None
    parts = _TAG_SPLIT.split(h or "")
    texts = [i for i, s in enumerate(parts) if s and not s.startswith("<") and s.strip()]
    if not texts:
        return None
    first, last = texts[0], texts[-1]
    parts[first] = parts[first].replace("(", "", 1)
    i = parts[last].rfind(")")
    parts[last] = parts[last][:i] + parts[last][i + 1:]
    return "".join(parts)


def _year_part(inner_plain: str) -> Optional[str]:
    """The year part of an author-date item ("Smith et al., 2019a" -> "2019a"),
    for a suppress-author item whose author is printed outside the field."""
    tail = (inner_plain.rsplit(",", 1)[-1] if "," in inner_plain
            else (inner_plain.split() or [""])[-1]).strip()
    return tail if tail and _YEARISH.search(tail) else None


def group_citation_html(renders: List[str], *,
                        suppress_author: Optional[List[bool]] = None) -> Optional[str]:
    """One pre-rendered citation from per-item author-date renders, or ``None``.

    Each render (Zotero ``include=citation`` HTML, e.g. ``(Smith et al., 2019)``)
    loses its outer parentheses; the items are sorted by an accent-folded key and
    joined with ``"; "`` inside one pair of parentheses, which is how APA and
    similar author-date styles print a group. A ``suppress_author`` item keeps
    only its year part (``(2019)``). Returns ``None`` (keep the placeholder) when
    any render is missing, not parenthesised, or has no author and year-like
    token (a numeric style's ``(1)``: its group would be ``(1,2)``, which per-item
    renders cannot produce)."""
    if not renders:
        return None
    flags = list(suppress_author or [False] * len(renders))
    if len(flags) != len(renders):
        return None
    items = []
    for h, sup in zip(renders, flags):
        inner = _strip_outer_parens_html(h or "")
        if inner is None:
            return None
        inner_plain = _strip_html(inner)
        if not (_LETTER.search(inner_plain) and _YEARISH.search(inner_plain)):
            return None
        if sup:
            year = _year_part(inner_plain)
            if year is None:
                return None
            inner = _esc(year)
            inner_plain = year
        items.append((_fold(inner_plain), inner))
    items.sort(key=lambda t: t[0])
    return "(" + "; ".join(h for _, h in items) + ")"


def grouped_citation_render(keys: List[str], *, style: str = DEFAULT_STYLE,
                            suppress_author: Optional[List[bool]] = None) -> str:
    """Pre-rendered HTML for ONE field citing ``keys`` (a group, or any item with
    ``suppress-author``): per-item ``zotero.formatted_citations(kind="citation")``
    grouped by :func:`group_citation_html`. Returns :data:`PLACEHOLDER` when the
    style's renders have no grouped form (numeric styles: by design, silent) and
    also, with a ``UserWarning`` so a shared draft is not silently left showing
    it, when the renders are unavailable (offline, degraded read, an omitted
    item)."""
    from . import zotero
    try:
        renders = zotero.formatted_citations(keys, style=style, kind="citation", strip=False)
    except Exception as exc:  # noqa: BLE001 - display value only; never fail the write
        renders, why = None, f"renders unavailable ({type(exc).__name__}: {exc})"
    else:
        why = (None if len(renders) == len(keys)
               else f"Zotero returned {len(renders)} render(s) for {len(keys)} item(s)")
    if why:
        warnings.warn(
            f"grouped citation pre-render skipped: {why}; the field shows "
            f"{PLACEHOLDER!r} until a Zotero Refresh", UserWarning, stacklevel=2)
        return PLACEHOLDER
    return group_citation_html(renders, suppress_author=suppress_author) or PLACEHOLDER


def _clean_bib_entry_html(h: str) -> str:
    """One bibliography entry's inline HTML from Zotero's ``include=bib`` output:
    the ``csl-entry`` content without the wrapping ``div``s, a numeric style's
    left-margin label joined to its text by a space, layout whitespace removed."""
    s = h or ""
    m = re.search(r'<div[^>]*class="csl-entry"[^>]*>(.*)</div>', s, re.S)
    if m:
        s = m.group(1)
    s = re.sub(r'</div>\s*<div[^>]*class="csl-right-inline"[^>]*>', " ", s)
    s = re.sub(r"</?div\b[^>]*>", "", s)
    return re.sub(r"\s*\n\s*", " ", s).strip()


def bibliography_entries_html(keys: List[str], *, style: str = DEFAULT_STYLE,
                              sort: bool = True) -> List[str]:
    """Per-item bibliography entry HTML for ``keys`` (``formatted_citations(
    kind="bib")``), cleaned for :func:`_runs_from_html`. ``sort`` orders the
    entries by an accent-folded key unless they are numbered (a numeric style's
    entries keep request order). Returns ``[]`` and warns when the renders are
    unavailable; the caller then writes a code-only bibliography field."""
    from . import zotero
    try:
        raw = zotero.formatted_citations(keys, style=style, kind="bib", strip=False)
    except Exception as exc:  # noqa: BLE001 - display value only
        warnings.warn(f"bibliography pre-render skipped ({type(exc).__name__}: {exc}); "
                      "the field stays empty until a Zotero Refresh", UserWarning, stacklevel=2)
        return []
    entries = [e for e in (_clean_bib_entry_html(h) for h in raw) if _strip_html(e)]
    numbered = entries and all(re.match(r"\s*[\[(]?\d", _strip_html(e)) for e in entries)
    if sort and not numbered:
        entries.sort(key=lambda e: _fold(_strip_html(e)))
    return entries


def insert_citation(
    doc: Docx,
    anchor: str,
    keys: List[str],
    *,
    itemdata: List[dict],
    uris: List[str],
    rendered: str = "(citation)",
    rendered_html: Optional[str] = None,
    style: str = DEFAULT_STYLE,
    extras: Optional[List[dict]] = None,
    add_bibliography: bool = False,
    bib_rendered: str = "",
) -> Docx:
    """Append a live Zotero citation field to the paragraph containing ``anchor``.

    Multiple ``keys`` produce ONE citation field grouping all items (Zotero
    renders e.g. "(1-3)"). ``extras`` (aligned with ``keys``) may carry per-item
    ``locator``/``label``/``prefix``/``suffix`` (e.g. a page number). ``itemdata``
    /``uris`` are the per-key CSL-JSON and group-library URIs. Adds the ZOTERO_PREF
    document prefs (style) and optionally a ZOTERO_BIBL bibliography field.
    """
    ensure_pref(doc, style)
    root = doc.tree(DOCUMENT)
    field_xml = _build_zotero_field_xml(
        [anchor], keys, itemdata, uris,
        rendered=rendered, rendered_html=rendered_html, extras=extras,
    )
    para = find_paragraph(root, anchor)
    for r in _runs(field_xml):
        para.append(r)

    if add_bibliography:
        bib_instr = 'ADDIN ZOTERO_BIBL {"uncited":[],"omitted":[],"custom":[]} CSL_BIBLIOGRAPHY'
        bib_p = _paragraph(_field_runs(bib_instr, _plain_run(bib_rendered) if bib_rendered else None))
        body = get_body(root)
        sect = body.find(qn("w:sectPr"))
        if sect is not None:
            sect.addprevious(bib_p)
        else:
            body.append(bib_p)
    return doc


_BIBL_INSTR = 'ADDIN ZOTERO_BIBL {"uncited":[],"omitted":[],"custom":[]} CSL_BIBLIOGRAPHY'


def has_bibliography_field(root: etree._Element) -> bool:
    """True when the document already contains a Zotero ``ZOTERO_BIBL`` field, so
    a caller can avoid inserting a second one (idempotent bib handling)."""
    return any("ZOTERO_BIBL" in (code or "") for code in _field_codes(root))


_STYLES_PART = "word/styles.xml"
BIBLIOGRAPHY_STYLE_ID = "Bibliography"
_HANGING_INDENT = '<w:ind w:left="720" w:hanging="720"/>'


def ensure_bibliography_style(doc: Docx) -> Optional[str]:
    """The styleId of the document's ``Bibliography`` paragraph style, adding one
    (hanging indent, based on the default paragraph style) to ``word/styles.xml``
    when it is missing. Idempotent. A style NAMED "Bibliography" under another id
    is reused. Returns ``None`` when there is no styles part or the id is taken by
    a non-paragraph style; the caller then indents the entries directly."""
    if not doc.has(_STYLES_PART):
        return None
    styles = doc.tree(_STYLES_PART)
    default_id = None
    for st in styles.iter(qn("w:style")):
        sid, typ = st.get(qn("w:styleId")), st.get(qn("w:type"))
        if sid == BIBLIOGRAPHY_STYLE_ID:
            return sid if typ == "paragraph" else None
        name = st.find(qn("w:name"))
        if (typ == "paragraph" and name is not None
                and (name.get(qn("w:val")) or "").casefold() == "bibliography"):
            return sid
        if (typ == "paragraph" and default_id is None
                and st.get(qn("w:default")) in ("1", "true", "on")):
            default_id = sid
    based = f'<w:basedOn w:val="{_esc(default_id)}"/>' if default_id else ""
    styles.append(etree.fromstring(
        f'<w:style xmlns:w="{W}" w:type="paragraph" w:styleId="{BIBLIOGRAPHY_STYLE_ID}">'
        f'<w:name w:val="Bibliography"/>{based}<w:uiPriority w:val="37"/><w:unhideWhenUsed/>'
        f'<w:pPr><w:spacing w:after="120" w:line="276" w:lineRule="auto"/>'
        f'{_HANGING_INDENT}</w:pPr></w:style>'))
    return BIBLIOGRAPHY_STYLE_ID


def _bibliography_paragraphs(entries_html: List[str], style_id: Optional[str]) -> List[etree._Element]:
    """One paragraph per entry carrying ONE multi-paragraph ``ZOTERO_BIBL`` field:
    begin/code/separate open the first entry, end closes the last. Each paragraph
    takes ``style_id`` (or a direct hanging indent when there is no style)."""
    out = []
    n = len(entries_html)
    for i, h in enumerate(entries_html):
        xml = (_FLD_BEGIN + _field_code(_BIBL_INSTR) + _FLD_SEPARATE) if i == 0 else ""
        xml += _runs_from_html(h) or _plain_run(_strip_html(h))
        if i == n - 1:
            xml += _FLD_END
        ppr = (f'<w:pPr><w:pStyle w:val="{_esc(style_id)}"/></w:pPr>' if style_id
               else f"<w:pPr>{_HANGING_INDENT}</w:pPr>")
        out.append(_paragraph(ppr + xml))
    return out


def _track_inserted_paragraph(p: etree._Element, ids, author: str, date: str) -> None:
    """Mark a NEW paragraph as a tracked insertion in place: its content runs go
    into one ``<w:ins>`` and its paragraph mark gets ``<w:pPr><w:rPr><w:ins/>``, so
    Reject removes the whole paragraph (no orphan empty one)."""
    ppr = p.find(qn("w:pPr"))
    if ppr is None:
        ppr = etree.Element(qn("w:pPr"))
        p.insert(0, ppr)
    ins = etree.Element(qn("w:ins"))
    ins.set(qn("w:id"), ids.next()); ins.set(qn("w:author"), author); ins.set(qn("w:date"), date)
    for c in [c for c in p if c is not ppr]:
        p.remove(c); ins.append(c)
    p.append(ins)
    rpr = ppr.find(qn("w:rPr"))
    if rpr is None:
        rpr = etree.SubElement(ppr, qn("w:rPr"))
    mark = etree.SubElement(rpr, qn("w:ins"))
    mark.set(qn("w:id"), ids.next()); mark.set(qn("w:author"), author); mark.set(qn("w:date"), date)


def _find_heading_paragraph(root: etree._Element, heading: str) -> etree._Element:
    """The ONE paragraph whose whole accepted-view text is ``heading`` (whitespace
    and case insensitive), a TOC entry losing a tie to the body heading."""
    want = re.sub(r"\s+", " ", heading or "").strip().casefold()
    if not want:
        raise ValueError("bibliography heading is empty")
    hits = [p for p in iter_paragraphs(root)
            if re.sub(r"\s+", " ", paragraph_text(p)).strip().casefold() == want]
    if len(hits) > 1:
        idx = ParaIndex(root)
        body = [p for p in hits if not idx.is_toc(p)]
        if len(body) == 1:
            hits = body
    if len(hits) != 1:
        raise LookupError(f"bibliography heading {heading!r} matched {len(hits)} "
                          "paragraphs (need exactly 1)")
    return hits[0]


def append_bibliography_field(
    doc: Docx,
    *,
    style: str = DEFAULT_STYLE,
    bib_rendered: str = "",
    heading: Optional[str] = None,
    track: bool = False,
    author: str = "zotero-word-cite",
    date: Optional[str] = None,
    entries_html: Optional[List[str]] = None,
    under_heading: Optional[str] = None,
) -> bool:
    """Write a Zotero ``ZOTERO_BIBL`` bibliography field, so a single Zotero Refresh
    renders the bibliography from the cited items. Idempotent: returns ``False``
    without writing if a ``ZOTERO_BIBL`` field already exists.

    Placement: at the end of the body (before the trailing ``sectPr``), or, with
    ``under_heading``, directly after the one existing paragraph whose text is that
    heading (``LookupError`` before any write when it is missing or ambiguous).
    ``heading`` instead creates a new heading paragraph before the field (not with
    ``under_heading``).

    Content: with ``entries_html`` (per-entry HTML, e.g.
    :func:`bibliography_entries_html`) the field is PRE-RENDERED across one
    paragraph per entry (begin in the first, end in the last; ``<i>``/``<b>``/
    ``<sup>`` kept via :func:`_runs_from_html`), each in the ``Bibliography``
    paragraph style (added with a hanging indent when missing,
    :func:`ensure_bibliography_style`). Otherwise one paragraph: code-only, or
    ``bib_rendered`` as its plain result. Registers the CSL ``style`` prefs.

    ``track=True`` inserts every new paragraph as a tracked insertion (runs and
    paragraph mark) and spreads this op's marks (one per paragraph) into distinct,
    ordered ``w:date``s ending at ``date``; marks that existed before are never
    re-stamped.

    This is intentionally ADDITIVE — it never deletes a manual bibliography (a
    caller that wants the manual block removed must do so explicitly, and only
    when the block is unambiguously located).
    """
    if heading and under_heading:
        raise ValueError("pass heading (new heading paragraph) or under_heading "
                         "(an existing one), not both")
    root = doc.tree(DOCUMENT)
    if has_bibliography_field(root):
        return False
    target = _find_heading_paragraph(root, under_heading) if under_heading else None
    ensure_pref(doc, style)
    date = date or now_iso()
    body = get_body(root)
    sect = body.find(qn("w:sectPr"))

    new_paras: List[etree._Element] = []
    if heading:
        new_paras.append(_paragraph(_plain_run(heading)))
    entries = [e for e in (entries_html or []) if _strip_html(e)]
    if entries:
        new_paras.extend(_bibliography_paragraphs(entries, ensure_bibliography_style(doc)))
    else:
        new_paras.append(_paragraph(_field_runs(
            _BIBL_INSTR, _plain_run(bib_rendered) if bib_rendered else None)))

    pre_marks = _snapshot_marks(doc, author) if track else None
    if track:
        ids = _RevIds(root)
        for p in new_paras:
            _track_inserted_paragraph(p, ids, author, date)

    if target is not None:
        prev = target
        for p in new_paras:
            prev.addnext(p)
            prev = p
    else:
        for p in new_paras:
            if sect is not None:
                sect.addprevious(p)
            else:
                body.append(p)
    if track and len(new_paras) > 1:
        _spread_op_marks(doc, author, date, pre_marks)
    return True


def cite_into(
    path,
    anchor: str,
    *,
    keys: Optional[List[str]] = None,
    items: Optional[List[dict]] = None,
    doi: Optional[str] = None,
    style: str = DEFAULT_STYLE,
    add_bibliography: bool = False,
    out=None,
    suppress_author: bool = False,
    bib_under: Optional[str] = None,
) -> Path:
    """High-level: resolve item(s) in the Zotero group library and insert ONE live
    citation field at ``anchor`` (grouping multiple items).

    Provide one of: ``items`` (list of ``{"key","locator?","label?","prefix?",
    "suffix?","suppress-author?"}`` for full control incl. page locators),
    ``keys`` (list), or ``doi`` (single). Fetches CSL-JSON + URIs + a pre-rendered
    marker. ``suppress_author`` sets CSL ``suppress-author`` on every item (a
    narrative cite: the author is typed before the field, which shows "(2019)").

    A group (several keys) or a suppress-author item is pre-rendered by
    :func:`grouped_citation_render` (author-date styles; else the placeholder
    plus a warning). ``add_bibliography`` writes ONE pre-rendered multi-paragraph
    ``ZOTERO_BIBL`` field (:func:`append_bibliography_field`; idempotent), at the
    body end or, with ``bib_under``, after that existing heading paragraph.
    """
    from . import zotero
    if items:
        keys = [it["key"] for it in items]
        extras = items
    elif doi and not keys:
        # Cached-index DOI resolve (O(1)), never the full-library scan — see resolve_doi_item.
        item = zotero.resolve_doi_item(doi)
        if not item:
            raise RuntimeError(f"no Zotero item with DOI {doi}")
        keys = [item["key"]]
        extras = [{}]
    elif keys:
        extras = [{} for _ in keys]
    else:
        raise ValueError("provide items, keys, or doi")
    # zotero.csljson now returns CSL-JSON in REQUEST order, one entry per key
    # (placeholders for any Zotero omitted), so itemdata[i] is guaranteed to
    # describe keys[i]. uris are derived per-key. Bind them explicitly and assert
    # the alignment so a future contract change can't silently mis-pair them in
    # the positional zip inside _build_zotero_field_xml (CRIT-3).
    itemdata = zotero.csljson(keys)
    uris = [zotero.item_uri_offline_safe(k) for k in keys]
    if not (len(itemdata) == len(uris) == len(keys)):
        raise RuntimeError(
            "Zotero metadata/URI lists are not aligned with keys "
            f"(keys={len(keys)}, itemdata={len(itemdata)}, uris={len(uris)}); "
            "refusing to insert a citation that could bind the wrong reference."
        )
    if suppress_author:
        extras = [dict(ex, **{"suppress-author": True}) for ex in extras]
    suppress = [_suppresses_author(ex) for ex in extras]
    if len(keys) > 1 or any(suppress):
        # A group renders as ONE marker. For an author-date style that marker is
        # the per-item renders without their parentheses, sorted and joined by
        # "; " (APA's citation order); a numeric style's "(1,2)" cannot be built
        # from per-item renders, so it keeps the neutral placeholder (warned). The
        # live field carries the real citationItems either way and Zotero
        # regenerates the marker on Refresh.
        rendered_html = grouped_citation_render(keys, style=style, suppress_author=suppress)
    else:
        rendered_html = "; ".join(
            zotero.formatted_citations(keys, style=style, kind="citation", strip=False)) or None
    if bib_under:
        add_bibliography = True
    doc = Docx(path)
    insert_citation(doc, anchor, keys, itemdata=itemdata, uris=uris, extras=extras,
                    rendered_html=rendered_html, style=style)
    if add_bibliography:
        append_bibliography_field(doc, style=style, under_heading=bib_under,
                                  entries_html=bibliography_entries_html(keys, style=style))
    return doc.save(Path(out) if out else Path(path))


# -- complex-field structure walker -----------------------------------------
class ComplexField:
    """One Word complex field (``fldChar begin`` .. ``end``) found by
    :func:`iter_complex_fields`.

    ``begin`` / ``separate`` / ``end`` are the fldChar-bearing ``w:r`` elements
    (``separate`` is ``None`` for a field with no result). ``code`` is the live
    ``w:instrText`` only (what :func:`scan_citations` reads); ``instr`` also
    includes ``w:delInstrText``, so a tracked-deleted field still names its type.
    ``members`` is every ``w:r`` after ``begin`` up to and including ``end`` in
    document order: instr runs, ``separate``, result runs, ``end``, and the runs
    of any nested field. ``parent`` is the enclosing field, if any.
    """

    __slots__ = ("begin", "separate", "end", "code", "instr", "members", "parent")

    def __init__(self, begin: etree._Element, parent: Optional["ComplexField"]) -> None:
        self.begin = begin
        self.separate: Optional[etree._Element] = None
        self.end: Optional[etree._Element] = None
        self.code = ""
        self.instr = ""
        self.members: List[etree._Element] = []
        self.parent = parent

    @property
    def kind(self) -> str:
        """The field type token: ``ZOTERO_ITEM`` for ``ADDIN ZOTERO_ITEM ...``,
        ``PAGEREF`` for ``PAGEREF _Toc1 \\h``, ``""`` when the code is empty."""
        words = self.instr.split()
        if words and words[0].upper() == "ADDIN" and len(words) > 1:
            return words[1]
        return words[0] if words else ""


def iter_complex_fields(root):
    """Yield every complete complex field under ``root`` as a :class:`ComplexField`,
    in the order their ``end`` is reached (an inner field before its outer one).

    The single field-structure walker: fields split across many runs, spanning
    paragraphs, and nested inside another field's code or result are handled with
    a stack. An unmatched ``end`` is ignored and a field still open at the end of
    ``root`` is not yielded. Runs are walked with ``root.iter`` (tracked wrappers,
    hyperlinks, content controls and ``mc:Fallback`` copies included), so callers
    that mutate the tree must materialise the generator first."""
    fld_tag, typ = qn("w:fldChar"), qn("w:fldCharType")
    instr_tag, dinstr_tag = qn("w:instrText"), qn("w:delInstrText")
    stack: List[ComplexField] = []
    for r in root.iter(qn("w:r")):
        for f in stack:
            f.members.append(r)
        fld = r.find(fld_tag)
        if fld is not None:
            t = fld.get(typ)
            if t == "begin":
                stack.append(ComplexField(r, stack[-1] if stack else None))
            elif t == "separate" and stack:
                if stack[-1].separate is None:
                    stack[-1].separate = r
            elif t == "end" and stack:
                f = stack.pop()
                f.end = r
                yield f
            continue
        if not stack or stack[-1].separate is not None:
            continue
        instr = r.find(instr_tag)
        if instr is not None:
            stack[-1].code += instr.text or ""
            stack[-1].instr += instr.text or ""
        dinstr = r.find(dinstr_tag)
        if dinstr is not None:
            stack[-1].instr += dinstr.text or ""


# -- scanning / broken-link detection ---------------------------------------
def _field_codes(root) -> List[str]:
    """Concatenated instrText for each Word complex field, in document order
    (handles field codes split across runs and nested fields via a stack)."""
    return [f.code for f in iter_complex_fields(root)]


def _key_from_uri(uri: Optional[str]) -> Optional[str]:
    return uri.rsplit("/", 1)[-1] if uri else None


def _library_from_uri(uri: Optional[str]) -> Optional[str]:
    m = re.search(r"zotero\.org/(groups|users)/(\d+)/items/", uri or "")
    return f"{m.group(1)}:{m.group(2)}" if m else None


def scan_citations(path) -> List[dict]:
    """List every Zotero citation field in ``path``.

    Returns one dict per citation: ``{"citationID", "items": [{"key","uri",
    "title","library","locator"}]}``.
    """
    root = Docx(path).read_tree(DOCUMENT)
    out: List[dict] = []
    for code in _field_codes(root):
        c = code.strip()
        if "ZOTERO_ITEM CSL_CITATION" not in c or "{" not in c:
            continue
        try:
            data = json.loads(c[c.index("{"): c.rindex("}") + 1])
        except json.JSONDecodeError:
            continue
        items = []
        for ci in data.get("citationItems", []):
            uri = (ci.get("uris") or [None])[0]
            items.append({
                "key": _key_from_uri(uri) or ci.get("id"),
                "uri": uri,
                "library": _library_from_uri(uri),
                "title": (ci.get("itemData") or {}).get("title", ""),
                "locator": ci.get("locator"),
            })
        out.append({"citationID": data.get("citationID"), "items": items})
    return out


def existing_renderings(root) -> set:
    """Rendered citation text (``formattedCitation`` / ``plainCitation``) of every
    existing ``ZOTERO_ITEM`` field in an already-loaded document ``root``.

    Used to skip RE-citing an in-text marker that is ALREADY a live Zotero field:
    refextract reads markers from rendered text and can't tell a live field's
    output (e.g. ``(1,2)``) from plain typed text, so a caller about to convert
    plain-text cites must avoid clobbering fields that are already managed.
    """
    out: set = set()
    for code in _field_codes(root):
        c = code.strip()
        if "ZOTERO_ITEM CSL_CITATION" not in c or "{" not in c:
            continue
        try:
            data = json.loads(c[c.index("{"): c.rindex("}") + 1])
        except json.JSONDecodeError:
            continue
        props = data.get("properties") or {}
        for k in ("formattedCitation", "plainCitation"):
            v = (props.get(k) or "").strip()
            if v:
                out.add(v)
    return out


def check_links(path) -> List[dict]:
    """Verify each cited item against the configured Zotero library.

    Returns one dict per cited item: ``{"key","title","uri","status"}`` where
    status is ``ok`` (present in the configured group), ``broken`` (its key is
    not in the configured library — deleted/moved), ``external`` (the URI points
    at a different library, which we can't check), or ``unverified`` (Zotero
    credentials are not configured, so a keyed item can't be checked against the
    library — we degrade rather than raise, since this is read-only QC).
    """
    from . import zotero
    cfg = zotero.zotero_config()
    mine = f"{cfg.get('library_type')}s:{cfg.get('library_id')}" if cfg else None
    # Without credentials we cannot reach the library at all; a keyed item is
    # therefore "unverified" (we never call item_exists, which would raise). A
    # keyless item (a structural defect) is still detectable offline as "broken".
    have_creds = bool(cfg)
    cache: dict = {}
    results: List[dict] = []
    for cit in scan_citations(path):
        for it in cit["items"]:
            key, lib = it["key"], it["library"]
            if lib and mine and lib != mine:
                status = "external"
            elif not key:
                status = "broken"
            elif not have_creds:
                status = "unverified"
            else:
                if key not in cache:
                    cache[key] = zotero.item_exists(key)
                status = "ok" if cache[key] else "broken"
            results.append({"key": key, "title": it["title"], "uri": it["uri"], "status": status})
    return results


# -- foreign-field replacement (citation conversion) ------------------------
_TRACK_DATE = "2026-01-01T00:00:00Z"


_REV_TAGS = (qn("w:ins"), qn("w:del"), qn("w:moveFrom"), qn("w:moveTo"))


def _next_rev_id(root) -> int:
    # ins/del/moveFrom/moveTo share ONE w:id space in Word: scan all four so a new
    # id can never collide with a move's.
    mx = 0
    for el in root.iter(*_REV_TAGS):
        try:
            mx = max(mx, int(el.get(qn("w:id"), 0)))
        except (TypeError, ValueError):
            pass
    return mx + 1


class _RevIds:
    """Revision-id allocator (``next()`` -> str), seeded past the document's max;
    the same interface as ``revisions._Ids`` so revisions primitives can share it."""

    def __init__(self, root) -> None:
        self.n = _next_rev_id(root) - 1

    def next(self) -> str:
        self.n += 1
        return str(self.n)


def _snapshot_marks(doc: Docx, author: str):
    """``revisions.snapshot_dated_marks`` (the author's marks BEFORE this op), or
    ``None`` when it is unavailable — the trailing spread is then skipped rather
    than run with a date-value-only scope that could re-stamp an older mark."""
    try:
        from .revisions import snapshot_dated_marks
        return snapshot_dated_marks(doc, author)
    except Exception:  # noqa: BLE001 - best effort; a missing owner skips the spread
        return None


def _spread_op_marks(doc: Docx, author: str, date: str, pre_marks) -> int:
    """Spread the marks THIS op stamped at ``date`` into distinct, ordered times
    (``revisions.spread_own_marks``, provenance-scoped by ``pre_marks``). Marks
    that existed before the op are never touched. Best effort: never fails a write."""
    if pre_marks is None:
        return 0
    try:
        from .revisions import spread_own_marks
        return spread_own_marks(doc, author, date, pre_existing=pre_marks)
    except Exception:  # noqa: BLE001
        return 0


def _wrap_runs_as_del(runs: List[etree._Element], rev_id: int, author: str, date: str) -> etree._Element:
    """Wrap a contiguous list of sibling runs in a single ``<w:del>`` (in place),
    converting any ``<w:t>`` to ``<w:delText>``. Returns the new ``<w:del>``."""
    parent = runs[0].getparent()
    idx = parent.index(runs[0])
    d = etree.Element(qn("w:del"))
    d.set(qn("w:id"), str(rev_id))
    d.set(qn("w:author"), author)
    d.set(qn("w:date"), date)
    for r in runs:
        parent.remove(r)
        _as_deleted_text(r)
        d.append(r)
    parent.insert(idx, d)
    return d


def _as_deleted_text(r: etree._Element) -> None:
    """Convert ``r``'s ``w:t`` / ``w:instrText`` to their tracked-deletion
    variants (``w:delText`` / ``w:delInstrText``), as content inside a ``w:del``
    must be.

    Uses ``iter`` (not ``findall``) so text/instr nested below the run — e.g.
    under a Word built-in citation's ``<w:sdtContent>`` when ``r`` is the
    ``<w:sdt>`` itself — is converted too. With ``findall`` only the run's DIRECT
    children were touched, leaving a LIVE ``<w:instrText>``/``<w:t>`` (a live field
    + original text) inside the ``<w:del>`` for the sdt carrier."""
    for t in r.iter(qn("w:t")):
        t.tag = qn("w:delText")
    for it in r.iter(qn("w:instrText")):
        it.tag = qn("w:delInstrText")


# -- tracked-field consistency (field_tracking_mismatch) ---------------------
# A field whose ``begin`` run sits in a tracked insertion/deletion must have EVERY
# run up to its ``end`` removed in the same view(s); otherwise resolving the change
# leaves a broken field or its stray result text. The shape in the wild: a Zotero
# "Refresh" in Word rewrites the result runs of a tracked-inserted citation OUTSIDE
# the field's ``w:ins``, so Reject All leaves a stray citation number (and, for a
# tracked-deleted field, Accept All does). Word never writes this shape itself.
_STORY_PART = re.compile(r"^word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml$")
_GONE_ON_REJECT = (qn("w:ins"), qn("w:moveTo"))
_GONE_ON_ACCEPT = (qn("w:del"), qn("w:moveFrom"))


def _story_parts(doc: Docx) -> List[str]:
    """Body, header, footer, footnote and endnote parts (where fields live)."""
    return [p for p in doc.parts() if _STORY_PART.match(p)]


def _nearest(el: etree._Element, tags) -> Optional[etree._Element]:
    anc = el.getparent()
    while anc is not None:
        if anc.tag in tags:
            return anc
        anc = anc.getparent()
    return None


def _field_mismatch(f: ComplexField) -> Optional[dict]:
    """The inconsistency of one field, or ``None`` when it has none.

    The begin's NEAREST ``w:ins`` / ``w:del`` ancestors are the templates (a begin
    in ``ins > del`` needs both). A member run is loose when it is not removed in a
    view that removes the begin: no ``w:ins``/``w:moveTo`` ancestor when the begin is
    inserted, no ``w:del``/``w:moveFrom`` ancestor when it is deleted. A begin
    tracked only by a move (``moveTo``/``moveFrom``) is out of scope."""
    ins_t = _nearest(f.begin, (qn("w:ins"),))
    del_t = _nearest(f.begin, (qn("w:del"),))
    if ins_t is None and del_t is None:
        return None
    loose = []
    for r in f.members:
        need_ins = ins_t is not None and _nearest(r, _GONE_ON_REJECT) is None
        need_del = del_t is not None and _nearest(r, _GONE_ON_ACCEPT) is None
        if need_ins or need_del:
            loose.append((r, need_ins, need_del))
    if not loose:
        return None
    return {"field": f, "ins": ins_t, "del": del_t, "loose": loose}


def _mismatches(root) -> List[dict]:
    return [m for m in (_field_mismatch(f) for f in iter_complex_fields(root)) if m]


def _describe(m: dict, part: str) -> dict:
    tmpl = m["ins"] if m["ins"] is not None else m["del"]
    kind = "+".join(k for k in ("ins", "del") if m[k] is not None)
    text = "".join(t.text or "" for r, _, _ in m["loose"]
                   for t in r.iter(qn("w:t"), qn("w:delText")))
    return {
        "part": part,
        "field": m["field"].kind,
        "tracked": kind,
        "author": tmpl.get(qn("w:author")),
        "date": tmpl.get(qn("w:date")),
        "loose_runs": len(m["loose"]),
        "loose_text": text,
        "fixable": all(not (ni and _nearest(r, _GONE_ON_ACCEPT) is not None)
                       for r, ni, _ in m["loose"]),
    }


def field_tracking_issues(doc: Docx) -> List[dict]:
    """Read-only: one dict per complex field (any type; ZOTERO_ITEM is the case
    seen in the wild) whose begin is tracked but some run up to its end is not
    removed with it. Keys: ``part``, ``field`` (type token), ``tracked``
    (``ins``/``del``/``ins+del``), the begin wrapper's ``author``/``date``,
    ``loose_runs``, ``loose_text`` (the text that would be left stray) and
    ``fixable`` (False only for a loose run already inside a bare deletion while
    the field is inserted: wrapping it would need an insertion around a deletion).
    Unparseable parts are skipped (the well-formedness check reports them)."""
    from .docxio import DocxLoadError
    out: List[dict] = []
    for part in _story_parts(doc):
        try:
            root = doc.read_tree(part)
        except DocxLoadError:
            continue
        out.extend(_describe(m, part) for m in _mismatches(root))
    return out


def _wrapper_like(template: etree._Element, ids) -> etree._Element:
    """A new empty wrapper of ``template``'s tag carrying ALL its attributes
    (author, date and any dateUtc/rsid) verbatim except a fresh ``w:id``."""
    el = etree.Element(template.tag)
    for k, v in template.attrib.items():
        el.set(k, v)
    el.set(qn("w:id"), ids.next())
    return el


def _wrap_group(runs: List[etree._Element], ins_t, del_t, ids) -> int:
    """Wrap contiguous sibling ``runs`` in place: ``w:ins`` (copy of ``ins_t``)
    outermost, then ``w:del`` (copy of ``del_t``) with its text converted to
    delText — the ``ins > del`` shape Word writes. Returns wrappers created."""
    parent, idx = runs[0].getparent(), runs[0].getparent().index(runs[0])
    outer = inner = None
    for t in (ins_t, del_t):
        if t is None:
            continue
        w = _wrapper_like(t, ids)
        if inner is None:
            outer = w
        else:
            inner.append(w)
        inner = w
    for r in runs:
        parent.remove(r)
        if del_t is not None:
            _as_deleted_text(r)
        inner.append(r)
    parent.insert(idx, outer)
    return (ins_t is not None) + (del_t is not None)


def fix_field_tracking(doc: Docx) -> dict:
    """Re-wrap the loose runs of every tracked field (see
    :func:`field_tracking_issues`) so the whole field resolves with its begin.

    Each loose run gets a new wrapper of the kind it lacks, copying the begin
    wrapper's author and ``w:date`` (and other attributes) verbatim, with a fresh
    ``w:id`` unique across the story parts; text inside a new ``w:del`` becomes
    ``w:delText`` / ``w:delInstrText``. Contiguous sibling runs share one wrapper.
    The innermost field is repaired first, so a nested field's runs take their own
    field's author. NOTHING is re-dated and no spread runs: the new wrappers
    continue an existing mark, so they carry its exact date (the one deliberate
    exception to the spread-own-marks rule). Consistent fields and every other
    element are untouched; a second run is a no-op. An unfixable run (see
    :func:`field_tracking_issues`) is left alone and counted in ``unfixed``.

    Returns ``{"fields", "runs", "wrappers", "unfixed"}``."""
    from .docxio import DocxLoadError
    roots = {}
    for part in _story_parts(doc):
        try:
            roots[part] = doc.read_tree(part)
        except DocxLoadError:
            continue
    ids = _RevIds(etree.Element(qn("w:document")))      # one id space across parts
    for root in roots.values():
        ids.n = max(ids.n, _next_rev_id(root) - 1)
    stats = {"fields": 0, "runs": 0, "wrappers": 0, "unfixed": 0}
    for part, root in roots.items():
        fields = list(iter_complex_fields(root))
        touched = False
        for f in fields:
            m = _field_mismatch(f)       # re-evaluated: an inner fix may cover runs
            if m is None:
                continue
            stats["fields"] += 1
            groups: List[list] = []
            loose_ids = {id(r) for r, _, _ in m["loose"]}
            for r, need_ins, need_del in m["loose"]:
                host = _nearest(r, (qn("w:r"),))
                if host is not None and id(host) in loose_ids:
                    continue                 # e.g. a textbox run: its host run is wrapped
                if need_ins and _nearest(r, _GONE_ON_ACCEPT) is not None:
                    stats["unfixed"] += 1
                    continue
                key = (need_ins, need_del)
                g = groups[-1] if groups else None
                if g and g[0] == key and g[1][-1].getnext() is r:
                    g[1].append(r)
                else:
                    groups.append([key, [r]])
            for (need_ins, need_del), runs in groups:
                stats["wrappers"] += _wrap_group(
                    runs, m["ins"] if need_ins else None,
                    m["del"] if need_del else None, ids)
                stats["runs"] += len(runs)
                touched = True
        if touched:
            doc.tree(part)               # mark the part dirty for save
    return stats


def replace_field_with_zotero(
    doc: Docx,
    locator: dict,
    *,
    keys: List[str],
    itemdata: List[dict],
    uris: List[str],
    rendered: str = "(citation)",
    rendered_html: Optional[str] = None,
    extras: Optional[List[dict]] = None,
    style: str = DEFAULT_STYLE,
    track: bool = False,
    author: str = "zotero-word-cite",
    date: Optional[str] = None,
    seed: Optional[object] = None,
) -> Docx:
    """Swap a located *foreign* citation field for a live Zotero ``ZOTERO_ITEM``
    field, preserving its paragraph position.

    ``locator`` is a descriptor produced by :mod:`zoterocite.citeconvert`'s field
    scanner. It must contain either:

    * ``{"runs": [<w:r>, ...]}`` — the contiguous run elements of a complex field
      (``begin`` … ``end``), all siblings of one parent; or
    * ``{"sdt": <w:sdt>}`` — a content-control element to replace wholesale
      (Word built-in citations live inside an ``<w:sdt>`` with ``<w:citation/>``).

    The new Zotero field is inserted at the foreign field's exact position. With
    ``track=True`` the removed field is wrapped in ``<w:del>`` and the inserted
    Zotero field in ``<w:ins>`` (note: complex-field codes inside tracked-change
    wrappers are best finalised with one Accept in Word).

    Multiple ``keys`` group into ONE Zotero field (a foreign multi-source cite
    maps to one grouped Zotero citation).

    ``seed`` is a per-field discriminator folded into the field's ``citationID``.
    Zotero requires every citation field in a document to carry a UNIQUE
    citationID; deriving it from ``keys`` alone would make two fields citing the
    same item(s) collide (Zotero then merges/renumbers them on refresh). Callers
    converting many fields MUST pass a value unique to each field (e.g. its
    document-order index). When ``None`` the legacy keys-only seed is used.
    """
    date = date or now_iso()
    ensure_pref(doc, style)
    root = doc.tree(DOCUMENT)
    # Unique citationID per field: fold the caller's per-field ``seed`` into the
    # seed so two fields citing the same key set don't share a citationID.
    id_seed = [str(seed), *keys] if seed is not None else list(keys)
    field_xml = _build_zotero_field_xml(
        id_seed, keys, itemdata, uris,
        rendered=rendered, rendered_html=rendered_html, extras=extras,
    )
    new_runs = _runs(field_xml)

    if "sdt" in locator and locator["sdt"] is not None:
        target = locator["sdt"]
        parent = target.getparent()
        idx = parent.index(target)
        old_runs = [target]
    else:
        old_runs = list(locator["runs"])
        parent = old_runs[0].getparent()
        idx = parent.index(old_runs[0])

    if track:
        rev_id = _next_rev_id(root)
        # delete the foreign field (wrap as tracked deletion)
        d = _wrap_runs_as_del(old_runs, rev_id, author, date)
        ins = etree.Element(qn("w:ins"))
        ins.set(qn("w:id"), str(rev_id + 1))
        ins.set(qn("w:author"), author)
        ins.set(qn("w:date"), date)
        for r in new_runs:
            ins.append(r)
        d.addnext(ins)
    else:
        # remove the foreign field runs/sdt and splice the Zotero field in place
        for r in old_runs:
            parent.remove(r)
        for offset, r in enumerate(new_runs):
            parent.insert(idx + offset, r)
    return doc


# -- in-place ad-hoc-marker replacement (text -> live Zotero field) ----------
_FIELD_MASK = "\x00"
_W_INS = qn("w:ins")


def _text_run_segments(para: etree._Element, *, mask_fields: bool = True):
    """Yield ``(run, w:t, text)`` for each accepted-view text-bearing run of
    ``para`` in document order.

    Runs inside a ``<w:del>`` or ``<w:moveFrom>`` (gone from the accepted view)
    are skipped so we never re-strike struck text. Only the FIRST ``<w:t>`` of a
    run is read — that is where an ad-hoc author-year / [CITE] marker lives.

    ``mask_fields`` (default): text inside an existing field — between a complex
    field's ``begin`` and ``end``, or inside a ``<w:fldSimple>`` — is yielded as
    the same number of ``\\x00`` characters. Offsets stay aligned with the
    accepted text, but no anchor can match a field's result (a converted citation
    whose result equals its old text is never found again) or straddle one.
    """
    skip = (qn("w:del"), qn("w:moveFrom"))
    simple, fld, t_tag = qn("w:fldSimple"), qn("w:fldChar"), qn("w:t")
    depth = 0
    for r in para.iter(qn("w:r")):
        anc, dropped, in_simple = r.getparent(), False, False
        while anc is not None and anc is not para:
            if anc.tag in skip:
                dropped = True
                break
            if anc.tag == simple:
                in_simple = True
            anc = anc.getparent()
        if dropped:
            continue
        fc = r.find(fld)
        if fc is not None:
            kind = fc.get(qn("w:fldCharType"))
            if kind == "begin":
                depth += 1
            elif kind == "end" and depth:
                depth -= 1
        t = r.find(t_tag)
        if t is None:
            continue
        text = t.text or ""
        if mask_fields and (depth or in_simple):
            text = _FIELD_MASK * len(text)
        yield r, t, text


def _clone_text_run(run: etree._Element, text: str) -> etree._Element:
    """Deep-copy ``run`` (preserving ``rPr`` and ``xml:space``) and set its
    ``<w:t>`` text to ``text``. Used to split a run at a marker boundary."""
    new = copy.deepcopy(run)
    t = new.find(qn("w:t"))
    t.text = text
    t.set(qn("xml:space"), "preserve")
    return new


def _has_ancestor(el: etree._Element, stop: etree._Element, tags) -> bool:
    anc = el.getparent()
    while anc is not None and anc is not stop:
        if anc.tag in tags:
            return True
        anc = anc.getparent()
    return False


def _marker_level(run: etree._Element) -> etree._Element:
    """Where a NEW sibling ``<w:ins>`` for this marker run would live: the run's
    parent, or, for a run directly inside a ``<w:ins>``, that insertion's parent."""
    par = run.getparent()
    return par.getparent() if par.tag == _W_INS else par


def _isolate_marker_runs(para: etree._Element, anchor: str, start: Optional[int] = None,
                         *, allow_tracked: bool = False):
    """Locate the literal ``anchor`` substring within ``para``'s accepted-view text
    (:func:`_text_run_segments`, field results masked) and split boundary runs so
    the marker is isolated into its own list of ``<w:r>`` elements.

    ``start`` pins the occurrence: an offset into the joined segment text where
    ``anchor`` must begin (``LookupError`` otherwise). ``None`` (the default) takes
    the FIRST occurrence outside any existing field.

    Returns ``(marker_runs, parent, idx)``: the runs that together contain exactly
    ``anchor`` (left in the tree, in order), the first one's parent, and its index
    there. Each split fragment is a clone of its run, so ``rPr`` is preserved.

    Shapes (all checked BEFORE any mutation; a refusal is a ``LookupError`` and the
    tree is untouched):

    * all marker runs share one parent — always isolated (the ordinary case);
    * the marker crosses tracked-change containers — base runs and runs directly
      inside ``<w:ins>`` elements, all at ONE level (an earlier review corrected a
      year inside the citation) — isolated only with ``allow_tracked`` (the
      tracked splice can strike/drop each run where it is);
    * a marker inside a ``<w:moveTo>``, inside an existing field result, crossing
      a hyperlink / content-control boundary, or inside a nested tracked change
      is refused.
    """
    segs = list(_text_run_segments(para))
    if not segs:
        raise LookupError(f"anchor {anchor!r} not found in paragraph (no text runs)")
    full = "".join(s[2] for s in segs)
    raw = "".join((s[1].text or "") for s in segs)
    if start is None:
        start = full.find(anchor)
        if start < 0:
            if anchor in raw:
                raise LookupError(f"anchor {anchor!r} occurs only inside an existing field result")
            raise LookupError(f"anchor {anchor!r} not found in paragraph text")
    elif start < 0 or full[start:start + len(anchor)] != anchor:
        if start >= 0 and raw[start:start + len(anchor)] == anchor:
            raise LookupError(f"marker {anchor!r} is inside an existing field result")
        raise LookupError(f"anchor {anchor!r} not found at offset {start}")
    end = start + len(anchor)  # exclusive

    # Build (run, t, text, span_start, span_end) per segment over the joined text.
    spans = []
    pos = 0
    for run, t, text in segs:
        spans.append((run, t, text, pos, pos + len(text)))
        pos += len(text)
    overlap = [sp for sp in spans if not (sp[4] <= start or sp[3] >= end)]

    # Shape guard — MUST run before any mutation below (the strike/splice step
    # removes runs from their parents; an unsupported mixed-parent set would
    # otherwise fail MID-mutation). Parents are compared by identity (`is`).
    if any(_has_ancestor(sp[0], para, (qn("w:moveTo"),)) for sp in overlap):
        raise LookupError(f"marker {anchor!r} is inside a tracked move destination "
                          "(w:moveTo) — not supported")
    parents: List[etree._Element] = []
    for sp in overlap:
        par = sp[0].getparent()
        if not any(par is q for q in parents):
            parents.append(par)
    if len(parents) > 1 or allow_tracked:
        levels: List[etree._Element] = []
        for sp in overlap:
            lv = _marker_level(sp[0])
            if not any(lv is q for q in levels):
                levels.append(lv)
        if len(levels) != 1:
            raise LookupError(
                f"anchor {anchor!r} spans an inline container "
                "(hyperlink/sdt/smartTag) — not supported"
            )
        lv = levels[0]
        if lv.tag in _REV_TAGS or (lv is not para and _has_ancestor(lv, para, _REV_TAGS)):
            # A new w:ins here would nest inside another tracked change.
            raise LookupError(f"marker {anchor!r} is inside a nested tracked change — not supported")
        if len(parents) > 1 and not allow_tracked:
            raise LookupError(
                f"anchor {anchor!r} spans an inline container (a tracked insertion "
                "boundary) — not supported without track=True"
            )

    marker_runs: List[etree._Element] = []
    parent = para
    insert_idx = None

    for run, t, text, s0, s1 in overlap:
        run_parent = run.getparent()
        idx = run_parent.index(run)
        local_start = max(start, s0) - s0      # marker start within this run's text
        local_end = min(end, s1) - s0          # marker end within this run's text
        before = text[:local_start]
        mid = text[local_start:local_end]
        after = text[local_end:]

        # Replace this run with: [before?] marker-mid [after?]  (clones preserve rPr)
        run_parent.remove(run)
        cursor = idx
        if before:
            run_parent.insert(cursor, _clone_text_run(run, before))
            cursor += 1
        mid_run = _clone_text_run(run, mid)
        run_parent.insert(cursor, mid_run)
        marker_runs.append(mid_run)
        cursor += 1
        if after:
            run_parent.insert(cursor, _clone_text_run(run, after))

        if insert_idx is None:
            parent = run_parent
            insert_idx = run_parent.index(mid_run)

    if insert_idx is None:  # pragma: no cover - guarded by find above
        raise LookupError(f"anchor {anchor!r} could not be isolated in paragraph")
    return marker_runs, parent, insert_idx


def _field_result_text(runs: List[etree._Element]) -> str:
    """Visible text of a built field's result (the ``w:t`` runs between its
    ``separate`` and ``end`` fldChars)."""
    out, state = [], None
    for r in runs:
        fc = r.find(qn("w:fldChar"))
        if fc is not None:
            kind = fc.get(qn("w:fldCharType"))
            state = "result" if kind == "separate" else (None if kind == "end" else state)
            continue
        if state == "result":
            out.extend(t.text or "" for t in r.iter(qn("w:t")))
    return "".join(out)


def _plain_contiguous(runs: List[etree._Element], para: etree._Element) -> bool:
    """True when ``runs`` are consecutive siblings of ONE parent, none inside a
    tracked change (ins/del/moveFrom/moveTo) — an untracked edit there is invisible
    to both views when the new text equals the old."""
    parent = runs[0].getparent()
    if any(r.getparent() is not parent for r in runs):
        return False
    if any(_has_ancestor(r, para, _REV_TAGS) for r in runs):
        return False
    return all(a.getnext() is b for a, b in zip(runs, runs[1:]))


def _rejected_text(para: etree._Element) -> str:
    """The paragraph's rejected-view text: ``w:t``/``w:delText`` outside any
    insertion (``w:ins``/``w:moveTo``) — for the post-splice self-check."""
    hide = (_W_INS, qn("w:moveTo"), qn("mc:Fallback"))
    return "".join(n.text or "" for n in para.iter(qn("w:t"), qn("w:delText"))
                   if not _has_ancestor(n, para, hide))


def _own_insertion(run: etree._Element, para: etree._Element, author: str):
    """The active ``author``'s own pending ``<w:ins>`` holding ``run`` (the single
    owner is ``revisions._own_ins_host``: exact author, plain ins, not nested), or
    ``None``. A revisions build without that owner treats every insertion as
    foreign: the run is then struck instead of dropped (same views, more marks)."""
    from . import revisions
    owner = getattr(revisions, "_own_ins_host", None)
    return owner(run, para, author) if owner is not None else None


def _tracked_run_splice(root, para, marker_runs, new_runs, *, author: str, date: str) -> None:
    """Tracked replacement of a marker whose runs may sit in different tracked
    containers (all at one level, see :func:`_isolate_marker_runs`):

    * a run in the active author's OWN pending insertion is removed outright
      (rejecting it would remove it anyway; striking it would only add noise);
    * every other run — base text or another author's insertion — is struck in
      place (``revisions._wrap_run_as_deletion``; inside a foreign ``w:ins`` that
      is the valid ins>del nesting Word writes);
    * the field goes right after the marker: into the author's own insertion when
      the marker's LAST run is in one written by THIS op (same ``w:date``), else as
      a NEW ``w:ins`` after the struck span — an insertion holding the last run
      (foreign, or the author's own from an earlier round) is split there first, so
      the new ``w:ins`` is a sibling (never nested), lands exactly at the marker,
      and an earlier round's insertion keeps its own ``w:date``.
    """
    from . import revisions
    ids = _RevIds(root)
    own = [_own_insertion(r, para, author) for r in marker_runs]
    last = marker_runs[-1]
    new_ins = None
    if own[-1] is not None and own[-1].get(qn("w:date")) == date:
        prev = last
        for x in new_runs:
            prev.addnext(x)
            prev = x
    else:
        new_ins = etree.Element(_W_INS)
        new_ins.set(qn("w:id"), ids.next())
        new_ins.set(qn("w:author"), author)
        new_ins.set(qn("w:date"), date)
        for x in new_runs:
            new_ins.append(x)
        host = last.getparent()
        if host.tag == _W_INS:
            tail = list(last.itersiblings())
            if tail:
                frag = etree.Element(_W_INS, dict(host.attrib))
                frag.set(qn("w:id"), ids.next())
                for el in tail:
                    frag.append(el)
                host.addnext(frag)
            # Place the field now, between the host and its tail fragment: the
            # loop below may empty and remove the host (an own earlier-round
            # insertion holding only the marker), which would leave it detached.
            host.addnext(new_ins)
            new_ins = None
    for r, host in zip(marker_runs, own):
        if host is not None:
            host.remove(r)
            if len(host) == 0:
                host.getparent().remove(host)
        else:
            revisions._wrap_run_as_deletion(r, ids, author, date)
    if new_ins is not None:
        last.getparent().addnext(new_ins)


def _restore_children(para: etree._Element, snapshot: etree._Element) -> None:
    """Put ``para`` back to ``snapshot`` (a deep copy) IN PLACE, keeping the
    element's identity for callers that hold it."""
    for c in list(para):
        para.remove(c)
    for c in list(snapshot):
        para.append(c)


def _splice_field_at_marker(
    doc: Docx,
    root: etree._Element,
    para: etree._Element,
    anchor: str,
    field_xml: str,
    *,
    track: bool,
    author: str,
    date: str,
    start: Optional[int] = None,
    reason_out: Optional[List[str]] = None,
) -> bool:
    """Isolate the FIRST occurrence of ``anchor`` in ``para`` (or the one at
    segment-text offset ``start``, see :func:`_isolate_marker_runs`) and splice the
    pre-built ``field_xml`` (a complete begin/instr/sep/result/end field) in its
    place. Returns ``True`` on a successful splice, ``False`` (tree untouched; the
    reason appended to ``reason_out``) when the marker is absent or its shape is
    refused.

    Modes, chosen per occurrence:

    * **untracked swap** — the field's result text equals ``anchor`` and the
      marker is plain contiguous untracked runs: swapped without redlines (both
      views read the same text before and after), even when ``track`` is on;
    * ``track=False`` — the marker runs are removed and the field spliced there;
    * ``track=True``, contiguous runs of one non-insertion parent — the marker is
      struck as one ``w:del`` with the field in a ``w:ins`` right after it;
    * ``track=True``, any other shape (crossing an earlier review's ins/del, or
      inside an insertion) — :func:`_tracked_run_splice`.

    A post-splice self-check compares the accepted text (the marker replaced by
    the field result, nothing else) and, for every mode that must not change it,
    the rejected text; a mismatch restores the paragraph and refuses.

    Shared inner step of every marker-to-field writer. After a splice the marker
    is gone and the field's result is masked from matching (see
    :func:`_text_run_segments`), so a re-scan lands on the next genuine
    occurrence and never re-matches the one just converted.
    """
    snapshot = copy.deepcopy(para)
    acc_before = "".join(s[2] for s in _text_run_segments(para, mask_fields=False))
    rej_before = _rejected_text(para)
    try:
        marker_runs, parent, idx = _isolate_marker_runs(para, anchor, start, allow_tracked=track)
    except LookupError as exc:
        if reason_out is not None:
            reason_out.append(str(exc))
        return False
    first = marker_runs[0]
    pos = 0
    for run, _t, text in _text_run_segments(para, mask_fields=False):
        if run is first:
            break
        pos += len(text)
    new_runs = _runs(field_xml)
    result = _field_result_text(new_runs)
    swap = result == anchor and _plain_contiguous(marker_runs, para)
    if swap or not track:
        for r in marker_runs:
            parent.remove(r)
        for offset, r in enumerate(new_runs):
            parent.insert(idx + offset, r)
    elif (parent.tag != _W_INS and all(r.getparent() is parent for r in marker_runs)
          and all(a.getnext() is b for a, b in zip(marker_runs, marker_runs[1:]))):
        # Strike the marker (one tracked deletion), then splice the field as a
        # tracked insertion immediately after it.
        rev_id = _next_rev_id(root)
        d = _wrap_runs_as_del(marker_runs, rev_id, author, date)
        ins = etree.Element(_W_INS)
        ins.set(qn("w:id"), str(rev_id + 1))
        ins.set(qn("w:author"), author)
        ins.set(qn("w:date"), date)
        for r in new_runs:
            ins.append(r)
        d.addnext(ins)
    else:
        _tracked_run_splice(root, para, marker_runs, new_runs, author=author, date=date)
    acc_after = "".join(s[2] for s in _text_run_segments(para, mask_fields=False))
    ok = acc_after == acc_before[:pos] + result + acc_before[pos + len(anchor):]
    if ok and (track or swap):
        ok = _rejected_text(para) == rej_before
    if not ok:
        _restore_children(para, snapshot)
        if reason_out is not None:
            reason_out.append(f"post-splice check failed for {anchor!r} (accepted/rejected "
                              "view would change beyond the marker); left unchanged")
        return False
    return True


def _segment_start_for(para: etree._Element, offset: int, anchor: str,
                       superscript: bool = False):
    """Translate ``offset`` (into ``paragraph_text(para)``, the accepted view
    refextract scans) to the matching offset in :func:`_text_run_segments`' joined
    text, which :func:`_isolate_marker_runs` splits on. The two views differ: tabs
    and breaks count in the first only; ``w:moveFrom`` / ``mc:Fallback`` text and a
    run's second ``w:t`` count in one only. Returns ``(segment_offset, None)`` or
    ``(None, reason)``.

    The marker's characters must all come from ``w:t`` elements that are each the
    FIRST ``w:t`` of a ``w:r`` (the only text the splitter sees); with
    ``superscript`` every such run must also carry ``vertAlign=superscript``.
    :func:`_isolate_marker_runs` then re-checks that the segment text at the
    returned offset IS ``anchor``, so a moved/fallback run between marker pieces
    refuses instead of striking the wrong text."""
    t_tag, tab_tag, r_tag = qn("w:t"), qn("w:tab"), qn("w:r")
    br_tags = (qn("w:br"), qn("w:cr"))
    drop_tags = (qn("w:del"), qn("w:moveFrom"), qn("mc:Fallback"))

    def _dropped(node) -> bool:
        anc = node.getparent()
        while anc is not None and anc is not para:
            if anc.tag in drop_tags:
                return True
            anc = anc.getparent()
        return False

    pieces: List[str] = []
    owners: List[Optional[etree._Element]] = []   # per char: its w:t, or None (tab/br)
    for node in para.iter(t_tag, tab_tag, *br_tags):
        if _dropped(node):
            continue
        if node.tag == t_tag:
            text = node.text or ""
            pieces.append(text)
            owners.extend([node] * len(text))
        else:
            pieces.append("\t" if node.tag == tab_tag else "\n")
            owners.append(None)
    full = "".join(pieces)
    if full != paragraph_text(para):
        return None, "paragraph text could not be mapped to its runs"
    end = offset + len(anchor)
    if offset < 0 or full[offset:end] != anchor:
        return None, "marker not found at its recorded location"
    covering: List[etree._Element] = []
    for t in owners[offset:end]:
        if t is None:
            return None, "marker spans a tab or line break"
        if not covering or covering[-1] is not t:
            covering.append(t)
    for t in covering:
        run = t.getparent()
        if run is None or run.tag != r_tag or run.find(t_tag) is not t:
            return None, "marker text is not in a plain text run"
        if superscript:
            va = run.find(f"{qn('w:rPr')}/{qn('w:vertAlign')}")
            if va is None or (va.get(qn("w:val")) or "").lower() != "superscript":
                return None, "marker run is no longer superscript"
    first_t = covering[0]
    first_run = first_t.getparent()
    # Characters of first_t before the marker (first_t may hold earlier text too).
    t_pos = offset
    while t_pos > 0 and owners[t_pos - 1] is first_t:
        t_pos -= 1
    pos = 0
    for run, t, text in _text_run_segments(para):
        if run is first_run:
            return pos + (offset - t_pos), None
        pos += len(text)
    return None, "marker run not found among the paragraph's text runs"


def replace_occurrences_with_zotero_fields(
    doc: Docx,
    occurrences: List[dict],
    *,
    style: str = DEFAULT_STYLE,
    track: bool = False,
    author: str = "zotero-word-cite",
    date: Optional[str] = None,
    spread: bool = True,
) -> List[Optional[str]]:
    """Convert EXACTLY the given marker occurrences, located by position, into
    live Zotero ``ZOTERO_ITEM`` fields. The location-scoped counterpart of
    :func:`replace_all_text_with_zotero_field` (which converts every occurrence
    of a text): use it when only some same-text occurrences are citations — a
    list enumerator "(2)" in one paragraph while "(2)" cites reference 2 in
    another — or when the marker text is not a safe search anchor at all (a
    superscript "12" also matches inside "2012").

    Each occurrence is a dict:

    * ``para`` — index into :func:`paras.iter_paragraphs` of the document (the
      enumeration :func:`refextract.extract_references` reports);
    * ``offset`` — where ``anchor`` starts in that paragraph's accepted-view
      :func:`paras.paragraph_text`;
    * ``anchor`` — the marker text, re-verified at that location;
    * ``keys`` / ``itemdata`` / ``uris`` and optional ``rendered`` /
      ``rendered_html`` / ``extras`` — as for :func:`replace_text_with_zotero_field`;
    * ``superscript`` (optional) — the marker's runs must still be superscript.

    Returns a list aligned with ``occurrences``: ``None`` where the occurrence
    was converted, else the reason it was refused (the document is untouched for
    a refused occurrence). Paragraph indices are resolved once, before any
    mutation, and each paragraph's occurrences are spliced RIGHT TO LEFT so an
    earlier offset stays valid after a later splice. Each field gets its own
    citationID (seeded by anchor + location).

    A marker crossing an earlier review's tracked changes converts with
    ``track=True`` (see :func:`_splice_field_at_marker`); a marker inside an
    existing field's result is refused. With ``track=True`` the whole call shares
    ONE op date and its marks are then spread into distinct, ordered ``w:date``s
    (one event per paragraph); marks that existed before are never re-stamped.
    ``spread=False`` skips that trailing spread: for a caller that makes several
    writes under one pinned ``date`` and spreads its whole op once at the end
    (``unify.apply_unification``)."""
    date = date or now_iso()
    root = doc.tree(DOCUMENT)
    paras = iter_paragraphs(root)
    pre_marks = _snapshot_marks(doc, author) if track and spread else None
    status: List[Optional[str]] = ["not attempted"] * len(occurrences)
    by_para: dict = {}
    for i, occ in enumerate(occurrences):
        pi = occ.get("para")
        if not isinstance(pi, int) or not 0 <= pi < len(paras):
            status[i] = "paragraph index out of range"
            continue
        if not occ.get("anchor"):
            status[i] = "empty anchor"
            continue
        if not isinstance(occ.get("offset"), int):
            status[i] = "no marker offset"
            continue
        by_para.setdefault(pi, []).append(i)
    prefs_done = False
    for pi, idxs in by_para.items():
        para = paras[pi]
        idxs.sort(key=lambda i: occurrences[i]["offset"], reverse=True)
        floor = None   # start of the occurrence spliced just before (to its right)
        for i in idxs:
            occ = occurrences[i]
            off, anchor = occ["offset"], occ["anchor"]
            if floor is not None and off + len(anchor) > floor:
                status[i] = "overlaps another marker"
                continue
            seg_start, reason = _segment_start_for(
                para, off, anchor, bool(occ.get("superscript")))
            if reason:
                status[i] = reason
                continue
            if not prefs_done:
                ensure_pref(doc, style)
                prefs_done = True
            field_xml = _build_zotero_field_xml(
                [anchor, f"p{pi}", f"o{off}"], occ["keys"], occ["itemdata"], occ["uris"],
                rendered=occ.get("rendered", "(citation)"),
                rendered_html=occ.get("rendered_html"), extras=occ.get("extras"),
            )
            why: List[str] = []
            if _splice_field_at_marker(doc, root, para, anchor, field_xml,
                                       track=track, author=author, date=date,
                                       start=seg_start, reason_out=why):
                status[i] = None
                floor = off
            else:
                status[i] = why[0] if why else "marker could not be isolated"
    if track and spread and any(st is None for st in status):
        _spread_op_marks(doc, author, date, pre_marks)
    return status


def replace_text_with_zotero_field(
    doc: Docx,
    anchor: str,
    keys: List[str],
    *,
    itemdata: List[dict],
    uris: List[str],
    rendered: str = "(citation)",
    rendered_html: Optional[str] = None,
    extras: Optional[List[dict]] = None,
    style: str = DEFAULT_STYLE,
    track: bool = False,
    author: str = "zotero-word-cite",
    date: Optional[str] = None,
) -> Docx:
    """Replace an ad-hoc citation marker (the literal ``anchor`` text, e.g.
    ``"(Smith et al., 2020)"`` or ``"[CITE: ...]"``) *in place* with a live
    Zotero ``ZOTERO_ITEM CSL_CITATION`` field, where the marker was.

    Unlike :func:`insert_citation` (which appends the field to the paragraph end
    and leaves the marker behind), this strikes the marker and splices the field
    at the marker's position. The field is built identically to
    :func:`insert_citation` via :func:`_build_zotero_field_xml` (same prefs
    registration, ``item_uri``/itemdata/rendered wiring); multiple ``keys`` group
    into ONE field.

    With ``track=True`` the struck marker runs are wrapped in a ``<w:del>``
    (``<w:t>`` → ``<w:delText>``, via :func:`_wrap_runs_as_del`) and the inserted
    field is wrapped in a ``<w:ins>`` (the field is *inserted*, not deleted, so
    its ``<w:instrText>`` stays normal — only deleted field code becomes
    ``<w:delInstrText>``). With ``track=False`` the marker runs are removed and
    the field spliced in their place.

    The result round-trips (``validate`` ok): the accepted view shows the field's
    rendered text where the marker was (and not the marker); the rejected view
    shows the original marker text (and not the field).

    Existing field results are not searched (:func:`_find_marker_paragraph`), so
    a marker already converted to a field whose result equals the old text is not
    found again. A marker crossing an earlier review's tracked changes converts
    with ``track=True``; when the field's result equals ``anchor`` over plain
    untracked runs the swap is untracked (see :func:`_splice_field_at_marker`).

    Raises ``LookupError`` if ``anchor`` does not match exactly one paragraph
    outside field results, or its shape is refused (the reason is the message;
    the document is unchanged).
    """
    date = date or now_iso()
    root = doc.tree(DOCUMENT)
    # Locate the marker BEFORE any mutation (prefs/field), so a missing anchor
    # raises LookupError without leaving the document half-modified.
    para = _find_marker_paragraph(root, anchor)  # raises LookupError unless exactly 1
    field_xml = _build_zotero_field_xml(
        [anchor], keys, itemdata, uris,
        rendered=rendered, rendered_html=rendered_html, extras=extras,
    )
    why: List[str] = []
    if not _splice_field_at_marker(
        doc, root, para, anchor, field_xml,
        track=track, author=author, date=date, reason_out=why,
    ):
        raise LookupError(why[0] if why else f"anchor {anchor!r} could not be isolated in paragraph")
    ensure_pref(doc, style)
    return doc


def _masked_contains(para: etree._Element, anchor: str) -> bool:
    return anchor in "".join(s[2] for s in _text_run_segments(para))


def _find_marker_paragraph(root: etree._Element, anchor: str,
                           index: Optional[ParaIndex] = None) -> etree._Element:
    """The ONE paragraph holding ``anchor`` OUTSIDE existing field results
    (:func:`paras.ParaIndex` candidates, then masked by
    :func:`_text_run_segments`; a TOC entry loses a tie to a body paragraph)."""
    index = index or ParaIndex(root)
    hits = index.find_all(anchor)
    live = [p for p in hits if _masked_contains(p, anchor)]
    if len(live) > 1:
        body = [p for p in live if not index.is_toc(p)]
        if len(body) == 1:
            live = body
    if len(live) != 1:
        extra = (f" ({len(hits) - len(live)} more only inside existing field results)"
                 if len(hits) > len(live) else "")
        raise LookupError(f"anchor {anchor!r} matched {len(live)} paragraphs (need exactly 1){extra}")
    return live[0]


def replace_all_text_with_zotero_field(
    doc: Docx,
    anchor: str,
    keys: List[str],
    *,
    itemdata: List[dict],
    uris: List[str],
    rendered: str = "(citation)",
    rendered_html: Optional[str] = None,
    extras: Optional[List[dict]] = None,
    style: str = DEFAULT_STYLE,
    track: bool = False,
    author: str = "zotero-word-cite",
    date: Optional[str] = None,
    index: Optional[ParaIndex] = None,
) -> int:
    """Replace EVERY occurrence of the literal ``anchor`` text across the whole
    document with the SAME live Zotero ``ZOTERO_ITEM CSL_CITATION`` field, in
    place. Returns the number of occurrences converted.

    This is the occurrence-scoped generalisation of
    :func:`replace_text_with_zotero_field`, which targets a marker that occurs in
    exactly ONE paragraph. A numeric bibliography marker like ``"(12)"`` recurs
    across many paragraphs (and can repeat within one paragraph), and — because in
    a numbered style the marker text is a *function* of the cited item(s)
    (``"(12)"`` always means reference 12) — every occurrence must become the same
    field. So we iterate ALL body paragraphs (:func:`find_paragraphs`) and, within
    each, repeatedly isolate-and-splice the FIRST remaining occurrence until none
    is left: once an occurrence is converted to a field its plain ``<w:t>`` text
    no longer matches, so re-scanning lands on the next genuine occurrence.

    Boundary safety: ``anchor`` is the FULL delimited token (e.g. ``"(12)"``
    including its parens/brackets), so a literal substring match can never fire
    inside ``"(112)"``, ``"(12,15)"`` or ``"(5-7)"`` — the closing delimiter must
    immediately follow. Pass the marker's own ``text`` (as extracted by
    refextract's numeric-cite regex) to inherit that guarantee; do NOT pass a bare
    number.

    Builds the field ONCE (identical citationID for all occurrences — Zotero
    re-derives unique ids on Refresh from the citationItems) and registers prefs
    once. With ``track=True`` each converted occurrence is a struck marker
    (``<w:del>``) + inserted field (``<w:ins>``); the document round-trips.
    """
    if not anchor:
        return 0
    _effective_rendered = rendered or ""
    if rendered_html:
        _effective_rendered = _strip_html(rendered_html) or _effective_rendered
    if anchor in _effective_rendered:
        # SAFETY: the inserted field's result run is itself <w:t>-bearing text.
        # If ``rendered`` (or the plain-text form of ``rendered_html``) contains
        # ``anchor``, the per-paragraph rescan would re-isolate the field's OWN
        # result and splice a nested field — a runaway that double-converts (one
        # spurious nest per occurrence). Refuse loudly rather than corrupt;
        # callers pass a NEUTRAL token (e.g. "(citation)").
        raise ValueError(
            f"rendered text {_effective_rendered!r} contains the anchor "
            f"{anchor!r}: the field's own result run would be re-isolated on "
            "rescan. Pass a neutral rendered token (e.g. '(citation)')."
        )
    date = date or now_iso()
    root = doc.tree(DOCUMENT)
    ensure_pref(doc, style)
    field_xml = _build_zotero_field_xml(
        [anchor], keys, itemdata, uris,
        rendered=rendered, rendered_html=rendered_html, extras=extras,
    )
    pre_marks = _snapshot_marks(doc, author) if track else None
    converted = 0
    # When a caller converts MANY distinct tokens against one document (cite-link's
    # token loop), it can pass a prebuilt ``index`` so the whole-doc paragraph scan
    # happens ONCE for all tokens instead of per token (O(tokens*doc) -> O(doc)).
    # Safe because each conversion replaces only THIS anchor's occurrences with a
    # NEUTRAL ``rendered`` placeholder (the guard above refuses ``anchor in rendered``)
    # that contains no other distinct token, so a later token's match-set is unchanged;
    # and the per-paragraph re-scan below still reads each paragraph's LIVE text.
    target_paras = index.find_all(anchor) if index is not None else find_paragraphs(root, anchor)
    for para in target_paras:
        # Re-scan this paragraph until no occurrence remains. A hard cap on
        # iterations (one per occurrence in the paragraph's current text, +1)
        # makes a non-progressing splice fail safe instead of looping forever.
        from .paras import paragraph_text
        guard = paragraph_text(para).count(anchor) + 1
        while guard > 0:
            guard -= 1
            if not _splice_field_at_marker(
                doc, root, para, anchor, field_xml,
                track=track, author=author, date=date,
            ):
                break
            converted += 1
    if track and converted:
        _spread_op_marks(doc, author, date, pre_marks)
    return converted
