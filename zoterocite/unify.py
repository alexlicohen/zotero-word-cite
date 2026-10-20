"""Reference unification — orchestrate a collaborator's messy draft into a
document whose citations are consistent **live Zotero fields**, importing any
missing items into the shared group library.

This module is the confirm-driven *pipeline* tying together the already-built
pieces (:mod:`refextract`, :mod:`refresolve`, :mod:`zotero`, :mod:`citecheck`,
:mod:`citeconvert`, :mod:`zoterofield`). It is deliberately split into two passes:

``plan_unification(path, ...)``
    A **read-only** planning pass. Inventories every citation-like object,
    resolves each free-text reference / placeholder to canonical metadata +
    confidence, links in-text markers to reference-list entries, checks the
    group library for existing items, flags retractions and metadata
    divergence, and tiers each reference (``high`` → auto, ``medium``/``low`` →
    confirm). NOTHING is written and the document is NOT modified. The returned
    plan is what a human (or model) reviews before confirming.

``apply_unification(path, plan, decisions, *, out, ...)``
    The **write** pass. Honours the plan + the user's ``decisions`` (which
    medium/low refs to accept, how placeholders resolve, whether to add missing
    items). Adds accepted-and-missing references to the group library (tagged +
    in the "Imported — review" collection), then rewrites the document: foreign
    FIELD-based citations via :func:`citeconvert.convert_to_zotero`, and
    plain-text in-text cites / resolved placeholders via
    :func:`zoterofield.insert_citation` (as tracked changes when ``track``).

Confirmation model (confirmed with the user)
--------------------------------------------
* HIGH-confidence references → auto-accepted (in ``plan["auto"]``).
* MEDIUM / LOW references → per-item confirm (in ``plan["needs_confirmation"]``);
  only applied if listed in ``decisions["accept"]``.
* Placeholders → always need input; resolved only via
  ``decisions["placeholder_resolutions"]``.

The ONLY network mutation is the Zotero write, which is confirm-gated,
dedup-hard, tagged, and reversible. Tests monkeypatch every network call.
"""
from __future__ import annotations

import functools
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import entrez
from . import refextract
from . import refresolve
from . import zotero
from . import citecheck
from . import citeconvert
from . import zoterofield
from .ooxml import now_iso, qn

# The dedicated collection + tag every added item gets (confirmed design).
IMPORTED_COLLECTION = "Imported — review"
ADDED_TAG = "added-by:zotero-word-cite"


def _resolution_heartbeat(prefix: str, done: int, total: int, state: dict) -> None:
    """Throttled STDERR liveness tick for a per-entry resolution/placement loop.

    Each entry can trigger a live Crossref/PubMed lookup, so a long reflist would
    otherwise look like an opaque multi-minute hang.  Emits via
    :func:`zotero._progress` (STDERR only — never in ``--json`` stdout) at most
    once per ~10 entries or ~2 s, plus a final tick at completion.  ``state`` is a
    per-loop mutable dict the caller seeds as ``{}``; kept cheap and non-fragile
    (tests do not assert stderr)."""
    now = time.time()
    if done >= total or done % 10 == 0 or now - state.get("t", 0.0) >= 2.0:
        state["t"] = now
        zotero._progress(f"{prefix}: resolved {done}/{total} references…")

# How a confidence level maps to a tier bucket.
_TIER_BY_CONFIDENCE = {
    "high": "high",
    "medium": "medium",
    "low": "low",
    "none": "low",
}

_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
# Surname token of an author-year item: refextract.AY_NAME is the single owner of
# the Unicode-aware name shape ("Pagán", "Ørsted", "Ó Briain").
_AY_NAME_RE = re.compile(refextract.AY_NAME)
# "(Smith, 2020; Table 2)": a capitalised non-surname in a year-less chunk is
# prose, not an undated work (refextract's own stop list).
_AY_STOP_RE = re.compile(refextract._AY_STOP)
# A 4-digit year inside an item or a reflist entry plus its optional same-author
# letter suffix ("2020a"): group 1 = digits, group 2 = suffix or None. ``\b``
# refused "2020a" outright; dropping the suffix instead would link "(Smith,
# 2020b)" to whichever Smith-2020 entry came first. The suffix is one lowercase
# letter not followed by another letter ("2020abc" is no suffix).
_ITEM_YEAR_RE = re.compile(r"(?<!\d)((?:1[6-9]|20)\d{2})(?!\d)([a-z](?![A-Za-z]))?")
# APA's suffix-only continuation of a suffixed year: the ", b" of "2020a, b".
_ITEM_SUFFIX_CONT_RE = re.compile(r",\s*([a-z])(?=\s*(?:[,;]|$))")
# Leading particle run of a reflist author ("van der Berg H" / "de Vries A").
_REF_FIRST_AUTHOR_RE = re.compile(
    rf"^\s*(?P<particles>(?:[a-z]{{1,4}}\s+){{0,3}})(?P<name>{refextract.AY_NAME})"
)


# ===========================================================================
# Internal helpers — linking in-text markers to reflist entries
# ===========================================================================

def _numbering_to_int(numbering: Optional[str]) -> Optional[int]:
    """Parse a reflist ``numbering`` token (e.g. ``"[12]"``, ``"12."``) to int."""
    if not numbering:
        return None
    m = re.search(r"\d+", numbering)
    return int(m.group(0)) if m else None


def _numeric_marker_targets(marker_text: str) -> List[int]:
    """All citation numbers in a numeric in-text marker, EXPANDING ranges.

    ``"[3,4]"`` → ``[3, 4]``; ``"[5-7]"`` → ``[5, 6, 7]``. A Vancouver/NIH range
    cites *every* reference in it, so the interior numbers must be linked too —
    splitting on the hyphen (the old behaviour) linked only the two endpoints and
    silently left every middle reference of a range with no in-text anchor.
    """
    inner = marker_text.strip().strip("[]()")
    nums: List[int] = []
    for part in inner.split(","):
        part = part.strip()
        m = re.fullmatch(r"(\d+)\s*[-–]\s*(\d+)", part)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            # Expand a sane ascending span; on a reversed or absurdly large range
            # (a typo) fall back to just the endpoints rather than emit thousands.
            if lo <= hi and hi - lo <= 100:
                nums.extend(range(lo, hi + 1))
            else:
                nums.extend(n for n in (lo, hi))
        elif part.isdigit():
            nums.append(int(part))
    return nums


def _author_year_items(marker_text: str) -> List[Tuple[str, str]]:
    """The cited items of an author-year marker, in printed order, as
    ``[(first_author_surname, year), ...]`` — the author-year counterpart of
    :func:`_numeric_marker_targets` and the SINGLE owner of grouped-marker
    splitting (linking, apply ordering, ``intext_keys`` item counts and PubMed
    candidates all call it).

    ``"(Smith et al., 2020)"`` → ``[("Smith", "2020")]``;
    ``"(Alpha et al., 2022; Beta, 2012)"`` and
    ``"(Alpha et al., 2022 and Beta et al., 2012)"`` → two items;
    ``"(Alpha and Beta, 2020)"`` → ONE two-author item ``[("Alpha", "2020")]``;
    ``"Delta and Epsilon (2019)"`` / ``"Gamma et al., in 2019"`` → one item.

    Groups split on ``;`` always and on ``and`` / ``&`` only where BOTH sides
    carry a year: the pieces between connectives are merged left to right and an
    item closes at the first piece that holds a year. The year KEEPS its letter
    suffix ("2020a"), which linking must honour (:func:`_author_year_item_link`).

    Every year of an item is its own cited work, so no co-citation can vanish:
    ``"Smith, 2019, 2020"`` and ``"Smith, 2020a, b"`` are two items each, and a
    year-only chunk ``"(Smith, 2019; 2020)"`` / ``"(Smith, 2019 and 2020)"``
    inherits the previous item's surname. A year with no name anywhere before it
    becomes ``("", year)``, which never links, so the all-or-nothing apply leaves
    its group as plain text.

    A ``;`` chunk left with a NAME but no year ("Smith et al., n.d.", "Smith,
    in press") is still a cited work: it becomes an item with year ``""``, which
    never links, so the all-or-nothing apply leaves its group as plain text
    instead of silently citing the other items. Leftover prose with no name
    ("and references therein") never forms an item.
    """
    body = marker_text.replace("(", " ").replace(")", " ")
    items: List[Tuple[str, str]] = []

    def _years(text: str) -> List[str]:
        out: List[str] = []
        for m in _ITEM_YEAR_RE.finditer(text):
            out.append(m.group(1) + (m.group(2) or ""))
            if m.group(2):
                pos = m.end()
                while True:
                    c = _ITEM_SUFFIX_CONT_RE.match(text, pos)
                    if c is None:
                        break
                    out.append(m.group(1) + c.group(1))
                    pos = c.end()
        return out

    def _emit(text: str, years: List[str]) -> None:
        name = _AY_NAME_RE.search(text)
        if name and (years or not _AY_STOP_RE.match(name.group(0))):
            surname = re.sub(r"\s+", " ", name.group(0))
        elif years:
            # Year-only chunk: the previous item's author ("" when there is none).
            surname = items[-1][0] if items else ""
        else:
            return
        for y in years or [""]:
            items.append((surname, y))

    for chunk in body.split(";"):
        acc: List[str] = []
        dated = False
        for piece in re.split(r"\s+(?:and|&)\s+|\s*&\s*", chunk):
            acc.append(piece)
            if _ITEM_YEAR_RE.search(piece) is None:
                continue
            item = " and ".join(acc)
            acc = []
            dated = True
            _emit(item, _years(item))
        if acc and not dated:
            # A wholly year-less chunk is an undated work; a leftover AFTER a
            # dated item in the same chunk is trailing prose and is ignored.
            _emit(" and ".join(acc), [])
    return items


def _split_year(year: str) -> Tuple[str, str]:
    """``"2020b"`` → ``("2020", "b")``; ``"2020"`` → ``("2020", "")``."""
    return year[:4], year[4:]


def _surname_key(name: str) -> str:
    """Comparable key for a surname: casefolded AND accent-insensitive.

    Accent-insensitive by design: reference lists exported from PubMed/Crossref
    or retyped by a collaborator often drop diacritics the in-text marker kept
    ("Pagan" vs "Pagán", "Orsted" vs "Ørsted"). A false link needs two distinct
    surnames that differ ONLY by diacritics AND share the cited year, which is
    far rarer than the dropped-accent miss. Reuses the toolkit's surname fold
    (:func:`refresolve._normalize_surname`), then drops spaces/hyphens so
    "Ó Briain" == "OBriain" and "García-López" == "Garcia Lopez".
    """
    return re.sub(r"[\s\-]", "", refresolve._normalize_surname(name))


@functools.lru_cache(maxsize=4096)
def _reflist_first_author_keys(text: str) -> Tuple[str, ...]:
    """Surname keys of a reference-list entry's first author (best effort).

    Strips the leading numbering, then reads the leading name with any
    lowercase particles. Returns both the with-particle key ("devries") and the
    bare-name key ("vries"), so "(De Vries, 2020)" and "(Vries, 2020)" both
    match "de Vries A". ``()`` when no name leads the entry. Cached: the
    linker asks once per (item, entry) pair.
    """
    stripped = re.sub(r"^\s*(\[\d+\]|\(?\d+\)?[.)])\s*", "", text)
    m = _REF_FIRST_AUTHOR_RE.match(stripped)
    if not m:
        return ()
    bare = _surname_key(m.group("name"))
    full = _surname_key(m.group("particles") + m.group("name"))
    return tuple(k for k in dict.fromkeys((full, bare)) if k)


