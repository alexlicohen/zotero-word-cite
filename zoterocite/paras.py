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
    return list(root.iter(qn("w:p")))


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
    """
    out = []
    t_tag = qn("w:t")
    del_tag = qn("w:del")
    tab_tag = qn("w:tab")
    br_tags = (qn("w:br"), qn("w:cr"))
    for node in p.iter():
        tag = node.tag
        if tag == t_tag:
            anc = node.getparent()
            deleted = False
            while anc is not None and anc is not p:
                if anc.tag == del_tag:
                    deleted = True
                    break
                anc = anc.getparent()
            if not deleted:
                out.append(node.text or "")
        elif tag == tab_tag:
            out.append("\t")
        elif tag in br_tags:
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
