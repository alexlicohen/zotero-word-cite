"""Journal -> CSL-style-id catalog + resolver.

A journal mandates a CSL (Citation Style Language) style; Zotero renders *any*
valid CSL id server-side, so the job here is twofold:

* map a free-text journal name (e.g. ``"Brain"``, ``"ann neurol"``) to the
  correct CSL **id** that Zotero will recognise, and
* validate that an id is plausible/real before it is written into a Zotero
  document pref (see :func:`zoterocite.zoterofield.ensure_pref`), instead of
  silently passing an unknown string through as a style URL.

The seed catalog below is in-module data (not under ``data/``, which is
gitignored) so loading needs **no network**. Every id was verified during
development against the authoritative CSL repository
(``github.com/citation-style-language/styles``, ``master``) via the GitHub
git-tree + contents APIs and cross-checked against Zotero's master
``styles.json`` export. See ``SEED_PROVENANCE`` at the bottom of this module for
the per-id evidence, including a few brief-supplied ids that do **not** exist
upstream and were corrected or dropped.

The online check (:func:`is_valid_style` with ``online=True``) is a *secondary*
confirmation only: a known-catalog id is always valid regardless of the
network, and an unreachable network yields a "cannot verify" result rather than
a hard failure.
"""
from __future__ import annotations

import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple

# Where a raw CSL style file lives (used only by the optional online check).
RAW_BASE = "https://raw.githubusercontent.com/citation-style-language/styles/master"

# A syntactically-plausible CSL id (slug): lowercase letters/digits separated by
# single hyphens. Zotero ids look like ``the-lancet-neurology`` / ``apa``.
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# Minimum total length and minimum length of at least one hyphen-separated
# segment for a slug to be considered plausible. These reject single-character
# (``x``), pure-numeric (``123``), and meaningless-segment (``a-b-c``) blobs
# that the bare _SLUG_RE happily accepted — such a string is almost never a real
# CSL id, yet writing it produced a dead zotero.org/styles/<slug> pref.
_SLUG_MIN_LEN = 3
_SLUG_MIN_SEGMENT = 3


@dataclass(frozen=True)
class Style:
    """One catalog entry: a journal/style mapped to its CSL id.

    ``numbered`` / ``superscript`` are filled only where confidently known from
    the style's CSL ``citation-format`` and layout; left ``None`` otherwise
    (we leave unknown rather than guess). ``aliases`` are extra match strings
    (abbreviations, former names) folded into name resolution.
    """

    name: str                       # canonical display name
    csl_id: str                     # the id Zotero renders (e.g. "the-lancet")
    issn: Optional[str] = None
    numbered: Optional[bool] = None       # True=numeric/citation-sequence, False=author-date
    superscript: Optional[bool] = None    # True if citation numbers are superscript
    aliases: Tuple[str, ...] = field(default_factory=tuple)
    note: Optional[str] = None            # provenance / caveat shown to callers
    et_al_after: Optional[int] = None     # truncate author list after N; None = unknown (caller uses default)


