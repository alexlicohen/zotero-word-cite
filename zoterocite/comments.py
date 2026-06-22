"""Authored Word comments anchored to paragraph text.

Writes valid `word/comments.xml` (creating the part, its content-type override, and
the document relationship if absent) plus comment-range markers + a reference run in
`document.xml`. When `commentsExtended/Ids/Extensible` parts already exist, matching
entries are appended for fidelity; we never fabricate those parts from scratch for a
plain top-level comment (Word opens fine with comments.xml alone), which keeps
new-document output robust.

The ONE deliberate exception is :func:`add_comment_reply`: a *native threaded
reply* (one that nests under its parent in Word's reviewing pane) cannot be
expressed in comments.xml alone -- the parent link lives in
`word/commentsExtended.xml` (``w15:paraIdParent``). So a threaded reply, and only
a threaded reply, creates `commentsExtended.xml` (and, for round-trip fidelity,
`commentsIds.xml`) when absent. Flat ``add_comment``/``add_comments_batch`` output
is unchanged -- they still emit comments.xml alone for a fresh document.
"""
from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from lxml import etree

from .docxio import DOCUMENT, Docx
from .ooxml import CT, NS, REL, now_iso, qn
from .paras import find_paragraph, iter_paragraphs, paragraph_text

W = NS["w"]
RELS_PART = "word/_rels/document.xml.rels"
COMMENTS_PART = "word/comments.xml"
EXTENDED_PART = "word/commentsExtended.xml"
IDS_PART = "word/commentsIds.xml"


@dataclass
class CommentItem:
    anchor: str
    text: str


# -- part / relationship bootstrap ------------------------------------------
def _ensure_comments_part(doc: Docx) -> etree._Element:
    part = "word/comments.xml"
    if not doc.has(part):
        nsmap = {k: NS[k] for k in ("w", "w14", "w15", "r")}
        root = etree.Element(qn("w:comments"), nsmap=nsmap)
        doc.add_part(part, etree.tostring(root, xml_declaration=True,
                                          encoding="UTF-8", standalone=True),
                     content_type=CT["comments"])
        _ensure_rel(doc, REL["comments"], "comments.xml")
    return doc.tree(part)


def _ensure_rel(doc: Docx, rel_type: str, target: str) -> None:
    if not doc.has(RELS_PART):
        nsmap = {None: NS["pr"]}
        rels = etree.Element(qn("pr:Relationships"), nsmap=nsmap)
        doc.add_part(RELS_PART, etree.tostring(rels, xml_declaration=True,
                                               encoding="UTF-8", standalone=True))
    rels = doc.tree(RELS_PART)
    for rel in rels:
        if rel.get("Target") == target and rel.get("Type") == rel_type:
            return
    used = {r.get("Id") for r in rels}
    i = 1
    while f"rId{i}" in used:
        i += 1
    el = etree.SubElement(rels, qn("pr:Relationship"))
    el.set("Id", f"rId{i}")
    el.set("Type", rel_type)
    el.set("Target", target)


# -- id / paraId allocation --------------------------------------------------
def _max_comment_id(comments_root) -> int:
    mx = -1
    for c in comments_root.findall(qn("w:comment")):
        try:
            mx = max(mx, int(c.get(qn("w:id"))))
        except (TypeError, ValueError):
            pass
    return mx


def _existing_para_ids(doc: Docx) -> set:
    ids = set()
    for part in ("word/comments.xml", DOCUMENT, "word/commentsExtended.xml"):
        if doc.has(part):
            ids |= set(re.findall(r'w1[45]:paraId="([0-9A-Fa-f]+)"', doc.raw(part).decode("utf-8", "ignore")))
    return ids


