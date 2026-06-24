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

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import refextract
from . import refresolve
from . import zotero
from . import citecheck
from . import citeconvert
from . import zoterofield

# The dedicated collection + tag every added item gets (confirmed design).
IMPORTED_COLLECTION = "Imported — review"
ADDED_TAG = "added-by:zotero-word-cite"

# How a confidence level maps to a tier bucket.
_TIER_BY_CONFIDENCE = {
    "high": "high",
    "medium": "medium",
    "low": "low",
    "none": "low",
}

_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
# First "Author" token of an author-year in-text marker, e.g. "(Smith et al., 2020)".
_FIRST_AUTHOR_RE = re.compile(r"[A-Za-z][A-Za-z'\-]+")


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


def _first_author_of_marker(marker_text: str) -> Optional[str]:
    """First author surname from an author-year marker (best effort)."""
    # Strip the enclosing parens / bare form; grab the first capitalised word.
    body = marker_text.strip().lstrip("(").rstrip(")")
    m = _FIRST_AUTHOR_RE.search(body)
    if not m:
        return None
    token = m.group(0)
    if token.lower() in ("et", "al"):
        return None
    return token


def _year_of_marker(marker_text: str) -> Optional[str]:
    m = _YEAR_RE.search(marker_text)
    return m.group(0) if m else None


def _reflist_first_author(text: str) -> Optional[str]:
    """First author surname of a reference-list entry (best effort).

    Handles ``"1. Smith J, Jones A. Title..."`` and ``"[1] Smith AB. ..."`` by
    stripping the leading numbering, then taking the first capitalised word.
    """
    stripped = re.sub(r"^\s*(\[\d+\]|\(?\d+\)?[.)])\s*", "", text).strip()
    m = _FIRST_AUTHOR_RE.search(stripped)
    return m.group(0) if m else None