# ---------------------------------------------------------------------------
# Seed catalog
# ---------------------------------------------------------------------------
# Ordering is irrelevant; resolution is by normalized name/alias, not position.
# Every csl_id here is real in CSL master OR (vancouver*) a long-standing
# Zotero-served id retained for back-compat — see SEED_PROVENANCE.
#
# Provenance caveat on ``et_al_after``: unlike the csl_ids (rigorously verified
# against CSL master, see SEED_PROVENANCE), the et_al_after seed integers are
# *unverified house values* — plausible author-truncation thresholds for these
# journals but not cited to a specific style spec. Each is flagged inline below.
_SEED: Tuple[Style, ...] = (
    # -- generic / default styles (the 4 the codebase already used) ----------
    Style("Vancouver (superscript)", "vancouver-superscript",
          numbered=True, superscript=True,
          et_al_after=6,  # unverified house value
          aliases=("vancouver superscript", "nih", "nlm superscript"),
          note="Zotero-served legacy id; not a file in CSL master (see provenance). "
               "Project default."),
    Style("Vancouver", "vancouver",
          numbered=True, superscript=False,
          et_al_after=6,  # unverified house value
          aliases=("nlm", "icmje"),
          note="Zotero-served legacy id; not a file in CSL master (see provenance)."),
    Style("APA (7th edition)", "apa",
          numbered=False, superscript=False,
          aliases=("apa 7", "american psychological association")),
    Style("Nature", "nature", issn="0028-0836",
          numbered=True, superscript=True,
          et_al_after=5),  # unverified house value

    # -- neuro / medical journals Alex plausibly targets ---------------------
    Style("Annals of Neurology", "annals-of-neurology", issn="0364-5134",
          numbered=True, superscript=True,
          aliases=("ann neurol", "annals neurology"),
          note="ICMJE numeric, superscript without brackets, citation order. "
               "Author rule et-al-min=5/use-first=3 (list <=4 authors in full; "
               ">=5 -> first 3 then 'et al.'), an asymmetry et_al_after cannot "
               "express; carried as data in packs/journals/annals-of-neurology.json "
               "reference_rules for the refstyle checker."),
    Style("Neurology", "neurology", issn="0028-3878",
          numbered=True, superscript=True),
    Style("JAMA Neurology", "jama",
          numbered=True, superscript=False,
          et_al_after=6,  # unverified house value
          aliases=("jama neurology", "jama neurol", "archives of neurology"),
          note="No dedicated jama-neurology CSL style exists upstream; mapped to "
               "the JAMA (AMA) style, which JAMA Neurology follows. Verify in proof."),
    Style("Brain", "brain", issn="0006-8950",
          numbered=True, superscript=True,
          aliases=("brain journal", "brain a journal of neurology"),
          note="Dependent style of american-medical-association (AMA): numeric, "
               "superscript in-text. AMA's author rule is et-al-min=7/use-first=3 "
               "(list <=6 authors in full; >=7 -> first 3 then 'et al.'), an "
               "asymmetry the single-value et_al_after cannot express, so it is "
               "left unset here and carried as data in packs/journals/brain.json "
               "reference_rules for the refstyle checker."),
    Style("Nature Neuroscience", "nature-neuroscience", issn="1097-6256",
          numbered=True, superscript=True,
          et_al_after=5,  # unverified house value
          aliases=("nat neurosci",)),
    Style("Nature Medicine", "nature-medicine", issn="1078-8956",
          numbered=True, superscript=True,
          et_al_after=5,  # unverified house value
          aliases=("nat med",)),
    Style("Nature Communications", "nature-communications", issn="2041-1723",
          numbered=True, superscript=True,
          et_al_after=5,  # unverified house value
          aliases=("nat commun", "ncomms")),
    Style("The Lancet", "the-lancet", issn="0140-6736",
          numbered=True, superscript=True,
          et_al_after=4,  # unverified house value
          aliases=("lancet",)),
    Style("The Lancet Neurology", "the-lancet-neurology", issn="1474-4422",
          numbered=True, superscript=True,
          et_al_after=4,  # unverified house value
          aliases=("lancet neurology", "lancet neurol")),
    Style("NeuroImage", "neuroimage", issn="1053-8119",
          numbered=False, superscript=False,
          aliases=("neuro image",)),
    Style("Epilepsia", "epilepsia", issn="0013-9580",
          numbered=True, superscript=True),
    Style("Proceedings of the National Academy of Sciences (PNAS)", "pnas",
          issn="0027-8424",
          numbered=True, superscript=False,
          aliases=("pnas", "proceedings of the national academy of sciences",
                   "proc natl acad sci")),
    Style("Cell", "cell", issn="0092-8674",
          numbered=True, superscript=True),
    Style("eLife", "elife", issn="2050-084X",
          numbered=False, superscript=False),
)

# index by csl_id (last wins; ids are unique here)
_BY_ID: Dict[str, Style] = {s.csl_id: s for s in _SEED}


# ---------------------------------------------------------------------------
# Name normalization + resolution
# ---------------------------------------------------------------------------
_THE_PREFIX = re.compile(r"^the\s+", re.IGNORECASE)
_NONALNUM = re.compile(r"[^a-z0-9]+")


def _norm(s: str) -> str:
    """Normalize a journal name for matching: lowercase, drop a leading "the",
    strip punctuation/ampersands, collapse whitespace."""
    s = (s or "").strip().lower()
    s = s.replace("&", " and ")
    s = _THE_PREFIX.sub("", s)
    s = _NONALNUM.sub(" ", s)
    return " ".join(s.split())


def _match_keys(style: Style) -> List[str]:
    """All normalized strings that should resolve to this style."""
    keys = [_norm(style.name), _norm(style.csl_id.replace("-", " "))]
    keys.extend(_norm(a) for a in style.aliases)
    return [k for k in keys if k]