# -- comment element ---------------------------------------------------------
def _comment_el(cid: int, author: str, initials: str, date: str, text: str, para_id: str):
    c = etree.Element(qn("w:comment"))
    c.set(qn("w:id"), str(cid))
    c.set(qn("w:author"), author)
    c.set(qn("w:date"), date)
    c.set(qn("w:initials"), initials)
    p = etree.SubElement(c, qn("w:p"))
    p.set(qn("w14:paraId"), para_id)
    p.set(qn("w14:textId"), "77777777")
    ppr = etree.SubElement(p, qn("w:pPr"))
    etree.SubElement(ppr, qn("w:pStyle")).set(qn("w:val"), "CommentText")
    r0 = etree.SubElement(p, qn("w:r"))
    rpr = etree.SubElement(r0, qn("w:rPr"))
    etree.SubElement(rpr, qn("w:rStyle")).set(qn("w:val"), "CommentReference")
    etree.SubElement(r0, qn("w:annotationRef"))
    r1 = etree.SubElement(p, qn("w:r"))
    t = etree.SubElement(r1, qn("w:t"))
    t.set(qn("xml:space"), "preserve")
    t.text = text
    return c


def _wrap_paragraph_with_comment(p, cid: int) -> None:
    """commentRangeStart after pPr; commentRangeEnd + reference run before paragraph end."""
    start = etree.Element(qn("w:commentRangeStart"))
    start.set(qn("w:id"), str(cid))
    ppr = p.find(qn("w:pPr"))
    if ppr is not None:
        ppr.addnext(start)
    else:
        p.insert(0, start)
    end = etree.SubElement(p, qn("w:commentRangeEnd"))
    end.set(qn("w:id"), str(cid))
    r = etree.SubElement(p, qn("w:r"))
    rpr = etree.SubElement(r, qn("w:rPr"))
    etree.SubElement(rpr, qn("w:rStyle")).set(qn("w:val"), "CommentReference")
    ref = etree.SubElement(r, qn("w:commentReference"))
    ref.set(qn("w:id"), str(cid))


def _append_extended(doc: Docx, para_id: str) -> None:
    part = "word/commentsExtended.xml"
    if not doc.has(part):
        return
    root = doc.tree(part)
    ex = etree.SubElement(root, qn("w15:commentEx"))
    ex.set(qn("w15:paraId"), para_id)
    ex.set(qn("w15:done"), "0")


# -- private element-level attach (shared by all comment entrypoints) --------
def _attach_comment_el(doc: Docx, p, text: str, *, author: str, initials: str,
                       date: str, comments_root, used_para: set, cid: int) -> int:
    """Wrap one already-resolved paragraph ELEMENT ``p`` with comment ``cid`` and
    register the comment body + paraId. Returns the next free comment id. Single
    owner of the per-paragraph comment plumbing — anchor-based and element-based
    entrypoints both route through here so they stay byte-identical."""
    pbase = 0x5A000000
    v = pbase + cid
    while format(v, "08X") in used_para:
        v += 1
    para_id = format(v, "08X")
    used_para.add(para_id)
    _wrap_paragraph_with_comment(p, cid)
    comments_root.append(_comment_el(cid, author, initials, date, text, para_id))
    _append_extended(doc, para_id)
    return cid + 1


# -- public ------------------------------------------------------------------
def add_comments_batch(doc: Docx, items: Iterable, *, author: str, initials: str = "",
                       date: Optional[str] = None) -> List[int]:
    date = date or now_iso()
    items = [it if isinstance(it, CommentItem) else CommentItem(*it) for it in items]
    root = doc.tree(DOCUMENT)
    comments_root = _ensure_comments_part(doc)
    cid = _max_comment_id(comments_root) + 1
    if cid < 1:
        cid = 1
    used_para = _existing_para_ids(doc)
    out: List[int] = []
    for it in items:
        p = find_paragraph(root, it.anchor)
        out.append(cid)
        cid = _attach_comment_el(doc, p, it.text, author=author, initials=initials,
                                 date=date, comments_root=comments_root,
                                 used_para=used_para, cid=cid)
    return out


