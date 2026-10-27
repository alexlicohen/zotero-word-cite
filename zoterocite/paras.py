"""Paragraph location by visible text — run-split-safe.

Grant docx files split a sentence across many <w:r> runs, so literal string
matching on the XML fails. We match on the *reassembled* paragraph text instead,
then operate on the element tree.
"""
from __future__ import annotations

import re
from typing import List, Optional

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


def paragraph_text(p: etree._Element, *, txbx_marker: Optional[str] = None) -> str:
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

    ``txbx_marker`` (anchor matching only): a text box's ``w:txbxContent`` is a
    SEPARATE story whose paragraphs are their own ``w:p`` elements. By default its
    text is read in place (the views/reconstruction contract above). When
    ``txbx_marker`` is given, text-box text is NOT read; each outermost visible text
    box contributes ``txbx_marker`` once instead, so body text on either side of it is
    never glued together into a match.
    """
    out = []
    t_tag = qn("w:t")
    del_tag = qn("w:del")
    move_from_tag = qn("w:moveFrom")
    fallback_tag = qn("mc:Fallback")
    txbx_tag = qn("w:txbxContent")
    tab_tag = qn("w:tab")
    br_tags = (qn("w:br"), qn("w:cr"))
    skip_txbx = txbx_marker is not None

    def _drop(node, *, also_del: bool) -> bool:
        # True if this node lives under an mc:Fallback (duplicate of the Choice we
        # already read) or, when *also_del*, under a tracked w:del OR w:moveFrom — both
        # are excluded from the accepted view, mirroring views._extract's <w:t> handling
        # (deletions gone; the tracked-move SOURCE gone, since moveFrom uses ordinary
        # <w:t> and would otherwise double the moved text at source AND destination).
        # With a txbx_marker, also under a w:txbxContent (a separate story).
        anc = node.getparent()
        while anc is not None and anc is not p:
            if anc.tag == fallback_tag or (
                also_del and anc.tag in (del_tag, move_from_tag)
            ) or (skip_txbx and anc.tag == txbx_tag):
                return True
            anc = anc.getparent()
        return False

    for node in p.iter():
        tag = node.tag
        if tag == txbx_tag:
            if skip_txbx and not _drop(node, also_del=True):
                out.append(txbx_marker)
        elif tag == t_tag:
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


# Anchor matching reads a paragraph's OWN story: a text box (``w:txbxContent``) is a
# separate story, so its text never matches the host paragraph. The text box stands in
# as one OBJECT REPLACEMENT CHARACTER, which no anchor contains, so an anchor can
# neither match inside the text box nor glue the body text on its two sides together.
_TXBX_MARK = "\ufffc"
TEXTBOX_ANCHOR_REASON = (
    "anchor is inside a text box: text-box text is a separate story and is not "
    "edited, commented or cited through its host paragraph (edit it in Word, or "
    "anchor on body text)"
)

# -- Text-box LOCKSTEP resolution (tracked TEXT edits only) ------------------
# A Word text box is stored twice inside one ``mc:AlternateContent``: the DrawingML
# copy (``mc:Choice`` > ... > ``wps:txbx`` > ``w:txbxContent``) and the legacy VML copy
# (``mc:Fallback`` > ``w:pict`` > ``v:shape`` > ``v:textbox`` > ``w:txbxContent``). A
# tracked text edit must change BOTH, identically, or Word (Choice) and older readers
# (Fallback) show different text. :meth:`ParaIndex.textbox_target` resolves an anchor
# that lives only in a text box to the pair; :func:`textbox_copies` pairs a text-box
# paragraph with its twin. Comments, citations and every other caller keep refusing
# with :data:`TEXTBOX_ANCHOR_REASON`. Refusals:
TEXTBOX_DRIFT_REASON = (
    "text box copies differ: the paragraph's DrawingML (mc:Choice) and VML "
    "(mc:Fallback) copies do not carry the same text, or the twin is missing, so a "
    "lockstep edit cannot be placed safely (edit it in Word)")
TEXTBOX_VML_ONLY_REASON = (
    "text box is VML-only (no DrawingML mc:Choice copy): legacy VML text boxes are not "
    "edited (edit it in Word)")
TEXTBOX_AMBIGUOUS_REASON = (
    "anchor matches more than one text-box paragraph (extend it so it is unique)")
TEXTBOX_UNSUPPORTED_REASON = (
    "text box shape not supported for editing: a text box nested in a text box, a "
    "text box outside the document body, an unrecognised AlternateContent shape, or "
    "tracked changes that already differ between its two copies (edit it in Word)")

_MC_CHOICE, _MC_FALLBACK = qn("mc:Choice"), qn("mc:Fallback")
_MC_AC = qn("mc:AlternateContent")
_V_TEXTBOX = "{urn:schemas-microsoft-com:vml}textbox"
_TXBX = qn("w:txbxContent")
_NOT_BODY_ROOTS = frozenset(qn(t) for t in ("w:hdr", "w:ftr", "w:footnotes",
                                            "w:endnotes", "w:comments"))
# Revision elements compared between the two copies (ids excluded: each copy has its own).
_COPY_REV_TAGS = tuple(qn("w:" + t) for t in ("ins", "del", "moveFrom", "moveTo",
                                              "rPrChange", "pPrChange"))


class TextboxTarget:
    """A resolved text-box edit target: ``choice`` (the DrawingML copy's paragraph, or
    the only copy), ``fallback`` (its VML twin, ``None`` for a Choice-only box) and
    ``reason`` (``None`` when resolved, else one of the ``TEXTBOX_*`` refusals, in
    which case both elements are ``None``)."""

    __slots__ = ("choice", "fallback", "reason")

    def __init__(self, choice=None, fallback=None, reason: Optional[str] = None):
        self.choice, self.fallback, self.reason = choice, fallback, reason

    @property
    def copies(self) -> List[etree._Element]:
        return [c for c in (self.choice, self.fallback) if c is not None]

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"TextboxTarget(copies={len(self.copies)}, reason={self.reason!r})"


def _story_of(p: etree._Element) -> Optional[etree._Element]:
    return next(p.iterancestors(_TXBX), None)


def _story_paras(story: etree._Element) -> List[etree._Element]:
    """The story's OWN paragraphs (not those of a text box nested inside it)."""
    return [q for q in story.iter(qn("w:p")) if _story_of(q) is story]


