"""Paragraph location by visible text — run-split-safe.

Grant docx files split a sentence across many <w:r> runs, so literal string
matching on the XML fails. We match on the *reassembled* paragraph text instead,
then operate on the element tree.
"""
from __future__ import annotations

import re
from typing import List

from lxml import etree

from .ooxml import NS, qn

W = NS["w"]


def get_body(root: etree._Element) -> etree._Element:
    body = root.find(qn("w:body"))
    return body if body is not None else root


def iter_paragraphs(root: etree._Element) -> List[etree._Element]:
    """Visible body paragraphs, each yielded EXACTLY once.

    A textbox is stored as an ``mc:AlternateContent`` block: an ``mc:Choice``
    (DrawingML ``wps`` textbox) and an ``mc:Fallback`` (legacy VML textbox), each
    wrapping a ``w:txbxContent`` whose inner ``w:p`` carries the SAME caption text.
    A naive ``root.iter(w:p)`` therefore yields THREE elements for one visible
    textbox — the body ``w:p`` plus the Choice-inner and Fallback-inner ``w:p`` —
    which (a) hard-crashes :func:`find_paragraph` whenever an anchor's text overlaps
    a caption (``matched 3 paragraphs``) and (b) triple-counts the caption in every
    per-paragraph consumer.

    The body ``w:p`` already carries the caption once: :func:`paragraph_text` walks
    its descendants, reads the Choice ``w:t`` and drops the Fallback copy. So the
    nested ``w:txbxContent`` paragraphs must NOT also be yielded as standalone
    paragraphs. We exclude every ``w:p`` that has a ``w:txbxContent`` ancestor — this
    drops BOTH the Choice-inner and Fallback-inner copies (a Fallback-only drop is
    insufficient: it would still yield body + Choice = 2 and still crash
    ``find_paragraph``). Each visible paragraph then maps to exactly one entry whose
    text matches the single read produced by :func:`zoterocite.views._extract`.
    """
    txbx_tag = qn("w:txbxContent")

    def _in_textbox(p: etree._Element) -> bool:
        anc = p.getparent()
        while anc is not None and anc is not root:
            if anc.tag == txbx_tag:
                return True
            anc = anc.getparent()
        return False

    return [p for p in root.iter(qn("w:p")) if not _in_textbox(p)]


def paragraph_text(p: etree._Element) -> str:
    """Visible text of a paragraph in the accepted view (tracked deletions excluded).

    Flow-affecting children are rendered exactly as :func:`zoterocite.views._extract`
    renders them: an intra-paragraph line break (``<w:br>``/``<w:cr>``) becomes a
    newline and a ``<w:tab>`` becomes a tab. Children are walked in document order
    so text on either side of a break/tab is never silently glued together — the
    old ``<w:t>``-only pass concatenated ``"Line one"`` + ``<w:br/>`` + ``"Line two"``
    into ``"Line oneLine two"``. A literal newline already inside a ``<w:t>`` (e.g. a
    run built by python-docx ``add_run("a\\nb")``) is preserved unchanged, so the
    result is consistent whether a break is encoded as an element or as raw text.

    An ``mc:AlternateContent`` block stores the same content twice — a modern
    ``mc:Choice`` and a legacy ``mc:Fallback`` (e.g. a DrawingML textbox plus its VML
    fallback). Per markup-compatibility a consumer uses the Choice and IGNORES the
    Fallback, never both, so ``<w:t>`` inside an ``mc:Fallback`` subtree is skipped —
    otherwise every such textbox's text would be read twice.
    """
    out = []
    t_tag = qn("w:t")
    del_tag = qn("w:del")
    move_from_tag = qn("w:moveFrom")
    fallback_tag = qn("mc:Fallback")
    tab_tag = qn("w:tab")
    br_tags = (qn("w:br"), qn("w:cr"))

    def _drop(node, *, also_del: bool) -> bool:
        # True if this node lives under an mc:Fallback (duplicate of the Choice we
        # already read) or, when *also_del*, under a tracked w:del OR w:moveFrom — both
        # are excluded from the accepted view, mirroring views._extract's <w:t> handling
        # (deletions gone; the tracked-move SOURCE gone, since moveFrom uses ordinary
        # <w:t> and would otherwise double the moved text at source AND destination).
        anc = node.getparent()
        while anc is not None and anc is not p:
            if anc.tag == fallback_tag or (
                also_del and anc.tag in (del_tag, move_from_tag)
            ):
                return True
            anc = anc.getparent()
        return False

    for node in p.iter():
        tag = node.tag
        if tag == t_tag:
            if not _drop(node, also_del=True):
                out.append(node.text or "")
        elif tag == tab_tag:
            # A tracked-STRUCK tab (under w:del / w:moveFrom) is gone from the
            # accepted view, exactly like a struck <w:t> — so also_del=True, matching
            # the t_tag branch and views._extract's tab guards. (also_del=False here
            # leaked a struck tab into the accepted view AND desynced the
            # revisions._plain_text_mask backstop, force-refusing granular edits.)
            if not _drop(node, also_del=True):
                out.append("\t")
        elif tag in br_tags:
            # Same as the tab branch: a struck break is excluded from the accepted view.
            if not _drop(node, also_del=True):
                out.append("\n")
    return "".join(out)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