@functools.lru_cache(maxsize=4096)
def _ref_years(text: str) -> frozenset:
    """The ``(digits, suffix)`` years a reflist entry carries (suffix ``""``
    when none). Cached: the linker asks per item."""
    return frozenset((m.group(1), m.group(2) or "") for m in _ITEM_YEAR_RE.finditer(text))


def _ref_matches_item(ref_text: str, surname: str, year: str) -> bool:
    """True when a reflist entry's first author is ``surname`` and it carries
    ``year`` — the author-year link predicate. A suffixed entry year ("2020a")
    matches only the same suffix; an unsuffixed entry year matches the item's
    digits whatever its suffix (Vancouver lists drop it). Uniqueness across
    the list is :func:`_author_year_item_link`'s job."""
    key = _surname_key(surname)
    digits, suffix = _split_year(year)
    if not (key and digits) or key not in _reflist_first_author_keys(ref_text):
        return False
    return any(d == digits and (not s or s == suffix) for d, s in _ref_years(ref_text))


def _author_year_item_link(surname: str, year: str, reflist: List[dict]) -> Optional[int]:
    """The ONE reflist entry index an author-year item links to, or ``None``.

    Every entry that matches (:func:`_ref_matches_item`) is a candidate. One
    candidate links; several are narrowed to those carrying the item's exact
    suffix; anything still ambiguous ("(Smith, 2020)" against two unsuffixed
    Smith-2020 entries) stays UNLINKED rather than taking the first, so a
    co-citation is never silently pointed at the wrong work.
    """
    hits = [e for e in reflist if _ref_matches_item(e["text"], surname, year)]
    digits, suffix = _split_year(year)
    if len(hits) > 1 and suffix:
        hits = [e for e in hits if (digits, suffix) in _ref_years(e["text"])]
    return hits[0]["index"] if len(hits) == 1 else None


def _author_year_item_links(marker_text: str, reflist: List[dict]) -> List[Optional[int]]:
    """Per item of an author-year marker, the reflist entry index it links to,
    or ``None`` (unlinked or ambiguous). Aligned with :func:`_author_year_items`;
    plan-time linking AND apply-time placement both read it."""
    return [_author_year_item_link(s, y, reflist) for s, y in _author_year_items(marker_text)]


def _marker_item_count(marker: dict) -> int:
    """How many works a marker cites: numeric → every expanded number; author-
    year → its items (at least 1, so an unparseable marker still counts as one)."""
    if marker.get("kind") == "numeric":
        return len(_numeric_marker_targets(marker.get("text") or ""))
    return max(1, len(_author_year_items(marker.get("text") or "")))


def _link_intext_to_reflist(intext: List[dict], reflist: List[dict]) -> Dict[int, List[int]]:
    """Map each reflist entry index → list of in-text marker indices pointing to it.

    * numeric marker → reflist entry whose ``numbering`` equals the cited number.
    * author_year marker → EVERY item of the marker (:func:`_author_year_items`)
      links to the ONE reflist entry whose first-author surname matches
      (casefolded, accent-insensitive) and whose text carries the item's year
      and suffix (:func:`_author_year_item_link`; ambiguous → unlinked).
      A grouped "(Alpha, 2022; Beta, 2012)" therefore links to both references.

    Returns ``{reflist_index: [intext_index, ...]}``. Markers that cannot be
    linked are reported separately by the caller (see ``unlinked_intext``).
    """
    # Index reflist by parsed numbering for O(1) numeric lookup.
    by_number: Dict[int, int] = {}
    for entry in reflist:
        n = _numbering_to_int(entry.get("numbering"))
        if n is not None and n not in by_number:
            by_number[n] = entry["index"]

    links: Dict[int, List[int]] = {}

    def _add(ref_idx: int, it_idx: int) -> None:
        links.setdefault(ref_idx, [])
        if it_idx not in links[ref_idx]:
            links[ref_idx].append(it_idx)

    for marker in intext:
        it_idx = marker["index"]
        if marker["kind"] == "numeric":
            for num in _numeric_marker_targets(marker["text"]):
                if num in by_number:
                    _add(by_number[num], it_idx)
        else:  # author_year
            for ref_idx in _author_year_item_links(marker["text"], reflist):
                if ref_idx is not None:
                    _add(ref_idx, it_idx)
    return links


# Default bound on DISTINCT (surname, year) PubMed queries per plan.
INTEXT_CANDIDATE_QUERY_CAP = 60
# Candidates kept per unlinked author-year item.
_INTEXT_CANDIDATES_PER_ITEM = 5


def _plan_intext_inventory(intext: List[dict], reflist: List[dict]) -> List[dict]:
    """Every in-text marker as the plan reports it: ``{index, text, kind,
    n_items}`` plus, for author-year markers, ``items`` =
    ``[{item, surname, year, ref_index}]`` (``ref_index`` ``None`` = that item
    has no reference-list link). This is what ``decisions["intext_keys"]`` is
    validated against at apply time."""
    out: List[dict] = []
    for m in intext:
        entry = {"index": m["index"], "text": m["text"], "kind": m["kind"],
                 "n_items": _marker_item_count(m)}
        if m["kind"] == "author_year":
            links = _author_year_item_links(m["text"], reflist)
            entry["items"] = [
                {"item": i, "surname": s, "year": y, "ref_index": links[i]}
                for i, (s, y) in enumerate(_author_year_items(m["text"]))
            ]
        out.append(entry)
    return out


def _pubmed_author_year_query(surname: str, year: str) -> Tuple[List[dict], str]:
    """Up to 5 PubMed records whose FIRST author is ``surname`` and publication
    year is ``year``: ``(candidates, status)``. Never raises; status is ``ok``,
    ``no_hits``, ``degraded`` (esearch/efetch failure) or ``error``."""
    name = f'"{surname}"' if " " in surname else surname
    term = f"{name}[1au] AND {year}[dp]"
    try:
        pmids, st = entrez.esearch_pmids_status(term, retmax=_INTEXT_CANDIDATES_PER_ITEM)
        if not pmids:
            return [], ("degraded" if st.get("degraded") else "no_hits")
        recs, fst = entrez.efetch_pubmed_status(pmids)
    except Exception:  # noqa: BLE001 — a candidate search must never fail planning
        return [], "error"
    cands: List[dict] = []
    for pmid in pmids[:_INTEXT_CANDIDATES_PER_ITEM]:
        rec = recs.get(pmid)
        if not rec:
            continue
        structured = rec.get("authors_structured") or []
        if structured and isinstance(structured[0], dict):
            first = structured[0].get("family") or ""
        else:
            plain = rec.get("authors") or []
            first = str(plain[0]).rsplit(" ", 1)[0] if plain else ""
        cands.append({
            "pmid": str(pmid),
            "doi": rec.get("doi") or None,
            "title": rec.get("title") or "",
            "first_author": first,
            "year": rec.get("year") or "",
            "journal": rec.get("journal") or "",
        })
    if not cands and fst.get("degraded"):
        return [], "degraded"
    return cands, ("ok" if cands else "no_hits")


def _pubmed_intext_candidates(plan_intext: List[dict], *, max_queries: int) -> Tuple[dict, dict]:
    """CONFIRM-ONLY PubMed candidates for every author-year item with no
    reference-list link: ``({str(intext_index): [item_entry, ...]}, summary)``.

    Nothing here is ever applied: a candidate reaches the document only when the
    user copies its PMID/DOI into ``decisions["intext_keys"]``. Bounded: one
    query per DISTINCT (surname key, year), at most ``max_queries`` of them;
    items past the cap get status ``not_queried_cap``. A failed search records
    a status and never raises.
    """
    wanted: List[Tuple[dict, dict]] = []   # (marker entry, item entry)
    for m in plan_intext:
        for it in m.get("items") or []:
            if it.get("ref_index") is None:
                wanted.append((m, it))
    cache: Dict[Tuple[str, str], Tuple[List[dict], str]] = {}
    out: Dict[str, List[dict]] = {}
    n_capped = 0
    hb: dict = {}
    for n, (m, it) in enumerate(wanted, 1):
        # PubMed's [dp] knows no "2020a": query by the digits, so "2020a" and
        # "2020b" by one author share a query (and its candidates).
        digits = _split_year(it["year"])[0]
        qkey = (_surname_key(it["surname"]), digits)
        if not digits:
            cands, status = [], "no_year"     # undated item: nothing to search by
        elif not qkey[0]:
            cands, status = [], "no_surname"  # year-only item with no author before it
        elif qkey not in cache:
            if len(cache) >= max(0, int(max_queries)):
                cands, status = [], "not_queried_cap"
                n_capped += 1
            else:
                cache[qkey] = _pubmed_author_year_query(it["surname"], digits)
                cands, status = cache[qkey]
        else:
            cands, status = cache[qkey]
        _resolution_heartbeat("unify pubmed candidates", n, len(wanted), hb)
        out.setdefault(str(m["index"]), []).append({
            "item": it["item"], "surname": it["surname"], "year": it["year"],
            "status": status, "candidates": [dict(c) for c in cands],
        })
    summary = {
        "items": len(wanted),
        "queries": len(cache),
        "query_cap": int(max_queries),
        "items_capped": n_capped,
        "degraded": sum(1 for _c, s in cache.values() if s in ("degraded", "error")),
    }
    return out, summary


def _decisions_template() -> dict:
    """The decisions JSON shape, emitted in every plan so a user can copy it."""
    return {
        "accept": [],
        "placeholder_resolutions": {},
        "intext_keys": {},
        "add_missing": True,
        "_help": {
            "accept": "ref_index values from needs_confirmation.references to accept "
                      "(HIGH-tier refs in 'auto' are accepted without listing).",
            "placeholder_resolutions": '{"<ph_index>": "suggestion" | <candidate index> '
                                       '| {metadata} | null}',
            "intext_keys": '{"<intext index>" or "<exact marker text>": "<identifier>" '
                           'or ["<identifier>", ...] one per cited item, in item order}. '
                           "Identifier: a DOI (10.xxxx/...), a PMID ('PMID:123' or bare "
                           "digits) or an 8-character Zotero item key in the group "
                           "library. An index key wins over a text key for the same "
                           "marker; a text key applies to every marker with that text. "
                           "Use for markers with no reference-list link "
                           "(see 'intext' / 'intext_candidates'); copy a candidate's "
                           "pmid or doi here to confirm it.",
            "add_missing": "false = never create Zotero items (same as --no-add-missing)",
        },
    }


