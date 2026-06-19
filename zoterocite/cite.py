"""Deterministic citation/reference formatting — no network, pure functions.

Two styles are supported:

``vancouver``
    NIH / AMA numbered style. A single reference renders as::

        Authors. Title. Journal. Year;Vol(Issue):Pages. PMID: <pmid>. doi:<doi>.

    Authors are joined with ", " and truncated to "et al." after 6 (NIH rule).
    A bibliography is a numbered list in *input order*; an in-text citation is
    the bracketed 1-based index ``[n]``.

``author_year``
    Name-year (loosely APA-ish). A single reference renders as::

        Authors (Year). Title. Journal, Vol(Issue), Pages.

    A bibliography is sorted by first-author surname then year; an in-text
    citation is ``(FirstAuthor et al., Year)``.

Every field on :class:`Reference` is optional except the author list and title;
formatting degrades gracefully when fields are missing (empty segments are
dropped rather than rendered as stray punctuation).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Union

from . import textpatterns
from .citecheck import _normalise_doi

# Default: after this many authors, Vancouver/NIH truncates the list to "et al.".
# Per-style overrides are resolved from csldb at call time.
ET_AL_AFTER = 6


def _resolve_et_al(csl_id: Optional[str]) -> int:
    """Return the et-al threshold for *csl_id*, or the global default.

    Imports csldb lazily to avoid a circular dependency at module level.
    Returns :data:`ET_AL_AFTER` when *csl_id* is ``None``, unknown, or the
    style's ``et_al_after`` is unset (``None`` in csldb).
    """
    if csl_id is None:
        return ET_AL_AFTER
    # Lazy import: csldb imports nothing from cite, so no cycle.
    from . import csldb  # noqa: PLC0415
    style = csldb.get_style(csl_id)
    if style is None or style.et_al_after is None:
        return ET_AL_AFTER
    return style.et_al_after


@dataclass
class Reference:
    """A single bibliographic reference.

    Authors use the "Family II" form used by NIH/PubMed, e.g. ``"Cohen AL"``
    (surname, space, initials with no periods).
    """

    authors: list[str] = field(default_factory=list)
    title: str = ""
    journal: str = ""
    year: Union[int, str, None] = None
    volume: str = ""
    issue: str = ""
    pages: str = ""
    pmid: str = ""
    doi: str = ""


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _clean(value: Any) -> str:
    """Coerce to a stripped string; ``None`` becomes ``""``."""
    if value is None:
        return ""
    return str(value).strip()


def _surname(author: str) -> str:
    """Best-effort first token (the family name) of a "Family II" author string."""
    author = _clean(author)
    return author.split()[0] if author else ""


def _join_authors_vancouver(authors: Iterable[str],
                            et_al_after: int = ET_AL_AFTER) -> str:
    """Comma-join authors, truncating to "et al." after *et_al_after*."""
    names = [_clean(a) for a in authors if _clean(a)]
    if not names:
        return ""
    if len(names) > et_al_after:
        names = names[:et_al_after] + ["et al."]
    return ", ".join(names)


def _ensure_period(text: str) -> str:
    """Append a period unless ``text`` already ends with sentence punctuation."""
    text = text.rstrip()
    if not text:
        return ""
    return text if text[-1] in ".?!" else text + "."


def _vol_issue_pages(ref: Reference) -> str:
    """Render the ``Vol(Issue):Pages`` locator, dropping missing parts."""
    vol = _clean(ref.volume)
    issue = _clean(ref.issue)
    pages = _clean(ref.pages)
    out = vol
    if issue:
        out += f"({issue})"
    if pages:
        out += f":{pages}" if out else pages
    return out


# --------------------------------------------------------------------------- #
# Per-style single-reference formatting
# --------------------------------------------------------------------------- #
def _format_vancouver(ref: Reference, et_al_after: int = ET_AL_AFTER) -> str:
    """``Authors. Title. Journal. Year;Vol(Issue):Pages. PMID: x. doi:y.``"""
    parts: list[str] = []

    authors = _join_authors_vancouver(ref.authors, et_al_after=et_al_after)
    if authors:
        parts.append(_ensure_period(authors))

    title = _clean(ref.title)
    if title:
        parts.append(_ensure_period(title))

    journal = _clean(ref.journal)
    if journal:
        parts.append(_ensure_period(journal))

    # Year;Vol(Issue):Pages  — assembled as one segment.
    year = _clean(ref.year)
    locator = _vol_issue_pages(ref)
    if year and locator:
        parts.append(f"{year};{locator}.")
    elif year:
        parts.append(f"{year}.")
    elif locator:
        parts.append(f"{locator}.")

    pmid = _clean(ref.pmid)
    if pmid:
        parts.append(f"PMID: {pmid}.")

    doi = _clean(ref.doi)
    if doi:
        parts.append(f"doi:{doi}.")

    return " ".join(parts).strip()


def _format_author_year(ref: Reference) -> str:
    """``Authors (Year). Title. Journal, Vol(Issue), Pages.``"""
    parts: list[str] = []

    authors = ", ".join(_clean(a) for a in ref.authors if _clean(a))
    year = _clean(ref.year)
    head = authors
    if year:
        head = f"{head} ({year})" if head else f"({year})"
    if head:
        parts.append(_ensure_period(head))

    title = _clean(ref.title)
    if title:
        parts.append(_ensure_period(title))

    journal = _clean(ref.journal)
    locator = _vol_issue_pages(ref)
    if journal and locator:
        # locator already contains ":" between vol/issue and pages; for the
        # name-year style commas read more naturally.
        loc = locator.replace(":", ", ")
        parts.append(_ensure_period(f"{journal}, {loc}"))
    elif journal:
        parts.append(_ensure_period(journal))
    elif locator:
        parts.append(_ensure_period(locator.replace(":", ", ")))

    return " ".join(parts).strip()


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def format_reference(ref: Reference, style: str = "vancouver",
                     csl_id: Optional[str] = None) -> str:
    """Format a single :class:`Reference` in ``style``.

    ``style`` is ``"vancouver"`` (NIH/AMA numbered) or ``"author_year"``.

    ``csl_id`` optionally names the CSL catalog id (e.g. ``"nature"``,
    ``"the-lancet"``) so the formatter can look up the per-style
    ``et_al_after`` threshold.  When omitted or unknown the global default
    (:data:`ET_AL_AFTER` = 6) is used, keeping all existing callers
    byte-identical.
    """
    if style == "vancouver":
        return _format_vancouver(ref, et_al_after=_resolve_et_al(csl_id))
    if style == "author_year":
        return _format_author_year(ref)
    raise ValueError(f"unknown style: {style!r}")


def format_bibliography(refs: Iterable[Reference], style: str = "vancouver",
                        csl_id: Optional[str] = None) -> str:
    """Format a list of references as a bibliography.

    ``vancouver`` numbers entries ``"1." "2." ...`` in *input order*.
    ``author_year`` sorts by first-author surname (case-insensitive) then by
    year, and renders one entry per line with no numbering.

    ``csl_id`` is forwarded to :func:`format_reference` for per-style
    ``et_al_after`` resolution (see that function's docstring).
    """
    refs = list(refs)
    if style == "vancouver":
        lines = [f"{i}. {format_reference(r, style, csl_id=csl_id)}"
                 for i, r in enumerate(refs, 1)]
        return "\n".join(lines)

    if style == "author_year":
        def _key(r: Reference) -> tuple[str, str]:
            surname = _surname(r.authors[0]) if r.authors else ""
            return (surname.casefold(), _clean(r.year))

        ordered = sorted(refs, key=_key)
        return "\n".join(format_reference(r, style, csl_id=csl_id) for r in ordered)

    raise ValueError(f"unknown style: {style!r}")


def in_text(index_or_ref: Union[int, Reference], style: str = "vancouver") -> str:
    """In-text citation marker.

    ``vancouver`` expects a 1-based integer index and returns ``"[n]"``.
    ``author_year`` expects a :class:`Reference` and returns an author-year
    parenthetical following standard name-year convention:

    * one author    -> ``"(Smith, 2020)"``
    * two authors   -> ``"(Smith & Jones, 2020)"`` (BOTH surnames; never drop
      the second author to ``et al.``)
    * three or more -> ``"(Smith et al., 2020)"``

    The year is omitted when the reference carries no usable year.
    """
    if style == "vancouver":
        return f"[{int(index_or_ref)}]"

    if style == "author_year":
        ref = index_or_ref
        if not isinstance(ref, Reference):
            raise TypeError("author_year in_text requires a Reference")
        authors = [a for a in (ref.authors or []) if _clean(a)]
        n = len(authors)
        if n == 0:
            names = "Anon"
        elif n == 1:
            names = _surname(authors[0])
        elif n == 2:
            names = f"{_surname(authors[0])} & {_surname(authors[1])}"
        else:
            names = f"{_surname(authors[0])} et al."
        year = _clean(ref.year)
        inner = f"{names}, {year}" if year else names
        return f"({inner})"

    raise ValueError(f"unknown style: {style!r}")


# --------------------------------------------------------------------------- #
# Mapping from a PubMed-MCP-style metadata dict
# --------------------------------------------------------------------------- #
def _coerce_authors(value: Any) -> list[str]:
    """Normalise a PubMed authors value into a list of "Family II" strings.

    Accepts a list of strings, or a list of dicts with ``name``/``lastname`` +
    ``initials``/``firstname`` keys, or a single delimited string.
    """
    if value is None:
        return []
    if isinstance(value, str):
        # Split on common author delimiters.
        for sep in (";", "|"):
            if sep in value:
                return [a.strip() for a in value.split(sep) if a.strip()]
        return [value.strip()] if value.strip() else []

    authors: list[str] = []
    for item in value:
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            name = (
                item.get("name")
                or item.get("fullname")
                or item.get("collectivename")
                or ""
            ).strip()
            if not name:
                last = (item.get("lastname") or item.get("family") or "").strip()
                initials = (
                    item.get("initials")
                    or item.get("firstname")
                    or item.get("given")
                    or ""
                ).strip()
                name = f"{last} {initials}".strip()
        else:  # pragma: no cover - defensive
            name = str(item).strip()
        if name:
            authors.append(name)
    return authors


def _first_present(d: dict, *keys: str) -> str:
    """Return the first non-empty value among ``keys`` (case-insensitive)."""
    lower = {k.lower(): v for k, v in d.items()}
    for key in keys:
        if key.lower() in lower:
            val = _clean(lower[key.lower()])
            if val:
                return val
    return ""


_YEAR_RE = re.compile(r"(?<!\d)(19|20)\d{2}(?!\d)")


def _year_from(d: dict) -> Union[str, None]:
    """Extract a 4-digit publication year from common PubMed date fields.

    Returns the first ``19xx``/``20xx`` run found (handles ``"2021 Dec"``,
    ``"2021-12-01"``, ``"2021"``).  Returns ``None`` when the field carries no
    plausible 4-digit year — values such as ``"in press"`` or a stray ``"21"``
    must NOT leak into the rendered year or the author-year sort key.
    """
    raw = _first_present(d, "year", "pubdate", "pubyear", "date", "pubmedpubdate")
    if not raw:
        return None
    m = _YEAR_RE.search(raw)
    return m.group(0) if m else None


def _doi_from(d: dict) -> str:
    """Extract a DOI from ``doi`` or a PubMed ``elocationid`` field.

    Extraction goes through :func:`textpatterns.extract_dois` (which strips any
    ``doi:`` / ``DOI:`` / ``https://doi.org/`` prefix and trailing prose
    punctuation, case-insensitively, without mangling an interior ``doi:``
    substring) and is then canonicalised via :func:`citecheck._normalise_doi`
    (lowercase). Returns ``""`` when no valid DOI is present.
    """
    for key in ("doi", "elocationid", "elocation_id", "elocation"):
        raw = _first_present(d, key)
        if not raw:
            continue
        found = textpatterns.extract_dois(raw)
        if found:
            return _normalise_doi(found[0])
    return ""


def from_pubmed_record(d: dict) -> Reference:
    """Map a PubMed-MCP-style metadata dict to a :class:`Reference`.

    Tolerant of missing keys and of common key aliases (``source``/``journal``,
    ``pubdate``/``year``, ``elocationid``/``doi``, ``pages``/``pagination``).
    """
    return Reference(
        authors=_coerce_authors(
            d.get("authors") or d.get("authorlist") or d.get("author")
        ),
        title=_first_present(d, "title", "articletitle"),
        journal=_first_present(d, "journal", "source", "fulljournalname"),
        year=_year_from(d),
        volume=_first_present(d, "volume"),
        issue=_first_present(d, "issue"),
        pages=_first_present(d, "pages", "pagination", "medlinepgn"),
        pmid=_first_present(d, "pmid", "uid", "id"),
        doi=_doi_from(d),
    )
