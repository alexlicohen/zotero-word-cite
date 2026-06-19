"""Shared OOXML namespace helpers used across the package."""
from __future__ import annotations

from datetime import datetime

# The shape Word stores in w:date: seconds precision, trailing 'Z'.
DATE_FMT = "%Y-%m-%dT%H:%M:%SZ"


def now_iso() -> str:
    """Current LOCAL wall-clock time formatted for a w:date attribute.

    Word displays w:date literally (it does not convert to the viewer's timezone),
    and Word itself writes the author's *local* wall-clock time with a trailing
    'Z'. Storing UTC here made every edit show hours off (e.g. an 8 PM EDT edit as
    12 AM). We match Word: local wall-clock, 'Z' suffix.
    """
    return datetime.now().strftime(DATE_FMT)

# WordprocessingML namespaces we touch. Keep this the single source of truth.
NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "w14": "http://schemas.microsoft.com/office/word/2010/wordml",
    "w15": "http://schemas.microsoft.com/office/word/2012/wordml",
    "w16cid": "http://schemas.microsoft.com/office/word/2016/wordml/cid",
    "w16cex": "http://schemas.microsoft.com/office/word/2018/wordml/cex",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
    "xml": "http://www.w3.org/XML/1998/namespace",
}

# Content-type + relationship-type constants for the comment parts.
CT = {
    "comments": "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml",
}
REL = {
    "comments": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments",
}


def qn(tag: str) -> str:
    """'w:ins' -> '{namespace}ins' (lxml Clark notation)."""
    prefix, _, local = tag.partition(":")
    if not local:
        return tag
    return "{%s}%s" % (NS[prefix], local)