# Precompute the normalized-key -> style map (exact matches) and the list of
# (key, token-set, style) triples for the whole-token-subset match below.
_EXACT: Dict[str, Style] = {}
_PAIRS: List[Tuple[str, Style]] = []
_TOKEN_PAIRS: List[Tuple[frozenset, Style]] = []
for _s in _SEED:
    for _k in _match_keys(_s):
        _EXACT.setdefault(_k, _s)
        _PAIRS.append((_k, _s))
        _TOKEN_PAIRS.append((frozenset(_k.split()), _s))


def resolve_style(journal_name: str) -> Optional[str]:
    """Resolve a free-text journal name to its CSL id, or ``None``.

    Resolution order, strict:

    1. **Exact** normalized name/alias match.
    2. The query already *is* a known csl id (with or without hyphens).
    3. **Whole-token subset**: the query's word set is a subset of a catalog
       key's word set — i.e. the query is a (possibly abbreviated) sub-phrase of
       exactly one catalog journal name. This is what lets a query like
       ``"Brain"`` (already exact here) or a partial name resolve, while
       refusing to map a *longer*, unknown journal onto a *shorter* catalog key.

    There is deliberately **no** substring-containment or fuzzy-ratio fallback.
    Both silently mapped real-but-uncataloged journals to a wrong-yet-valid CSL
    style (e.g. "Brain Communications"->``brain``, "Science"->``pnas``,
    "Nature Reviews Neuroscience"->``nature-neuroscience``) — the exact class of
    silent corruption this catalog exists to prevent. On any ambiguity (the
    query subset matches >1 distinct csl_id) or no whole-token match, return
    ``None`` rather than guess; callers can surface :func:`nearest_styles` as a
    non-committal "did you mean".

    Crucially the subset test is **one-directional**: only ``query ⊆ key`` (the
    query is contained in a longer catalog name). The reverse — a short catalog
    key contained in a longer query, e.g. catalog ``brain`` inside the query
    "brain communications" — is exactly the dangerous case and is rejected.

    Returns the CSL **id** (e.g. ``"the-lancet-neurology"``), never a URL.
    """
    q = _norm(journal_name)
    if not q:
        return None
    if q in _EXACT:
        return _EXACT[q].csl_id

    # If the query already *is* a known csl id (with/without hyphens), accept it.
    raw = (journal_name or "").strip().lower()
    if raw in _BY_ID:
        return raw

    # Whole-token subset: the query's tokens are a subset of a catalog key's
    # tokens (query is a sub-phrase of a longer catalog name). Collect the
    # distinct csl_ids reachable this way; resolve only if exactly one — any
    # ambiguity yields None, never a shortest-key/longest-key guess.
    qtokens = frozenset(q.split())
    matched_ids = set()
    matched_style: Optional[Style] = None
    for ktokens, style in _TOKEN_PAIRS:
        if qtokens <= ktokens:
            if style.csl_id not in matched_ids:
                matched_ids.add(style.csl_id)
                matched_style = style
    if len(matched_ids) == 1:
        return matched_style.csl_id
    return None


def nearest_styles(journal_or_id: str, n: int = 3) -> List[Style]:
    """Return up to ``n`` catalog styles closest to the input (for error
    messages / "did you mean"). Ranked by best fuzzy ratio across the style's
    keys; deterministic for ties (by csl_id)."""
    q = _norm(journal_or_id) or _norm((journal_or_id or "").replace("-", " "))
    scored: List[Tuple[float, Style]] = []
    seen = set()
    for style in _SEED:
        if style.csl_id in seen:
            continue
        seen.add(style.csl_id)
        best = max((SequenceMatcher(None, q, k).ratio() for k in _match_keys(style)),
                   default=0.0)
        scored.append((best, style))
    scored.sort(key=lambda t: (-t[0], t[1].csl_id))
    return [s for _, s in scored[:n]]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def is_known_id(csl_id: str) -> bool:
    """True if ``csl_id`` is an exact id in the seed catalog."""
    return csl_id in _BY_ID