def _candidates_divergent(resolved: dict) -> bool:
    """Heuristic divergence flag: the top candidate's title/year disagrees with
    the cited text strongly enough to suspect a mis-cite or fabricated ref.

    We reuse refresolve's own validators against the *input text* — if the
    matched metadata's year is present but absent from the cited text, OR the
    first-author surname is present but absent from the cited text, flag it. This
    catches AI-hallucinated references whose canonical match disagrees with what
    the collaborator wrote.
    """
    meta = resolved.get("metadata")
    if not meta:
        return False
    text = resolved.get("input") or ""
    candidate = {
        "authors": [
            {"family": (a.get("family") or "")} if isinstance(a, dict) else {"family": str(a)}
            for a in (meta.get("authors") or [])
        ],
        "year": meta.get("year"),
    }
    # If there is an identifier source (doi/pmid), the metadata is authoritative
    # and any text disagreement is the *text's* problem — still worth flagging.
    year = meta.get("year")
    text_years = set(_YEAR_RE.findall(text))
    year_div = bool(year) and bool(text_years) and (year not in text_years)

    author_div = False
    authors = meta.get("authors") or []
    if authors:
        first = authors[0]
        fam = (first.get("family") if isinstance(first, dict) else str(first)) or ""
        fam = fam.strip()
        # Only treat as divergence when the input *looks* like a full reference
        # (long enough to contain an author block), not a bare placeholder.
        if fam and len(text) > 25 and not re.search(re.escape(fam), text, re.IGNORECASE):
            author_div = True

    return year_div or author_div


def _top_candidate(resolved: dict) -> Optional[dict]:
    cands = resolved.get("candidates") or []
    return cands[0] if cands else None


# ===========================================================================
# plan_unification — read-only planning pass
# ===========================================================================

def plan_unification(
    path,
    *,
    fetch: bool = True,
    check_retractions: bool = True,
    offline: bool = False,
    max_candidate_queries: int = INTEXT_CANDIDATE_QUERY_CAP,
) -> dict:
    """Read-only planning pass for reference unification (NO writes, NO doc edits).

    Inventories every citation-like object via :func:`refextract.extract_references`,
    resolves each reference-list entry and placeholder via
    :func:`refresolve.resolve_reference`, links in-text markers to reflist entries,
    checks the group library + Retraction Watch, tiers every item, and returns a
    structured plan a human/model reviews before confirming.

    Parameters
    ----------
    path:
        Path to the .docx draft.
    fetch:
        Passed through to :func:`refresolve.resolve_reference` (``False`` =
        identifier-only, no network).  Ignored (forced ``False``) when
        ``offline=True``.
    check_retractions:
        When ``True``, load the Retraction Watch DB (cached/offline-resilient)
        and flag resolved DOIs that have been retracted.
    offline:
        **Strict offline kill-switch.** When ``True``:

        * ``fetch`` is forced to ``False`` — no Crossref/PubMed resolution.
        * The Zotero library read uses cache-only (``max_age_hours=inf``); if
          no usable cache exists, degrades to an empty DOI index rather than
          attempting a network call.  Zero network calls are guaranteed.

        The default (``False``) preserves the existing online behaviour.
    max_candidate_queries:
        Bound on DISTINCT (first-author surname, year) PubMed searches made for
        author-year items with no reference-list link (default
        :data:`INTEXT_CANDIDATE_QUERY_CAP`). Searches run only when ``fetch``
        is on (never offline / ``--no-fetch``); ``0`` disables them.

    Returns
    -------
    dict
        ``{"summary", "references", "placeholders", "foreign_fields",
        "needs_confirmation", "auto", "intext", "intext_candidates",
        "decisions_template"}`` — see module docstring / spec.
        ``intext_candidates`` maps ``str(intext index)`` to per-item PubMed
        candidates; they are CONFIRM-ONLY and never applied unless copied into
        ``decisions["intext_keys"]``.
    """
    if offline:
        fetch = False
    inv = refextract.extract_references(path)
    reflist = inv["reflist"]
    intext = inv["intext"]
    raw_placeholders = inv["placeholders"]

    # ---- link in-text markers to reflist entries --------------------------
    ref_to_intext = _link_intext_to_reflist(intext, reflist)
    # index → the marker object, so each reference can carry its linked markers'
    # TEXT (not just a positional index). Apply uses the text to re-locate the
    # marker in the file it actually mutates (F7), surviving an index shift from
    # foreign-field conversion.
    intext_by_idx = {m["index"]: m for m in intext}
    linked_intext_indices = {i for lst in ref_to_intext.values() for i in lst}
    unlinked_intext = [
        {"index": m["index"], "text": m["text"], "kind": m["kind"]}
        for m in intext
        if m["index"] not in linked_intext_indices
    ]
    plan_intext = _plan_intext_inventory(intext, reflist)

    # ---- PubMed candidates for unlinked author-year items (confirm-only) ---
    intext_candidates: dict = {}
    candidates_summary: Optional[dict] = None
    if fetch:
        intext_candidates, candidates_summary = _pubmed_intext_candidates(
            plan_intext, max_queries=max_candidate_queries)

    # ---- retraction DB (loaded once, offline-resilient) -------------------
    # Single owner: citecheck.load_retraction_map. allow_network=True (explicit)
    # preserves this caller's always-allow-refresh policy (was a no-arg
    # ensure_retraction_db()); it routes through ensure_retraction_db so the
    # unify tests' monkeypatch on citecheck.ensure_retraction_db / .load_retraction_db
    # is still honoured. Degrades to {} (never fails planning).
    rw_db: Dict[str, dict] = {}
    if check_retractions:
        rw_db = citecheck.load_retraction_map(allow_network=True)

    def _retracted(doi: Optional[str]) -> bool:
        return citecheck.is_retraction(doi or "", rw_db)

    # ---- fetch library index ONCE for all reference lookups -----------------
    # READ-ONLY (coverage) path: use strict=False so a DEGRADED combined read
    # falls back to the library_doi_index disk cache (DOI-only coverage) instead
    # of going fully empty. A DOI cache survives the SAME outage that leaves the
    # combined cache cold, so a ref whose DOI is in that cache still reads
    # in_library rather than false-"missing". Only when NEITHER cache is loadable
    # does library_index raise; we degrade THAT to empty (nothing is written
    # here — apply_unification keeps its OWN fail-closed guard so a degraded read
    # at write time still refuses to create). See zotero.library_index docs.
    #
    # When offline=True, pass max_age_hours=inf so that a stale cache is still
    # used without a live network refresh. Either way: zero network calls.
    network_note: Optional[str] = None
    try:
        _doi_kw = {"max_age_hours": float("inf")} if offline else {}
        lib_index, _lib_status = zotero.library_index_status(strict=False, **_doi_kw)
    except zotero.LibraryUnavailableError:
        lib_index = {"doi": {}, "pmid": {}, "title": {}}
        _lib_status = {"degraded": True, "doi_only": True}
        network_note = (
            "Zotero library unavailable and no cache — coverage unknown; "
            "treat 'missing from library' counts as provisional"
        )
    else:
        if _lib_status.get("degraded"):
            network_note = (
                "Zotero unreachable — used cached DOI-only library coverage; "
                "PMID/title matches unavailable, so 'missing from library' "
                "counts are provisional (DOI matches are real)"
            )
    # Back-compat alias: existing code below reads ``doi_index`` for DOI lookups.
    doi_index = lib_index.get("doi") or {}

    # ---- resolve each reflist entry ---------------------------------------
    references: List[dict] = []
    n_in_library = 0
    n_missing = 0
    n_retracted = 0
    tier_counts = {"high": 0, "medium": 0, "low": 0}

    _hb: dict = {}
    for _n, entry in enumerate(reflist, 1):
        resolved = refresolve.resolve_reference(entry["text"], fetch=fetch)
        _resolution_heartbeat("unify", _n, len(reflist), _hb)
        meta = resolved.get("metadata")
        confidence = resolved.get("confidence") or "none"
        tier = _TIER_BY_CONFIDENCE.get(confidence, "low")
        doi = (meta or {}).get("doi") if meta else None

        # Decide presence by DOI → PMID → normalized-title. A real shared library
        # has many items WITHOUT a DOI in their index entry, so a DOI-only test
        # false-flags present refs as "missing" — and a confident --apply would
        # then mass-duplicate the group. The PMID source falls back to the
        # resolver's ``identifiers`` (always populated when the input bore a PMID,
        # even if the metadata dict doesn't carry one).
        existing_key = _lookup_in_library(
            lib_index,
            doi=doi,
            pmid=_meta_pmid(meta) or (resolved.get("identifiers") or {}).get("pmid"),
            title=_meta_title(meta),
        )
        in_library = existing_key is not None

        retracted = _retracted(doi)
        divergent = _candidates_divergent(resolved)

        if in_library:
            n_in_library += 1
        elif meta is not None:
            n_missing += 1
        if retracted:
            n_retracted += 1
        tier_counts[tier] += 1

        references.append({
            "ref_index": entry["index"],
            "input": entry["text"],
            "metadata": meta,
            "confidence": confidence,
            "tier": tier,
            "source": resolved.get("source"),
            "candidates": resolved.get("candidates") or [],
            "in_library": in_library,
            "existing_key": existing_key,
            "retracted": retracted,
            "divergent": divergent,
            # The reflist entry's PRINTED number (e.g. "3." -> 3). This is exactly
            # the number a numeric in-text marker uses to cite this reference
            # (``_link_intext_to_reflist`` keys the link off ``by_number``), so the
            # apply pass uses it to place each ref at its correct slot inside a
            # GROUPED marker like "[3,4]" (cite 3 then 4). ``None`` when the entry
            # carried no parseable numbering (an author-year bibliography), in which
            # case no numeric marker links to it anyway.
            "printed_number": _numbering_to_int(entry.get("numbering")),
            "intext_links": list(ref_to_intext.get(entry["index"], [])),
            # F7: carry each linked marker's text/kind (not just its index) so the
            # apply pass can re-locate it in the mutated file. Back-compat:
            # ``intext_links`` stays a list[int]; this is an additive sibling.
            "intext_markers": [
                {"index": _i, "text": intext_by_idx[_i]["text"],
                 "kind": intext_by_idx[_i]["kind"]}
                for _i in ref_to_intext.get(entry["index"], [])
                if _i in intext_by_idx
            ],
        })

    # ---- resolve each placeholder -----------------------------------------
    placeholders: List[dict] = []
    _hb_ph: dict = {}
    for ph_idx, ph in enumerate(raw_placeholders):
        resolved = refresolve.resolve_reference(ph["text"], fetch=fetch)
        _resolution_heartbeat("unify placeholders", ph_idx + 1, len(raw_placeholders), _hb_ph)
        suggestion = resolved.get("metadata") or _top_candidate(resolved)
        placeholders.append({
            "ph_index": ph_idx,
            "text": ph["text"],
            "kind": ph["kind"],
            "location": ph["location"],
            "context": ph.get("context", ""),
            "confidence": resolved.get("confidence") or "none",
            "candidates": resolved.get("candidates") or [],
            "suggestion": suggestion,
        })

    # ---- foreign FIELD-based citations (handled at apply via convert) ------
    fcounts = inv["fields"]["counts"]
    foreign_fields = {
        "endnote": fcounts.get("endnote", 0),
        "mendeley": fcounts.get("mendeley", 0),
        "word": fcounts.get("word", 0),
        "manual": fcounts.get("manual", 0),
        "zotero": fcounts.get("zotero", 0),
    }
    n_foreign_fields = (
        foreign_fields["endnote"] + foreign_fields["mendeley"] + foreign_fields["word"]
    )

    # ---- confirmation buckets ---------------------------------------------
    auto = [r["ref_index"] for r in references if r["tier"] == "high"]
    needs_confirmation_refs = [
        r["ref_index"] for r in references if r["tier"] in ("medium", "low")
    ]
    needs_confirmation_placeholders = [p["ph_index"] for p in placeholders]

    summary = {
        "buckets": {
            "reflist": len(reflist),
            "intext": len(intext),
            "placeholders": len(placeholders),
            "intext_linked": len(linked_intext_indices),
            "intext_unlinked": len(unlinked_intext),
        },
        "tiers": dict(tier_counts),
        "n_in_library": n_in_library,
        "n_missing": n_missing,
        "n_retracted": n_retracted,
        "n_foreign_fields": n_foreign_fields,
        "library_degraded": bool(_lib_status.get("degraded")),
        "library_doi_only": bool(_lib_status.get("doi_only")),
    }
    if network_note:
        summary["network_note"] = network_note
    if candidates_summary is not None:
        summary["intext_candidates"] = candidates_summary

    return {
        "summary": summary,
        "references": references,
        "placeholders": placeholders,
        "foreign_fields": foreign_fields,
        "unlinked_intext": unlinked_intext,
        "needs_confirmation": {
            "references": needs_confirmation_refs,
            "placeholders": needs_confirmation_placeholders,
        },
        "auto": auto,
        "intext": plan_intext,
        "intext_candidates": intext_candidates,
        "decisions_template": _decisions_template(),
    }