# -- Table-of-contents detection (anchor tie-break) --------------------------
# A TOC entry repeats its heading's text, so a heading anchor substring-matches
# BOTH the heading and its TOC line. ``ParaIndex.find`` uses these predicates to
# break that one tie in favour of the body paragraph. Self-contained on purpose:
# this module is brand-synced to the public mirror and must import nothing
# grant-only.
_TOC_STYLE_PREFIX = "TOC"            # TOC1..TOC9 and TOCHeading (built-in styleIds)
_TOC_GALLERY = "table of contents"   # sdtPr/docPartObj/docPartGallery@w:val


def _is_toc_instr(instr: str) -> bool:
    # "TOC \o "1-3" \h \z \u" and a table of figures ("TOC \h \z \c "Figure"").
    s = instr.strip().upper()
    return s == "TOC" or s.startswith("TOC ") or s.startswith("TOC\\")


def _toc_style(p: etree._Element) -> bool:
    ps = p.find(f"{qn('w:pPr')}/{qn('w:pStyle')}")
    return ps is not None and (ps.get(qn("w:val")) or "").startswith(_TOC_STYLE_PREFIX)


def _toc_container(p: etree._Element) -> bool:
    """Inside a ``w:sdt`` whose docPartGallery is 'Table of Contents', or holding a
    ``w:fldSimple`` whose instr is a TOC field."""
    for fs in p.iter(qn("w:fldSimple")):
        if _is_toc_instr(fs.get(qn("w:instr")) or ""):
            return True
    sdt_tag = qn("w:sdt")
    anc = p.getparent()
    while anc is not None:
        if anc.tag == sdt_tag:
            for path in ("w:docPartObj", "w:docPartList"):
                g = anc.find(f"{qn('w:sdtPr')}/{qn(path)}/{qn('w:docPartGallery')}")
                if g is not None and (g.get(qn("w:val")) or "").strip().lower() == _TOC_GALLERY:
                    return True
        anc = anc.getparent()
    return False


def _toc_field_paragraphs(root: etree._Element, paras: List[etree._Element]) -> set:
    """``id()`` of every paragraph in *paras* that shows text while a TOC complex
    field is open.

    A complex field spans paragraphs: ``fldChar begin`` → ``instrText`` →
    ``fldChar separate`` → result → ``fldChar end``. We walk the whole tree in
    document order keeping a field stack (fields nest: each TOC entry carries a
    PAGEREF), and mark a paragraph when one of its ``w:t`` is read while ANY open
    field is a TOC. Keying on visible text rather than on "open at paragraph start"
    keeps a body paragraph that merely carries the TOC's trailing ``fldChar end``
    at its start from being misread as a TOC entry. ``mc:Fallback`` duplicates are
    skipped, as in :func:`paragraph_text`; a nested textbox paragraph is attributed
    to its outer body paragraph.
    """
    pset = {id(p): p for p in paras}
    fld_tag, instr_tag, t_tag = qn("w:fldChar"), qn("w:instrText"), qn("w:t")
    fallback_tag = qn("mc:Fallback")
    stack: List[list] = []   # each entry: [instr_text, in_instr_phase]
    out: set = set()

    def _owner(node):
        anc = node.getparent()
        while anc is not None:
            if anc.tag == fallback_tag:
                return None
            if id(anc) in pset:
                return anc
            anc = anc.getparent()
        return None

    for node in root.iter(fld_tag, instr_tag, t_tag):
        owner = _owner(node)
        if owner is None:
            continue
        if node.tag == fld_tag:
            kind = node.get(qn("w:fldCharType"))
            if kind == "begin":
                stack.append(["", True])
            elif kind == "separate":
                if stack:
                    stack[-1][1] = False
            elif kind == "end":
                if stack:
                    stack.pop()
        elif node.tag == instr_tag:
            if stack and stack[-1][1]:
                stack[-1][0] += node.text or ""
        elif stack and any(_is_toc_instr(f[0]) for f in stack):
            out.add(id(owner))
    return out


