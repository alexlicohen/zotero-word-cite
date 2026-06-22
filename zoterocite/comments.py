"""Authored Word comments anchored to paragraph text.

Writes valid `word/comments.xml` (creating the part, its content-type override, and
the document relationship if absent) plus comment-range markers + a reference run in
`document.xml`. When `commentsExtended/Ids/Extensible` parts already exist, matching
entries are appended for fidelity; we never fabricate those parts from scratch
(Word opens fine with comments.xml alone), which keeps new-document output robust.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from lxml import etree

from .docxio import DOCUMENT, Docx
from .ooxml import CT, NS, REL, now_iso, qn
from .paras import find_paragraph

W = NS["w"]
RELS_PART = "word/_rels/document.xml.rels"


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
    # Do NOT sign with w:initials by default: w:author already identifies the
    # commenter and Word derives the initials avatar from it. Only emit the
    # attribute if a caller explicitly supplies non-empty initials (opt-in).
    if initials:
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
    if not doc.has("word/comments.xml"):
        return []
    root = doc.read_tree("word/comments.xml")
    res = []
    for c in root.findall(qn("w:comment")):
        text = "".join(t.text or "" for t in c.iter(qn("w:t")))
        res.append((int(c.get(qn("w:id"))), c.get(qn("w:author")) or "", text))
    return res
