"""Round-trip-safe reading and writing of .docx (OOXML zip) packages.

A .docx is a zip of XML parts. The golden rule for editing grant documents is:
*touch only the parts you change, preserve everything else byte-for-byte and in
order*, so Word never shows the "unreadable content / repair" dialog.

`Docx` loads every part into memory, hands out parsed lxml trees on demand, and
re-serializes only the parts whose tree was mutated when you `save()`.
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

from lxml import etree

from .ooxml import NS, qn

DOCUMENT = "word/document.xml"


class DocxLoadError(Exception):
    """A .docx package could not be opened or a requested part could not be
    parsed: a non-zip / truncated / renamed file, a zip missing the part, or a
    part whose XML is malformed.  Callers (CLI, validate) catch this to surface
    a clean message instead of a raw zipfile/lxml traceback.
    """


class Docx:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._order: List[zipfile.ZipInfo] = []
        self._raw: Dict[str, bytes] = {}
        self._trees: Dict[str, etree._Element] = {}
        self._dirty: set[str] = set()
        try:
            with zipfile.ZipFile(self.path, "r") as z:
                for info in z.infolist():
                    self._order.append(info)
                    self._raw[info.filename] = z.read(info.filename)
        except (zipfile.BadZipFile, OSError, EOFError) as e:
            # Non-zip / truncated / renamed file, unreadable member, or a
            # missing path (FileNotFoundError is an OSError subclass).
            raise DocxLoadError(f"not a readable .docx: {self.path} ({e})") from e

    # -- part access ---------------------------------------------------------
    def has(self, part: str) -> bool:
        return part in self._raw

    def raw(self, part: str) -> bytes:
        return self._raw[part]

    def _parsed(self, part: str) -> etree._Element:
        """Parse (and cache) a part's tree, raising DocxLoadError on a missing
        part or malformed XML instead of leaking KeyError/XMLSyntaxError/IndexError.
        """
        if part not in self._trees:
            if part not in self._raw:
                raise DocxLoadError(f"missing part {part!r} in {self.path}")
            try:
                self._trees[part] = etree.fromstring(self._raw[part])
            except etree.XMLSyntaxError as e:
                raise DocxLoadError(f"corrupt XML in {part!r}: {e}") from e
            except IndexError as e:  # some malformed packages surface here
                raise DocxLoadError(f"corrupt XML in {part!r}: {e}") from e
        return self._trees[part]

    def tree(self, part: str = DOCUMENT) -> etree._Element:
        """Parsed root element for a part. Mutating it marks the part dirty."""
        el = self._parsed(part)
        self._dirty.add(part)  # handing out the mutable tree => assume edited
        return el

    def read_tree(self, part: str = DOCUMENT) -> etree._Element:
        """Parsed root element for read-only inspection (does NOT mark dirty)."""
        return self._parsed(part)

    def set_part_bytes(self, part: str, data: bytes) -> None:
        self._raw[part] = data
        self._trees.pop(part, None)
        self._dirty.discard(part)

    def parts(self) -> List[str]:
        return [i.filename for i in self._order]

    # -- save ----------------------------------------------------------------
    def _flush_trees(self) -> None:
        for part in list(self._dirty):
            self._raw[part] = etree.tostring(
                self._trees[part], xml_declaration=True, encoding="UTF-8", standalone=True
            )
        self._dirty.clear()

    def to_bytes(self) -> bytes:
        self._flush_trees()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for info in self._order:
                data = self._raw[info.filename]
                # preserve original compression type per part
                z.writestr(info, data, compress_type=info.compress_type)
        return buf.getvalue()

    def save(self, out: Optional[str | Path] = None) -> Path:
        out = Path(out) if out else self.path
        out.write_bytes(self.to_bytes())
        return out

    # -- convenience ---------------------------------------------------------
    def add_part(self, name: str, data: bytes, content_type: Optional[str] = None) -> None:
        """Add a brand-new part (e.g. comments.xml). Registers a [Content_Types] override."""
        if name not in self._raw:
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            self._order.append(info)
        self._raw[name] = data
        self._trees.pop(name, None)
        self._dirty.discard(name)
        if content_type:
            self._register_content_type(name, content_type)

    def _register_content_type(self, name: str, content_type: str) -> None:
        ct = self.tree("[Content_Types].xml")
        part_name = "/" + name
        for ov in ct.findall(qn("ct:Override")):
            if ov.get("PartName") == part_name:
                return
        ov = etree.SubElement(ct, qn("ct:Override"))
        ov.set("PartName", part_name)
        ov.set("ContentType", content_type)


def text_of(el: etree._Element, *, include_ins=True, include_del=False) -> str:
    """Concatenate run text under `el`, honoring tracked-change wrappers.

    include_ins=True  keep text inside <w:ins>
    include_del=True  keep text inside <w:del> (which uses <w:delText>)
    """
    out: List[str] = []
    W = NS["w"]
    for node in el.iter():
        tag = node.tag
        if tag == "{%s}t" % W:
            if _within(node, el, "ins") and not include_ins:
                continue
            out.append(node.text or "")
        elif tag == "{%s}delText" % W:
            if include_del:
                out.append(node.text or "")
        elif tag == "{%s}tab" % W:
            out.append("\t")
        elif tag in ("{%s}br" % W, "{%s}cr" % W):
            out.append("\n")
        elif tag == "{%s}p" % W:
            out.append("\n")
    return "".join(out)


def _within(node, root, localname: str) -> bool:
    """Is `node` inside a <w:LOCALNAME> ancestor (stopping at root)?"""
    target = qn("w:" + localname)
    parent = node.getparent()
    while parent is not None and parent is not root.getparent():
        if parent.tag == target:
            return True
        if parent is root:
            break
        parent = parent.getparent()
    return False