def is_plausible_id(csl_id: str) -> bool:
    """True if ``csl_id`` is a syntactically-plausible CSL slug.

    This is what lets a *new* journal style Alex names (not yet in the seed) be
    accepted rather than rejected, while a URL, an empty string, or a typo'd
    ``ZOTERO_...`` blob is refused. It is the *only* gate on a write to a Zotero
    style pref for an uncataloged id (see :func:`zoterocite.zoterofield.
    _resolve_style_url`), so it must be strict enough that a slug passing it is
    plausibly a real CSL id — not a stray token.

    A slug is plausible iff **all** hold:

    * it matches :data:`_SLUG_RE` (lowercase ``a-z0-9`` segments joined by single
      hyphens — no leading/trailing/double hyphen, no caps/underscores/spaces/URL);
    * its length is at least :data:`_SLUG_MIN_LEN` (rejects ``"x"``);
    * it contains at least one letter (rejects ``"123"`` / pure-numeric);
    * at least one hyphen-separated segment is at least :data:`_SLUG_MIN_SEGMENT`
      chars long (rejects ``"a-b-c"`` — a slug of only stub segments is noise).

    Note: catalog ids never reach here on the validation path — :func:`is_valid_style`
    checks :func:`is_known_id` first — so this gate constrains only *uncataloged*
    slugs. Real CSL ids such as ``apa`` / ``pnas`` (single ≥3-char segment) and
    ``the-lancet-neurology`` still pass.
    """
    if not csl_id or not _SLUG_RE.match(csl_id):
        return False
    if len(csl_id) < _SLUG_MIN_LEN:
        return False
    if not any(c.isalpha() for c in csl_id):
        return False
    if not any(len(seg) >= _SLUG_MIN_SEGMENT for seg in csl_id.split("-")):
        return False
    return True


def _online_exists(csl_id: str, timeout: float = 4.0) -> Optional[bool]:
    """Single short GET to the raw CSL file. Returns:

    * ``True``  -> the file exists (HTTP 200),
    * ``False`` -> the host answered that it does not (HTTP 404),
    * ``None``  -> *cannot verify* (network unreachable / timeout / other HTTP
      error). Callers must treat ``None`` as "unknown", never as invalid.
    """
    url = f"{RAW_BASE}/{csl_id}.csl"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted host)
            return 200 <= getattr(resp, "status", 200) < 300
    except urllib.error.HTTPError as e:  # definitive answer from the server
        if e.code == 404:
            return False
        return None  # 403/5xx/etc. -> cannot verify
    except (urllib.error.URLError, OSError):
        return None  # DNS/timeout/offline -> cannot verify


def is_valid_style(csl_id: str, *, online: bool = False) -> bool:
    """Validate a CSL id for use as a Zotero style.

    Default (``online=False``): valid iff it is a known catalog id **or** a
    syntactically-plausible CSL slug. No network.

    With ``online=True``: a known catalog id is *always* valid (the network is
    only a secondary check and must never reject an id we vouch for). For a
    plausible-but-unknown slug we additionally consult the CSL repo:

    * file present (200)            -> ``True``
    * file absent (404)             -> ``False``
    * cannot verify (offline/error) -> ``True`` (fail-open to the offline rule;
      we never hard-fail on an unreachable network, per the API contract).

    A syntactically-implausible value (URL, blank, etc.) is always invalid.

    Important — "valid" is not "verified".  Offline, an uncataloged-but-plausible
    slug returns ``True`` so that a brand-new journal style the user names is not
    rejected (this is the intended fail-open behaviour).  That ``True`` means
    "accept and write the pref", NOT "this id was confirmed to exist".  Callers
    that want to warn the user when they are about to write an *unverified* style
    should consult :func:`style_validation_note`, which distinguishes a vouched-
    for catalog id from an accepted-but-unconfirmed slug.
    """
    if is_known_id(csl_id):
        return True
    if not is_plausible_id(csl_id):
        return False
    if not online:
        return True
    exists = _online_exists(csl_id)
    if exists is None:        # cannot verify -> defer to the offline rule
        return True
    return exists


