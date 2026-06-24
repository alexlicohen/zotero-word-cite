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
    fallback_tag = qn("mc:Fallback")
    tab_tag = qn("w:tab")
    br_tags = (qn("w:br"), qn("w:cr"))

    def _drop(node, *, also_del: bool) -> bool:
        # True if this node lives under an mc:Fallback (duplicate of the Choice we
        # already read) or, when *also_del*, under a tracked w:del (accepted view).
        anc = node.getparent()
        while anc is not None and anc is not p:
            if anc.tag == fallback_tag or (also_del and anc.tag == del_tag):
                return True
            anc = anc.getparent()
        return False

    for node in p.iter():
        tag = node.tag
        if tag == t_tag:
            if not _drop(node, also_del=True):
                out.append(node.text or "")
        elif tag == tab_tag:
            if not _drop(node, also_del=False):
                out.append("\t")
        elif tag in br_tags:
            if not _drop(node, also_del=False):
                out.append("\n")
    return "".join(out)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def find_paragraphs(root: etree._Element, anchor: str) -> List[etree._Element]:
    a = _norm(anchor)
    return [p for p in iter_paragraphs(root) if a in _norm(paragraph_text(p))]


def find_paragraph(root: etree._Element, anchor: str) -> etree._Element:
    hits = find_paragraphs(root, anchor)
    if len(hits) != 1:
        raise LookupError(f"anchor {anchor!r} matched {len(hits)} paragraphs (need exactly 1)")
    return hits[0]
