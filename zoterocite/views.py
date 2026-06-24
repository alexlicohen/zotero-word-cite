"""Three text views of a (possibly tracked-changes) .docx, plus word/char counts.

- accepted: text as if every tracked change were ACCEPTED (insertions kept, deletions gone)
- rejected: text as if every tracked change were REJECTED (original restored)
- raw:      current markup with both insertions and deletions shown as text

Word counts for NIH limit checks are computed on the *accepted* view (what a
reviewer sees) unless you ask otherwise.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict

from lxml import etree

from .docxio import DOCUMENT, Docx
from .ooxml import NS, qn

W = NS["w"]


def _has_ancestor(node: etree._Element, localname: str, root: etree._Element) -> bool:
    target = qn("w:" + localname)
    p = node.getparent()
    while p is not None and p is not root:
        if p.tag == target:
            return True
        p = p.getparent()
    return False


def _in_fallback(node: etree._Element, root: etree._Element) -> bool:
    """True if *node* sits inside an ``mc:Fallback`` subtree.

    An ``mc:AlternateContent`` block stores the same content twice (a modern
    ``mc:Choice`` and a legacy ``mc:Fallback`` — e.g. a DrawingML textbox plus its
    VML fallback). Markup-compatibility says a consumer uses the Choice and IGNORES
    the Fallback, never both, so text under ``mc:Fallback`` must be dropped from
    every view — otherwise such textbox text (and its word count) is doubled.
    """
    target = qn("mc:Fallback")
    p = node.getparent()
    while p is not None and p is not root:
        if p.tag == target:
            return True
        p = p.getparent()
    return False


def _extract(root: etree._Element, mode: str) -> str:
    out = []
    t_tag, del_tag = "{%s}t" % W, "{%s}delText" % W
    p_tag, tab_tag = "{%s}p" % W, "{%s}tab" % W
    br_tags = ("{%s}br" % W, "{%s}cr" % W)
    for node in root.iter():
        tag = node.tag
        if tag == t_tag:
            if _in_fallback(node, root):
                continue
            if mode == "accepted" and _has_ancestor(node, "del", root):
                continue
            if mode == "rejected" and _has_ancestor(node, "ins", root):
                continue
            # Tracked moves: moveFrom/moveTo content uses normal <w:t>, so both
            # copies would otherwise be counted. Drop the location that does not
            # survive in each resolved view (raw shows both).
            if mode == "accepted" and _has_ancestor(node, "moveFrom", root):
                continue
            if mode == "rejected" and _has_ancestor(node, "moveTo", root):
                continue
            out.append(node.text or "")
        elif tag == del_tag:
            if mode == "accepted":
                continue
            if _in_fallback(node, root):
                continue
            # Insert-then-deleted text (<w:ins><w:del><w:delText>): rejecting all
            # changes removes the insertion entirely, so it must not appear in the
            # rejected view. (raw still shows it.)
            if mode == "rejected" and _has_ancestor(node, "ins", root):
                continue
            out.append(node.text or "")
        elif tag == tab_tag:
            if _in_fallback(node, root):
                continue
            out.append("\t")
        elif tag in br_tags:
            if _in_fallback(node, root):
                continue
            out.append("\n")
        elif tag == p_tag:
            # An mc:Fallback's inner <w:p> (a VML textbox paragraph) duplicates a
            # Choice paragraph already emitted, so it must not add another newline.
            if _in_fallback(node, root):
                continue
            out.append("\n")
    return "".join(out)


def read_views(path: str | Path) -> Dict[str, str]:
    doc = Docx(path)
    root = doc.read_tree(DOCUMENT)
    return {m: _extract(root, m).strip() for m in ("raw", "accepted", "rejected")}


def counts(text: str) -> Dict[str, int]:
    return {
        "words": len(text.split()),
        "chars": len(text),
        "chars_no_space": len(text.replace(" ", "").replace("\n", "").replace("\t", "")),
    }