class ParaIndex:
    """One-shot anchor resolver: walk the tree ONCE, then resolve many anchors.

    :func:`find_paragraph` / :func:`find_paragraphs` re-walk the whole tree on
    EVERY call — :func:`iter_paragraphs` (a full ``root.iter(w:p)`` plus a textbox
    ancestor walk per paragraph) and :func:`paragraph_text` (a subtree walk per
    paragraph). Inside a per-item / per-token / per-placement loop that is
    O(items * doc) — quadratic. ``ParaIndex`` does those tree walks ONCE at
    construction, caching ``(p_el, _norm(paragraph_text(p)))`` per paragraph in
    :func:`iter_paragraphs` order, then resolves each anchor by a cheap substring
    scan over the cached normalized strings: O(doc) build + O(doc) substring checks
    per query, with NO tree walk per query.

    Resolution is BYTE-IDENTICAL to the module functions: it reuses the very same
    :func:`iter_paragraphs` ordering, the same :func:`paragraph_text` reassembly,
    the same :func:`_norm` normalization, the same ``substring`` match rule, and
    the same unique / ambiguous / not-found handling (:meth:`find` raises the
    identical ``LookupError``). The module functions now delegate here, so there is
    a SINGLE matching implementation.

    Caching contract (the caller's responsibility): the cached normalized text is a
    SNAPSHOT taken at construction. Build the index on a stable tree and resolve
    before mutating, OR only under mutations that leave BOTH the paragraph SET
    (:func:`iter_paragraphs`) AND :func:`paragraph_text` of every still-to-resolve
    anchor unchanged. The zotero-word-cite write callers satisfy this: feedback validate
    never mutates; feedback apply resolves every anchor in a pre-scan BEFORE the
    edit pass; comment-wrapping adds ``commentRangeStart/End``/``commentReference``
    runs (no ``w:t``/``w:tab``/``w:br``, so ``paragraph_text`` is unchanged, and no
    ``w:p`` is added/removed); cite-link/unify field-splice replaces only an
    already-resolved token's own occurrences with a NEUTRAL placeholder that
    contains no other (distinct) anchor, so a not-yet-resolved anchor's match-set is
    invariant.
    """

    __slots__ = ("_entries", "_root", "_toc_fields")

    def __init__(self, root: etree._Element) -> None:
        self._root = root
        self._entries = [(p, _norm(paragraph_text(p))) for p in iter_paragraphs(root)]
        self._toc_fields = None   # lazily built on the first ambiguous anchor

    def find_all(self, anchor: str) -> List[etree._Element]:
        a = _norm(anchor)
        return [p for (p, t) in self._entries if a in t]

    def is_toc(self, p: etree._Element) -> bool:
        """Is *p* a table-of-contents paragraph? True if its pStyle starts with
        ``TOC`` (TOC1..TOC9, TOCHeading), it sits in a ``w:sdt`` whose
        docPartGallery is 'Table of Contents', it holds a TOC ``w:fldSimple``, or
        it shows text inside a TOC complex field (see :func:`_toc_field_paragraphs`)."""
        if _toc_style(p) or _toc_container(p):
            return True
        if self._toc_fields is None:
            self._toc_fields = _toc_field_paragraphs(
                self._root, [e[0] for e in self._entries])
        return id(p) in self._toc_fields

    def candidates(self, anchor: str) -> List[etree._Element]:
        """:meth:`find_all`, with ONE tie-break: when several paragraphs match and
        exactly one of them is not a TOC paragraph, return just that one. A heading
        anchor otherwise also hits its own TOC entry. Zero or one hit, or an
        ambiguity the tie-break cannot settle, comes back unchanged, so a caller can
        still tell "not found" from "not unique"."""
        hits = self.find_all(anchor)
        if len(hits) > 1:
            body = [p for p in hits if not self.is_toc(p)]
            if len(body) == 1:
                return body
        return hits

    def find(self, anchor: str) -> etree._Element:
        hits = self.candidates(anchor)
        if len(hits) != 1:
            raise LookupError(f"anchor {anchor!r} matched {len(hits)} paragraphs (need exactly 1)")
        return hits[0]


def find_paragraphs(root: etree._Element, anchor: str) -> List[etree._Element]:
    return ParaIndex(root).find_all(anchor)


def find_paragraph_candidates(root: etree._Element, anchor: str) -> List[etree._Element]:
    """:meth:`ParaIndex.candidates` for a one-off lookup."""
    return ParaIndex(root).candidates(anchor)


def find_paragraph(root: etree._Element, anchor: str) -> etree._Element:
    return ParaIndex(root).find(anchor)