def add_comment(doc: Docx, anchor: str, text: str, *, author: str, initials: str = "",
                date: Optional[str] = None) -> int:
    return add_comments_batch(doc, [CommentItem(anchor, text)],
                              author=author, initials=initials, date=date)[0]


def add_comment_el(doc: Docx, p_el, text: str, *, author: str, initials: str = "",
                   date: Optional[str] = None) -> int:
    """Attach a margin comment to a CACHED paragraph ELEMENT ``p_el`` (already
    resolved by the caller against the document tree), instead of re-locating it
    by an anchor substring.

    Use this when an edit on a SIBLING paragraph may have rewritten the text this
    comment quotes: re-resolving by anchor against the post-edit tree can drop or
    mis-anchor the comment. ``p_el`` must be a ``<w:p>`` element of ``doc``'s
    cached document tree. Reuses the same comment plumbing as
    :func:`add_comments_batch` (one part, ids, paraId allocation) — nothing
    duplicated. Returns the new comment id."""
    date = date or now_iso()
    doc.tree(DOCUMENT)  # mark the document part dirty (we mutate a sub-element)
    comments_root = _ensure_comments_part(doc)
    cid = _max_comment_id(comments_root) + 1
    if cid < 1:
        cid = 1
    used_para = _existing_para_ids(doc)
    _attach_comment_el(doc, p_el, text, author=author, initials=initials, date=date,
                       comments_root=comments_root, used_para=used_para, cid=cid)
    return cid


def list_comments(doc: Docx) -> List[Tuple[int, str, str]]:
    """Return (id, author, text) for each comment."""
    if not doc.has(COMMENTS_PART):
        return []
    root = doc.read_tree(COMMENTS_PART)
    res = []
    for c in root.findall(qn("w:comment")):
        text = "".join(t.text or "" for t in c.iter(qn("w:t")))
        res.append((int(c.get(qn("w:id"))), c.get(qn("w:author")) or "", text))
    return res


# == native threaded replies ================================================
#
# OOXML comment-threading model (verified against a Word-authored fixture and
# ECMA-376 / MS-DOCX):
#   * comments.xml          -- each comment (parent OR reply) is a sibling
#                              <w:comment> with its own w:id; its content <w:p>
#                              carries a unique w14:paraId. The reply is NOT a
#                              child of the parent here -- replies are flat.
#   * document.xml          -- the whole thread shares ONE comment range. A reply
#                              adds its own <w:commentReference w:id="reply"/> run
#                              at the parent's anchor; it does NOT get its own
#                              commentRangeStart/End. Word groups a thread by the
#                              parent link below, not by range, and emitting a
#                              second range for the same span is what triggers the
#                              repair prompt -- so we deliberately add only a
#                              reference run, placed right after the parent's.
#   * commentsExtended.xml  -- THE thread link: a <w15:commentEx> per comment,
#                              keyed by its w14:paraId. A reply's entry carries
#                              w15:paraIdParent = the PARENT comment's paraId; the
#                              parent's entry has no paraIdParent. w15:done holds
#                              the resolved flag.
#   * commentsIds.xml       -- a <w16cid:commentId> per paraId with an 8-hex
#                              durableId. Not required for threading (Word rebuilds
#                              it), but written for round-trip fidelity; treated as
#                              non-fatal if it cannot be created.


def _comment_paraid_index(comments_root) -> Dict[str, int]:
    """Map each comment's content-paragraph ``w14:paraId`` -> its ``w:id``.

    This is the inverse of what ``commentsExtended`` stores (it keys on paraId),
    letting us turn a ``w15:paraIdParent`` back into a parent comment id.
    """
    out: Dict[str, int] = {}
    for c in comments_root.findall(qn("w:comment")):
        try:
            cid = int(c.get(qn("w:id")))
        except (TypeError, ValueError):
            continue
        p = c.find(qn("w:p"))
        para_id = p.get(qn("w14:paraId")) if p is not None else None
        if para_id:
            out[para_id] = cid
    return out