def _branch_of(story: etree._Element) -> Optional[etree._Element]:
    """The nearest ``mc:Choice`` / ``mc:Fallback`` enclosing the text box."""
    for anc in story.iterancestors():
        if anc.tag in (_MC_CHOICE, _MC_FALLBACK):
            return anc
    return None


def textbox_twin(p: etree._Element) -> Optional[etree._Element]:
    """STRUCTURAL twin of a text-box paragraph: the paragraph at the same index of the
    same-order text box in the OTHER branch (Choice <-> Fallback) of the same
    ``mc:AlternateContent``, or ``None`` (not in a text box, no other branch, or the
    branches' text-box / paragraph counts differ). Text is NOT compared: the timestamp
    spreader uses this so a pair always shares one stamp."""
    story = _story_of(p)
    if story is None:
        return None
    branch = _branch_of(story)
    ac = branch.getparent() if branch is not None else None
    if ac is None or ac.tag != _MC_AC:
        return None
    choices = ac.findall(_MC_CHOICE)
    fb = ac.find(_MC_FALLBACK)
    if len(choices) != 1 or fb is None:
        return None
    other = fb if branch.tag == _MC_CHOICE else choices[0]
    own_stories = list(branch.iter(_TXBX))
    other_stories = list(other.iter(_TXBX))
    if len(own_stories) != len(other_stories):
        return None
    twin_story = other_stories[own_stories.index(story)]
    own, twin = _story_paras(story), _story_paras(twin_story)
    if len(own) != len(twin):
        return None
    return twin[own.index(p)]


def _rejected_text(p: etree._Element) -> str:
    """``p``'s text with every tracked change rejected (insertions gone, deletions
    back) — only for comparing the two copies of a text-box paragraph."""
    out = []
    skip = (qn("w:ins"), qn("w:moveTo"), _MC_FALLBACK, qn("w:pPr"))
    text_tags = (qn("w:t"), qn("w:delText"))
    for node in p.iter(*text_tags, qn("w:tab"), qn("w:br"), qn("w:cr")):
        anc, hidden = node.getparent(), False
        while anc is not None and anc is not p:
            if anc.tag in skip:
                hidden = True
                break
            anc = anc.getparent()
        if hidden:
            continue
        out.append((node.text or "") if node.tag in text_tags
                   else ("\t" if node.tag == qn("w:tab") else "\n"))
    return "".join(out)