# ===========================================================================
# apply_unification — the write pass
# ===========================================================================

def _resolve_placeholder_meta(ph: dict, choice: Any) -> Optional[dict]:
    """Resolve a placeholder ``decisions`` choice to concrete metadata.

    ``choice`` may be:
      * a dict   → used as the metadata directly (the model/user supplied one);
      * an int   → index into the placeholder's ``candidates`` list;
      * ``"suggestion"`` (str) → use the plan's top suggestion;
      * ``None`` → unresolved.
    """
    if choice is None:
        return None
    if isinstance(choice, dict):
        return choice
    if isinstance(choice, str) and choice == "suggestion":
        return ph.get("suggestion")
    if isinstance(choice, int):
        cands = ph.get("candidates") or []
        if 0 <= choice < len(cands):
            return cands[choice]
    return None


def _meta_doi(meta: Optional[dict]) -> Optional[str]:
    if not meta:
        return None
    doi = (meta.get("doi") or "").strip()
    return doi or None


def _meta_title(meta: Optional[dict]) -> str:
    if not meta:
        return ""
    return (meta.get("title") or "").strip()


def _meta_pmid(meta: Optional[dict]) -> Optional[str]:
    if not meta:
        return None
    pmid = str(meta.get("pmid") or "").strip()
    return pmid or None


def _lookup_in_library(
    lib_index: Optional[dict],
    *,
    doi: Optional[str],
    pmid: Optional[str],
    title: Optional[str],
) -> Optional[str]:
    """Return an existing library item key for a ref, or ``None`` if absent.

    Thin re-export of :func:`zotero.lookup_index_key` — the SINGLE owner of the
    DOI → PMID → normalized-title presence decision (consolidated there so
    :mod:`citeconvert`, which cannot import :mod:`unify` without a cycle, shares
    the exact same precedence). The owning rationale: the DOI-only test this
    replaced false-flagged present-but-DOI-less refs as missing, which under a
    confident ``--apply`` mass-duplicated the shared group library; a match on
    ANY of the three identifiers means the ref is already present.

    ``lib_index`` may be ``None`` or partial (a degraded read degrades to empty
    maps); a missing sub-map simply yields no match for that identifier.
    """
    return zotero.lookup_index_key(lib_index, doi=doi, pmid=pmid, title=title)


def _strip_score(meta: Optional[dict]) -> Optional[dict]:
    """Candidate dicts from resolve carry a ``score``; drop it before sending to
    Zotero / the report."""
    if not isinstance(meta, dict):
        return meta
    return {k: v for k, v in meta.items() if k != "score"}


def _itemdata_from_meta(meta: Optional[dict], key: str) -> dict:
    """Build a minimal CSL-JSON ``itemData`` block from resolved metadata, so an
    inserted citation field displays plausibly before the first Zotero refresh.

    We do NOT call back to the live library for csljson here (the item may have
    just been created and the resolved metadata is what we have on hand). Zotero
    overwrites this on Refresh using the URI/key binding.
    """
    meta = meta or {}
    authors = []
    for a in meta.get("authors") or []:
        lit = refresolve.author_literal(a)
        if lit:
            authors.append({"literal": lit})    # group author, in place
        elif isinstance(a, dict):
            authors.append({
                "family": (a.get("family") or "").strip(),
                "given": (a.get("given") or "").strip(),
            })
        elif isinstance(a, str):
            authors.append({"family": a.strip(), "given": ""})
    idata: dict = {
        "id": key,
        "type": meta.get("type") or "article-journal",
        "title": (meta.get("title") or "").strip(),
    }
    if authors:
        idata["author"] = authors
    if meta.get("year"):
        idata["issued"] = {"date-parts": [[int(str(meta["year"])[:4])]] if str(meta["year"])[:4].isdigit() else []}
    if meta.get("journal"):
        idata["container-title"] = meta["journal"]
    if _meta_doi(meta):
        idata["DOI"] = _meta_doi(meta)
    return idata


def _resolve_link_markers(
    placement: dict,
    intext_by_text: Dict[str, List[dict]],
    intext_by_index: Dict[int, dict],
) -> List[dict]:
    """Markers (``{index,text,kind}``) to anchor for ``placement``, resolved
    against the inventory of the file actually being MUTATED (F7).

    Robust join, in priority order:

    1. If the plan carried ``intext_markers`` (text + kind per linked marker),
       look each marker's TEXT up in ``intext_by_text`` — the rewrite_src
       inventory. This is index-shift-proof: a foreign-field conversion that
       renumbered the in-text set cannot mis-key it, and a marker whose text no
       longer exists in the mutated file (e.g. it WAS a foreign field that got
       converted) is silently dropped rather than mis-resolved to a coincidental
       same-index marker. When a text occurs multiple times in the mutated file
       the first unconsumed inventory marker is taken; a located (numeric)
       marker is then converted at its own ``para``/``offset``, and a marker
       without a location is gated downstream by ``_anchor_is_unique``.
    2. Otherwise (old or JSON-roundtripped plan with only ``intext_links``), fall
       back to the raw index into the rewrite_src inventory — the legacy
       behaviour, kept for back-compat.

    Each physical inventory marker is yielded at most once.
    """
    out: List[dict] = []
    seen_ids: set = set()
    markers = placement.get("intext_markers")
    if markers:
        # Track how many times we've consumed a given text so repeated markers
        # (same text, distinct positions) each bind to a distinct inventory entry.
        consumed: Dict[str, int] = {}
        for pm in markers:
            text = pm.get("text")
            cands = intext_by_text.get(text or "", [])
            i = consumed.get(text, 0)
            if i < len(cands):
                m = cands[i]
                consumed[text] = i + 1
                if id(m) not in seen_ids:
                    seen_ids.add(id(m))
                    out.append(m)
            # text absent from the mutated file → drop (do not fall back to index)
        return out
    # Legacy: index-keyed lookup into the rewrite_src inventory.
    for it_idx in placement.get("intext_links", []):
        m = intext_by_index.get(it_idx)
        if m is not None and id(m) not in seen_ids:
            seen_ids.add(id(m))
            out.append(m)
    return out


# ===========================================================================
# decisions["intext_keys"] — in-text marker → identifier(s)
# ===========================================================================

class IntextKeysError(ValueError):
    """``decisions["intext_keys"]`` failed validation. Raised by
    :func:`apply_unification` BEFORE any Zotero write or document change; the
    message names the marker index (and the marker's own text, never more of
    the document)."""


_ZOTERO_ITEM_KEY_RE = re.compile(r"[A-Z0-9]{8}")
_PMID_IDENT_RE = re.compile(r"(?i)pmid\s*:?\s*(\d{1,9})")
_DOI_IDENT_RE = re.compile(r"10\.\d{4,9}/\S+")


def _parse_intext_identifier(raw: Any) -> Tuple[str, str]:
    """``(kind, value)`` for one ``intext_keys`` identifier, ``kind`` in
    ``doi`` / ``pmid`` / ``key``; raises ``ValueError`` when malformed.

    * ``"PMID:123"`` (any case, optional space/colon) or bare digits → PMID.
      An all-digit string is ALWAYS a PMID, never a Zotero key.
    * exactly 8 characters ``[A-Z0-9]`` → Zotero item key.
    * otherwise a DOI (``doi:`` / ``https://doi.org/`` prefixes allowed),
      normalised by :func:`citecheck._normalise_doi`.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("identifier must be a non-empty string")
    s = raw.strip()
    m = _PMID_IDENT_RE.fullmatch(s)
    if m:
        return "pmid", m.group(1)
    if s.isdigit():
        if len(s) > 9:
            raise ValueError("PMID too long")
        return "pmid", s
    if _ZOTERO_ITEM_KEY_RE.fullmatch(s):
        return "key", s
    doi = citecheck._normalise_doi(re.sub(r"(?i)^doi:\s*", "", s))
    if doi and _DOI_IDENT_RE.fullmatch(doi):
        return "doi", doi
    raise ValueError("not a DOI, PMID or Zotero item key")


def _plan_intext_markers(plan: dict) -> List[dict]:
    """The plan-time in-text inventory ``intext_keys`` is validated against:
    ``plan["intext"]``; for an older plan without it, the union of
    ``unlinked_intext`` and every reference's ``intext_markers``."""
    if isinstance(plan.get("intext"), list):
        return [m for m in plan["intext"] if isinstance(m, dict) and "index" in m]
    seen: Dict[int, dict] = {}
    for m in plan.get("unlinked_intext") or []:
        seen.setdefault(int(m["index"]), m)
    for ref in plan.get("references") or []:
        for m in ref.get("intext_markers") or []:
            seen.setdefault(int(m["index"]), m)
    return [seen[i] for i in sorted(seen)]