def _comment_paraid(comment_el) -> Optional[str]:
    """The ``w14:paraId`` of a comment's first content paragraph (its thread key)."""
    p = comment_el.find(qn("w:p"))
    return p.get(qn("w14:paraId")) if p is not None else None


def _anchor_paragraph_index(doc_root) -> Dict[int, int]:
    """Map ``commentRangeStart`` id -> index of its containing paragraph in
    ``iter_paragraphs(doc_root)``. Built once per ``read_comments`` call so we do
    not re-walk the body for every comment."""
    paras = iter_paragraphs(doc_root)
    pos = {id(p): i for i, p in enumerate(paras)}
    out: Dict[int, int] = {}
    for el in doc_root.iter(qn("w:commentRangeStart")):
        raw = el.get(qn("w:id"))
        try:
            rid = int(raw)
        except (TypeError, ValueError):
            continue
        anc = el.getparent()
        while anc is not None and anc.tag != qn("w:p"):
            anc = anc.getparent()
        if anc is not None and id(anc) in pos:
            out[rid] = pos[id(anc)]
    return out


def read_comments(doc: Docx) -> List[dict]:
    """Rich reader of every comment in *doc*.

    Returns one dict per ``<w:comment>``::

        {"id", "author", "initials", "date", "text",
         "anchor_text", "paragraph_index", "parent_id", "is_reply", "done"}

    Robust against documents with no comments part (``[]``), no
    ``commentsExtended`` (``parent_id=None``, ``done=False`` for every comment),
    and comments from multiple authors. ``parent_id`` is resolved by mapping a
    reply's ``w15:paraIdParent`` back to the parent comment's ``w:id``; a parent
    link that points at an unknown paraId degrades to ``parent_id=None``.
    """
    if not doc.has(COMMENTS_PART):
        return []
    croot = doc.read_tree(COMMENTS_PART)

    # paraId -> commentId, so we can turn a paraIdParent into a parent w:id.
    pid_to_cid = _comment_paraid_index(croot)

    # paraId -> (paraIdParent, done) from commentsExtended, if present.
    ext: Dict[str, Tuple[Optional[str], bool]] = {}
    if doc.has(EXTENDED_PART):
        try:
            eroot = doc.read_tree(EXTENDED_PART)
        except Exception:
            eroot = None
        if eroot is not None:
            for e in eroot.findall(qn("w15:commentEx")):
                pid = e.get(qn("w15:paraId"))
                if not pid:
                    continue
                done_raw = (e.get(qn("w15:done")) or "").strip().lower()
                ext[pid] = (e.get(qn("w15:paraIdParent")),
                            done_raw in ("1", "true"))

    # commentRangeStart id -> paragraph index (best-effort anchor resolution).
    anchor_idx: Dict[int, int] = {}
    paras: List = []
    if doc.has(DOCUMENT):
        droot = doc.read_tree(DOCUMENT)
        paras = iter_paragraphs(droot)
        anchor_idx = _anchor_paragraph_index(droot)

    out: List[dict] = []
    for c in croot.findall(qn("w:comment")):
        try:
            cid = int(c.get(qn("w:id")))
        except (TypeError, ValueError):
            continue
        text = "".join(t.text or "" for t in c.iter(qn("w:t")))
        para_id = _comment_paraid(c)
        parent_pid, done = ext.get(para_id, (None, False))
        parent_id = pid_to_cid.get(parent_pid) if parent_pid else None
        p_index = anchor_idx.get(cid)
        anchor_text = (paragraph_text(paras[p_index]).strip()
                       if p_index is not None and p_index < len(paras) else None)
        out.append({
            "id": cid,
            "author": c.get(qn("w:author")) or "",
            "initials": c.get(qn("w:initials")) or "",
            "date": c.get(qn("w:date")) or "",
            "text": text,
            "anchor_text": anchor_text,
            "paragraph_index": p_index,
            "parent_id": parent_id,
            "is_reply": parent_id is not None,
            "done": done,
        })
    return out