def style_validation_note(csl_id: str, *, online: bool = False) -> Optional[str]:
    """Return a human-readable WARN for an *accepted-but-unverified* style, else
    ``None``.

    This is the advisory companion to :func:`is_valid_style`.  ``is_valid_style``
    deliberately fails *open* offline — a plausible slug we have never cataloged
    is accepted as ``True`` so a new journal style is not rejected — but that
    acceptance is silent, so the user is never told they just wrote a style id
    that zotero-word-cite could not confirm exists.  This function surfaces that gap.

    Returns ``None`` (no warning) when:

    * the id is a known catalog id (we vouch for it — includes the incumbent
      ``apa`` / ``nature`` / ``vancouver`` / ``vancouver-superscript`` styles,
      which are all cataloged), or
    * ``online=True`` and the CSL repo confirms the file exists (HTTP 200).

    Returns a WARN string when:

    * the id is uncataloged and ``online=False`` (accepted offline purely on
      slug shape — never checked against the catalog or the repo), or
    * ``online=True`` but the repo could not confirm it (network unreachable /
      error -> "cannot verify"; ``is_valid_style`` still accepts it fail-open,
      but the user should know it is unconfirmed).

    A syntactically-implausible value returns ``None`` here — that case is a hard
    rejection handled by :func:`is_valid_style` / ``_resolve_style_url`` (a
    ``ValueError``), not a soft warning.  Never raises; never required for the
    write to proceed — it is purely informational.
    """
    if is_known_id(csl_id):
        return None
    if not is_plausible_id(csl_id):
        # Implausible -> not our concern here; is_valid_style hard-rejects it.
        return None
    if online:
        exists = _online_exists(csl_id)
        if exists is True:
            return None
        if exists is False:
            # is_valid_style returns False here; the caller rejects outright.
            return (
                f"CSL style {csl_id!r} was not found in the CSL repository "
                f"(HTTP 404). It will be rejected."
            )
        # exists is None -> cannot verify; accepted fail-open but unconfirmed.
        return (
            f"CSL style {csl_id!r} is not in zotero-word-cite's catalog and could "
            f"not be verified against the CSL repository (network unreachable). "
            f"It will be written as-is but has NOT been confirmed to exist — "
            f"check it renders correctly in Zotero."
        )
    return (
        f"CSL style {csl_id!r} is not in zotero-word-cite's catalog and was not "
        f"verified online. It is syntactically plausible so it will be written "
        f"as-is, but has NOT been confirmed to exist — re-run with --online to "
        f"verify, or check it renders correctly in Zotero."
    )


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------
def list_styles() -> List[dict]:
    """Return the seed catalog as a list of plain dicts (display name, csl id,
    issn, numbered/superscript flags, aliases, note), sorted by display name."""
    out = [
        {
            "name": s.name,
            "csl_id": s.csl_id,
            "issn": s.issn,
            "numbered": s.numbered,
            "superscript": s.superscript,
            "et_al_after": s.et_al_after,
            "aliases": list(s.aliases),
            "note": s.note,
        }
        for s in _SEED
    ]
    out.sort(key=lambda d: d["name"].lower())
    return out


def get_style(csl_id: str) -> Optional[Style]:
    """Return the seed :class:`Style` for an exact id, or ``None``."""
    return _BY_ID.get(csl_id)


# ---------------------------------------------------------------------------
# Provenance (development-time verification; not used at runtime)
# ---------------------------------------------------------------------------
# Verified 2026-06-16 against citation-style-language/styles@master via the
# GitHub git-tree (recursive, untruncated, 10,881 paths) + contents API, and
# cross-checked against Zotero master styles.json (10,850 entries).
#
#   EXISTS upstream (independent, root *.csl):
#     apa, nature, annals-of-neurology, neurology, the-lancet, epilepsia,
#     cell, elife, pnas
#   EXISTS upstream (dependent/*.csl; same id Zotero resolves):
#     brain, nature-neuroscience, nature-medicine, nature-communications,
#     the-lancet-neurology, neuroimage (neuroimage is author-date)
#   CORRECTED from the brief:
#     proceedings-of-the-national-academy-of-sciences -> pnas
#         (the long id only exists for the India A/B sections; US PNAS is `pnas`)
#     jama-neurology -> jama
#         (no jama-neurology style exists upstream; JAMA Neurology follows JAMA/AMA)
#   DROPPED from the brief (no such style exists upstream, so not seeded):
#     brain-communications, annals-of-clinical-and-translational-neurology
#   RETAINED despite not being files in CSL master (Zotero serves them via its
#   style repo; they are the codebase's incumbent, working ids):
#     vancouver, vancouver-superscript
SEED_PROVENANCE = {
    "pnas": "corrected from proceedings-of-the-national-academy-of-sciences",
    "jama": "JAMA Neurology mapped to jama (no jama-neurology upstream)",
    "vancouver": "Zotero-served; absent from CSL master files",
    "vancouver-superscript": "Zotero-served; absent from CSL master files; project default",
    "_dropped": ["brain-communications",
                 "annals-of-clinical-and-translational-neurology"],
}
