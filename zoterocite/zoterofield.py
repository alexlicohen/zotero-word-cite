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
from pathlib import Path
from typing import List, Optional

from lxml import etree

from . import csldb
from .docxio import DOCUMENT, Docx
from .ooxml import NS, now_iso, qn
from .paras import ParaIndex, find_paragraph, find_paragraphs, get_body

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


def _field_runs(instr: str, result_xml: Optional[str]) -> str:
    """XML run string for a Word complex field. ``result_xml`` is the visible
    result runs (Zotero overwrites on refresh); ``None`` => code-only."""
    s = '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
    s += f'<w:r><w:instrText xml:space="preserve"> {_esc(instr)} </w:instrText></w:r>'
    if result_xml is not None:
        s += '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        s += result_xml
    s += '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
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


def append_bibliography_field(
    doc: Docx,
    *,
    style: str = DEFAULT_STYLE,
    bib_rendered: str = "",
    heading: Optional[str] = None,
    track: bool = False,
    author: str = "zotero-word-cite",
    date: Optional[str] = None,
) -> bool:
    """Append a Zotero ``ZOTERO_BIBL`` bibliography field at the end of the body
    (before the trailing ``sectPr``), so a single Zotero Refresh renders the
    bibliography from the cited items. Idempotent: returns ``False`` without
    writing if a ``ZOTERO_BIBL`` field already exists.

    Registers the CSL ``style`` prefs (so Refresh knows the style). The field
    starts code-only (``None`` result) unless ``bib_rendered`` is given; Zotero
    fills the rendered list on Refresh. An optional ``heading`` paragraph (e.g.
    "References") is inserted before the field. With ``track=True`` the inserted
    paragraph(s) are wrapped as a tracked insertion.

    This is intentionally ADDITIVE — it never deletes a manual bibliography (a
    caller that wants the manual block removed must do so explicitly, and only
    when the block is unambiguously located).
    """
    root = doc.tree(DOCUMENT)
    if has_bibliography_field(root):
        return False
    ensure_pref(doc, style)
    date = date or now_iso()
    body = get_body(root)
    sect = body.find(qn("w:sectPr"))

    new_paras: List[etree._Element] = []
    if heading:
        new_paras.append(_paragraph(_plain_run(heading)))
    bib_p = _paragraph(_field_runs(
        _BIBL_INSTR, _plain_run(bib_rendered) if bib_rendered else None))
    new_paras.append(bib_p)

    if track:
        rev_id = _next_rev_id(root)
        for p in new_paras:
            # Wrap each inserted paragraph's runs in <w:ins>; the paragraph mark
            # itself is marked inserted via <w:rPr><w:ins/></w:rPr> in <w:pPr>.
            ins = etree.Element(qn("w:ins"))
            ins.set(qn("w:id"), str(rev_id)); rev_id += 1
            ins.set(qn("w:author"), author); ins.set(qn("w:date"), date)
            for r in list(p):
                p.remove(r); ins.append(r)
            p.append(ins)
            ppr = etree.SubElement(p, qn("w:pPr"))
            p.remove(ppr); p.insert(0, ppr)
            rpr = etree.SubElement(ppr, qn("w:rPr"))
            mark_ins = etree.SubElement(rpr, qn("w:ins"))
            mark_ins.set(qn("w:id"), str(rev_id)); rev_id += 1
            mark_ins.set(qn("w:author"), author); mark_ins.set(qn("w:date"), date)

    for p in new_paras:
        if sect is not None:
            sect.addprevious(p)
        else:
            body.append(p)
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
) -> Path:
    """High-level: resolve item(s) in the Zotero group library and insert ONE live
    citation field at ``anchor`` (grouping multiple items).

    Provide one of: ``items`` (list of ``{"key","locator?","label?","prefix?",
    "suffix?"}`` for full control incl. page locators), ``keys`` (list), or
    ``doi`` (single). Fetches CSL-JSON + URIs + a placeholder marker.
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
    if len(keys) > 1:
        # A grouped citation field renders as ONE marker (e.g. "(1,2)"/"(1-3)");
        # Zotero's per-item ``include=citation`` API cannot synthesise that, and
        # "; ".join fabricates a separator Zotero never produces, corrupting the
        # cached display + the existing_renderings dedup guard. Store a neutral
        # placeholder — the live field carries the real citationItems and
        # Word/Zotero regenerate the true grouped marker on refresh. (Single-key
        # path is unchanged: a one-element join has no separator.)
        rendered_html = "(citation)"
    else:
        rendered_html = "; ".join(
            zotero.formatted_citations(keys, style=style, kind="citation", strip=False)) or None
    bib = "\n".join(zotero.formatted_citations(keys, style=style, kind="bib")) if add_bibliography else ""
    doc = Docx(path)
    insert_citation(doc, anchor, keys, itemdata=itemdata, uris=uris, extras=extras,
                    rendered_html=rendered_html, style=style, add_bibliography=add_bibliography,
                    bib_rendered=bib)
    return doc.save(Path(out) if out else Path(path))


# -- scanning / broken-link detection ---------------------------------------
def _field_codes(root) -> List[str]:
    """Concatenated instrText for each Word complex field, in document order
    (handles field codes split across runs and nested fields via a stack)."""
    codes: List[str] = []
    stack: List[dict] = []
    for r in root.iter(qn("w:r")):
        fld = r.find(qn("w:fldChar"))
        if fld is not None:
            t = fld.get(qn("w:fldCharType"))
            if t == "begin":
                stack.append({"code": "", "sep": False})
            elif t == "separate" and stack:
                stack[-1]["sep"] = True
            elif t == "end" and stack:
                codes.append(stack.pop()["code"])
            continue
        instr = r.find(qn("w:instrText"))
        if instr is not None and stack and not stack[-1]["sep"]:
            stack[-1]["code"] += instr.text or ""
    return codes


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


def _next_rev_id(root) -> int:
    mx = 0
    for el in root.iter():
        if el.tag in (qn("w:ins"), qn("w:del")):
            try:
                mx = max(mx, int(el.get(qn("w:id"), 0)))
            except (TypeError, ValueError):
                pass
    return mx + 1


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
        # Use ``iter`` (not ``findall``) so text/instr nested below the run — e.g.
        # under a Word built-in citation's ``<w:sdtContent>`` when ``r`` is the
        # ``<w:sdt>`` itself — is converted to its tracked-deletion variant. With
        # ``findall`` only the run's DIRECT children were touched, leaving a LIVE
        # ``<w:instrText>``/``<w:t>`` (a live field + original text) inside the
        # ``<w:del>`` for the sdt carrier.
        for t in r.iter(qn("w:t")):
            t.tag = qn("w:delText")
        for it in r.iter(qn("w:instrText")):
            it.tag = qn("w:delInstrText")
        d.append(r)
    parent.insert(idx, d)
    return d


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
def _text_run_segments(para: etree._Element):
    """Yield ``(run, w:t, text)`` for each *non-deleted* text-bearing run of
    ``para`` in document order, reassembling the visible (accepted-view) text.

    Runs already inside a ``<w:del>`` (a pending deletion) are skipped so we
    never try to re-strike text that's already struck. Only ``<w:t>``-bearing
    runs are considered — that is where an ad-hoc author-year / [CITE] marker
    lives; field runs and breaks are not part of a literal text marker.
    """
    del_tag = qn("w:del")
    for r in para.iter(qn("w:r")):
        t = r.find(qn("w:t"))
        if t is None:
            continue
        anc = r.getparent()
        deleted = False
        while anc is not None and anc is not para:
            if anc.tag == del_tag:
                deleted = True
                break
            anc = anc.getparent()
        if deleted:
            continue
        yield r, t, (t.text or "")


def _clone_text_run(run: etree._Element, text: str) -> etree._Element:
    """Deep-copy ``run`` (preserving ``rPr`` and ``xml:space``) and set its
    ``<w:t>`` text to ``text``. Used to split a run at a marker boundary."""
    new = copy.deepcopy(run)
    t = new.find(qn("w:t"))
    t.text = text
    t.set(qn("xml:space"), "preserve")
    return new


def _isolate_marker_runs(para: etree._Element, anchor: str):
    """Locate the literal ``anchor`` substring within ``para``'s visible text and
    split boundary runs so the marker is isolated into its own contiguous list of
    sibling ``<w:r>`` elements.

    Returns ``(marker_runs, parent, idx)`` where ``marker_runs`` are the runs that
    together contain exactly ``anchor`` (left in the tree, in order), ``parent`` is
    their shared parent element, and ``idx`` is the position of the first marker
    run in ``parent`` (where the replacement field is spliced).

    Run-splitting: the run holding the marker's start is replaced by up to two
    runs (the text before the marker, then the marker head); likewise the run
    holding the marker's end is split into (marker tail, text after). Each split
    fragment is a clone of the original run, so its ``rPr`` formatting is
    preserved on every piece. Runs wholly inside the marker are reused as-is.

    Raises ``LookupError`` if ``anchor`` is not found as a contiguous substring of
    the paragraph's visible text (mirrors :func:`paras.find_paragraph`).
    """
    segs = list(_text_run_segments(para))
    if not segs:
        raise LookupError(f"anchor {anchor!r} not found in paragraph (no text runs)")
    full = "".join(s[2] for s in segs)
    start = full.find(anchor)
    if start < 0:
        raise LookupError(f"anchor {anchor!r} not found in paragraph text")
    end = start + len(anchor)  # exclusive

    # Build (run, t, text, span_start, span_end) per segment over the joined text.
    spans = []
    pos = 0
    for run, t, text in segs:
        spans.append((run, t, text, pos, pos + len(text)))
        pos += len(text)

    # Cross-container guard — MUST run before any mutation below. _text_run_segments
    # descends through <w:ins>/<w:hyperlink>/<w:sdt> (para.iter(w:r)), so when a
    # marker straddles a container boundary the runs it overlaps sit under DIFFERENT
    # parents. The strike/splice step (_wrap_runs_as_del, _splice_field_at_marker)
    # removes every marker run from a SINGLE parent, so a mixed-parent set raises an
    # lxml ValueError MID-mutation, corrupting the doc. Detect it here while the tree
    # is still pristine and refuse cleanly with LookupError (which
    # _splice_field_at_marker turns into a skippable no-op) instead. Uses the EXACT
    # same "entirely outside" predicate as the mutation loop, so the overlap set it
    # checks is identical to the set that loop would touch. Parents are compared by
    # identity (`is`) — lxml's proxy caching keeps this reliable while refs are held;
    # id() would not be.
    overlap_parents: List[etree._Element] = []
    for run, _t, _text, s0, s1 in spans:
        if s1 <= start or s0 >= end:
            continue  # run entirely outside the marker
        par = run.getparent()
        if not any(par is q for q in overlap_parents):
            overlap_parents.append(par)
    if len(overlap_parents) > 1:
        raise LookupError(
            f"anchor {anchor!r} spans an inline container "
            "(ins/hyperlink/sdt) — not supported"
        )

    marker_runs: List[etree._Element] = []
    parent = para
    insert_idx = None

    for run, t, text, s0, s1 in spans:
        if s1 <= start or s0 >= end:
            continue  # run entirely outside the marker
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
) -> bool:
    """Isolate the FIRST occurrence of ``anchor`` in ``para`` and splice the
    pre-built ``field_xml`` (a complete begin/instr/sep/result/end field) in its
    place, striking the marker. Returns ``True`` on a successful splice, ``False``
    if ``anchor`` is not present in this paragraph's visible text.

    Shared inner step of :func:`replace_text_with_zotero_field` (single occurrence)
    and :func:`replace_all_text_with_zotero_field` (paragraph-iterating loop). It
    converts exactly ONE occurrence so a caller can re-scan and convert the next:
    after a splice the marker text is gone (struck or removed) and replaced by the
    field's result runs, so a fresh :func:`_isolate_marker_runs` lands on the next
    genuine occurrence and never re-matches the one just converted.
    """
    try:
        marker_runs, parent, idx = _isolate_marker_runs(para, anchor)
    except LookupError:
        return False
    new_runs = _runs(field_xml)
    if track:
        rev_id = _next_rev_id(root)
        # Strike the marker (tracked deletion), then splice the field as a
        # tracked insertion immediately after it (so document order is
        # del-marker then ins-field at the marker's former position).
        d = _wrap_runs_as_del(marker_runs, rev_id, author, date)
        ins = etree.Element(qn("w:ins"))
        ins.set(qn("w:id"), str(rev_id + 1))
        ins.set(qn("w:author"), author)
        ins.set(qn("w:date"), date)
        for r in new_runs:
            ins.append(r)
        d.addnext(ins)
    else:
        for r in marker_runs:
            parent.remove(r)
        for offset, r in enumerate(new_runs):
            parent.insert(idx + offset, r)
    return True


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

    Raises ``LookupError`` if ``anchor`` does not match exactly one paragraph, or
    is not found as a contiguous substring of that paragraph's visible text.
    """
    date = date or now_iso()
    root = doc.tree(DOCUMENT)
    # Locate the marker BEFORE any mutation (prefs/field), so a missing anchor
    # raises LookupError without leaving the document half-modified.
    para = find_paragraph(root, anchor)  # raises LookupError unless exactly 1
    ensure_pref(doc, style)
    field_xml = _build_zotero_field_xml(
        [anchor], keys, itemdata, uris,
        rendered=rendered, rendered_html=rendered_html, extras=extras,
    )
    if not _splice_field_at_marker(
        doc, root, para, anchor, field_xml,
        track=track, author=author, date=date,
    ):  # pragma: no cover — find_paragraph already guaranteed a contiguous match
        raise LookupError(f"anchor {anchor!r} could not be isolated in paragraph")
    return doc


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
    return converted