# -- extended / ids part bootstrap (created only for a threaded reply) --------
def _ensure_extended_part(doc: Docx) -> etree._Element:
    """Return the ``w15:commentsEx`` root, creating the part + its content-type
    override + document relationship if absent (mirrors ``_ensure_comments_part``)."""
    if not doc.has(EXTENDED_PART):
        nsmap = {k: NS[k] for k in ("w15", "w16cex", "w", "w14")}
        root = etree.Element(qn("w15:commentsEx"), nsmap=nsmap)
        doc.add_part(EXTENDED_PART, etree.tostring(root, xml_declaration=True,
                                                   encoding="UTF-8", standalone=True),
                     content_type=CT["commentsExtended"])
        _ensure_rel(doc, REL["commentsExtended"], "commentsExtended.xml")
    return doc.tree(EXTENDED_PART)


def _ensure_ids_part(doc: Docx) -> Optional[etree._Element]:
    """Return the ``w16cid:commentsIds`` root, creating the part if absent. Durable
    ids are not required for threading (Word rebuilds them), so a failure to create
    the part is non-fatal -- we return ``None`` and the caller skips id round-trip."""
    if not doc.has(IDS_PART):
        try:
            nsmap = {k: NS[k] for k in ("w16cid", "w")}
            root = etree.Element(qn("w16cid:commentsIds"), nsmap=nsmap)
            doc.add_part(IDS_PART, etree.tostring(root, xml_declaration=True,
                                                  encoding="UTF-8", standalone=True),
                         content_type=CT["commentsIds"])
            _ensure_rel(doc, REL["commentsIds"], "commentsIds.xml")
        except Exception:
            return None
    return doc.tree(IDS_PART)


def _ext_paraids(root) -> set:
    return {e.get(qn("w15:paraId")) for e in root.findall(qn("w15:commentEx"))}


def _ensure_commentex(ext_root, para_id: str, *, parent_para_id: Optional[str] = None,
                      done: str = "0") -> None:
    """Add a ``w15:commentEx`` for *para_id* if one is not already present.
    Idempotent: a parent that already has an entry is left untouched."""
    if para_id in _ext_paraids(ext_root):
        return
    e = etree.SubElement(ext_root, qn("w15:commentEx"))
    e.set(qn("w15:paraId"), para_id)
    if parent_para_id is not None:
        e.set(qn("w15:paraIdParent"), parent_para_id)
    e.set(qn("w15:done"), done)


def _ids_paraids(root) -> set:
    return {e.get(qn("w16cid:paraId")) for e in root.findall(qn("w16cid:commentId"))}


def _used_durable_ids(root) -> set:
    return {e.get(qn("w16cid:durableId")) for e in root.findall(qn("w16cid:commentId"))}


def _ensure_commentid(ids_root, para_id: str) -> None:
    """Add a ``w16cid:commentId`` (paraId + fresh 8-hex durableId) if absent."""
    if para_id in _ids_paraids(ids_root):
        return
    used = _used_durable_ids(ids_root)
    durable = format(secrets.randbelow(0xFFFFFFFF) + 1, "08X")
    while durable in used:
        durable = format(secrets.randbelow(0xFFFFFFFF) + 1, "08X")
    e = etree.SubElement(ids_root, qn("w16cid:commentId"))
    e.set(qn("w16cid:paraId"), para_id)
    e.set(qn("w16cid:durableId"), durable)


def _add_reply_reference(doc_root, parent_id: int, reply_id: int) -> None:
    """Place a ``<w:commentReference w:id=reply_id/>`` run immediately after the
    parent's reference run, so the reply renders inside the parent's thread at the
    same anchor. Reuses the reference-run shape from ``_wrap_paragraph_with_comment``
    -- we add a reference for the reply id, NOT a new range."""
    for ref in doc_root.iter(qn("w:commentReference")):
        if ref.get(qn("w:id")) == str(parent_id):
            parent_run = ref.getparent()  # the <w:r> wrapping the reference
            r = etree.Element(qn("w:r"))
            rpr = etree.SubElement(r, qn("w:rPr"))
            etree.SubElement(rpr, qn("w:rStyle")).set(qn("w:val"), "CommentReference")
            rref = etree.SubElement(r, qn("w:commentReference"))
            rref.set(qn("w:id"), str(reply_id))
            parent_run.addnext(r)
            return
    raise ValueError(
        f"parent comment id {parent_id} has no commentReference in {DOCUMENT}"
    )