def textbox_copy_signature(p: etree._Element):
    """What must be identical between a text-box paragraph and its twin: accepted text,
    rejected text, and every revision mark's kind, author and date in document order
    (``w:id`` excluded: each copy carries its own)."""
    aq, dq = qn("w:author"), qn("w:date")
    revs = tuple((el.tag, el.get(aq), el.get(dq)) for el in p.iter(*_COPY_REV_TAGS))
    return paragraph_text(p), _rejected_text(p), revs


def textbox_copies(p: etree._Element) -> Optional[TextboxTarget]:
    """Pair a text-box paragraph with its twin for a LOCKSTEP tracked text edit.

    ``None`` when ``p`` is not in a text box (an ordinary paragraph: edit it alone).
    Otherwise a :class:`TextboxTarget`: both copies (``p`` may be either one), the
    single copy of a DrawingML box with no ``mc:Fallback``, or a refusal:
    :data:`TEXTBOX_VML_ONLY_REASON` (no DrawingML copy), :data:`TEXTBOX_DRIFT_REASON`
    (the Fallback holds no matching twin, or the twins' text differs),
    :data:`TEXTBOX_UNSUPPORTED_REASON` (a nested text box, a part other than the
    document body, an unrecognised AlternateContent, or tracked changes that already
    differ between the copies)."""
    story = _story_of(p)
    if story is None:
        return None
    refuse = TextboxTarget
    if (_story_of(story) is not None or any(t is not story for t in story.iter(_TXBX))
            or p.getroottree().getroot().tag in _NOT_BODY_ROOTS):
        return refuse(reason=TEXTBOX_UNSUPPORTED_REASON)
    holder = story.getparent()
    vml = holder is not None and holder.tag == _V_TEXTBOX
    branch = _branch_of(story)
    if branch is None:
        # A bare text box (no AlternateContent): DrawingML = its one copy; VML-only refuses.
        return refuse(reason=TEXTBOX_VML_ONLY_REASON) if vml else TextboxTarget(p, None)
    ac = branch.getparent()
    if ac is None or ac.tag != _MC_AC or len(ac.findall(_MC_CHOICE)) != 1:
        return refuse(reason=TEXTBOX_UNSUPPORTED_REASON)
    choice, fb = ac.find(_MC_CHOICE), ac.find(_MC_FALLBACK)
    if branch.tag == _MC_FALLBACK and next(choice.iter(_TXBX), None) is None:
        return refuse(reason=TEXTBOX_VML_ONLY_REASON)    # the Choice is no text box
    if branch.tag == _MC_CHOICE and vml:
        return refuse(reason=TEXTBOX_UNSUPPORTED_REASON)  # a VML box as the Choice
    if branch.tag == _MC_CHOICE and fb is None:
        return TextboxTarget(p, None)                     # Choice only: one copy
    twin = textbox_twin(p)
    if twin is None:
        return refuse(reason=TEXTBOX_DRIFT_REASON)
    c_p, f_p = (p, twin) if branch.tag == _MC_CHOICE else (twin, p)
    if _story_of(f_p).getparent().tag != _V_TEXTBOX:
        return refuse(reason=TEXTBOX_DRIFT_REASON)        # the Fallback is no VML text box
    if paragraph_text(c_p) != paragraph_text(f_p):
        return refuse(reason=TEXTBOX_DRIFT_REASON)
    if textbox_copy_signature(c_p) != textbox_copy_signature(f_p):
        return refuse(reason=TEXTBOX_UNSUPPORTED_REASON)  # tracked changes differ
    return TextboxTarget(c_p, f_p)


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

    Matching reads each paragraph's OWN story (``paragraph_text(p,
    txbx_marker=_TXBX_MARK)``): text that lives only in a text box never resolves to
    the host body paragraph, whose runs cannot reach it (every edit then failed with
    a misleading reason). :meth:`in_textbox` names the hosts such an anchor would
    have hit, so callers can refuse with :data:`TEXTBOX_ANCHOR_REASON`; :meth:`find`
    appends that reason to its not-found ``LookupError``.

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

    __slots__ = ("_entries", "_root", "_toc_fields", "_txbx_entries", "_txbx_paras")

    def __init__(self, root: etree._Element) -> None:
        self._root = root
        self._entries = []
        self._txbx_entries = []   # (p, own text, full text incl. text boxes)
        for p in iter_paragraphs(root):
            own = _norm(paragraph_text(p, txbx_marker=_TXBX_MARK))
            self._entries.append((p, own))
            if _TXBX_MARK in own:
                self._txbx_entries.append((p, own, _norm(paragraph_text(p))))
        self._toc_fields = None   # lazily built on the first ambiguous anchor
        self._txbx_paras = None   # lazily built on the first textbox_target()

    def find_all(self, anchor: str) -> List[etree._Element]:
        a = _norm(anchor)
        return [p for (p, t) in self._entries if a in t]

    def in_textbox(self, anchor: str) -> List[etree._Element]:
        """Host paragraphs the anchor matches ONLY when their text-box text is read
        (it lies inside a text box, or spans from body text into one). These are never
        :meth:`find_all` hits; a caller that finds nothing uses this to refuse with
        :data:`TEXTBOX_ANCHOR_REASON` instead of a plain "not found"."""
        a = _norm(anchor)
        return [p for (p, own, full) in self._txbx_entries if a in full and a not in own]

    def textbox_target(self, anchor: str) -> Optional[TextboxTarget]:
        """Resolve an anchor that lives ONLY in a text box (:meth:`in_textbox` is
        non-empty) to a lockstep pair, for tracked TEXT edits only; every other caller
        keeps refusing with :data:`TEXTBOX_ANCHOR_REASON`. ``None`` when the anchor is
        not a text-box anchor.

        The anchor must lie in ONE text-box paragraph's own text (normalized as
        :meth:`find_all` does; a nested box stands in as one marker), matched over
        every text-box paragraph except the ``mc:Fallback`` copies, which are paired,
        never matched. That paragraph and its twin come from :func:`textbox_copies`.
        Refusals: :data:`TEXTBOX_ANCHOR_REASON` (the anchor spans body and box text,
        or two box paragraphs), :data:`TEXTBOX_AMBIGUOUS_REASON` (several box
        paragraphs match), else :func:`textbox_copies`' own."""
        if not self.in_textbox(anchor):
            return None
        if self._txbx_paras is None:
            self._txbx_paras = [
                (q, _norm(paragraph_text(q, txbx_marker=_TXBX_MARK)))
                for q in self._root.iter(qn("w:p"))
                if _story_of(q) is not None
                and next(q.iterancestors(_MC_FALLBACK), None) is None]
        a = _norm(anchor)
        hits = [q for (q, t) in self._txbx_paras if a in t]
        if not hits:
            return TextboxTarget(reason=TEXTBOX_ANCHOR_REASON)
        if len(hits) > 1:
            return TextboxTarget(reason=TEXTBOX_AMBIGUOUS_REASON)
        return textbox_copies(hits[0])

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
            msg = f"anchor {anchor!r} matched {len(hits)} paragraphs (need exactly 1)"
            if not hits and self.in_textbox(anchor):
                msg += f": {TEXTBOX_ANCHOR_REASON}"
            raise LookupError(msg)
        return hits[0]


def find_paragraphs(root: etree._Element, anchor: str) -> List[etree._Element]:
    return ParaIndex(root).find_all(anchor)


def find_paragraph_candidates(root: etree._Element, anchor: str) -> List[etree._Element]:
    """:meth:`ParaIndex.candidates` for a one-off lookup."""
    return ParaIndex(root).candidates(anchor)


def in_textbox_text(p: etree._Element, s: str) -> bool:
    """True when ``s`` occurs in ``p``'s accepted-view text ONLY because its text-box
    text is read (``s`` lies inside a text box, or spans from body text into one) —
    the same rule as :meth:`ParaIndex.in_textbox`, on raw (unnormalized) text."""
    return bool(s) and s in paragraph_text(p) and s not in paragraph_text(
        p, txbx_marker=_TXBX_MARK)


def anchor_in_textbox(root: etree._Element, anchor: str) -> bool:
    """:meth:`ParaIndex.in_textbox` for a one-off lookup: does the anchor match only
    text-box text (so no body paragraph is its home)?"""
    return bool(ParaIndex(root).in_textbox(anchor))


def find_paragraph(root: etree._Element, anchor: str) -> etree._Element:
    return ParaIndex(root).find(anchor)