def _link_intext_to_reflist(intext: List[dict], reflist: List[dict]) -> Dict[int, List[int]]:
    """Map each reflist entry index → list of in-text marker indices pointing to it.

    * numeric marker → reflist entry whose ``numbering`` equals the cited number.
    * author_year marker → reflist entry whose first-author surname + year both
      appear in the entry text.

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
            author = _first_author_of_marker(marker["text"])
            year = _year_of_marker(marker["text"])
            if not author or not year:
                continue
            for entry in reflist:
                etext = entry["text"]
                ref_author = _reflist_first_author(etext)
                if (
                    ref_author
                    and ref_author.lower() == author.lower()
                    and year in _YEAR_RE.findall(etext)
                ):
                    _add(entry["index"], it_idx)
                    break
    return links


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

    Returns
    -------
    dict
        ``{"summary", "references", "placeholders", "foreign_fields",
        "needs_confirmation", "auto"}`` — see module docstring / spec.
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

    for entry in reflist:
        resolved = refresolve.resolve_reference(entry["text"], fetch=fetch)
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
    for ph_idx, ph in enumerate(raw_placeholders):
        resolved = refresolve.resolve_reference(ph["text"], fetch=fetch)
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
        if isinstance(a, dict):
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
       the first unconsumed inventory marker is taken; uniqueness is still gated
       downstream by ``_anchor_is_unique`` before any write.
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


def apply_unification(
    path,
    plan: dict,
    decisions: dict,
    *,
    out,
    add_missing: bool = True,
    track: bool = True,
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
        "retracted_flagged", "unresolved_in_doc"}``.
    """
    decisions = decisions or {}
    if decisions.get("add_missing") is not None:
        add_missing = bool(decisions["add_missing"])
    accept = set(decisions.get("accept", []))
    ph_resolutions = decisions.get("placeholder_resolutions", {}) or {}
    source_label = source_label or Path(path).name

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
    try:
        lib_index = zotero.library_index(strict=True)
    except zotero.LibraryUnavailableError:
        lib_index = None
    # Back-compat: step 4 / create_items take a flat DOI→key map. ``None`` is the
    # fail-closed sentinel and MUST propagate as ``None`` (not {}) so creation is
    # refused on a degraded read.
    doi_index = None if lib_index is None else (lib_index.get("doi") or {})

    report: dict = {
        "out": None,
        "added": [],
        "matched": [],
        "replaced": 0,
        "needs_input": [],
        "retracted_flagged": [],
        "unresolved_in_doc": [],
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

    for ref in accepted_refs:
        meta = _strip_score(ref.get("metadata"))
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
                    "title": pl["title"],
                    "doi": _meta_doi(pl["meta"]),
                    "reason": "Zotero library could not be read (degraded/unavailable); "
                              "refusing to create to avoid duplicate writes to the "
                              "shared group. Re-run when the library is reachable.",
                })
        elif not add_missing:
            for pl in to_create_back:
                report["needs_input"].append({
                    "title": pl["title"],
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
                    "title": pl["title"],
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
            )
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
                        report["matched"].append({"title": pl["title"], "key": sk})
                        continue
                if key is None and (meta_title_l in failed_titles or title_l in failed_titles):
                    report["needs_input"].append({
                        "title": pl["title"],
                        "doi": doi,
                        "reason": "Zotero create failed for this item.",
                    })
                    continue
                if key is None:
                    report["needs_input"].append({
                        "title": pl["title"],
                        "doi": doi,
                        "reason": "could not map a Zotero key to this item after create.",
                    })
                    continue
                pl["key"] = key
                report["added"].append({"title": pl["title"], "key": key})

    # -----------------------------------------------------------------------
    # 5. Rewrite the document.
    #    (a) Foreign FIELD-based citations via convert_to_zotero (on the input);
    #        operate on its output for the plain-text pass so we don't double
    #        process the same file.
    # -----------------------------------------------------------------------
    out_path = Path(out)
    n_foreign = plan.get("summary", {}).get("n_foreign_fields", 0)

    convert_result = None
    rewrite_src = Path(path)
    if n_foreign:
        # Convert foreign fields straight into the final output, then re-open
        # that file for the plain-text insertion pass.
        convert_result = citeconvert.convert_to_zotero(path, out=out_path, track=track)
        report["replaced"] += len(convert_result.get("converted", []))
        rewrite_src = out_path

    doc = zoterofield.Docx(rewrite_src)
    root = doc.read_tree(zoterofield.DOCUMENT)

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
        from .paras import find_paragraphs
        return len(find_paragraphs(root, anchor)) == 1

    # Rendered text of citations that are ALREADY live Zotero fields. refextract
    # reads markers from rendered text, so a field rendering "(1,2)" is indistinguishable
    # from plain typed "(1,2)"; re-citing such a marker would overwrite the existing
    # field with a raw-key render. Snapshot BEFORE any insertion mutates the tree, and
    # skip those markers below — only manual/foreign-text cites should be converted.
    already_live = zoterofield.existing_renderings(root)

    # in-text markers for accepted references
    for pl in placements:
        if pl["key"] is None or pl["is_ph"]:
            continue
        key = pl["key"]
        idata = _itemdata_from_meta(pl["meta"], key)
        # Degrade-safe: a matched-key placement under a DEGRADED read (offline /
        # creds missing) must still write the field (carries the KEY; Word rebinds
        # the URI on Refresh) rather than crash on item_uri. See zotero single owner.
        uri = zotero.item_uri_offline_safe(key)
        # F7: resolve each linked marker against the inventory of the MUTATED file
        # (rewrite_src). Prefer the plan-carried marker text (index-shift-proof);
        # fall back to the raw plan index only for old/serialized plans that lack
        # ``intext_markers``. ``_dedup_markers`` keeps each physical marker once.
        for marker in _resolve_link_markers(pl, intext_by_text, intext_by_index):
            anchor = marker["text"]
            if anchor.strip() in already_live:
                # Already a live Zotero field — leave it untouched (re-citing would
                # clobber the managed field and lose its rendered text).
                continue
            if not _anchor_is_unique(anchor):
                report["unresolved_in_doc"].append({
                    "marker": anchor,
                    "kind": marker["kind"],
                    "reason": "anchor not uniquely located in document",
                })
                continue
            try:
                zoterofield.replace_text_with_zotero_field(
                    doc, anchor, [key],
                    itemdata=[idata], uris=[uri],
                    rendered=f"({key})", track=track,
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
                rendered=f"({key})", track=track,
            )
            report["replaced"] += 1
        except Exception as exc:  # noqa: BLE001
            report["unresolved_in_doc"].append({
                "marker": anchor,
                "kind": ph.get("kind", "bracket"),
                "reason": f"replace failed: {exc}",
            })

    # -----------------------------------------------------------------------
    # 6. Save the rewritten document.
    # -----------------------------------------------------------------------
    doc.save(out_path)
    report["out"] = str(out_path)
    return report
