"""Detect and convert *foreign* citations to live Zotero fields.

A grant document assembled over years often carries citations from several
reference managers: Zotero (the target), EndNote, Mendeley, Word's built-in
citation tool, and plain typed references. This module:

1. **Detects / classifies** every citation field by its managing system
   (:func:`classify_citation_sources`), extracting embedded metadata where the
   format carries it.
2. **Converts** the convertible foreign fields (EndNote / Mendeley / Word) into
   live ``ZOTERO_ITEM CSL_CITATION`` fields bound to the user's Zotero group
   library (:func:`convert_to_zotero`), with DOI/title matching and dedup so the
   whole document becomes Zotero-managed and consistent.

What is auto-converted vs report-only
-------------------------------------
* **Auto-convertible (field-based):** EndNote (``ADDIN EN.CITE`` — DOI in
  ``<electronic-resource-num>``, title/authors/year in XML), Mendeley (bare
  ``ADDIN CSL_CITATION`` CSL-JSON), Word built-in (``CITATION Tag`` + the
  ``b:Sources`` store). These carry structured metadata we can match against the
  library by DOI then normalized title.
* **Report-only (never auto-converted):** manual / typed references. Their text
  is unstructured; a wrong silent match would corrupt the bibliography. We surface
  them (with a best-effort DOI/title and ``low_confidence``) for the user to
  handle. Word ``CITATION`` fields whose source record lacks a DOI *and* whose
  title can't be resolved are likewise reported, not guessed.

No network in the detection path; conversion calls the Zotero client, which tests
monkeypatch.
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from lxml import etree

from . import textpatterns
from .citecheck import _normalise_doi
from .docxio import DOCUMENT, Docx
from .findings import Finding
from .ooxml import NS, qn
from .paras import iter_paragraphs, paragraph_text
from .sections import REFERENCES_HEADING_RE, split_body_vs_references
from .zoterofield import replace_field_with_zotero, _field_codes

W = NS["w"]
B_NS = "http://schemas.openxmlformats.org/officeDocument/2006/bibliography"

# Shared bare-DOI body pattern (byte-identical to the old local copy — a perfect
# single-source, no behaviour change).  See zoterocite.textpatterns.DOI_BARE_RE.
_DOI_RE = textpatterns.DOI_BARE_RE
# Shared standalone-References heading detector (recall-superset of the old local
# copy: adds the "citations"/"cited literature" synonyms and \s+ multi-word
# tolerance).  See zoterocite.sections.REFERENCES_HEADING_RE.
_REF_HEADING_RE = REFERENCES_HEADING_RE
# A line that looks like a reference-list entry: "1. ...", "[1] ...", "(1) ...",
# or an author-year line ("Smith J, et al. 2019. ...").  Uses the shared
# textpatterns.NUMBERED_REF_RE (recall-superset of the old local copy — adds the
# "(1)" and spaced-bracket forms; verified behaviour-preserving for the manual-
# reference detector by the test suite).
_NUMBERED_REF_RE = textpatterns.NUMBERED_REF_RE
# Genuine INLINE citation shapes in body text — parenthetical author-year markers
# such as "(Smith et al., 2020)" or "(Smith, 2020)".  Requires a parenthetical
# opening so plain narrative sentences that merely contain a year (e.g. "The
# trial began in 2015...") do NOT trigger.  A DOI/PMID in body text is caught
# upstream via _DOI_RE in _detect_manual_refs.
_INLINE_CITE_RE = re.compile(
    r"\(\s*[A-Z][A-Za-z’\-]+(?:\s+et\s+al\.?)?(?:,\s*[A-Z][A-Za-z’\-]+)*"
    r",?\s*(19|20)\d{2}\s*\)",
    re.IGNORECASE,
)

MANAGERS = ("zotero", "mendeley", "endnote", "word", "manual")
CONVERTIBLE = ("endnote", "mendeley", "word")

# Sentinel: a confirmed library miss (distinct from "not yet looked up").
_MISS = object()


# ===========================================================================
# Position-aware field scanner (complex fields, fldSimple, sdt content controls)
# ===========================================================================

def _instr_of_fldsimple(el: etree._Element) -> str:
    return el.get(qn("w:instr"), "") or ""


def _iter_fields(root: etree._Element) -> List[dict]:
    """Yield every citation field, each with a UNIQUE positional handle.

    Each descriptor::

        {
          "carrier": "complex" | "fldSimple" | "sdt",
          "instruction": "<full concatenated instrText / instr attr>",
          "runs": [<w:r>, ...]   # complex: begin..end runs (siblings)
          "el":   <w:fldSimple>  # fldSimple carrier
          "sdt":  <w:sdt>        # sdt carrier (Word built-in citation)
          "index": int,          # unique per-field handle (see ordering note)
        }

    Ordering: fields are emitted GROUPED BY CARRIER — first every Word built-in
    citation ``<w:sdt>``, then every ``<w:fldSimple>``, then every complex field
    — and ``index`` is assigned in that emission order. It is NOT a document
    position. The only guarantee callers may rely on is that ``index`` is unique
    per field (it discriminates two fields citing the same key set so their
    Zotero ``citationID``s don't collide); current consumers also only need
    uniqueness/visit-once, never document order.

    Complex fields split across runs are reassembled (instrText concatenated).
    Nested fields are tracked with a stack; the OUTERMOST field is what we hand
    back for replacement (a foreign citation is one complex field).
    """
    fields: List[dict] = []
    idx = 0

    # 1. Word built-in citations live inside <w:sdt> with <w:citation/>; capture
    #    those whole and skip their inner field when walking complex fields.
    #    NOTE: hold the sdt elements in a list (lxml proxies are only stable while
    #    referenced); test ancestry via _in_citation_sdt rather than id() sets,
    #    which are unreliable across separate tree iterations.
    citation_sdts: List[etree._Element] = []
    for sdt in root.iter(qn("w:sdt")):
        sdtpr = sdt.find(qn("w:sdtPr"))
        if sdtpr is None:
            continue
        is_citation = sdtpr.find(qn("w:citation")) is not None
        is_bibliography = sdtpr.find(qn("w:bibliography")) is not None
        if not (is_citation or is_bibliography):
            continue
        # gather the inner field instruction (CITATION Tag / BIBLIOGRAPHY)
        instr = _collect_complex_instr(sdt)
        if not instr:
            # fldSimple inside the sdt content
            content = sdt.find(qn("w:sdtContent"))
            if content is not None:
                fs = content.find(".//" + qn("w:fldSimple"))
                if fs is not None:
                    instr = _instr_of_fldsimple(fs)
        citation_sdts.append(sdt)
        fields.append({
            "carrier": "sdt", "instruction": instr or "", "sdt": sdt, "index": idx,
        })
        idx += 1

    def _in_sdt(el: etree._Element) -> bool:
        for sdt in citation_sdts:
            anc = el
            while anc is not None:
                if anc is sdt:
                    return True
                anc = anc.getparent()
        return False

    # 2. fldSimple fields (instruction in the w:instr attribute)
    for fs in root.iter(qn("w:fldSimple")):
        if _in_sdt(fs):
            continue  # already accounted for inside a citation sdt
        fields.append({
            "carrier": "fldSimple", "instruction": _instr_of_fldsimple(fs),
            "el": fs, "index": idx,
        })
        idx += 1

    # 3. Complex fields (begin/instrText/separate/end), reassembled with a stack.
    #    Track the run sequence of each OUTERMOST field for in-place replacement.
    stack: List[dict] = []
    for r in root.iter(qn("w:r")):
        if _in_sdt(r):
            continue  # belongs to a citation sdt — already captured
        fld = r.find(qn("w:fldChar"))
        if fld is not None:
            t = fld.get(qn("w:fldCharType"))
            if t == "begin":
                stack.append({"code": "", "sep": False, "runs": [r]})
            elif t == "separate" and stack:
                stack[-1]["sep"] = True
                stack[-1]["runs"].append(r)
            elif t == "end" and stack:
                frame = stack.pop()
                frame["runs"].append(r)
                if not stack:  # outermost field closed
                    fields.append({
                        "carrier": "complex", "instruction": frame["code"],
                        "runs": frame["runs"], "index": idx,
                    })
                    idx += 1
                else:
                    stack[-1]["runs"].extend(frame["runs"])
            continue
        if stack:
            stack[-1]["runs"].append(r)
            instr = r.find(qn("w:instrText"))
            if instr is not None and not stack[-1]["sep"]:
                stack[-1]["code"] += instr.text or ""
    return fields


def _collect_complex_instr(scope: etree._Element) -> str:
    """Concatenate instrText of the first complex field within ``scope``."""
    code = ""
    started = False
    sep = False
    depth = 0
    for r in scope.iter(qn("w:r")):
        fld = r.find(qn("w:fldChar"))
        if fld is not None:
            t = fld.get(qn("w:fldCharType"))
            if t == "begin":
                depth += 1
                started = True
            elif t == "separate" and depth == 1:
                sep = True
            elif t == "end":
                depth -= 1
                if depth == 0:
                    break
            continue
        if started and depth >= 1 and not sep:
            instr = r.find(qn("w:instrText"))
            if instr is not None:
                code += instr.text or ""
    return code


# ===========================================================================
# Classification
# ===========================================================================

def _classify(instruction: str) -> Optional[str]:
    """Return the manager for a field instruction, or ``None`` if not a citation.

    Order matters: Zotero first (it embeds CSL_CITATION too), then EndNote, then
    Mendeley (bare CSL_CITATION), then Word built-in.
    """
    c = instruction or ""
    cl = c.lower()
    # Detect Zotero by the FIELD PREFIX only — not a free substring anywhere in
    # the JSON payload (a Mendeley/other CSL_CITATION whose JSON happens to contain
    # "zotero_item" in a title/note/URI would otherwise be misclassified).
    _cl_stripped = cl.lstrip()
    if (
        _cl_stripped.startswith("addin zotero_item")
        or _cl_stripped.startswith("addin zotero_bibl")
        or _cl_stripped.startswith("addin zotero_pref")
    ):
        return "zotero"
    if "en.cite" in cl or "en.ref" in cl or "<endnote>" in cl or "<cite>" in cl:
        return "endnote"
    if "csl_citation" in cl or '"mendeley"' in cl or "mendeley.com" in cl:
        # bare CSL_CITATION without ZOTERO_ITEM => Mendeley
        return "mendeley"
    # Word native CITATION / BIBLIOGRAPHY fields. Use a token match so we don't
    # confuse a literal word in body text (these come from field instructions).
    if re.search(r"\bCITATION\b", c) or re.search(r"\bBIBLIOGRAPHY\b", c):
        return "word"
    return None


# ===========================================================================
# Metadata extraction
# ===========================================================================

def _first_json_obj(s: str) -> Optional[dict]:
    if "{" not in s:
        return None
    try:
        return json.loads(s[s.index("{"): s.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        return None


def _csl_authors(item: dict) -> List[str]:
    out: List[str] = []
    for a in item.get("author", []) or []:
        if not isinstance(a, dict):
            continue
        fam = (a.get("family") or "").strip()
        giv = (a.get("given") or "").strip()
        if fam:
            initials = "".join(t[0].upper() for t in giv.split() if t)
            out.append(f"{fam} {initials}".strip())
        elif a.get("literal") or a.get("name"):
            out.append(str(a.get("literal") or a.get("name")).strip())
    return out


def _csl_year(item: dict) -> Optional[str]:
    issued = item.get("issued") or {}
    parts = issued.get("date-parts") or []
    if parts and parts[0]:
        y = parts[0][0]
        try:
            return str(int(y))
        except (TypeError, ValueError):
            return str(y)
    raw = item.get("issued", {}).get("raw") if isinstance(item.get("issued"), dict) else None
    return str(raw) if raw else None


def _extract_csljson(instruction: str) -> dict:
    """Extract metadata from a Zotero/Mendeley CSL_CITATION instruction.

    Returns ``{doi?, title?, authors?, year?}`` from the FIRST citationItem (for
    single-item foreign cites; grouped cites are handled item-by-item by the
    caller via :func:`_extract_csljson_items`)."""
    items = _extract_csljson_items(instruction)
    return items[0] if items else {}


def _extract_csljson_items(instruction: str) -> List[dict]:
    data = _first_json_obj(instruction)
    if not data:
        return []
    out: List[dict] = []
    for ci in data.get("citationItems", []) or []:
        idata = ci.get("itemData") or {}
        meta: dict = {}
        doi = (idata.get("DOI") or idata.get("doi") or "").strip()
        if doi:
            meta["doi"] = doi
        title = (idata.get("title") or "").strip()
        if title:
            meta["title"] = title
        authors = _csl_authors(idata)
        if authors:
            meta["authors"] = authors
        year = _csl_year(idata)
        if year:
            meta["year"] = year
        out.append(meta)
    return out


def _t(el: Optional[etree._Element]) -> str:
    return (el.text or "").strip() if el is not None else ""


def _extract_endnote(instruction: str) -> dict:
    """Extract metadata from the FIRST <Cite>/<record> in an EN.CITE field.

    The XML is entity-escaped in document.xml but lxml has already unescaped it
    when we read instrText.text, so we parse the raw XML directly. (We also
    tolerate a still-escaped string by unescaping first.)"""
    items = _extract_endnote_items(instruction)
    return items[0] if items else {}


def _extract_endnote_items(instruction: str) -> List[dict]:
    xml = instruction
    lt = xml.lower().find("<endnote>")
    if lt == -1:
        # maybe still entity-escaped
        xml = html.unescape(xml)
        lt = xml.lower().find("<endnote>")
        if lt == -1:
            return []
    end = xml.lower().rfind("</endnote>")
    frag = xml[lt: end + len("</endnote>")] if end != -1 else xml[lt:]
    try:
        node = etree.fromstring(frag.encode("utf-8"))
    except etree.XMLSyntaxError:
        try:
            node = etree.fromstring(("<root>" + frag + "</root>").encode("utf-8"))
        except etree.XMLSyntaxError:
            return []
    out: List[dict] = []
    for rec in node.iter("record"):
        meta: dict = {}
        doi = _extract_doi(_t(rec.find("electronic-resource-num")))
        if doi:
            meta["doi"] = doi
        title = _t(rec.find("titles/title"))
        if title:
            meta["title"] = _strip_style(title)
        authors = [
            _strip_style(_t(a))
            for a in rec.findall("contributors/authors/author")
            if _t(a)
        ]
        if authors:
            meta["authors"] = authors
        year = _t(rec.find("dates/year"))
        if year:
            meta["year"] = _strip_style(year)
        out.append(meta)
    return out


_STYLE_TAG = re.compile(r"<[^>]+>")


def _strip_style(s: str) -> str:
    return html.unescape(_STYLE_TAG.sub("", s or "")).strip()


def _word_source_index(
    doc: Docx, *, errors: Optional[List[str]] = None
) -> Dict[str, dict]:
    """Build a Tag -> metadata index from any ``b:Sources`` store in the package
    (typically ``word/customXml/itemN.xml``). Empty if no native-Word sources.

    When *errors* is supplied, the part name of any ``b:Sources`` store that
    fails to parse is appended to it.  A malformed store means EVERY Word
    citation backed by that store silently loses its embedded metadata (it then
    presents as an ordinary 'unmatched' citation), so the caller surfaces a
    diagnostic Finding rather than degrading silently.
    """
    index: Dict[str, dict] = {}
    for part in doc.parts():
        if not (part.endswith(".xml")):
            continue
        raw = doc.raw(part)
        if b"Sources" not in raw or B_NS.encode() not in raw:
            continue
        try:
            root = etree.fromstring(raw)
        except etree.XMLSyntaxError:
            if errors is not None:
                errors.append(part)
            continue
        for src in root.iter("{%s}Source" % B_NS):
            tag = _t(src.find("{%s}Tag" % B_NS))
            if not tag:
                continue
            meta: dict = {}
            doi = _extract_doi(_t(src.find("{%s}DOI" % B_NS)))
            if doi:
                meta["doi"] = doi
            title = _t(src.find("{%s}Title" % B_NS))
            if title:
                meta["title"] = title
            year = _t(src.find("{%s}Year" % B_NS))
            if year:
                meta["year"] = year
            authors: List[str] = []
            for person in src.iter("{%s}Person" % B_NS):
                last = _t(person.find("{%s}Last" % B_NS))
                first = _t(person.find("{%s}First" % B_NS))
                if last:
                    initials = "".join(t[0].upper() for t in first.split() if t)
                    authors.append(f"{last} {initials}".strip())
            if authors:
                meta["authors"] = authors
            index[tag] = meta
    return index


_CITATION_TAG_RE = re.compile(r"\bCITATION\b\s+(?:\\\w+\s+\S+\s+)*?([^\s\\]+)")


# Word CITATION-field switches that take a one-token argument; the rest
# (\n \y \t — suppress Name/Year/Title) are boolean and take none. Used to skip
# switch arguments when locating the source Tag (switch order is not guaranteed).
_WORD_VALUE_SWITCHES = {"\\l", "\\p", "\\v", "\\f", "\\s", "\\m"}


def _word_tag_from_instr(instruction: str) -> Optional[str]:
    """Pull the source Tag out of a ``CITATION Tag \\l 1033`` instruction.

    Switch order is not guaranteed, so we scan for the first token that is neither
    a switch nor a value-switch's argument. Arity comes from
    :data:`_WORD_VALUE_SWITCHES` (value switches consume one argument; boolean
    switches consume none)."""
    m = re.search(r"\bCITATION\b(.*)", instruction)
    if not m:
        return None
    toks = m.group(1).strip().split()
    i = 0
    while i < len(toks):
        tok = toks[i]
        if tok.startswith("\\"):
            i += 1
            if tok.lower() in _WORD_VALUE_SWITCHES and i < len(toks):
                i += 1  # consume this switch's single argument
            continue
        return tok
    return None


def _extract_word(instruction: str, source_index: Dict[str, dict]) -> dict:
    tag = _word_tag_from_instr(instruction)
    if tag and tag in source_index:
        meta = dict(source_index[tag])
        meta["tag"] = tag
        return meta
    return {"tag": tag} if tag else {}


# ===========================================================================
# Manual / unmanaged reference detection
# ===========================================================================

def _paragraphs_with_fields(root: etree._Element) -> List[etree._Element]:
    """List of paragraph elements that contain ANY citation field, so we can tell
    them apart from field-free (manual) reference paragraphs. Held by reference
    (not id()) for stable identity across iterations."""
    paras: List[etree._Element] = []
    for f in _iter_fields(root):
        if f["carrier"] == "complex":
            el = f["runs"][0]
        elif f["carrier"] == "sdt":
            el = f["sdt"]
        else:
            el = f["el"]
        anc = el
        while anc is not None and anc.tag != qn("w:p"):
            anc = anc.getparent()
        if anc is not None:
            paras.append(anc)
    return paras


def _detect_manual_refs(root: etree._Element) -> List[dict]:
    """Find suspected manual/unmanaged reference lines in the document BODY only.

    Problem B fix: uses split_body_vs_references to restrict the scan to body
    paragraphs, preventing the document's own rendered bibliography from being
    flagged as "unmanaged references — add to Zotero and re-cite."

    Line heuristic (Problem A fix): a body paragraph is flagged only when it
    carries a genuine citation shape with no citation field —
      * a numbered-list entry matching _NUMBERED_REF_RE, OR
      * a parenthetical inline author-year marker "(Author..., YEAR)" matching
        _INLINE_CITE_RE (a DOI/PMID alone also counts, via _DOI_RE below).
    A sentence that merely contains a 4-digit year ("The trial began in 2015")
    does NOT trigger. Returns descriptors with best-effort doi/title and
    ``low_confidence=True``.
    """
    all_para_elems = list(iter_paragraphs(root))
    all_para_texts = [paragraph_text(p).strip() for p in all_para_elems]

    # Restrict to body (drop the rendered bibliography — it's output, not inline).
    body_texts, _ = split_body_vs_references(all_para_texts)
    body_limit = len(body_texts)  # first ref-section index (or len if no refs found)

    field_paras = _paragraphs_with_fields(root)
    out: List[dict] = []
    for pi in range(body_limit):
        p = all_para_elems[pi]
        txt = all_para_texts[pi]
        if not txt:
            continue
        has_field = any(p is fp for fp in field_paras)
        if has_field:
            continue
        # A numbered entry OR a parenthetical inline author-year citation.
        # A DOI/PMID in body text is detected below via _DOI_RE and also counts.
        looks_like_inline = bool(_NUMBERED_REF_RE.match(txt)) or bool(_INLINE_CITE_RE.search(txt))
        has_doi = bool(_DOI_RE.search(txt))
        if not (looks_like_inline or has_doi):
            continue
        m = _DOI_RE.search(txt)
        doi = _extract_doi(m.group(0)) if m else None
        title = _rough_title(txt)
        out.append({
            "manager": "manual",
            "para_index": pi,
            "instruction_excerpt": txt[:160],
            "extracted": {k: v for k, v in (("doi", doi), ("title", title)) if v},
            "low_confidence": True,
        })
    return out


def _rough_title(line: str) -> Optional[str]:
    """Very rough title guess: the longest sentence-ish span between the leading
    author block and the journal/year tail. Best-effort only."""
    s = re.sub(r"^\s*(\[\d+\]|\(?\d+\)?[.)])\s*", "", line).strip()
    # split into clauses on '. '; pick the longest middle clause
    clauses = [c.strip() for c in re.split(r"\.\s+", s) if c.strip()]
    if len(clauses) >= 2:
        candidate = max(clauses[1:], key=len) if len(clauses) > 1 else clauses[0]
        candidate = candidate.strip()
        if len(candidate) >= 12:
            return candidate[:200]
    return None


# ===========================================================================
# PART 1 — classify_citation_sources
# ===========================================================================

def classify_citation_sources(path) -> dict:
    """Scan every field code and content control; classify each citation by
    managing system and extract embedded metadata.

    Returns::

        {
          "counts": {"zotero", "mendeley", "endnote", "word", "manual"},
          "items": [
            {"manager", "location", "index", "instruction_excerpt",
             "extracted": {"doi"?, "title"?, "authors"?, "year"?},
             "low_confidence"? }
          ],
        }

    Metadata is mined from each format's embedded payload: Zotero/Mendeley
    CSL-JSON, EndNote EN.CITE XML, Word source tag (via the ``b:Sources`` store).
    Manual references are best-effort (DOI regex + rough title), ``low_confidence``.
    """
    doc = Docx(path)
    root = doc.read_tree(DOCUMENT)
    source_errors: List[str] = []
    source_index = _word_source_index(doc, errors=source_errors)

    counts = {m: 0 for m in MANAGERS}
    items: List[dict] = []

    for f in _iter_fields(root):
        manager = _classify(f["instruction"])
        if manager is None:
            continue
        # bibliography fields don't carry per-item metadata; count under manager
        instr = f["instruction"]
        is_bib = (
            "zotero_bibl" in instr.lower()
            or "en.ref" in instr.lower()
            or re.search(r"\bBIBLIOGRAPHY\b", instr) is not None
        )
        extracted: dict = {}
        if not is_bib:
            if manager in ("zotero", "mendeley"):
                extracted = _extract_csljson(instr)
            elif manager == "endnote":
                extracted = _extract_endnote(instr)
            elif manager == "word":
                extracted = _extract_word(instr, source_index)
        counts[manager] += 1
        items.append({
            "manager": manager,
            "location": f["carrier"],
            "index": f["index"],
            "instruction_excerpt": _excerpt(instr),
            "extracted": extracted,
        })

    for ref in _detect_manual_refs(root):
        counts["manual"] += 1
        items.append({
            "manager": "manual",
            "location": f"para {ref['para_index']}",
            "index": ref["para_index"],
            "instruction_excerpt": ref["instruction_excerpt"],
            "extracted": ref["extracted"],
            "low_confidence": True,
        })

    return {"counts": counts, "items": items, "source_errors": source_errors}


def _excerpt(instr: str, n: int = 140) -> str:
    s = " ".join((instr or "").split())
    return s[:n] + ("…" if len(s) > n else "")


def classification_findings(result: dict) -> List[Finding]:
    """Turn a :func:`classify_citation_sources` result into Findings.

    INFO summary of the manager mix; WARN per non-Zotero managed citation; WARN
    per suspected manual reference.
    """
    findings: List[Finding] = []
    counts = result["counts"]
    mix = ", ".join(f"{m}={counts[m]}" for m in MANAGERS if counts[m])
    findings.append(Finding(
        check="CITE-SOURCES",
        severity="INFO",
        message=(
            "Citation manager mix: " + (mix or "none detected") + ". "
            "Zotero fields are live/managed; others should be converted for consistency."
        ),
        source="citeconvert",
    ))
    # A malformed b:Sources store strips metadata from every Word citation it
    # backed (they then present as ordinary 'unmatched' citations). Surface it
    # so the failure is visible rather than a silent degradation.
    for part in result.get("source_errors", []):
        findings.append(Finding(
            check="CITE-SOURCE-PARSE",
            severity="WARN",
            message=(
                f"Word citation source store unparseable: {part}. "
                "Word citations backed by this store lost their embedded "
                "metadata and may show as unmatched — repair or re-export the "
                "document's bibliography source."
            ),
            location=part,
            source="citeconvert",
        ))
    for it in result["items"]:
        mgr = it["manager"]
        if mgr == "zotero":
            continue
        ex = it["extracted"]
        ident = ex.get("doi") or (ex.get("title") or "")[:80] or it["instruction_excerpt"][:60]
        if mgr in CONVERTIBLE:
            findings.append(Finding(
                check="CITE-FOREIGN",
                severity="WARN",
                message=(
                    f"Citation managed by {mgr.title()} — convert to Zotero for "
                    f"consistency. Identified as: {ident or '(no metadata)'}"
                ),
                location=it["location"],
                source="citeconvert",
            ))
        elif mgr == "manual":
            findings.append(Finding(
                check="CITE-MANUAL",
                severity="WARN",
                message=(
                    "Suspected manual/unmanaged reference (no citation field). "
                    "Not auto-converted — add it to Zotero and re-cite. "
                    f"Text: {it['instruction_excerpt'][:80]}"
                ),
                location=it["location"],
                source="citeconvert",
            ))
    return findings


# ===========================================================================
# PART 2 — convert_to_zotero
# ===========================================================================

def normalize_title(title: str) -> str:
    """Normalise a title for cross-source matching: lowercase, collapse every
    run of non-alphanumeric characters to a single space, and strip.

    PUBLIC (documented) contract: other modules (e.g. ``endnote``) import this to
    match titles against the same key-space ``convert_to_zotero`` uses.
    """
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


# Public DOI normaliser — single-sourced from
# :func:`zoterocite.citecheck._normalise_doi` (the one canonical implementation).
# PUBLIC (documented) contract for cross-module import.
normalize_doi = _normalise_doi


def _extract_doi(raw: str) -> Optional[str]:
    """Extract and canonicalise the first DOI from *raw*, or ``None``.

    Goes through the two DOI owners: :func:`textpatterns.extract_dois` (strips a
    ``doi:`` / ``DOI:`` / ``https://doi.org/`` prefix and trailing prose
    punctuation including ``]``, case-insensitively, without mangling an interior
    ``doi:`` substring) then :func:`normalize_doi` (lowercase).  Replaces the old
    unanchored ``doi.replace("doi:", "")`` which corrupted DOIs whose body
    legitimately contained ``doi:``.
    """
    found = textpatterns.extract_dois(raw or "")
    return normalize_doi(found[0]) if found else None


# Private back-compat aliases.  The leading-underscore names are retained so
# existing imports (and this module's own ``_dk`` / ``_match_in_library`` call
# sites) keep working; new code should prefer the public names above.
_normalize_title = normalize_title
_normalize_doi = normalize_doi


def _match_in_library(
    meta: dict, zotero, *, title_cache: dict, lib_index: Optional[dict] = None
) -> Optional[dict]:
    """Match extracted metadata to a Zotero item; return the item dict or None.

    OFFLINE-FIRST and FAIL-CLOSED. Resolution order:

    1. **Resilient cached index (offline, no network).** If ``lib_index`` (the
       ``{"doi","pmid","title"}`` map from :func:`zotero.library_index`, which
       degrades to the DOI cache) is supplied, decide presence via
       :func:`zotero.lookup_index_key` (DOI → PMID → title; the single owner of
       that precedence). A hit returns a minimal ``{"key": ...}`` stub — all the
       caller needs to write the field — without ANY per-item live search. This
       is what lets ``unify-refs --apply`` run with no credentials/network.
    2. **Live search ONLY as a reachable fallback, NEVER a crash.** On an index
       miss, attempt a live ``get_item_by_doi`` / ``search_items`` so the normal
       (Zotero-available) path still resolves works absent from a stale cache.
       Any failure — ``RuntimeError`` (missing creds, raised by
       ``zotero.build_request``), a network error, or
       :class:`zotero.LibraryUnavailableError` — is CAUGHT and treated as "no
       match" (returns ``None``). An unmatched ref is left unconverted upstream;
       NOTHING is ever created here. The matching is read-only either way.
    """
    doi = _normalize_doi(meta.get("doi", ""))
    title = meta.get("title", "")

    # (1) Offline match against the resilient cached index first.
    if lib_index:
        key = zotero.lookup_index_key(
            lib_index, doi=doi or None, pmid=meta.get("pmid"), title=title or None
        )
        if key:
            return {"key": key, "data": {"key": key}}

    # (2) Live fallback — only when reachable, and never allowed to crash.
    if doi:
        try:
            item = zotero.get_item_by_doi(doi)
        except (RuntimeError, OSError):
            # RuntimeError covers missing creds (build_request) AND
            # LibraryUnavailableError; OSError covers urllib network failures.
            item = None
        if item:
            return item
    if not title:
        return None
    ntitle = _normalize_title(title)
    if not ntitle:
        return None
    if ntitle not in title_cache:
        try:
            title_cache[ntitle] = zotero.search_items(title)
        except (RuntimeError, OSError):
            title_cache[ntitle] = []
    candidates = title_cache[ntitle]
    year = str(meta.get("year") or "").strip()
    first_author = ""
    if meta.get("authors"):
        first_author = (meta["authors"][0].split() or [""])[0].lower()
    best = None
    for cand in candidates:
        data = cand.get("data", cand) if isinstance(cand, dict) else {}
        cand_title = _normalize_title(data.get("title", ""))
        if cand_title != ntitle:
            continue
        # title matches; prefer one that also matches year / first author
        cand_year = ""
        for ch in str(data.get("date", "")):
            if ch.isdigit():
                cand_year += ch
                if len(cand_year) == 4:
                    break
            else:
                cand_year = ""
        if year and cand_year and year != cand_year:
            continue
        if first_author:
            creators = data.get("creators", []) or []
            fams = [(c.get("lastName") or "").lower() for c in creators if isinstance(c, dict)]
            if fams and first_author not in fams:
                continue
        best = cand
        break
    return best


def _item_key(item: dict) -> str:
    if not isinstance(item, dict):
        return ""
    return item.get("key") or item.get("data", {}).get("key", "")


def _item_uri_offline_safe(zotero, key: str) -> str:
    """Canonical Zotero item URI for ``key``, NEVER raising on a degraded read.

    ``zotero.item_uri`` only needs the library id/type (no network) but RAISES
    when credentials are entirely unset. On a DEGRADED read (offline match, creds
    missing) we cannot form the canonical URI — return ``""`` rather than crash.
    The written field still carries the matched item KEY, so Word/Zotero rebind
    the full URI/itemData on the first Refresh.
    """
    try:
        return zotero.item_uri(key)
    except (RuntimeError, OSError):
        return ""


def convert_to_zotero(
    path,
    *,
    out=None,
    managers: Tuple[str, ...] = ("endnote", "mendeley", "word"),
    add_missing: bool = False,
    track: bool = False,
    style: str = None,
) -> dict:
    """Convert foreign field-based citations to live Zotero fields, with dedup.

    For each non-Zotero, field-based citation whose manager is in ``managers``:
    extract its metadata, MATCH it to the user's Zotero group library (DOI first,
    else normalized-title + year/first-author), and REPLACE the foreign field
    in-place with a ``ZOTERO_ITEM CSL_CITATION`` field for the matched item.

    Dedup
    -----
    A per-run cache keyed by normalized DOI / normalized title ensures multiple
    foreign citations of the same work resolve to ONE library lookup and reuse
    ONE Zotero item URI. The cache is pre-seeded with the keys/DOIs/titles of
    citations ALREADY Zotero-managed in the document, so we never re-add a work
    that is already cited.

    No match / not auto-convertible
    -------------------------------
    * No library match → recorded in ``unmatched`` (never fabricated). If
      ``add_missing=True``, this function intentionally does not write to the shared
      library; the supported way to add missing items is ``unify-refs --apply``
      (zoterocite/unify.py), which DOES write. ``add_missing`` is a documented
      no-op that simply reports what *would* be added, leaving those in
      ``unmatched`` with ``would_add=True``.
    * Manual/plain-text references are never auto-converted (unreliable); they are
      reported in ``manual_skipped``.

    Returns ``{out, converted, unmatched, manual_skipped, deduped, classification}``
    and writes the converted doc to ``out`` (nothing is saved when ``out`` is None
    → dry-run/report only).
    """
    from . import zotero
    from .zoterofield import DEFAULT_STYLE
    if style is None:
        style = DEFAULT_STYLE

    doc = Docx(path)
    root = doc.tree(DOCUMENT)
    source_index = _word_source_index(doc)

    converted: List[dict] = []
    unmatched: List[dict] = []
    manual_skipped: List[dict] = []
    deduped = 0

    # Dedup state:
    #   match_cache: doi/title key -> {"key","uri","itemdata"} match, or _MISS.
    #   already_zotero: doi/title keys of works ALREADY cited via Zotero in the
    #     doc — we must not re-add/re-convert these (leave the existing cite).
    match_cache: Dict[str, object] = {}
    title_cache: Dict[str, list] = {}
    already_zotero: set = set()

    # Resilient cached library index (DOI→PMID→title), loaded ONCE per run for
    # OFFLINE matching. ``strict=False`` degrades a failed read to the DOI cache
    # rather than raising; wrap defensively so even a hard
    # LibraryUnavailableError (neither cache loadable) cannot crash the rewrite —
    # we simply fall through to the per-item live fallback (also crash-proof) and,
    # if that is unreachable too, leave refs unconverted (fail-closed).
    try:
        lib_index = zotero.library_index(strict=False)
    except Exception:  # noqa: BLE001 — degrade to no offline index, never crash
        lib_index = None

    for f_instr in _field_codes(root):
        if f_instr.strip().lower().startswith("addin zotero_item"):
            for meta in _extract_csljson_items(f_instr):
                ck = _meta_cache_key(meta)
                if ck:
                    already_zotero.add(ck)

    fields = _iter_fields(root)
    # detect manual refs once for reporting
    for ref in _detect_manual_refs(root):
        manual_skipped.append({
            "para_index": ref["para_index"],
            "extracted": ref["extracted"],
            "text": ref["instruction_excerpt"],
            "reason": "manual/unmanaged reference — not auto-converted",
        })

    for f in fields:
        manager = _classify(f["instruction"])
        if manager is None or manager == "zotero":
            continue
        if manager not in managers:
            continue
        # bibliography fields: skip (rebuilt by Zotero from the converted cites)
        instr = f["instruction"]
        if (
            "en.ref" in instr.lower()
            or (manager == "word" and re.search(r"\bBIBLIOGRAPHY\b", instr) and not re.search(r"\bCITATION\b", instr))
        ):
            continue

        # one foreign field may group several items
        if manager in ("mendeley",):
            metas = _extract_csljson_items(instr) or [{}]
        elif manager == "endnote":
            metas = _extract_endnote_items(instr) or [{}]
        else:  # word
            metas = [_extract_word(instr, source_index)]

        resolved_keys: List[str] = []
        resolved_itemdata: List[dict] = []
        resolved_uris: List[str] = []
        any_miss = False
        n_fresh = 0          # resolved items NOT already cited via Zotero elsewhere
        for meta in metas:
            cache_key = _meta_cache_key(meta)

            # (a) An item already cited via a Zotero field ELSEWHERE in the doc is
            # a duplicate — but in a GROUPED foreign cite it must still be kept so
            # the co-citation at THIS location survives. Dropping it (the old
            # behaviour) silently lost a reference from the sentence. Count it as
            # deduped and resolve it like any library item; the WHOLE field is
            # skipped after the loop only when EVERY item is already-Zotero (a pure
            # duplicate of existing cites — see the n_fresh check below).
            already = bool(cache_key and cache_key in already_zotero)
            if already:
                deduped += 1

            # (b) seen this work already in THIS run (another foreign cite) → reuse.
            if cache_key and cache_key in match_cache:
                cached = match_cache[cache_key]
                if cached is _MISS:
                    any_miss = True
                    continue
                if not already:
                    deduped += 1
                hit = cached  # type: ignore[assignment]
                resolved_keys.append(hit["key"])
                resolved_itemdata.append(hit["itemdata"])
                resolved_uris.append(hit["uri"])
                if not already:
                    n_fresh += 1
                continue

            # (c) fresh library lookup.
            item = _match_in_library(
                meta, zotero, title_cache=title_cache, lib_index=lib_index
            )
            if item is None:
                if cache_key:
                    match_cache[cache_key] = _MISS
                any_miss = True
                unmatched.append({
                    "manager": manager,
                    "location": f["carrier"],
                    "extracted": meta,
                    "would_add": bool(add_missing),
                    "reason": (
                        "not found in Zotero group library"
                        + (" (to add missing items, use unify-refs --apply)" if add_missing else "")
                    ),
                })
                continue
            key = _item_key(item)
            # csljson/item_uri network or require creds; on a DEGRADED read
            # (matched offline against the cache, Zotero unreachable) they must
            # not crash. The field is still writable: it carries the matched KEY
            # and (best-effort) URI, so Word/Zotero rebind the full itemData on
            # the first Refresh. idata is a pre-refresh display nicety → {} is
            # fine; the URI is recovered offline from the cached config below.
            idata = {}
            if key:
                try:
                    idata_list = zotero.csljson([key])
                    idata = idata_list[0] if idata_list else {}
                except (RuntimeError, OSError):
                    idata = {}
            uri = _item_uri_offline_safe(zotero, key) if key else ""
            hit = {"key": key, "uri": uri, "itemdata": idata}
            if cache_key:
                match_cache[cache_key] = hit
            resolved_keys.append(key)
            resolved_itemdata.append(idata)
            resolved_uris.append(uri)
            if not already:
                n_fresh += 1

        if not resolved_keys:
            continue  # nothing matched for this field — leave it untouched
        if n_fresh == 0:
            # Every resolved item is already cited via Zotero elsewhere — this
            # whole field is a pure duplicate; leave the existing cites untouched.
            continue

        # Render the in-text marker stored as formattedCitation/plainCitation (a
        # PRE-REFRESH display value also used by the existing_renderings dedup
        # guard). Zotero's per-item ``include=citation`` returns ONE marker per
        # item; a single grouped field, however, renders as ONE marker (e.g.
        # "(1,2)" / "(1-3)"), which the per-item API cannot synthesise. Joining
        # the per-item markers with "; " (the old behaviour) fabricated a
        # separator Zotero never produces for one field ("(1); (2)"), corrupting
        # both first-open display and the dedup guard. For a single resolved key
        # the per-item marker IS correct; for a GROUP we store a neutral
        # placeholder (the live field carries the real citationItems and Word/
        # Zotero regenerate the true grouped marker on refresh).
        if len(resolved_keys) == 1:
            try:
                rendered = (
                    "".join(
                        zotero.formatted_citations(resolved_keys, style=style, kind="citation")
                    )
                    or "(citation)"
                )
            except (RuntimeError, OSError):
                # Degraded read: the live marker is unavailable. The placeholder
                # is a pre-refresh display value only; Word/Zotero regenerate the
                # real marker on Refresh from the field's citationItems.
                rendered = "(citation)"
        else:
            rendered = "(citation)"

        locator = {"runs": f["runs"]} if f["carrier"] == "complex" else {"sdt": f.get("sdt")}
        if f["carrier"] == "fldSimple":
            # treat a fldSimple as a single-element run sequence
            locator = {"runs": [f["el"]]}
        replace_field_with_zotero(
            doc, locator,
            keys=resolved_keys, itemdata=resolved_itemdata, uris=resolved_uris,
            rendered=rendered, style=style, track=track,
            # Per-field discriminator so two fields citing the SAME item(s) get
            # distinct citationIDs (Zotero requires per-field-unique IDs).
            seed=f["index"],
        )
        converted.append({
            "manager": manager,
            "location": f["carrier"],
            "keys": resolved_keys,
            "uris": resolved_uris,
            "partial": any_miss,
        })

    result = {
        "out": None,
        "converted": converted,
        "unmatched": unmatched,
        "manual_skipped": manual_skipped,
        "deduped": deduped,
        "classification": classify_citation_sources(path),
    }
    if out is not None and converted:
        result["out"] = str(doc.save(Path(out)))
    elif out is not None:
        # nothing converted but caller wants a file → still write the (unchanged) doc
        result["out"] = str(doc.save(Path(out)))
    return result


def _dk(doi: str) -> str:
    return "doi:" + _normalize_doi(doi)


def _tk(title: str) -> str:
    return "title:" + _normalize_title(title)


def _meta_cache_key(meta: dict) -> Optional[str]:
    if meta.get("doi"):
        return _dk(meta["doi"])
    if meta.get("title"):
        return _tk(meta["title"])
    return None