def _validate_intext_keys(plan: dict, decisions: dict, lib_index: Optional[dict]) -> List[dict]:
    """Validate ``decisions["intext_keys"]`` against the plan; return
    ``[{"index", "marker", "idents": [(kind, value), ...]}]`` sorted by index.

    Fails closed (:class:`IntextKeysError`) on: a non-object value; an unknown
    marker index or marker text; the same index given twice; a value list whose
    length differs from the marker's item count; a malformed identifier; a
    marker also linked to a reference the user listed in ``decisions["accept"]``
    (two decisions for one marker); a Zotero key absent from a successful
    library read. Precedence: an INDEX key wins over a TEXT key for the same
    marker; a text key applies to every marker carrying exactly that text.
    """
    raw = decisions.get("intext_keys")
    if raw is None or raw == {}:
        return []
    if not isinstance(raw, dict):
        raise IntextKeysError(
            "decisions['intext_keys'] must be an object mapping an in-text marker "
            "(plan intext index, or its exact text) to an identifier or a list of "
            "identifiers")
    inventory = _plan_intext_markers(plan)
    by_index = {int(m["index"]): m for m in inventory}
    by_text: Dict[str, List[dict]] = {}
    for m in inventory:
        by_text.setdefault(m.get("text") or "", []).append(m)

    assigned: Dict[int, Any] = {}
    text_keys: List[Tuple[str, Any]] = []
    for k, v in raw.items():
        if isinstance(k, int) and not isinstance(k, bool) or (
            isinstance(k, str) and k.strip().isdigit()
        ):
            idx = int(k)
            if idx not in by_index:
                raise IntextKeysError(f"intext_keys: no in-text marker with index {idx} in the plan")
            if idx in assigned:
                raise IntextKeysError(f"intext_keys: marker {idx} is given more than once")
            assigned[idx] = v
        elif isinstance(k, str) and k in by_text:
            text_keys.append((k, v))
        else:
            raise IntextKeysError(f"intext_keys: no in-text marker with text {k!r} in the plan")
    for text, v in text_keys:
        for m in by_text[text]:
            assigned.setdefault(int(m["index"]), v)   # an index key wins

    explicit = set(decisions.get("accept", []) or [])
    accepted_link: Dict[int, Any] = {}
    for ref in plan.get("references") or []:
        if ref.get("ref_index") in explicit:
            linked = list(ref.get("intext_links") or [])
            linked += [m.get("index") for m in ref.get("intext_markers") or []]
            for i in linked:
                if i is not None:
                    accepted_link.setdefault(int(i), ref["ref_index"])

    lib_keys: Optional[set] = None
    if lib_index is not None:
        lib_keys = set()
        for sub in ("doi", "pmid", "title"):
            lib_keys.update((lib_index.get(sub) or {}).values())

    out: List[dict] = []
    for idx in sorted(assigned):
        m = by_index[idx]
        label = f"intext_keys: marker {idx} {m.get('text')!r}"
        if idx in accepted_link:
            raise IntextKeysError(
                f"{label} is also handled by decisions['accept'] (reference "
                f"{accepted_link[idx]}); keep one of the two")
        v = assigned[idx]
        vals = v if isinstance(v, list) else [v]
        n = _marker_item_count(m)
        if len(vals) != n:
            raise IntextKeysError(
                f"{label}: {len(vals)} identifier(s) given but the marker cites {n} "
                "item(s); give a list with one identifier per item, in item order")
        idents: List[Tuple[str, str]] = []
        for pos, val in enumerate(vals):
            try:
                kind, ident = _parse_intext_identifier(val)
            except ValueError as exc:
                raise IntextKeysError(
                    f"{label} item {pos}: malformed identifier {val!r} ({exc})") from None
            if kind == "key" and lib_keys is not None and ident not in lib_keys:
                raise IntextKeysError(
                    f"{label} item {pos}: Zotero key {ident} is not in the group library")
            idents.append((kind, ident))
        out.append({"index": idx, "marker": m, "idents": idents})
    return out


def _intext_key_placements(
    assignments: List[dict],
    lib_index: Optional[dict],
    *,
    add_missing: bool,
    report: dict,
    to_create_metas: List[dict],
    to_create_back: List[dict],
) -> List[dict]:
    """Turn validated ``intext_keys`` assignments into placements (one per cited
    item, carrying ``intext_index`` + ``item_pos``) and route each identifier:

    * Zotero key → cited as-is (validation already proved it is in the library;
      on a degraded read it cannot be proved → ``needs_input``, not cited).
    * DOI / PMID present in the library (:func:`zotero.lookup_index_key`, the
      single presence owner) → cited by that key.
    * missing DOI / PMID → appended to ``to_create_metas``/``to_create_back`` so
      the caller's step 4 decides (degraded read → refuse; ``add_missing`` off →
      skipped; write gate; ``create_items(dedup, index_degraded)``). Metadata is
      fetched (``refresolve._crossref_doi_fetch`` / ``_pubmed_fetch``, the
      identifier-first fetchers ``resolve_reference`` itself uses) ONLY when a
      create can actually happen (readable library and ``add_missing``); a
      failed lookup is a ``needs_input``, never a bibliographic guess. A work
      already queued for creation (same DOI/PMID) is not queued twice: the
      placement becomes a ``_siblings`` entry of the queued one.

    Mutates ``report`` (matched / needs_input) and the two to-create lists.
    """
    placements: List[dict] = []
    resolved: Dict[Tuple[str, str], dict] = {}

    def _queued(meta: dict) -> Optional[dict]:
        doi = _meta_doi(meta)
        ndoi = citecheck._normalise_doi(doi) if doi else None
        pmid = _meta_pmid(meta)
        for qpl, qmeta in zip(to_create_back, to_create_metas):
            qdoi = _meta_doi(qmeta)
            if ndoi and qdoi and citecheck._normalise_doi(qdoi) == ndoi:
                return qpl
            if pmid and _meta_pmid(qmeta) == pmid:
                return qpl
        return None

    def _resolve(kind: str, ident: str, pl: dict) -> dict:
        label = {"doi": "DOI", "pmid": "PMID", "key": "Zotero key"}[kind] + f" {ident}"
        pl["title"] = label
        if kind == "key":
            if lib_index is None:
                report["needs_input"].append({
                    "title": label, "doi": None, **_intext_ref(pl),
                    "reason": "Zotero library could not be read (degraded/unavailable); "
                              "cannot confirm this item key exists, so the marker is "
                              "left as plain text. Re-run when the library is reachable.",
                })
                return {}
            report["matched"].append({"title": label, "key": ident, **_intext_ref(pl)})
            return {"key": ident, "meta": None}
        doi = ident if kind == "doi" else None
        pmid = ident if kind == "pmid" else None
        key = _lookup_in_library(lib_index, doi=doi, pmid=pmid, title=None) if lib_index else None
        if key:
            report["matched"].append({"title": label, "key": key, **_intext_ref(pl)})
            return {"key": key, "meta": None}
        meta: dict = {"doi": doi} if doi else {"pmid": pmid}
        if lib_index is not None and add_missing:
            # The identifier fetchers directly (not resolve_reference): a failed
            # DOI/PMID lookup must not fall through to a bibliographic search.
            try:
                rmeta = (refresolve._crossref_doi_fetch(doi) if doi
                         else refresolve._pubmed_fetch(pmid))
            except Exception:  # noqa: BLE001 — a lookup failure is a needs_input
                rmeta = None
            if not isinstance(rmeta, dict):
                report["needs_input"].append({
                    "title": label, "doi": doi, **_intext_ref(pl),
                    "reason": "could not resolve this identifier's metadata "
                              "(Crossref/PubMed); nothing to add to Zotero or cite.",
                })
                return {}
            meta = _strip_score(dict(rmeta))
            if doi and not meta.get("doi"):
                meta["doi"] = doi
            if pmid and not meta.get("pmid"):
                meta["pmid"] = pmid
            pl["title"] = _meta_title(meta) or label
            key = _lookup_in_library(lib_index, doi=_meta_doi(meta),
                                     pmid=_meta_pmid(meta), title=_meta_title(meta))
            if key:
                report["matched"].append({"title": pl["title"], "key": key, **_intext_ref(pl)})
                return {"key": key, "meta": meta}
            queued = _queued(meta)
            if queued is not None:
                return {"rep": queued, "meta": meta}
        pl["meta"] = meta
        to_create_metas.append(meta)
        to_create_back.append(pl)
        return {"rep": pl, "meta": meta}

    for a in assignments:
        m = a["marker"]
        for pos, (kind, ident) in enumerate(a["idents"]):
            pl = {
                "meta": None, "key": None, "is_ph": False, "ph": None,
                "ref_index": None, "printed_number": None,
                "intext_links": [m["index"]],
                "intext_markers": [{"index": m["index"], "text": m["text"],
                                    "kind": m.get("kind")}],
                "intext_index": m["index"], "item_pos": pos, "title": "",
            }
            placements.append(pl)
            st = resolved.get((kind, ident))
            if st is None:
                st = resolved[(kind, ident)] = _resolve(kind, ident, pl)
            else:
                pl["title"] = (st.get("rep") or {}).get("title") or f"{kind} {ident}"
            if st.get("key"):
                pl["key"], pl["meta"] = st["key"], st.get("meta")
            elif st.get("rep") is not None and st["rep"] is not pl:
                pl["meta"] = st.get("meta")
                st["rep"].setdefault("_siblings", []).append(pl)
    return placements


def _intext_ref(pl: dict) -> dict:
    """Report fields naming the in-text marker an ``intext_keys`` placement
    came from (empty for reference / placeholder placements)."""
    if pl.get("intext_index") is None:
        return {}
    return {"intext_index": pl["intext_index"], "item": pl.get("item_pos")}


def _mark_key(el) -> tuple:
    """A mark's identity that survives a save + re-open: tag, revision/comment id
    and date. A mark written later in the same op can never share it — new
    tracked-change ids are allocated past the document's maximum."""
    return (el.tag, el.get(qn("w:id")), el.get(qn("w:date")))


def _pre_mark_keys(doc, author: str) -> Optional[set]:
    """Keys of ``author``'s dated marks that exist BEFORE this op, or ``None``
    when they cannot be snapshotted (the op-level spread is then skipped rather
    than run with a scope that could re-stamp an older mark)."""
    snap = zoterofield._snapshot_marks(doc, author)
    return None if snap is None else {_mark_key(el) for el in snap}


def _rebind_pre_marks(doc, author: str, keys: set) -> Optional[set]:
    """The elements of the (re-opened) ``doc`` that are the pre-op marks named by
    ``keys`` — the ``pre_existing`` scope for ``spread_own_marks``."""
    snap = zoterofield._snapshot_marks(doc, author)
    return None if snap is None else {el for el in snap if _mark_key(el) in keys}