def add_comment_reply(doc: Docx, parent_id: int, text: str, *, author: str,
                      initials: str = "", date: Optional[str] = None) -> int:
    """Add a NATIVE threaded reply to the comment whose ``w:id == parent_id``.

    The reply nests under its parent in Word's reviewing pane (modern threaded
    model) and the document opens without a repair prompt. Returns the new reply
    comment id.

    Raises ``ValueError`` if *parent_id* does not exist. Missing optional parts
    (``commentsExtended``/``commentsIds``) are created rather than corrupting the
    doc; ``commentsIds`` is best-effort (non-fatal if it cannot be created).
    """
    date = date or now_iso()
    if not doc.has(COMMENTS_PART):
        raise ValueError(f"comment id {parent_id} does not exist (no comments part)")
    comments_root = doc.tree(COMMENTS_PART)

    # Locate the parent comment + its content paraId.
    parent_el = None
    for c in comments_root.findall(qn("w:comment")):
        if c.get(qn("w:id")) == str(parent_id):
            parent_el = c
            break
    if parent_el is None:
        raise ValueError(f"comment id {parent_id} does not exist")
    parent_para_id = _comment_paraid(parent_el)
    if parent_para_id is None:
        # A parent authored without a w14:paraId cannot anchor a thread link;
        # mint one so the reply has a stable parent key.
        used = _existing_para_ids(doc)
        v = 0x5A000000 + parent_id
        while format(v, "08X") in used:
            v += 1
        parent_para_id = format(v, "08X")
        pp = parent_el.find(qn("w:p"))
        if pp is not None:
            pp.set(qn("w14:paraId"), parent_para_id)

    # New reply comment id + a unique paraId for its content paragraph.
    reply_id = _max_comment_id(comments_root) + 1
    if reply_id < 1:
        reply_id = 1
    used_para = _existing_para_ids(doc)
    used_para.add(parent_para_id)
    v = 0x5A000000 + reply_id
    while format(v, "08X") in used_para:
        v += 1
    reply_para_id = format(v, "08X")

    # 1. document.xml: a reference run for the reply at the parent's anchor.
    #    This must come FIRST so the operation is ATOMIC: if the parent has no
    #    <w:commentReference> run in document.xml, _add_reply_reference raises
    #    ValueError BEFORE comments.xml is mutated, leaving no orphan reply
    #    <w:comment> for a caller that catches the error and saves (which would
    #    trigger Word's repair prompt). The two trees are independent, so the
    #    success-path output is byte-identical to the prior (append-first) order.
    doc_root = doc.tree(DOCUMENT)
    _add_reply_reference(doc_root, parent_id, reply_id)

    # 2. comments.xml: append the reply comment (only after the doc.xml anchor
    #    is confirmed/inserted above).
    comments_root.append(_comment_el(reply_id, author, initials, date, text, reply_para_id))

    # 3. commentsExtended.xml: ensure the part, the PARENT entry, then the reply
    #    entry carrying the parent link.
    ext_root = _ensure_extended_part(doc)
    _ensure_commentex(ext_root, parent_para_id)  # parent: no paraIdParent
    _ensure_commentex(ext_root, reply_para_id, parent_para_id=parent_para_id)

    # 4. commentsIds.xml: durable ids for round-trip fidelity (non-fatal).
    ids_root = _ensure_ids_part(doc)
    if ids_root is not None:
        _ensure_commentid(ids_root, parent_para_id)
        _ensure_commentid(ids_root, reply_para_id)

    return reply_id