def apply_unification(
    path,
    plan: dict,
    decisions: dict,
    *,
    out,
    add_missing: bool = True,
    track: bool = True,
    author: Optional[str] = None,
    source_label: Optional[str] = None,
    attach_pdfs: bool = False,
) -> dict:
    """Apply a confirmed unification plan: add missing refs to Zotero + rewrite
    the document to live Zotero fields.

    Parameters
    ----------
    path:
        The input .docx draft (unchanged on disk).
    plan:
        The dict returned by :func:`plan_unification`.
    decisions:
        ``{"accept": [ref_index, ...],
           "placeholder_resolutions": {ph_index: choice},
           "add_missing": bool}``. HIGH-tier refs in ``plan["auto"]`` are accepted
        by default; MEDIUM/LOW only when their index is in ``accept``.
        ``decisions["add_missing"]`` (if present) overrides the ``add_missing``
        keyword. ``choice`` is a metadata dict, a candidate index, the string
        ``"suggestion"``, or ``None`` (see :func:`_resolve_placeholder_meta`).
        ``decisions["intext_keys"]`` maps an in-text marker (plan ``intext``
        index as a string, or the exact marker text) to an identifier — DOI,
        PMID (``"PMID:123"`` / bare digits) or 8-character Zotero item key — or a
        list of them, one per cited item in item order. Each marker becomes a
        Zotero citation field even with no reference list: an identifier already
        in the library is cited by its key; a missing DOI/PMID is resolved
        (Crossref/PubMed) and created through the same gated create path as a
        missing reference (or reported in ``needs_input`` when ``add_missing``
        is off / the library read is degraded). It is validated BEFORE any
        Zotero write or document change; any problem raises
        :class:`IntextKeysError` and nothing is written
        (see :func:`_validate_intext_keys`).
    out:
        Destination path for the rewritten document.
    add_missing:
        Whether to create accepted-but-missing references in the group library.
    track:
        Insert citation swaps as tracked changes when ``True``.
    source_label:
        Tag/label identifying the source document. Defaults to the basename of
        ``path``.
    attach_pdfs:
        OPT-IN (default ``False``). Pass through to :func:`zotero.create_items`
        so each newly-created item gets a best-effort open-access PDF attached.
        Off by default — existing behaviour is unchanged.

    Returns
    -------
    dict
        ``{"out", "added", "matched", "replaced", "needs_input",
        "retracted_flagged", "unresolved_in_doc", "create_failed",
        "skipped_degraded_read", "author_check"}``. ``author_check`` holds one
        PubMed author decision per item sent to create (``pubmed`` /
        ``confirmed`` / ``authors_unverified``, with a ``reason``).

        **``replaced`` counts PHYSICAL in-text citation LOCATIONS, not
        references.** Each is one increment: a converted foreign field
        (:mod:`citeconvert`), a resolved placeholder, and — crucially — each
        plain-text in-text MARKER rewritten to a live Zotero field. A GROUPED
        numeric marker like ``"[3,4]"`` becomes ONE field citing both refs and so
        increments ``replaced`` by 1 (NOT by 2). This is the marker-granular
        contract downstream summaries rely on; do not switch it to per-reference
        without updating every consumer (currently only the CLI summary line).
    """
    decisions = decisions or {}
    if decisions.get("add_missing") is not None:
        add_missing = bool(decisions["add_missing"])
    accept = set(decisions.get("accept", []))
    ph_resolutions = decisions.get("placeholder_resolutions", {}) or {}
    source_label = source_label or Path(path).name
    # Tracked-change author: the CLI passes the CONFIGURED author (cmd_unify_refs ->
    # `a.author or AUTHOR`), which flows through to the inserted Zotero fields. A
    # library caller that leaves author=None falls through to zoterofield's own
    # "zotero-word-cite" default (pass it only when set). unify is a brand-only SYNCED
    # module — it must NOT import zotero-word-cite config (zoterocite has none); resolution
    # lives at the zotero-word-cite CLI boundary, never here.
    _author_kw = {} if author is None else {"author": author}

    auto = set(plan.get("auto", []))
    references = plan.get("references", [])
    plan_placeholders = plan.get("placeholders", [])

    # Fetch the library index ONCE (DOI+PMID+title) for all presence lookups in
    # this function. A DEGRADED READ (LibraryUnavailableError) is NOT an empty
    # library: if we proceeded with empty maps every accepted work would look
    # "missing" and we would create DUPLICATES in the shared group. Fail closed —
    # keep ``lib_index = None`` as a sentinel and refuse to create below (step 4).
    # strict=True (explicit): the WRITE path NEVER serves a stale DOI-only
    # fallback — a stale cache could miss recently-added items and mass-duplicate
    # the shared group. A degraded read here MUST raise so we fail closed.
    # Use the status-aware entry point so we can thread the degraded flag into
    # create_items as belt-and-suspenders. Under strict=True a degraded read
    # RAISES (→ lib_index=None → fail closed below), so index_degraded is False on
    # the success path; capturing it defends against any future change that lets
    # strict=True return a degraded index instead of raising.
    index_degraded = False
    try:
        lib_index, _apply_status = zotero.library_index_status(strict=True)
        index_degraded = bool(_apply_status.get("degraded"))
    except zotero.LibraryUnavailableError:
        lib_index = None
    # Back-compat: step 4 / create_items take a flat DOI→key map. ``None`` is the
    # fail-closed sentinel and MUST propagate as ``None`` (not {}) so creation is
    # refused on a degraded read.
    doi_index = None if lib_index is None else (lib_index.get("doi") or {})

    # Validate decisions["intext_keys"] FIRST: everything above is a read, and a
    # bad marker / identifier must stop the run before any Zotero write or
    # document change (raises IntextKeysError).
    intext_assignments = _validate_intext_keys(plan, decisions, lib_index)

    report: dict = {
        "out": None,
        "added": [],
        "matched": [],
        "replaced": 0,
        "needs_input": [],
        "retracted_flagged": [],
        "unresolved_in_doc": [],
        # Surfaced by the CLI so a "added 0" is never silently ambiguous.
        "create_failed": [],
        "skipped_degraded_read": [],
        # Per created item: PubMed author check (zotero.create_items
        # verify_authors -> refresolve.reconcile_create_authors).
        "author_check": [],
    }

    # -----------------------------------------------------------------------
    # 1. Decide which references are accepted.
    #    HIGH → auto-accepted; MEDIUM/LOW → only if explicitly in `accept`.
    # -----------------------------------------------------------------------
    accepted_refs: List[dict] = []
    for ref in references:
        idx = ref["ref_index"]
        is_accepted = idx in auto or idx in accept
        if not is_accepted:
            continue
        if ref.get("retracted"):
            # Flag retracted items; only proceed if EXPLICITLY accepted
            # (not merely auto). Auto-acceptance never imports a retracted ref.
            report["retracted_flagged"].append({
                "title": _meta_title(ref.get("metadata")) or ref["input"][:80],
                "doi": _meta_doi(ref.get("metadata")),
                "ref_index": idx,
                "imported": idx in accept,
            })
            if idx not in accept:
                continue
        accepted_refs.append(ref)

    # -----------------------------------------------------------------------
    # 2. Resolve placeholders the user confirmed → metadata.
    # -----------------------------------------------------------------------
    accepted_placeholders: List[dict] = []  # {ph, meta}
    for ph in plan_placeholders:
        ph_idx = ph["ph_index"]
        # decisions keys may be ints or str(ints) (JSON round-trips)
        choice = ph_resolutions.get(ph_idx, ph_resolutions.get(str(ph_idx)))
        meta = _strip_score(_resolve_placeholder_meta(ph, choice))
        if meta is None:
            report["needs_input"].append({
                "ph_index": ph_idx,
                "text": ph["text"],
                "kind": ph["kind"],
                "location": ph["location"],
                "reason": "no resolution supplied",
            })
            continue
        accepted_placeholders.append({"ph": ph, "meta": meta})

    # -----------------------------------------------------------------------
    # 3. Map each accepted reference / placeholder → a Zotero item key.
    #    Refs already in the library reuse their existing key (recorded in
    #    "matched"); missing ones are collected for creation.
    # -----------------------------------------------------------------------
    # Each work-to-place: {"meta", "key"(filled later), "intext_links", "is_ph", "ph", "title"}
    placements: List[dict] = []
    to_create_metas: List[dict] = []         # metas needing creation
    to_create_back: List[dict] = []          # placement entries aligned w/ to_create_metas

    _hb_place: dict = {}
    for _pn, ref in enumerate(accepted_refs, 1):
        _resolution_heartbeat("unify apply", _pn, len(accepted_refs), _hb_place)
        meta = _strip_score(ref.get("metadata"))
        # An accepted ref that resolved to NO usable metadata (refresolve miss) has
        # nothing to create from and no key to cite — it must be SURFACED with a
        # clear reason, never silently dropped. Without this it would be threaded
        # into to_create_metas as ``None``; create_items() silently filters None
        # entries, and the placement then surfaced only via a MISLEADING
        # "could not map a Zotero key after create" message (no create was ever
        # possible). Filter it here so the user sees the true cause. (The
        # fail-closed CREATE logic below is unchanged — this only diverts refs that
        # have no metadata to create in the first place.)
        if not (ref.get("in_library") and ref.get("existing_key")) and meta is None:
            report["needs_input"].append({
                "ref_index": ref["ref_index"],
                "title": ref["input"][:80],
                "doi": None,
                "reason": "accepted reference resolved to no usable metadata "
                          "(could not be identified); nothing to add to Zotero or "
                          "cite — supply an identifier (DOI/PMID) or fuller citation.",
            })
            continue
        placement = {
            "meta": meta,
            "key": None,
            "intext_links": ref.get("intext_links", []),
            # F7: carried alongside the indices; absent on old/serialized plans.
            "intext_markers": ref.get("intext_markers", []),
            "is_ph": False,
            "ph": None,
            "title": _meta_title(meta) or ref["input"][:80],
            "ref_index": ref["ref_index"],
            # Printed reflist number; orders this ref inside a grouped in-text
            # marker at apply time. ``None`` on old/serialized plans (falls back
            # to reflist order — see the group-by-marker pass below).
            "printed_number": ref.get("printed_number"),
        }
        if ref.get("in_library") and ref.get("existing_key"):
            placement["key"] = ref["existing_key"]
            report["matched"].append({"title": placement["title"], "key": ref["existing_key"]})
        else:
            to_create_metas.append(meta)
            to_create_back.append(placement)
        placements.append(placement)

    for entry in accepted_placeholders:
        ph, meta = entry["ph"], entry["meta"]
        placement = {
            "meta": meta,
            "key": None,
            "intext_links": [],
            "is_ph": True,
            "ph": ph,
            "title": _meta_title(meta) or ph["text"][:80],
            "ref_index": None,
        }
        # A confirmed placeholder's metadata might already be in the library —
        # by DOI, PMID, or title. Use the same DOI→PMID→title fallback as the
        # plan so a present-but-DOI-less item isn't re-created. ``lib_index is
        # None`` is the degraded-read sentinel: skip the lookup (no match) and
        # let step 4's fail-closed guard refuse creation.
        existing_key = None
        if lib_index is not None:
            existing_key = _lookup_in_library(
                lib_index,
                doi=_meta_doi(meta),
                pmid=_meta_pmid(meta),
                title=_meta_title(meta),
            )
        if existing_key:
            placement["key"] = existing_key
            report["matched"].append({"title": placement["title"], "key": existing_key})
        else:
            to_create_metas.append(meta)
            to_create_back.append(placement)
        placements.append(placement)

    # 3b. decisions["intext_keys"]: one placement per cited ITEM of each named
    #     marker. Identifiers already in the library cite their key; missing
    #     DOIs/PMIDs join to_create so step 4's gates (degraded read, add_missing,
    #     write access, create_items dedup + index_degraded) apply unchanged. An
    #     identifier needed by several items is resolved and created ONCE; the
    #     other placements are its "_siblings" and take its key after step 4.
    intext_placements = _intext_key_placements(
        intext_assignments, lib_index, add_missing=add_missing,
        report=report, to_create_metas=to_create_metas,
        to_create_back=to_create_back)

    # -----------------------------------------------------------------------
    # 4. Create the missing items (confirm-gated, write-guarded).
    # -----------------------------------------------------------------------
    if to_create_metas:
        if doi_index is None:
            # Degraded library read (LibraryUnavailableError): we cannot tell which
            # of these are already in the group, so creating any of them risks
            # duplicates. Refuse all creation; the user must re-run once the
            # library is reachable. Fail closed — never create on uncertain reads.
            for pl in to_create_back:
                report["needs_input"].append({
                    "title": pl["title"], **_intext_ref(pl),
                    "doi": _meta_doi(pl["meta"]),
                    "reason": "Zotero library could not be read (degraded/unavailable); "
                              "refusing to create to avoid duplicate writes to the "
                              "shared group. Re-run when the library is reachable.",
                })
        elif not add_missing:
            for pl in to_create_back:
                report["needs_input"].append({
                    "title": pl["title"], **_intext_ref(pl),
                    "doi": _meta_doi(pl["meta"]),
                    "reason": "missing from library; add_missing=False",
                })
        elif not (_write_status := zotero.key_can_write_status()):
            # Fail closed on BOTH a definitive "no write access" and an
            # unverifiable result — but tell the truth about which it was, so the
            # user retries on a transient failure instead of being told (wrongly)
            # that the key lacks write access.
            if _write_status is zotero.WRITE_ACCESS_UNKNOWN:
                _reason = ("Could not reach Zotero to verify the API key's write "
                           "access — refusing to create (fail-closed). Re-run when "
                           "Zotero is reachable.")
            else:
                _reason = ("Zotero API key has no write access — add this item "
                           "to the group library manually, then re-run.")
            for pl in to_create_back:
                report["needs_input"].append({
                    "title": pl["title"], **_intext_ref(pl),
                    "doi": _meta_doi(pl["meta"]),
                    "reason": _reason,
                })
        else:
            created = zotero.create_items(
                to_create_metas,
                collection=IMPORTED_COLLECTION,
                tags=[ADDED_TAG, source_label],
                dedup=True,
                doi_index=doi_index,
                pmid_index=(lib_index.get("pmid") if lib_index else None),
                attach_pdfs=attach_pdfs,
                index_degraded=index_degraded,
                verify_authors=True,
            )
            report["author_check"] = list(created.get("author_check", []))
            # Surface the raw create failures / degraded-read refusals so a
            # "added 0" is never silently ambiguous (the CLI prints these).
            report["create_failed"] = list(created.get("failed", []))
            report["skipped_degraded_read"] = list(created.get("skipped_degraded_read", []))
            # Map created/skipped results back to placements by DOI then title.
            created_by_doi: Dict[str, str] = {}
            created_by_title: Dict[str, str] = {}
            for c in created.get("created", []):
                if c.get("doi"):
                    created_by_doi[citecheck._normalise_doi(c["doi"])] = c["key"]
                if c.get("title"):
                    created_by_title[c["title"].strip().lower()] = c["key"]
            skipped_by_title: Dict[str, str] = {}
            for s in created.get("skipped_existing", []):
                if s.get("title"):
                    skipped_by_title[s["title"].strip().lower()] = s.get("existing_key", "")
            failed_titles = {
                (f.get("title") or "").strip().lower() for f in created.get("failed", [])
            }
            # Items refused because the library read was degraded (fail-closed):
            # route them to needs_input with the true reason rather than the
            # misleading "could not map a key" fallthrough below.
            degraded_titles = {
                (s.get("title") or "").strip().lower()
                for s in created.get("skipped_degraded_read", [])
            }

            for pl in to_create_back:
                doi = _meta_doi(pl["meta"])
                title_l = pl["title"].strip().lower()
                meta_title_l = _meta_title(pl["meta"]).strip().lower()
                key = None
                if doi and citecheck._normalise_doi(doi) in created_by_doi:
                    key = created_by_doi[citecheck._normalise_doi(doi)]
                if key is None:
                    key = created_by_title.get(meta_title_l) or created_by_title.get(title_l)
                if key is None:
                    sk = skipped_by_title.get(meta_title_l) or skipped_by_title.get(title_l)
                    if sk:
                        pl["key"] = sk
                        report["matched"].append({"title": pl["title"], "key": sk, **_intext_ref(pl)})
                        continue
                if key is None and (meta_title_l in degraded_titles or title_l in degraded_titles):
                    report["needs_input"].append({
                        "title": pl["title"], **_intext_ref(pl),
                        "doi": doi,
                        "reason": "Zotero library read was degraded — refused to create "
                                  "(fail-closed) to avoid a duplicate write; re-run when "
                                  "the library is reachable.",
                    })
                    continue
                if key is None and (meta_title_l in failed_titles or title_l in failed_titles):
                    report["needs_input"].append({
                        "title": pl["title"], **_intext_ref(pl),
                        "doi": doi,
                        "reason": "Zotero create failed for this item.",
                    })
                    continue
                if key is None:
                    report["needs_input"].append({
                        "title": pl["title"], **_intext_ref(pl),
                        "doi": doi,
                        "reason": "could not map a Zotero key to this item after create.",
                    })
                    continue
                pl["key"] = key
                report["added"].append({"title": pl["title"], "key": key, **_intext_ref(pl)})

    # An identifier shared by several placements was queued ONCE (see
    # _intext_key_placements); its siblings cite whatever key it ended with.
    for pl in to_create_back:
        for sib in pl.get("_siblings", []):
            sib["key"] = pl["key"]
            sib["meta"] = sib.get("meta") or pl["meta"]

    # -----------------------------------------------------------------------
    # 5. Rewrite the document.
    #    (a) Foreign FIELD-based citations via convert_to_zotero (on the input);
    #        operate on its output for the plain-text pass so we don't double
    #        process the same file.
    # -----------------------------------------------------------------------
    out_path = Path(out)
    n_foreign = plan.get("summary", {}).get("n_foreign_fields", 0)

    # Tracked apply = ONE op: every write below (foreign-field swaps, location
    # batch, text-path markers, placeholders) is stamped with this pinned date and
    # the active author, and the op's marks are spread ONCE at the end (step 6).
    # INVARIANT: marks that existed before the op — any author, including this
    # author's earlier rounds — keep their exact w:date. They are snapshotted
    # here, BEFORE any write. The foreign-field pass writes to out_path and the
    # file is re-opened, so the snapshot is carried as stable mark keys and
    # re-bound to the re-opened tree's elements (_rebind_pre_marks).
    op_date = now_iso()
    spread_author = author if author is not None else "zotero-word-cite"
    pre_mark_keys = None
    if track:
        pre_mark_keys = _pre_mark_keys(zoterofield.Docx(path), spread_author)

    convert_result = None
    rewrite_src = Path(path)
    if n_foreign:
        # Convert foreign fields straight into the final output, then re-open
        # that file for the plain-text insertion pass. spread=False: this op
        # spreads once, at the end, over all of its marks.
        convert_result = citeconvert.convert_to_zotero(
            path, out=out_path, track=track, date=op_date, spread=False, **_author_kw)
        report["replaced"] += len(convert_result.get("converted", []))
        rewrite_src = out_path

    doc = zoterofield.Docx(rewrite_src)
    root = doc.read_tree(zoterofield.DOCUMENT)
    pre_marks = (_rebind_pre_marks(doc, spread_author, pre_mark_keys)
                 if pre_mark_keys is not None else None)
    # One anchor index for every uniqueness check below: each marker would
    # otherwise re-walk the whole document (O(markers*doc)). Each PHYSICAL marker
    # is now converted EXACTLY ONCE (the group-by-marker pass below), replacing
    # only its own marker text with a "(key; ...)" field render that contains no
    # other (distinct) marker, so this snapshot's per-marker occurrence count stays
    # valid across the whole loop; the splice in replace_text_with_zotero_field
    # still reads live text. (The old per-reference loop converted a grouped marker
    # once per co-cited ref, so the SAME anchor was re-checked against this stale
    # snapshot after its own conversion — the trap the group-by-marker pass closes.)
    from .paras import ParaIndex
    _para_index = ParaIndex(root)

    # (b) Plain-text in-text cites + resolved placeholders → live Zotero fields.
    #     We anchor each insertion at a uniquely-locatable marker; if the anchor
    #     can't be uniquely found, record it in unresolved_in_doc (best-effort).
    #
    #     F7: extract the marker inventory from ``rewrite_src`` (the file actually
    #     being MUTATED) so each ``marker["text"]`` describes the tree we anchor
    #     against (``root``/``doc``), not the pre-conversion original ``path``.
    #     The plan's ``intext_links`` indices were assigned over the ORIGINAL
    #     ``path`` extraction, so when foreign-field conversion ran (n_foreign>0)
    #     and changed the in-text marker set, those indices no longer key this
    #     inventory 1:1. We therefore resolve each link by recovering its marker
    #     TEXT from the plan-time inventory (carried on the placement, see below)
    #     and matching it in the rewrite_src inventory, falling back to the raw
    #     index only when no text is carried (old/serialized plans).
    inv = refextract.extract_references(rewrite_src)
    intext_by_index = {m["index"]: m for m in inv["intext"]}
    # Text → marker(s) present in the mutated tree, for the robust join below.
    intext_by_text: Dict[str, List[dict]] = {}
    for _m in inv["intext"]:
        intext_by_text.setdefault(_m["text"], []).append(_m)

    def _anchor_is_unique(anchor: str) -> bool:
        return len(_para_index.find_all(anchor)) == 1

    # Rendered text of citations that are ALREADY live Zotero fields. refextract
    # reads markers from rendered text, so a field rendering "(1,2)" is indistinguishable
    # from plain typed "(1,2)"; re-citing such a marker would overwrite the existing
    # field with a raw-key render. Snapshot BEFORE any insertion mutates the tree, and
    # skip those markers below — only manual/foreign-text cites should be converted.
    already_live = zoterofield.existing_renderings(root)

    # in-text markers for accepted references — GROUPED BY PHYSICAL MARKER.
    #
    # A grouped numeric marker like "[3,4]" is carried on BOTH ref3's and ref4's
    # ``intext_markers`` (``_link_intext_to_reflist`` links it to every cited
    # number). The old per-REFERENCE loop converted that one physical marker once
    # per co-cited ref: ref3 first replaced "[3,4]" with a field citing ONLY ref3,
    # then ref4's pass found the anchor gone and its co-citation was silently
    # DROPPED — "[3,4]" became a field citing only [3]. We now convert each
    # PHYSICAL marker EXACTLY ONCE into a single field citing ALL its grouped
    # references, in the marker's printed order — mirroring
    # ``citelink.apply_cite_link``'s all-or-nothing grouped-marker policy: if any
    # cited number is not accepted-and-keyed, the WHOLE marker is left as plain
    # text and reported, never converted to a lossy subset.
    #
    # First pass: bucket every accepted, keyed, non-placeholder placement by the
    # identity of the physical inventory marker it resolves to (ref3 and ref4 both
    # resolve to the SAME marker object, so ``id(marker)`` groups them). A grouped
    # author-year marker gathers one member per linked item the same way.
    #
    # decisions["intext_keys"] placements are bucketed FIRST into groups flagged
    # ``intext``: the user's explicit identifiers win for that physical marker, so
    # a reference merely auto-linked to the same marker is NOT added to it (it is
    # still placed at its other markers). Validation already refused a marker
    # that is also linked to an explicitly ACCEPTED reference.
    marker_groups: Dict[int, dict] = {}
    for pl in intext_placements:
        for marker in _resolve_link_markers(pl, intext_by_text, intext_by_index):
            g = marker_groups.setdefault(
                id(marker), {"marker": marker, "members": [], "intext": True})
            g["members"].append(pl)
    for pl in placements:
        if pl["is_ph"]:
            continue
        # F7: resolve each linked marker against the inventory of the MUTATED file
        # (rewrite_src). Prefer the plan-carried marker text (index-shift-proof);
        # fall back to the raw plan index only for old/serialized plans that lack
        # ``intext_markers``. Placements with ``key is None`` (accepted but not
        # keyable — e.g. create failed) are KEPT in the group so the all-or-nothing
        # check below sees the cited number is unwritable and refuses the marker.
        for marker in _resolve_link_markers(pl, intext_by_text, intext_by_index):
            g = marker_groups.get(id(marker))
            if g is None:
                g = {"marker": marker, "members": []}
                marker_groups[id(marker)] = g
            elif g.get("intext"):
                continue   # intext_keys owns this marker
            g["members"].append(pl)

    # Second pass: convert each physical marker once.
    # The plan's whole reference list (every entry, accepted or not), so the
    # author-year branch re-links each item exactly as plan time did.
    ay_reflist = [{"index": r["ref_index"], "text": r.get("input") or ""}
                  for r in references if r.get("ref_index") is not None]
    # Markers that carry their LOCATION (refextract numeric markers: ``para`` +
    # ``offset`` into the very tree being mutated) are converted exactly there,
    # all in ONE batched call after this loop (offsets are resolved before any
    # splice and spliced right to left — zoterofield owns that). A text search
    # would convert the FIRST same-text copy in the paragraph (a list enumerator
    # before the real cite) and refuse a marker cited in two paragraphs.
    # Markers without a location (author-year) keep the text path + uniqueness
    # gate, deferred until AFTER the batch so no text write shifts an offset.
    loc_occurrences: List[dict] = []
    loc_meta: List[dict] = []
    text_jobs: List[dict] = []
    for g in marker_groups.values():
        marker = g["marker"]
        members = g["members"]
        anchor = marker["text"]
        if anchor.strip() in already_live:
            # Already a live Zotero field — leave it untouched (re-citing would
            # clobber the managed field and lose its rendered text).
            continue

        # Order the references to cite, and enforce the all-or-nothing policy.
        # Branch on the marker KIND, never on _numeric_marker_targets: that helper
        # returns the YEAR for an author-year marker ("(Smith, 2020)" -> [2020]),
        # so it cannot distinguish numeric from author-year.
        targets = _numeric_marker_targets(anchor) if marker["kind"] == "numeric" else []
        if g.get("intext"):
            # decisions["intext_keys"]: each member carries its item position;
            # cite them in item order. All-or-nothing: an item whose identifier
            # could not be keyed (skipped / refused / failed) leaves the whole
            # marker as plain text.
            n_items = _marker_item_count(marker)
            by_pos: Dict[int, dict] = {}
            for pl in members:
                if pl.get("key") is not None:
                    by_pos.setdefault(pl["item_pos"], pl)
            missing = [i for i in range(n_items) if i not in by_pos]
            if missing:
                report["unresolved_in_doc"].append({
                    "marker": anchor,
                    "kind": marker["kind"],
                    "intext_index": members[0].get("intext_index"),
                    "reason": (
                        f"intext_keys item(s) {missing} have no Zotero item (see "
                        "needs_input); left as plain text (all-or-nothing) so no "
                        "co-citation is silently dropped"
                    ),
                })
                continue
            ordered = [by_pos[i] for i in range(n_items)]
        elif marker["kind"] == "numeric":
            # NUMERIC marker (single "[3]" or grouped "[3,4]"/"[5-7]"): cite EVERY
            # number, in the marker's printed order. Map each number to its keyed
            # placement via the carried printed reflist number.
            by_num: Dict[int, dict] = {}
            missing_pn = False
            for pl in members:
                if pl.get("key") is None:
                    continue
                pn = pl.get("printed_number")
                if pn is None:
                    missing_pn = True
                    continue
                by_num.setdefault(pn, pl)
            if missing_pn and not by_num:
                # BACK-COMPAT: an old/serialized plan carries no printed_number.
                # Fall back to reflist (member) order, which matches the printed
                # order of a standard ascending bibliography. Still all-or-nothing:
                # only convert when the count of keyed members equals the count of
                # cited numbers (a grouped marker missing a member is left plain).
                keyed = [pl for pl in members if pl.get("key") is not None]
                if len(keyed) != len(targets):
                    report["unresolved_in_doc"].append({
                        "marker": anchor,
                        "kind": marker["kind"],
                        "reason": (
                            f"grouped citation not fully accepted/keyed (cites "
                            f"{targets}, have {len(keyed)} of {len(targets)}); left "
                            "as plain text (all-or-nothing) so no co-citation is "
                            "silently dropped"
                        ),
                    })
                    continue
                ordered = keyed
            else:
                missing = [n for n in targets if n not in by_num]
                if missing:
                    report["unresolved_in_doc"].append({
                        "marker": anchor,
                        "kind": marker["kind"],
                        "reason": (
                            f"grouped citation not fully accepted/keyed (cited "
                            f"number(s) {missing} are unaccepted or unkeyable); left "
                            "as plain text (all-or-nothing) so no co-citation is "
                            "silently dropped"
                        ),
                    })
                    continue
                ordered = [by_num[n] for n in targets]
        else:
            # Author-year marker: every item links to its own reference. Place
            # each item by RE-RUNNING the plan's link owner over the plan's
            # full reference list (_author_year_item_links: suffix-aware,
            # ambiguous → None), then look up that reference's keyed placement.
            # All-or-nothing, symmetric with the numeric branch: the marker is
            # converted only when EVERY item resolves to its own keyed member and
            # no two items share one (a collapsed "(Smith, 2020; Smith, 2020)"
            # would cite fewer works than it prints). Otherwise plain text +
            # unresolved_in_doc, never a silent subset.
            items = _author_year_items(anchor)
            n_items = max(1, len(items))
            keyed = {pl["ref_index"]: pl for pl in members
                     if pl.get("key") is not None and pl.get("ref_index") is not None}
            item_refs = _author_year_item_links(anchor, ay_reflist)
            ordered = [keyed[r] for r in item_refs if r is not None and r in keyed]
            distinct = len({id(pl) for pl in ordered})
            if len(item_refs) != n_items or len(ordered) != n_items or distinct != n_items:
                collapsed = len(ordered) == n_items and distinct != n_items
                report["unresolved_in_doc"].append({
                    "marker": anchor,
                    "kind": marker["kind"],
                    "reason": (
                        "grouped citation has two items resolving to the same "
                        "reference; left as plain text (all-or-nothing) so no "
                        "co-citation is silently dropped" if collapsed else
                        "grouped citation not fully accepted/keyed (an author-year "
                        "item is unlinked, ambiguous, unaccepted or unkeyable); left "
                        "as plain text (all-or-nothing) so no co-citation is "
                        "silently dropped"
                    ),
                })
                continue

        if not ordered:
            # Nothing keyable to cite here (e.g. every member's create failed).
            # Leave the marker as plain text — the unkeyable refs are already
            # surfaced via needs_input from the create pass.
            continue

        keys = [pl["key"] for pl in ordered]
        itemdata = [_itemdata_from_meta(pl["meta"], pl["key"]) for pl in ordered]
        # Degrade-safe: a matched-key placement under a DEGRADED read (offline /
        # creds missing) must still write the field (carries the KEY; Word rebinds
        # the URI on Refresh) rather than crash on item_uri. See zotero single owner.
        uris = [zotero.item_uri_offline_safe(k) for k in keys]
        # rendered is the pre-Refresh placeholder text; for one key this is
        # byte-identical to the old "({key})". Keys are alphanumeric so the
        # render can never re-introduce a numeric marker into the tree.
        rendered = "(" + "; ".join(keys) + ")"

        if isinstance(marker.get("para"), int) and isinstance(marker.get("offset"), int):
            occ = {"para": marker["para"], "offset": marker["offset"], "anchor": anchor,
                   "keys": keys, "itemdata": itemdata, "uris": uris, "rendered": rendered}
            if marker.get("superscript"):
                occ["superscript"] = True
            loc_occurrences.append(occ)
            loc_meta.append(marker)
            continue

        if not _anchor_is_unique(anchor):
            report["unresolved_in_doc"].append({
                "marker": anchor,
                "kind": marker["kind"],
                "reason": "anchor not uniquely located in document",
            })
            continue
        text_jobs.append({"marker": marker, "keys": keys, "itemdata": itemdata,
                          "uris": uris, "rendered": rendered})

    # ONE batched location call for every located marker (before any text-path
    # write, so every recorded offset still describes the tree). A refused
    # occurrence is left as plain text and reported with zoterofield's reason.
    if loc_occurrences:
        try:
            loc_status = zoterofield.replace_occurrences_with_zotero_fields(
                doc, loc_occurrences, track=track, date=op_date, spread=False,
                **_author_kw,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort, never fail the run
            loc_status = [f"replace failed: {exc}"] * len(loc_occurrences)
        for marker, occ, st in zip(loc_meta, loc_occurrences, loc_status):
            if st is None:
                # Per-PHYSICAL-marker count: one grouped "[3,4]" -> +1, not +len(keys).
                report["replaced"] += 1
            else:
                report["unresolved_in_doc"].append({
                    "marker": occ["anchor"],
                    "kind": marker["kind"],
                    "reason": f"not converted at its location (para {occ['para']}): {st}",
                })

    for job in text_jobs:
        marker = job["marker"]
        anchor = marker["text"]
        try:
            zoterofield.replace_text_with_zotero_field(
                doc, anchor, job["keys"],
                itemdata=job["itemdata"], uris=job["uris"],
                rendered=job["rendered"],
                track=track, date=op_date, **_author_kw,
            )
            report["replaced"] += 1
        except Exception as exc:  # noqa: BLE001 — best-effort, never fail the run
            report["unresolved_in_doc"].append({
                "marker": anchor,
                "kind": marker["kind"],
                "reason": f"replace failed: {exc}",
            })

    # resolved placeholders → insert at the placeholder's literal text anchor
    for pl in placements:
        if pl["key"] is None or not pl["is_ph"]:
            continue
        key = pl["key"]
        ph = pl["ph"]
        anchor = ph["text"]
        idata = _itemdata_from_meta(pl["meta"], key)
        uri = zotero.item_uri_offline_safe(key)  # degrade-safe (see above / zotero owner)
        if ph.get("kind") == "comment" or not _anchor_is_unique(anchor):
            # Comment-embedded placeholders have no in-body text anchor; and a
            # non-unique bracket can't be safely targeted. Surface for input.
            report["unresolved_in_doc"].append({
                "marker": anchor,
                "kind": ph.get("kind", "bracket"),
                "reason": "placeholder anchor not uniquely located in document body",
            })
            continue
        try:
            zoterofield.replace_text_with_zotero_field(
                doc, anchor, [key],
                itemdata=[idata], uris=[uri],
                rendered=f"({key})", track=track, date=op_date, **_author_kw,
            )
            report["replaced"] += 1
        except Exception as exc:  # noqa: BLE001
            report["unresolved_in_doc"].append({
                "marker": anchor,
                "kind": ph.get("kind", "bracket"),
                "reason": f"replace failed: {exc}",
            })

    # -----------------------------------------------------------------------
    # 6. Spread THIS op's marks once (distinct, ordered w:dates ending at the
    #    pinned op date; pre-op marks excluded by provenance), then save.
    # -----------------------------------------------------------------------
    if track:
        zoterofield._spread_op_marks(doc, spread_author, op_date, pre_marks)
    doc.save(out_path)
    report["out"] = str(out_path)
    return report
