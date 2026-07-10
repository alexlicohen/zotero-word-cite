"""Zotero Web API v3 client for a shared *group* (or personal *user*) library.

Stdlib only (``urllib``/``json``) — no third-party HTTP client.

Credentials come from the environment, never from code:

============================  =================================================
``ZOTERO_API_KEY``            required for any network call
``ZOTERO_GROUP_ID``           id of a shared *group* library (preferred)
``ZOTERO_USER_ID``            id of a personal *user* library (fallback)
============================  =================================================

If a group id is present it wins (a shared library is the whole point of this
module). If neither id, or the key, is set, :func:`build_request` (and thus any
network function) raises a :class:`RuntimeError` that names the missing vars —
so tests can exercise request construction with ``monkeypatch.setenv`` and can
assert the failure mode without touching the network or needing real secrets.

The headline feature for a shared library is :func:`formatted_citations`, which
asks Zotero to render citations *server-side* in any CSL style — so the whole
group gets identical, correctly-styled output without bundling CSL locally.

Write capability (v3):
  - :func:`key_can_write` — check that the configured key has group write access.
  - :func:`csljson_to_zotero_item` — map CSL-JSON-ish metadata to a Zotero item object.
  - :func:`ensure_collection` — return (or create) a named collection, returning its key.
  - :func:`create_items` — batch-create items with deduplication and tagging.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import secrets
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import warnings
from pathlib import Path
from typing import Any, Optional

from . import _http
from . import cite
from . import citecheck

API_BASE = "https://api.zotero.org"
API_VERSION = "3"

_HTML_TAG = re.compile(r"<[^>]+>")


class LibraryUnavailableError(RuntimeError):
    """Raised when the library DOI index could NOT be loaded (read failure).

    Distinguishes a *degraded read* — 401/403/429/5xx/timeout during the
    paginated ``fetch_all`` with no usable disk cache — from a *genuinely empty
    library* (a successful fetch that returned no items). The two look identical
    if both collapse to ``{}``: a consumer that builds ``in_library`` /
    ``existing_key`` from an empty index would flip the WHOLE document from
    "match existing" to "create new", risking mass duplicate writes to the
    shared group library.

    WRITE paths (``unify.apply_unification``, ``endnote`` plan/apply) MUST treat
    this as fail-closed and refuse to create. Read-only paths (``litsearch``,
    ``libcoverage``) already wrap the index load in ``try/except Exception`` and
    degrade to ``{}`` (no library annotation), which is safe — they never write.
    """


def strip_html(s: str) -> str:
    """Zotero returns rendered citations as HTML; reduce to plain text."""
    return html.unescape(_HTML_TAG.sub("", s or "")).strip()


# ---------------------------------------------------------------------------
# Transient-failure retry (local — kept SEPARATE from zoterocite._http)
# ---------------------------------------------------------------------------
# ``_http.http_get`` is the shared retry primitive for the *unauthenticated*
# public-API clients (entrez/icite/mybib/orcid).  Zotero is deliberately NOT
# routed through it: every Zotero request carries the API key (and writes carry
# a ``Zotero-API-Token``) in its own header path, and Zotero's contract differs
# (it must surface a 404 to ``get_item`` and propagate hard errors so write
# paths can fail closed).  So the retry/Retry-After logic is replicated here,
# locally, around the three network primitives — with an injectable sleep so
# tests exercise the retry path without really sleeping.
#
# Zotero documents both a ``Backoff`` header (server is under load; pause before
# the *next* request) and ``Retry-After`` (on 429/503; do not retry until it
# elapses).  We honour either, clamped, before our ONE bounded retry on a 429 /
# transient 5xx.  A 429 mid-``fetch_all`` therefore retries once instead of
# raising an unhandled traceback.

# Statuses worth one retry: 429 (rate limit) + transient 5xx.  Mirrors _http.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# Clamp any server-sent wait so a CLI never blocks on a multi-minute sleep.
_MAX_RETRY_WAIT = 5.0
_DEFAULT_RETRY_WAIT = 1.0

# Module-level back-off sleep seam.  Tests monkeypatch ``zotero._retry_sleep``
# to a no-op so the retry path runs without really sleeping; production uses
# ``time.sleep``.  ``_urlopen_retrying`` also accepts a per-call ``_sleep`` arg.
_retry_sleep = time.sleep


def _effective_timeout(timeout: float) -> float:
    """Clamp *timeout* to the shared ``ZOTERO_WORD_CITE_HTTP_TIMEOUT`` ceiling, if set.

    The authenticated Zotero read/write path does NOT route through
    :func:`zoterocite._http.http_get`, so it historically ignored the
    process-wide timeout ceiling that the unauthenticated public-API clients
    (entrez/icite/orcid/…) already honour.  This applies the SAME ceiling (owned
    by :func:`_http._timeout_ceiling`) so a deployment can bound every Zotero
    socket read/write the way it already bounds those clients.  With no env var
    set the ceiling is ``None`` and *timeout* is returned unchanged
    (byte-identical default behaviour).
    """
    ceiling = _http._timeout_ceiling()
    if ceiling is not None and ceiling < timeout:
        return ceiling
    return timeout


def _progress(msg: str) -> None:
    """Emit a progress heartbeat to STDERR (never stdout).

    Long-running network loops (``fetch_all`` pagination; the per-entry
    reference-resolution loops in :mod:`unify`, which reuse this helper) print
    liveness here so a multi-minute run is visibly alive rather than an opaque
    hang.  STDERR only — ``--json`` consumers read stdout, which must stay a
    clean JSON document.  Best-effort: a broken/closed stderr never raises.
    """
    try:
        print(msg, file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 — a heartbeat must never break the caller
        pass


# Overall wall-clock budget (seconds) for a full paginated ``fetch_all``.  A cold
# library-index build fetches the WHOLE group (thousands of items); without an
# overall deadline a stalled or very large fetch runs unbounded until an external
# kill, leaving the index uncached.  Read at call time from
# ``ZOTERO_WORD_CITE_ZOTERO_FETCH_BUDGET``; a too-small value is floored (never
# sub-second) so a typo cannot abort every fetch instantly — mirrors
# :func:`refresolve.resolve_timeout`'s clamp.
_DEFAULT_FETCH_BUDGET = 120.0
_MIN_FETCH_BUDGET = 5.0


def _fetch_budget() -> float:
    """Overall ``fetch_all`` wall-clock budget in seconds, read at CALL time.

    Honours ``ZOTERO_WORD_CITE_ZOTERO_FETCH_BUDGET``; a malformed/absent/non-positive
    value yields :data:`_DEFAULT_FETCH_BUDGET`, and a too-small positive value is
    clamped up to :data:`_MIN_FETCH_BUDGET`.
    """
    raw = os.environ.get("ZOTERO_WORD_CITE_ZOTERO_FETCH_BUDGET")
    if not raw:
        return _DEFAULT_FETCH_BUDGET
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_FETCH_BUDGET
    if val <= 0:
        return _DEFAULT_FETCH_BUDGET
    return max(val, _MIN_FETCH_BUDGET)


def _parse_retry_wait(value: Optional[str]) -> float:
    """Seconds to wait per a ``Retry-After`` / ``Backoff`` header, clamped.

    Honours only the integer-seconds form (the form Zotero sends); an HTTP-date
    or anything unparseable falls back to the small default so we still back off
    once.  Clamped to ``[0, _MAX_RETRY_WAIT]``.
    """
    if value is None:
        return _DEFAULT_RETRY_WAIT
    try:
        secs = float(str(value).strip())
    except (TypeError, ValueError):
        return _DEFAULT_RETRY_WAIT
    if secs < 0:
        return _DEFAULT_RETRY_WAIT
    return min(secs, _MAX_RETRY_WAIT)


def _retry_after_from_headers(headers) -> Optional[str]:
    """Best-effort read of ``Retry-After`` (preferred) then ``Backoff`` from a
    mapping/HTTP-message; never raises (header access on some objects can)."""
    if headers is None:
        return None
    try:
        return headers.get("Retry-After") or headers.get("Backoff")
    except Exception:  # noqa: BLE001 — header access must never raise here
        return None


def _urlopen_retrying(
    req: urllib.request.Request,
    *,
    timeout: float,
    retries: int = 1,
    _sleep=None,
) -> bytes:
    """``urlopen(req)`` returning the raw body, with ONE bounded retry on a 429 /
    transient 5xx, honouring a ``Retry-After`` / ``Backoff`` header.

    Unlike :func:`zoterocite._http.http_get`, this PROPAGATES the final error
    rather than swallowing it to ``None`` — Zotero callers each apply their own
    contract on top (``get_item`` catches 404; ``library_doi_index`` catches all
    and fails closed; ``fetch_all``/``count_items`` let it propagate to the
    read-only wrappers that already ``try/except``).  The single retry just means
    a *transient* 429/5xx mid-pagination no longer raises on the first blip.

    ``_sleep`` is an injection seam (tests may pass a no-op); when ``None`` the
    module-level ``_retry_sleep`` is used (also monkeypatchable).  Not part of
    the public contract.  The request URL/headers (which carry the API key +
    write token) are never logged or surfaced here.
    """
    sleep = _sleep if _sleep is not None else _retry_sleep
    attempts = max(0, int(retries)) + 1  # original + retries
    timeout = _effective_timeout(timeout)  # honour ZOTERO_WORD_CITE_HTTP_TIMEOUT ceiling
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted host)
                return resp.read()
        except urllib.error.HTTPError as exc:
            status = getattr(exc, "code", None)
            if status not in _RETRY_STATUSES or attempt + 1 >= attempts:
                raise
            wait = _parse_retry_wait(
                _retry_after_from_headers(getattr(exc, "headers", None))
            )
            if wait > 0:
                sleep(wait)
            continue
    # Unreachable: the loop either returns a body or raises on the last attempt.
    raise RuntimeError("zotero retry loop exited without a result")  # pragma: no cover


def zotero_config() -> dict:
    """Read Zotero credentials from the environment.

    Returns a dict ``{"api_key", "library_type", "library_id"}`` where
    ``library_type`` is ``"group"`` or ``"user"``. A present group id is
    preferred over a user id. If the key or both ids are missing, returns an
    **empty dict** (this function never raises — callers decide what to do).
    """
    api_key = os.environ.get("ZOTERO_API_KEY")
    group_id = os.environ.get("ZOTERO_GROUP_ID")
    user_id = os.environ.get("ZOTERO_USER_ID")

    if not api_key:
        return {}
    if group_id:
        return {"api_key": api_key, "library_type": "group", "library_id": group_id}
    if user_id:
        return {"api_key": api_key, "library_type": "user", "library_id": user_id}
    return {}


def _missing_env() -> list[str]:
    """Names of the env vars that are missing for a valid configuration."""
    missing: list[str] = []
    if not os.environ.get("ZOTERO_API_KEY"):
        missing.append("ZOTERO_API_KEY")
    if not (os.environ.get("ZOTERO_GROUP_ID") or os.environ.get("ZOTERO_USER_ID")):
        missing.append("ZOTERO_GROUP_ID or ZOTERO_USER_ID")
    return missing


def build_request(
    path: str,
    params: Optional[dict[str, Any]] = None,
    *,
    extra_headers: Optional[dict[str, str]] = None,
) -> urllib.request.Request:
    """Build a signed GET :class:`urllib.request.Request` — *pure*, no network.

    The URL is ``{API_BASE}/{groups|users}/{id}/{path}?{params}`` and carries the
    ``Zotero-API-Key`` and ``Zotero-API-Version`` headers. ``params`` values are
    URL-encoded.  ``extra_headers`` (optional) are merged in AFTER the auth
    headers — used to carry a conditional-read precondition such as
    ``If-Modified-Since-Version`` on the delta-sync fast path; the default of
    ``None`` leaves the request byte-identical to the two-arg form.

    Raises :class:`RuntimeError` naming the missing env vars if credentials are
    not configured, so tests can construct/inspect requests without networking.
    """
    cfg = zotero_config()
    if not cfg:
        missing = ", ".join(_missing_env())
        raise RuntimeError(
            f"Zotero credentials not configured; set env var(s): {missing}"
        )

    prefix = "groups" if cfg["library_type"] == "group" else "users"
    base = f"{API_BASE}/{prefix}/{cfg['library_id']}/{path.lstrip('/')}"
    query = urllib.parse.urlencode(params or {})
    url = f"{base}?{query}" if query else base

    headers: dict[str, str] = {
        "Zotero-API-Key": cfg["api_key"],
        "Zotero-API-Version": API_VERSION,
    }
    if extra_headers:
        headers.update(extra_headers)
    return urllib.request.Request(url, method="GET", headers=headers)


def _get_json(req: urllib.request.Request, timeout: float = 15.0) -> Any:
    """Perform ``req`` (one bounded retry on a transient 429/5xx) and parse the
    JSON body.  A 404 (and any non-transient error) propagates unchanged, so
    ``get_item`` can still catch it.  The per-call default is 15 s; a configured
    ``ZOTERO_WORD_CITE_HTTP_TIMEOUT`` ceiling clamps it further (via
    :func:`_urlopen_retrying`)."""
    return json.loads(_urlopen_retrying(req, timeout=timeout).decode("utf-8"))


def _urlopen_with_headers_retrying(
    req: urllib.request.Request,
    *,
    timeout: float,
    retries: int = 1,
    _sleep=None,
) -> tuple[bytes, dict]:
    """``urlopen(req)`` returning ``(raw_body, headers_dict)`` with ONE bounded
    retry on a transient 429/5xx, honouring a ``Retry-After`` / ``Backoff`` header.

    The SINGLE retry-loop shared by BOTH the header-reading read path
    (:func:`_get_json_headers`, which parses the body as JSON) and the
    version-checked write path (:func:`_urlopen_write_with_headers`, which needs
    the raw body + response headers) — so a future retry-policy change lives in
    ONE place and cannot silently diverge between the two paths.  Body and headers
    are read from the *same* successful response (the connection is opened here).
    A retried request is SAFE for a version-checked write: it still carries the
    original ``If-Unmodified-Since-Version`` precondition, so a lost-response retry
    fails CLOSED on 412 rather than double-applying.  Non-transient errors (incl.
    412/409) propagate to the caller for classification.  ``_sleep`` is a test
    injection seam; when ``None`` the module-level ``_retry_sleep`` is used.  The
    request URL/headers (which carry the API key) are never logged or surfaced.
    """
    sleep = _sleep if _sleep is not None else _retry_sleep
    attempts = max(0, int(retries)) + 1  # original + retries
    timeout = _effective_timeout(timeout)  # honour ZOTERO_WORD_CITE_HTTP_TIMEOUT ceiling
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                body = resp.read()
                headers = {k: v for k, v in resp.headers.items()}
                return body, headers
        except urllib.error.HTTPError as exc:
            status = getattr(exc, "code", None)
            if status not in _RETRY_STATUSES or attempt + 1 >= attempts:
                raise
            wait = _parse_retry_wait(
                _retry_after_from_headers(getattr(exc, "headers", None))
            )
            if wait > 0:
                sleep(wait)
            continue
    raise RuntimeError("zotero retry loop exited without a result")  # pragma: no cover


def _get_json_headers(req: urllib.request.Request, timeout: float = 15.0):
    """Perform ``req``; return ``(parsed_json, headers_dict)`` — for pagination.

    Retries once on a transient 429/5xx (via the shared
    :func:`_urlopen_with_headers_retrying` loop) so a rate-limit hiccup
    mid-``fetch_all`` does not raise an unhandled traceback.  Both the body and
    the response headers (incl. ``Total-Results``) come from the *same* successful
    response.
    """
    body, headers = _urlopen_with_headers_retrying(req, timeout=timeout, retries=1)
    return json.loads(body.decode("utf-8")), headers


def _probe_items(
    *, top: bool = True, query: Optional[str] = None, qmode: Optional[str] = None
) -> tuple[Optional[int], Optional[int]]:
    """One cheap ``limit=1`` GET → ``(total_results, last_modified_version)``.

    The item COUNT (``Total-Results``) and the library VERSION
    (``Last-Modified-Version``) live in the headers of the SAME minimal response,
    so a caller that needs either reads ONE request, not two.  This is the single
    owner of that probe — :func:`count_items` and :func:`_read_library_version`
    both delegate here rather than each hand-building the byte-identical request
    and reading a different header off it.  Each element is ``None`` when its
    header is absent/unparseable; the caller decides how to fail (``count_items``
    fails closed on a missing count, the delta sync declines to persist a
    watermark on a missing version).
    """
    params: dict[str, Any] = {"format": "json", "limit": "1"}
    if query:
        params["q"] = query
    if qmode:
        params["qmode"] = qmode
    _data, headers = _get_json_headers(
        build_request("items/top" if top else "items", params)
    )
    raw_total = headers.get("Total-Results")
    total: Optional[int] = None
    if raw_total is not None:
        try:
            total = int(raw_total)
        except (TypeError, ValueError):
            total = None
    version = _version_from_headers(headers, fallback=None)
    return total, version


def count_items(query: Optional[str] = None, qmode: Optional[str] = None,
                top: bool = True) -> int:
    """Total number of (top-level) items in the library, or matching ``query``.

    Reads Zotero's ``Total-Results`` header (the authoritative count) via the
    shared :func:`_probe_items` probe, so it is not limited by the 25-item
    default page size.
    """
    total, _version = _probe_items(top=top, query=query, qmode=qmode)
    if total is None:
        # A missing/unparseable header is a DEGRADED read, not an empty library.
        # Defaulting to 0 would fail OPEN (report "no items" for an unreadable
        # count and invite blind writes), so fail CLOSED with the standard
        # degraded-read signal instead — same posture as :func:`fetch_all`.
        raise LibraryUnavailableError(
            "Zotero response is missing or has an unparseable Total-Results "
            "header; cannot read the item count. Treating as a degraded read "
            "(fail-closed)."
        )
    return total


def fetch_all(query: Optional[str] = None, qmode: Optional[str] = None,
              max_items: Optional[int] = None, top: bool = True) -> list[dict]:
    """Fetch ALL (top-level) items, paginating past the 25-item default page.

    Pages with ``limit=100`` and ``start`` until ``Total-Results`` is exhausted
    (or ``max_items`` is reached). ``top=True`` excludes child notes/attachments.

    Bounded by an OVERALL wall-clock budget (``ZOTERO_WORD_CITE_ZOTERO_FETCH_BUDGET``,
    default 120 s): a cold-cache index build fetches the whole group, and without
    a deadline a stalled or very large paginated fetch would run unbounded until
    an external kill.  On exceeding the budget it raises
    :class:`LibraryUnavailableError` — the SAME degraded-read signal any fetch
    failure raises — so :func:`library_doi_index` / :func:`library_index` fall
    back to the disk cache or fail closed, never mistaking a timed-out partial
    fetch for a complete (or empty) library.  A throttled progress heartbeat is
    emitted to STDERR after each page (never stdout, so ``--json`` stays clean).
    """
    out: list[dict] = []
    start = 0
    budget = _fetch_budget()
    deadline_start = time.time()
    # AUTHORITATIVE completeness signal: Zotero's ``Total-Results`` header. Its
    # ABSENCE (a proxy stripped it, or a degraded response) means we CANNOT
    # confirm the whole library was fetched. Treating a partial list as complete
    # is a fail-OPEN: a strict write path (``library_index(strict=True)`` →
    # ``create_items``) then sees the un-fetched items as absent and RE-CREATES
    # them, mass-duplicating the shared group. So this loop fails CLOSED —
    # raising :class:`LibraryUnavailableError` (the same degraded-read signal a
    # fetch failure raises, routing index builders to cache fallback / refuse)
    # rather than silently truncating. ``None`` = not yet known / unparseable.
    expected_total: Optional[int] = None
    while True:
        # Overall deadline check BETWEEN pages: bound the total fetch, not just
        # each socket read.  Raising the degraded-read exception routes the index
        # builders to their cache fallback / fail-closed path (see docstring).
        if time.time() - deadline_start > budget:
            raise LibraryUnavailableError(
                f"Zotero fetch exceeded its {budget:.0f}s budget "
                f"(ZOTERO_WORD_CITE_ZOTERO_FETCH_BUDGET) after {len(out)} item(s); "
                "treating as a degraded read (cache fallback / fail-closed)."
            )
        params: dict[str, Any] = {"format": "json", "limit": "100", "start": str(start)}
        if query:
            params["q"] = query
        if qmode:
            params["qmode"] = qmode
        data, headers = _get_json_headers(build_request("items/top" if top else "items", params))

        # Read the authoritative count for THIS page. Present+parseable updates
        # ``expected_total``; absent/unparseable leaves ``header_ok`` False so the
        # completeness checks below fail closed instead of defaulting to
        # "len-so-far == complete" (the silent-truncation bug).
        raw_total = headers.get("Total-Results")
        header_ok = False
        if raw_total is not None:
            try:
                expected_total = int(raw_total)
                header_ok = True
            except (TypeError, ValueError):
                header_ok = False

        if not isinstance(data, list):
            # Malformed (non-list) response — a degraded read, not an empty
            # library. Fail closed rather than returning a truncated result.
            raise LibraryUnavailableError(
                "Zotero returned a non-list items response; treating as a "
                "degraded read (fail-closed) rather than a complete library."
            )
        if not data:
            break
        out.extend(data)
        start += len(data)
        disp_total = expected_total if expected_total is not None else "?"
        _progress(f"zotero: fetched {len(out)}/{disp_total} items…")

        # A bounded read (explicit ``max_items``) is a deliberate partial fetch,
        # not a whole-library completeness claim — return what was asked for.
        if max_items is not None and len(out) >= max_items:
            return out[:max_items]

        if not header_ok:
            # No authoritative count on this page → cannot page safely and cannot
            # confirm completeness. Fail closed rather than break-and-truncate.
            raise LibraryUnavailableError(
                "Zotero response is missing/invalid the Total-Results header; "
                "cannot confirm the library was fully fetched. Treating as a "
                "degraded read (fail-closed) to avoid duplicate writes from a "
                "partial index."
            )
        if start >= expected_total:
            break

    # Positively confirm completeness before returning a list callers treat as
    # the WHOLE library. (Bounded ``max_items`` reads already returned above.)
    if expected_total is None:
        # Reached only when the FIRST page was empty AND carried no valid
        # Total-Results — an empty library reports ``Total-Results: 0``, so a
        # header-less empty response is a degraded read, not an empty library.
        raise LibraryUnavailableError(
            "Zotero response is missing the Total-Results header; cannot confirm "
            "the library was fully fetched (fail-closed)."
        )
    if len(out) != expected_total:
        # A page returned empty (or short) mid-pagination — we have fewer items
        # than the library holds. Fail closed to avoid a partial index.
        raise LibraryUnavailableError(
            f"Zotero fetch is incomplete: got {len(out)} of {expected_total} "
            "item(s) (a page truncated mid-pagination). Treating as a degraded "
            "read (fail-closed) to avoid duplicate writes from a partial index."
        )
    return out


# ---------------------------------------------------------------------------
# Incremental (delta) sync — a cached, cheap-to-refresh whole-library read
# ---------------------------------------------------------------------------
#
# ``fetch_all`` re-walks the ENTIRE library on every call (~2 min for a few
# thousand items) even when almost nothing changed.  :func:`synced_fetch_all` is
# an ADDITIVE wrapper that keeps a local disk cache (the full item list + a
# ``last_synced_version`` watermark) and, on each call, asks the server only for
# what changed since that watermark — the documented Web-API delta protocol:
#
#   1. Fast no-op: the discovery request carries ``If-Modified-Since-Version``;
#      a ``304 Not Modified`` means nothing changed library-wide → serve the
#      cached list with ZERO further calls.
#   2. Changed-key discovery: ``items[/top]?since=<v>&format=versions&
#      includeTrashed=1`` returns ``{itemKey: version}`` for only the changed
#      items (``format=versions`` has no default/max limit, so the whole delta
#      arrives in one object; a ``Total-Results`` that ever exceeds it is treated
#      as a partial read and fails closed).
#   3. Full data for changed keys: ``items?itemKey=k1,...`` in batches of ≤50.
#   4. Deletions: ``deleted?since=<v>`` — purged keys removed from the cache.
#
# The merged result is a DROP-IN for ``fetch_all``'s live-item set: a change
# that TRASHES an item is only visible with ``includeTrashed=1`` (the plain
# listing hides it), so the cache holds a superset and trashed items are filtered
# OUT of the returned list — exactly what a plain ``fetch_all`` would omit.
#
# FAIL CLOSED, matching ``fetch_all``/``get_item_children``: if any step of the
# delta path is unexpected/unparseable/incomplete (missing version header,
# non-dict/non-list response, a changed key neither fetched nor deleted, a
# ``Total-Results`` mismatch), we do NOT serve a half-merged cache — we fall back
# to a full :func:`fetch_all` from scratch (which itself raises
# :class:`LibraryUnavailableError` on a degraded read).  A stale-and-wrong merge
# is never returned as if it were current.
#
# ``fetch_all`` itself is UNTOUCHED — existing callers (``library_doi_index``,
# ``library_index``, ``export_backup``) keep their exact behaviour.

# Disk caches for the delta sync — gitignored ``data/`` (same convention as the
# DOI/library-index caches above).  Kept per SCOPE (top-level vs all items),
# because the two are different item sets and must not share a watermark.
_SYNC_CACHE_TOP = Path(__file__).parent.parent / "data" / "zotero_sync_cache_top.json"
_SYNC_CACHE_ALL = Path(__file__).parent.parent / "data" / "zotero_sync_cache_all.json"


def _default_sync_cache_path(top: bool) -> Path:
    """The default gitignored cache file for the requested item scope."""
    return _SYNC_CACHE_TOP if top else _SYNC_CACHE_ALL


def _atomic_write_json(path: Path, payload: Any, *, default=str) -> None:
    """Serialise ``payload`` to ``path`` as JSON via a temp-file atomic replace.

    The SINGLE owner of the "mkdir parent → write ``<path>.tmp`` → ``os``-atomic
    ``replace``" pattern shared by the disk caches and the pre-write backup
    (previously hand-rolled in four places).  ``default=str`` mirrors the backup
    export's tolerant serialisation.  This primitive RAISES on any IO/serialise
    failure — best-effort callers (the caches) wrap it in ``try/except`` and
    swallow, while the pre-write backup lets it propagate so a failed backup
    aborts the apply.  Never writes an API key (the caller controls the payload).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, default=default)
    tmp.replace(path)


def has_sync_cache(top: bool = True) -> bool:
    """True only if a USABLE warm delta-sync cache exists for the given scope.

    Routes through the SAME validation :func:`synced_fetch_all` uses
    (:func:`_load_sync_cache`) rather than a bare ``path.exists()``: a corrupt,
    stale, scope-mismatched, or wrong-library cache file exists on disk but is
    NOT usable — ``synced_fetch_all`` would silently fall back to a slow cold
    :func:`fetch_all` for it.  Reporting such a file as "warm" would suppress the
    cold-fetch budget hint exactly when the slow path is about to run (the bug the
    hint exists to prevent, reached through a different door).  So the answer
    tracks what the fetch will actually do.  Never raises — an unexpected probe
    error degrades to "not warm" (eligible for the hint), the fail-safe direction.
    """
    try:
        return _load_sync_cache(_default_sync_cache_path(top), top=top) is not None
    except Exception:  # noqa: BLE001 — a warm-cache probe must never abort a caller
        return False


def _is_trashed(item: Any) -> bool:
    """True if a Zotero item is in the trash (``data.deleted`` truthy).

    A plain (non-``includeTrashed``) listing omits these, so the delta merge
    filters them out of the returned list to mirror :func:`fetch_all`.
    """
    data = item.get("data") if isinstance(item, dict) else None
    return bool(data.get("deleted")) if isinstance(data, dict) else False


def _live_items(items: list[dict]) -> list[dict]:
    """The non-trashed subset — the set a plain :func:`fetch_all` would return."""
    return [it for it in items if not _is_trashed(it)]


def _read_library_version(*, top: bool = True) -> Optional[int]:
    """Current library version via a cheap ``limit=1`` GET (``Last-Modified-Version``).

    Delegates to the shared :func:`_probe_items` probe (same request
    :func:`count_items` issues).  Used to seed/advance the sync watermark.
    Returns the int version, or ``None`` if the header is absent/unparseable (the
    caller then declines to persist a watermark rather than guess one).
    """
    return _probe_items(top=top)[1]


def _cache_library_identity() -> tuple[Optional[str], Optional[str]]:
    """The ``(library_type, library_id)`` a cache is bound to, from the env config.

    A delta-sync cache belongs to exactly ONE Zotero library.  Capturing the
    configured identity lets :func:`_load_sync_cache` refuse a cache whose library
    changed between runs (switched group, or a multi-user setup) rather than
    merge one library's items with another's delta.  Returns ``(None, None)`` when
    credentials are unconfigured — a cache written with no identity is treated as
    unverifiable and rejected on load (fail closed).
    """
    cfg = zotero_config()
    return cfg.get("library_type"), cfg.get("library_id")


def _load_sync_cache(path: Path, *, top: bool) -> Optional[dict]:
    """Load a delta-sync cache file, or ``None`` if absent/corrupt/mismatched.

    Returns ``{"version": int, "items": list}`` only when the file is a well-
    formed cache whose ``top`` scope AND library identity match the current
    request/config; anything else (missing file, unparseable JSON, non-int
    version, non-list items, wrong scope, different or absent library binding)
    yields ``None`` so the caller does a cold full resync rather than trust a bad
    or cross-library cache.
    """
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            cached = json.load(fh)
    except Exception:  # noqa: BLE001 — corrupt/unreadable cache → cold resync
        return None
    if not isinstance(cached, dict):
        return None
    items = cached.get("items")
    if not isinstance(items, list):
        return None
    try:
        version = int(cached.get("version"))
    except (TypeError, ValueError):
        return None
    if bool(cached.get("top", True)) != bool(top):
        return None  # cache is for the other item scope — do not cross-use it
    # Library-identity binding: a cache tagged for a DIFFERENT library (or a
    # legacy/untagged cache we cannot confirm belongs to the current one) must not
    # have its items merged with the current library's delta — that would corrupt
    # the audit a human reviews before any --apply.  Fail closed (full resync
    # re-tags it), exactly like the scope mismatch above.
    want_type, want_id = _cache_library_identity()
    if cached.get("library_type") != want_type or cached.get("library_id") != want_id:
        return None
    return {"version": version, "items": items}


def _write_sync_cache(path: Path, *, version: int, top: bool, items: list[dict]) -> None:
    """Persist the merged item list + watermark + library binding (best-effort).

    A write failure never raises — the caller has already computed a correct
    result; a missing cache just means the next call re-syncs from scratch. The
    library identity is stamped so a later run under a DIFFERENT configured
    library rejects this cache instead of merging across libraries. The API key
    is never part of the payload.
    """
    lib_type, lib_id = _cache_library_identity()
    payload = {
        "version": int(version),
        "top": bool(top),
        "library_type": lib_type,
        "library_id": lib_id,
        "fetched_at": time.time(),
        "items": items,
    }
    try:
        _atomic_write_json(path, payload)
    except Exception:  # noqa: BLE001 — cache write is best-effort; never break the read
        pass


def _sync_discovery(last_version: int, *, top: bool):
    """Changed-key discovery + fast no-op check in ONE conditional request.

    Returns ``(versions_dict, headers)`` where ``versions_dict`` maps changed
    ``itemKey -> version``, or ``(None, {})`` on a ``304 Not Modified`` (nothing
    changed since ``last_version``).  Any other HTTP error propagates so the
    caller fails closed to a full resync.
    """
    params = {
        "since": str(int(last_version)),
        "format": "versions",
        "includeTrashed": "1",
    }
    req = build_request(
        "items/top" if top else "items",
        params,
        extra_headers={"If-Modified-Since-Version": str(int(last_version))},
    )
    try:
        body, headers = _urlopen_with_headers_retrying(req, timeout=15.0, retries=1)
    except urllib.error.HTTPError as exc:
        if getattr(exc, "code", None) == 304:
            return None, {}  # Not Modified — zero further work
        raise
    return json.loads(body.decode("utf-8")), headers


def _fetch_items_by_keys(
    keys: list[str],
    *,
    batch_size: int = 50,
    deadline_start: Optional[float] = None,
    budget: Optional[float] = None,
) -> list[dict]:
    """Full item data for ``keys`` (``items?itemKey=...``), batched at ≤50 keys.

    ``includeTrashed=1`` so a changed key that was TRASHED still returns its data
    (the merge then filters it from the live view).  A non-list page is a
    degraded read and raises :class:`LibraryUnavailableError` (the caller catches
    it and falls back to a full resync).

    Bounded by the SAME overall wall-clock budget :func:`fetch_all` enforces
    (``ZOTERO_WORD_CITE_ZOTERO_FETCH_BUDGET``, checked BETWEEN batches, not just per
    socket): a large pending delta (a bulk import, or a long-neglected cache) can
    span many ``itemKey`` batches, and with only the per-request 15 s timeout it
    would otherwise run unbounded — none of the protection ``fetch_all`` gives.
    On exceeding the budget it raises :class:`LibraryUnavailableError` (the same
    degraded-read signal), which :func:`synced_fetch_all` catches and turns into a
    safe full resync.  ``deadline_start``/``budget`` default to "now" / the env
    budget so a direct caller is bounded too; the delta path threads its own.
    """
    if budget is None:
        budget = _fetch_budget()
    if deadline_start is None:
        deadline_start = time.time()
    out: list[dict] = []
    for i in range(0, len(keys), batch_size):
        # Overall deadline check BETWEEN batches — bound the total delta fetch,
        # not just each socket read (mirrors ``fetch_all``'s between-page check).
        if time.time() - deadline_start > budget:
            raise LibraryUnavailableError(
                f"Zotero delta fetch exceeded its {budget:.0f}s budget "
                f"(ZOTERO_WORD_CITE_ZOTERO_FETCH_BUDGET) after {len(out)} item(s); "
                "treating as a degraded read (full resync / fail-closed)."
            )
        chunk = keys[i:i + batch_size]
        params: dict[str, Any] = {
            "itemKey": ",".join(chunk),
            "format": "json",
            "includeTrashed": "1",
            "limit": "100",  # chunk ≤ 50 ≤ 100 → one page returns every match
        }
        data, _headers = _get_json_headers(build_request("items", params))
        if not isinstance(data, list):
            raise LibraryUnavailableError(
                "Zotero returned a non-list itemKey response during delta sync; "
                "treating as a degraded read (fail-closed)."
            )
        out.extend(data)
        _progress(f"zotero: delta-fetched {len(out)}/{len(keys)} changed item(s)…")
    return out


def _fetch_deleted_item_keys(since: int) -> list[str]:
    """Item keys purged since ``since`` (``deleted?since=<v>`` → its ``items`` list).

    A non-dict response is a degraded read and raises
    :class:`LibraryUnavailableError`; a well-formed response with no ``items``
    key yields ``[]`` (nothing purged).
    """
    data, _headers = _get_json_headers(build_request("deleted", {"since": str(int(since))}))
    if not isinstance(data, dict):
        raise LibraryUnavailableError(
            "Zotero returned a non-dict deleted response during delta sync; "
            "treating as a degraded read (fail-closed)."
        )
    keys = data.get("items")
    return [k for k in keys if isinstance(k, str)] if isinstance(keys, list) else []


def _full_resync(*, top: bool, cache_path: Path) -> list[dict]:
    """A cold, from-scratch :func:`fetch_all` that (re)seeds the sync cache.

    Reads the library version BEFORE the (multi-minute) full fetch so the
    persisted watermark is never AHEAD of the snapshot: if items change during
    the fetch, the next delta re-fetches them (idempotent replace) rather than
    skipping them.  ``fetch_all`` stays fail-closed — it raises
    :class:`LibraryUnavailableError` on a degraded read, so no cache is written
    for a partial library.  If the version cannot be read, the correct full list
    is still returned but NOT cached (the next call simply resyncs).
    """
    version: Optional[int] = None
    try:
        version = _read_library_version(top=top)
    except Exception:  # noqa: BLE001 — watermark read is best-effort
        version = None
    items = fetch_all(top=top)  # fail-closed on a degraded read
    if version is not None:
        _write_sync_cache(cache_path, version=version, top=top, items=items)
    return items


def synced_fetch_all(
    *, top: bool = True, cache_path: Optional[str | Path] = None, fresh: bool = False
) -> list[dict]:
    """Whole-library read, refreshed INCREMENTALLY against a local disk cache.

    A drop-in for :func:`fetch_all` (returns the same live-item list) that avoids
    re-walking the entire library when little changed:

    * **No cache** → a full :func:`fetch_all`, persisting the item list + a
      ``last_synced_version`` watermark.
    * **Cache present** → try the ``If-Modified-Since-Version`` fast path; a 304
      returns the cached list with zero further calls.  Otherwise fetch only the
      changed items (batched) and the purged keys, MERGE onto the cached list
      (replace/add by key, drop deleted), persist, and return.

    Fails CLOSED on any anomaly in the delta path (see the module-section note):
    it falls back to a full :func:`fetch_all` rather than ever serving a
    stale-and-wrong merged cache.  ``cache_path`` overrides the default gitignored
    location (used by tests for isolation); ``top`` selects the item scope exactly
    like :func:`fetch_all``.  ``fresh=True`` skips the cache read and forces a cold
    full resync that ALSO re-seeds the cache at the fresh watermark (so a repair
    read leaves a fresh cache behind, not the stale one it was run to fix).  The
    API key is never written to the cache or a log.
    """
    cache = Path(cache_path) if cache_path else _default_sync_cache_path(top)
    if fresh:
        # Forced cold read + reseed — ignore whatever is on disk (it may be the
        # very cache the caller is repairing) and rewrite it at the fresh version.
        return _full_resync(top=top, cache_path=cache)
    cached = _load_sync_cache(cache, top=top)
    if cached is None:
        return _full_resync(top=top, cache_path=cache)

    last_version = cached["version"]
    cached_items = cached["items"]

    # 1) Fast no-op + changed-key discovery in one conditional request.
    try:
        versions, disc_headers = _sync_discovery(last_version, top=top)
    except Exception:  # noqa: BLE001 — any discovery failure → safe full resync
        return _full_resync(top=top, cache_path=cache)

    if versions is None:
        # 304 Not Modified — nothing changed library-wide since the watermark.
        return _live_items(cached_items)
    if not isinstance(versions, dict):
        return _full_resync(top=top, cache_path=cache)

    # The new watermark comes from the discovery response (the version as of the
    # delta window).  Without it we cannot advance safely → full resync.
    new_version = _version_from_headers(disc_headers, fallback=None)
    if new_version is None:
        return _full_resync(top=top, cache_path=cache)

    # Defensive completeness of the versions object: format=versions carries the
    # whole delta (no default/max limit), but if a Total-Results header ever
    # reports more keys than we received, the delta is partial — fail closed.
    raw_total = disc_headers.get("Total-Results") if isinstance(disc_headers, dict) else None
    if raw_total is not None:
        try:
            if int(raw_total) > len(versions):
                return _full_resync(top=top, cache_path=cache)
        except (TypeError, ValueError):
            return _full_resync(top=top, cache_path=cache)

    changed_keys = list(versions.keys())

    # 2) Full data for changed keys + 3) purged keys.  The batch loop shares ONE
    # overall wall-clock budget (the same one ``fetch_all`` uses) so a large
    # pending delta cannot run unbounded — on exceeding it the loop raises and we
    # fall back to a full resync (itself budget-bounded / fail-closed).
    delta_deadline = time.time()
    delta_budget = _fetch_budget()
    try:
        fetched = _fetch_items_by_keys(
            changed_keys, deadline_start=delta_deadline, budget=delta_budget
        ) if changed_keys else []
        deleted = _fetch_deleted_item_keys(last_version)
    except Exception:  # noqa: BLE001 — any delta-fetch failure → safe full resync
        return _full_resync(top=top, cache_path=cache)

    fetched_by_key: dict[str, dict] = {}
    for it in fetched:
        k = _item_key_of(it)
        if k:
            fetched_by_key[k] = it
    deleted_set = set(deleted)

    # Completeness: every reported-changed key must be accounted for — either its
    # full data was fetched, or it was purged.  A key in NEITHER means the delta
    # is incomplete; never serve a half-merged cache — fail closed to a resync.
    for k in changed_keys:
        if k not in fetched_by_key and k not in deleted_set:
            return _full_resync(top=top, cache_path=cache)

    # 4) Merge onto the cached list: replace/add changed, drop purged.
    merged: dict[str, dict] = {}
    for it in cached_items:
        k = _item_key_of(it)
        if not k:
            # A cached item with no recoverable key is a corrupt cache. Silently
            # dropping it would PERMANENTLY purge it from the rewritten cache
            # (unlike every OTHER anomaly here, which fails closed). Fail closed
            # to a full resync instead, matching the rest of this function.
            return _full_resync(top=top, cache_path=cache)
        merged[k] = it
    merged.update(fetched_by_key)
    for k in deleted_set:
        merged.pop(k, None)

    merged_items = list(merged.values())
    _write_sync_cache(cache, version=new_version, top=top, items=merged_items)
    return _live_items(merged_items)


def search_items(query: str, qmode: Optional[str] = None) -> list[dict]:
    """Quick-search the library; return the raw list of Zotero item objects.

    ``qmode`` is Zotero's quick-search mode; pass ``"everything"`` to search all
    fields (including DOI/extra), not just title/creator/year (the default).
    """
    params = {"q": query, "format": "json"}
    if qmode:
        params["qmode"] = qmode
    req = build_request("items", params)
    result = _get_json(req)
    return result if isinstance(result, list) else []


def _doi_matches(item: dict, target: str) -> bool:
    data = item.get("data", {}) if isinstance(item, dict) else {}
    return (data.get("DOI") or data.get("doi") or "").strip().lower() == target


def get_item(key: str) -> Optional[dict]:
    """Fetch a single item by ``key``; ``None`` if it no longer exists (HTTP 404).
    Used to detect broken citation links against the group library."""
    try:
        return _get_json(build_request(f"items/{key}", {"format": "json"}))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def item_exists(key: str) -> bool:
    return get_item(key) is not None


def get_item_by_doi(
    doi: str, *, doi_index: Optional[dict[str, str]] = None
) -> Optional[dict]:
    """Find the item whose DOI matches ``doi`` (case-insensitive).

    Zotero's quick-search does NOT index the DOI field, so we (1) try a cheap
    quick-search (catches a DOI that happens to sit in title/extra), then (2)
    fall back to scanning the whole library — the only reliable way, since the
    Web API has no server-side DOI filter. The fallback is O(library size).

    ``doi_index`` — an already-built ``{normalised_doi: item_key}`` map (from
    :func:`library_doi_index`). When supplied it is the AUTHORITATIVE answer for
    presence/absence: a hit returns a minimal ``{"key": ...}`` stub (the caller
    only needs the key) and a MISS short-circuits to ``None`` WITHOUT the
    full-library ``fetch_all`` scan. This is what lets a batch dedup against the
    library in O(1) per item instead of O(library size) per item (the F3
    O(N·M) blow-up). Without ``doi_index`` the legacy scan is used.
    """
    target = (doi or "").strip().lower()
    if not target:
        return None
    if doi_index is not None:
        # Authoritative: the index already enumerated the whole library once.
        norm = citecheck._normalise_doi(doi) or target
        key = doi_index.get(norm)
        return {"key": key} if key else None
    for item in search_items(doi, qmode="everything"):
        if _doi_matches(item, target):
            return item
    for item in fetch_all():           # authoritative fallback
        if _doi_matches(item, target):
            return item
    return None


def resolve_doi_item(doi: str, *, refresh_on_miss: bool = True) -> Optional[dict]:
    """Resolve a single DOI to its item (a minimal ``{"key": ...}`` stub) via the CACHED
    ``{doi: key}`` index — O(1), NOT :func:`get_item_by_doi`'s full-library ``fetch_all``
    scan (which hung ~25 s on a 3,259-item group library). THE single owner for INTERACTIVE
    single-DOI lookups (``zotero --doi``, ``cite-into --doi``); the BATCH dedup path passes
    its own pre-built ``doi_index`` to :func:`get_item_by_doi` directly.

    ``refresh_on_miss`` (default ``True``): on a cached-index MISS, refresh the index ONCE
    (a full re-fetch) so a JUST-ADDED item is still found before giving up — the safe default
    for interactive/cite flows. Set ``False`` for **cache-only** membership (the ``--offline``
    path): a miss returns ``None`` WITHOUT the full refresh, reading only the on-disk DOI
    index (``_load_doi_cache_only``, ignoring TTL) and never touching the network — so a warm
    cache (e.g. right after ``library-coverage``) makes single-DOI membership instant ACROSS
    process boundaries. Cache-only may be stale (a genuinely just-added item can read as
    absent); that is the explicit offline trade-off, matching ``library-coverage --offline``."""
    if not refresh_on_miss:
        # Cache-only: the on-disk DOI index, ignoring TTL, never a network fetch.
        return get_item_by_doi(doi, doi_index=_load_doi_cache_only() or {})
    item = get_item_by_doi(doi, doi_index=library_doi_index())
    if item is None:
        item = get_item_by_doi(doi, doi_index=library_doi_index(refresh=True))
    return item


# Path to the DOI index cache, relative to this file's package root.
_DOI_INDEX_CACHE = Path(__file__).parent.parent / "data" / "zotero_doi_index.json"

# Module-level in-memory cache: (index_dict, fetched_at_unix_seconds)
_doi_index_mem: Optional[tuple[dict, float]] = None

# Combined (doi+pmid+title) index cache — independent of the legacy DOI-only
# cache above so library_doi_index keeps its exact on-disk format/back-compat.
_LIB_INDEX_CACHE = Path(__file__).parent.parent / "data" / "zotero_library_index.json"

# Module-level in-memory cache: (index_dict, fetched_at_unix_seconds), where
# index_dict is {"doi": {...}, "pmid": {...}, "title": {...}}.
_lib_index_mem: Optional[tuple[dict, float]] = None

# Status of the MOST RECENT ``library_index`` call (mirrors the ``*_status``
# pattern). Set as a side effect of every ``library_index`` invocation so
# ``library_index_status`` can report degradation WITHOUT re-fetching — and so it
# observes the result of a monkeypatched ``library_index`` in tests/callers that
# patch that name. ``doi_only`` ⊆ ``degraded``: True only on a strict=False
# DOI-cache fallback after a failed combined read.
_last_index_status: dict = {"degraded": False, "doi_only": False}


def _extract_doi_from_item(item: dict) -> Optional[str]:
    """Pull a DOI from a Zotero item's ``data`` block (checks ``DOI`` then ``extra``)."""
    data = item.get("data", {}) if isinstance(item, dict) else {}
    doi = (data.get("DOI") or data.get("doi") or "").strip()
    if doi:
        return doi
    # DOI may appear in the ``extra`` field as "DOI: 10.xxxx/..."
    extra = (data.get("extra") or "").strip()
    for line in extra.splitlines():
        m = re.match(r"^\s*DOI:\s*(.+)", line, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


# A PMID is a bare integer (Zotero has no native PMID field, so it lives in
# ``extra`` as "PMID: NNN" — or occasionally a ``PMID``/``pmid`` data key).
_PMID_EXTRA_RE = re.compile(r"^\s*PMID:\s*(\d+)", re.IGNORECASE)


def _extract_pmid_from_item(item: dict) -> Optional[str]:
    """Pull a PMID from a Zotero item's ``data`` block.

    Checks a ``PMID``/``pmid`` data key first (rare; some imports set one), then
    parses the ``extra`` field for a ``PMID: NNN`` line (the canonical Zotero
    home for a PMID). Returns the bare digits or ``None``.
    """
    data = item.get("data", {}) if isinstance(item, dict) else {}
    raw = (str(data.get("PMID") or data.get("pmid") or "")).strip()
    if raw:
        m = re.search(r"\d+", raw)
        if m:
            return m.group(0)
    extra = (data.get("extra") or "").strip()
    for line in extra.splitlines():
        m = _PMID_EXTRA_RE.match(line)
        if m:
            return m.group(1)
    return None


def library_doi_index(*, refresh: bool = False, max_age_hours: float = 24.0) -> dict[str, str]:
    """Return a ``{normalized_doi: item_key}`` map for the whole configured library.

    The index is built by fetching all library items once (paginated via
    :func:`fetch_all`) and extracting each item's DOI from ``data.DOI`` or
    the ``extra`` field.  DOIs are normalized with
    :func:`citecheck._normalise_doi` (lowercase, ``https://doi.org/`` stripped).

    **Caching** — two layers:

    1. In-memory: a module-level tuple ``(index, fetched_at)`` is reused
       within the same process if younger than ``max_age_hours``.
    2. On-disk: ``data/zotero_doi_index.json`` (``data/`` is gitignored).
       Written after every successful fetch; reused on subsequent process
       starts as long as the ``fetched_at`` timestamp is within TTL.

    ``refresh=True`` bypasses both caches and forces a full re-fetch.

    **Failure vs. empty** — a successful fetch that returns no items yields an
    empty ``{}`` (the library really is empty; callers may safely create). A
    fetch *failure* (401/403/429/5xx/timeout, missing credentials) falls back to
    an existing disk cache if one is present; if no usable cache exists it
    **raises** :class:`LibraryUnavailableError` rather than returning ``{}`` —
    so a degraded read can never be mistaken for an empty library and silently
    trigger duplicate writes to the shared group. Read-only callers that want to
    degrade gracefully should wrap this in ``try/except Exception`` (and may
    ``or {}``); write callers MUST let it propagate and refuse to create.

    The API key is never written to the cache file or any log.
    """
    global _doi_index_mem

    now = time.time()
    ttl_seconds = max_age_hours * 3600.0

    # ---- in-memory cache check ----
    if not refresh and _doi_index_mem is not None:
        idx, fetched_at = _doi_index_mem
        if now - fetched_at < ttl_seconds:
            return idx

    # ---- disk cache check ----
    if not refresh and _DOI_INDEX_CACHE.exists():
        try:
            with _DOI_INDEX_CACHE.open("r", encoding="utf-8") as fh:
                cached = json.load(fh)
            fetched_at = float(cached.get("fetched_at", 0))
            if now - fetched_at < ttl_seconds:
                idx = cached.get("index", {})
                _doi_index_mem = (idx, fetched_at)
                return idx
        except Exception:  # noqa: BLE001 — corrupt cache; fall through to fetch
            pass

    # ---- fetch from Zotero ----
    try:
        items = fetch_all()
        idx: dict[str, str] = {}
        for item in items:
            key = (item.get("key") or (item.get("data") or {}).get("key") or "").strip()
            if not key:
                continue
            doi_raw = _extract_doi_from_item(item)
            if not doi_raw:
                continue
            norm = citecheck._normalise_doi(doi_raw)
            if norm:
                idx[norm] = key

        fetched_at = now
        _doi_index_mem = (idx, fetched_at)

        # Write disk cache (best-effort; never raise) via the shared atomic writer.
        try:
            _atomic_write_json(_DOI_INDEX_CACHE, {"fetched_at": fetched_at, "index": idx})
        except Exception:  # noqa: BLE001
            pass

        return idx

    except Exception as exc:  # noqa: BLE001 — network error or missing credentials
        # Fall back to a usable disk cache if one is present; otherwise this is a
        # DEGRADED READ, not an empty library — raise so write paths fail closed.
        if _DOI_INDEX_CACHE.exists():
            try:
                with _DOI_INDEX_CACHE.open("r", encoding="utf-8") as fh:
                    cached = json.load(fh)
                idx = cached.get("index")
                if isinstance(idx, dict):
                    return idx
            except Exception:  # noqa: BLE001 — corrupt/unreadable cache: no fallback
                pass
        raise LibraryUnavailableError(
            "Could not load the Zotero library DOI index (read failed and no "
            "usable cache). Refusing to treat this as an empty library."
        ) from exc


def _build_indexes_from_items(items: list[dict]) -> dict[str, dict[str, str]]:
    """Build {"doi","pmid","title"} maps from a single list of library items.

    One pass over the items the caller already fetched: DOI via
    :func:`_extract_doi_from_item`, PMID via :func:`_extract_pmid_from_item`,
    title via :func:`_normalize_title` of ``data.title``. Each maps the
    identifier/normalized-title to the item key.
    """
    doi_idx: dict[str, str] = {}
    pmid_idx: dict[str, str] = {}
    title_idx: dict[str, str] = {}
    for item in items:
        key = (item.get("key") or (item.get("data") or {}).get("key") or "").strip()
        if not key:
            continue
        doi_raw = _extract_doi_from_item(item)
        if doi_raw:
            norm = citecheck._normalise_doi(doi_raw)
            if norm:
                doi_idx[norm] = key
        pmid = _extract_pmid_from_item(item)
        if pmid:
            pmid_idx[pmid] = key
        data = item.get("data", {}) if isinstance(item, dict) else {}
        nt = _normalize_title(data.get("title") or "")
        if nt:
            # First write wins so a present ref maps to a stable key; collisions
            # across duplicate titles are unlikely and either key is "present".
            title_idx.setdefault(nt, key)
    return {"doi": doi_idx, "pmid": pmid_idx, "title": title_idx}


def _load_doi_cache_only() -> Optional[dict[str, str]]:
    """Best-effort load of the ``library_doi_index`` disk cache, ignoring TTL.

    Returns the ``{normalized_doi: item_key}`` map from
    ``data/zotero_doi_index.json`` if it is present and parseable, else ``None``.
    Used by :func:`library_index` (``strict=False``) to degrade a failed combined
    fetch to DOI-only coverage rather than empty — the DOI cache survives outages
    that leave the combined ``library_index`` cache cold. NEVER raises.
    """
    if not _DOI_INDEX_CACHE.exists():
        return None
    try:
        with _DOI_INDEX_CACHE.open("r", encoding="utf-8") as fh:
            cached = json.load(fh)
        idx = cached.get("index")
        if isinstance(idx, dict):
            return idx
    except Exception:  # noqa: BLE001 — corrupt/unreadable cache: nothing to serve
        pass
    return None


def library_index(
    *, refresh: bool = False, max_age_hours: float = 24.0, strict: bool = True
) -> dict[str, dict[str, str]]:
    """Return ``{"doi": {...}, "pmid": {...}, "title": {...}}`` for the whole library.

    A single :func:`fetch_all` pass builds all three identifier indexes
    (normalised-DOI→key, PMID→key, normalised-title→key) so a caller can decide
    presence by DOI → PMID → title without three separate fetches.

    Caching mirrors :func:`library_doi_index` — a two-layer cache (in-memory
    tuple + the gitignored disk file ``data/zotero_library_index.json``),
    ``refresh=True`` bypasses both, and an empty library returns three empty maps.

    **Failure semantics depend on ``strict`` (the WRITE-vs-READ split):**

    * ``strict=True`` (default — the WRITE/fail-closed path, e.g.
      :func:`unify.apply_unification`): on a fetch failure with no fresh combined
      cache, RAISE :class:`LibraryUnavailableError`. A stale DOI-only fallback is
      DELIBERATELY refused — a stale cache could miss recently-added items and
      cause duplicate creation in the shared group. This is the historical
      behaviour and MUST NOT change.
    * ``strict=False`` (the READ/coverage path, e.g.
      :func:`unify.plan_unification`, :mod:`citelink` coverage): on a fetch
      failure with no fresh combined cache, FALL BACK to the ``library_doi_index``
      disk cache and return a DEGRADED, DOI-only index
      ``{"doi": <that map>, "pmid": {}, "title": {}}`` — far better than empty
      (empty would flip every reference to "missing"). Only raises if NEITHER
      cache is loadable. Use :func:`library_index_status` to detect and label the
      degraded mode in a coverage summary.

    As a side effect, records the degraded/doi_only flags for the most recent
    call in the module-level ``_last_index_status`` so :func:`library_index_status`
    can report them without a second fetch. The API key is never written to the
    cache file or any log.
    """
    global _lib_index_mem, _last_index_status

    # Default the recorded status to clean; overwritten only on the DOI-cache
    # degrade branch. (A raise leaves it clean — there is no served index.)
    _last_index_status = {"degraded": False, "doi_only": False}

    now = time.time()
    ttl_seconds = max_age_hours * 3600.0

    # ---- in-memory cache check ----
    if not refresh and _lib_index_mem is not None:
        idx, fetched_at = _lib_index_mem
        if now - fetched_at < ttl_seconds:
            return idx

    # ---- disk cache check ----
    if not refresh and _LIB_INDEX_CACHE.exists():
        try:
            with _LIB_INDEX_CACHE.open("r", encoding="utf-8") as fh:
                cached = json.load(fh)
            fetched_at = float(cached.get("fetched_at", 0))
            if now - fetched_at < ttl_seconds:
                idx = cached.get("index", {})
                if isinstance(idx, dict) and "doi" in idx:
                    idx = {
                        "doi": idx.get("doi") or {},
                        "pmid": idx.get("pmid") or {},
                        "title": idx.get("title") or {},
                    }
                    _lib_index_mem = (idx, fetched_at)
                    return idx
        except Exception:  # noqa: BLE001 — corrupt cache; fall through to fetch
            pass

    # ---- fetch from Zotero ----
    try:
        items = fetch_all()
        idx = _build_indexes_from_items(items)

        fetched_at = now
        _lib_index_mem = (idx, fetched_at)

        # Write disk cache (best-effort; never raise) via the shared atomic writer.
        try:
            _atomic_write_json(_LIB_INDEX_CACHE, {"fetched_at": fetched_at, "index": idx})
        except Exception:  # noqa: BLE001
            pass

        return idx

    except Exception as exc:  # noqa: BLE001 — network error or missing credentials
        # First fallback: the combined disk cache — but ONLY while it is FRESH
        # (within the TTL). "Complete" is not "fresh": a TTL-EXPIRED combined
        # cache predating recently-added group items would classify those items
        # as missing and, on the WRITE path, drive DUPLICATE creation — the very
        # thing strict=True exists to prevent. So enforce the same TTL the normal
        # disk read uses. A FRESH cache here is reached only on refresh=True (the
        # normal read above already returns a fresh cache before any fetch).
        if _LIB_INDEX_CACHE.exists():
            try:
                with _LIB_INDEX_CACHE.open("r", encoding="utf-8") as fh:
                    cached = json.load(fh)
                idx = cached.get("index")
                fetched_at = float(cached.get("fetched_at", 0))
                if isinstance(idx, dict) and "doi" in idx:
                    combined = {
                        "doi": idx.get("doi") or {},
                        "pmid": idx.get("pmid") or {},
                        "title": idx.get("title") or {},
                    }
                    if now - fetched_at < ttl_seconds:
                        # Fresh & complete: trustworthy, NOT degraded.
                        return combined
                    if not strict:
                        # READ/coverage MAY use a stale combined index (richer
                        # than DOI-only) but flags it degraded so consumers
                        # surface a "provisional" note. The WRITE path must NOT —
                        # it falls through below and fails closed.
                        _last_index_status = {"degraded": True, "doi_only": False}
                        return combined
                    # strict + STALE: do not serve; fall through to fail closed.
            except Exception:  # noqa: BLE001 — corrupt/unreadable: keep degrading
                pass

        # Second fallback (READ/coverage path ONLY, strict=False): the
        # library_doi_index disk cache. The combined cache is cold but the DOI
        # cache survived the SAME outage (library_doi_index degrades to it), so
        # serve DOI-only coverage instead of empty. PMID/title are unavailable in
        # this mode. The WRITE path (strict=True) NEVER takes this branch — a
        # stale DOI cache could miss recently-added items and drive duplicate
        # creation, so writes must fail closed.
        if not strict:
            doi_cache = _load_doi_cache_only()
            if doi_cache is not None:
                _last_index_status = {"degraded": True, "doi_only": True}
                return {"doi": doi_cache, "pmid": {}, "title": {}}

        raise LibraryUnavailableError(
            "Could not load the Zotero library index (read failed and no usable "
            "cache). Refusing to treat this as an empty library."
        ) from exc


def library_index_status(
    *, refresh: bool = False, max_age_hours: float = 24.0, strict: bool = True
) -> tuple[dict[str, dict[str, str]], dict]:
    """Like :func:`library_index`, but also report whether the read was DEGRADED.

    Returns ``(index, status)`` where ``status`` is
    ``{"degraded": bool, "doi_only": bool}`` — mirroring the ``*_status`` pattern
    used by :func:`entrez.efetch_pubmed_status`. ``degraded``/``doi_only`` are
    ``True`` only when a live/combined-cache read FAILED and we served the
    ``library_doi_index`` disk cache as a DOI-only fallback (``strict=False``
    only). A normal fresh/cached read reports ``{"degraded": False,
    "doi_only": False}``.

    This is a thin wrapper over :func:`library_index` (it reads the status that
    call records as a side effect), so callers and tests that monkeypatch
    ``library_index`` are observed correctly — a patched non-raising
    ``library_index`` is reported as non-degraded; a patched raising one
    propagates :class:`LibraryUnavailableError` to the caller as before.

    See :func:`library_index` for the ``strict`` contract and failure semantics.
    """
    global _last_index_status
    # Reset before the call so a monkeypatched ``library_index`` (which bypasses
    # the real body's reset) is reported as non-degraded rather than leaking a
    # prior real call's status.
    _last_index_status = {"degraded": False, "doi_only": False}
    idx = library_index(refresh=refresh, max_age_hours=max_age_hours, strict=strict)
    return idx, dict(_last_index_status)


def lookup_index_key(
    lib_index: Optional[dict],
    *,
    doi: Optional[str] = None,
    pmid: Optional[str] = None,
    title: Optional[str] = None,
) -> Optional[str]:
    """Return an existing library item *key* for a ref, or ``None`` if absent.

    THE single owner of "is this ref already in the library, and under what
    key?" — decides presence by DOI → PMID → normalized-title against the three
    maps of a ``{"doi","pmid","title"}`` index built by :func:`library_index`.
    A match on ANY of the three identifiers wins (a DOI-less but title-present
    ref is still PRESENT — the DOI-only test this consolidates false-flagged such
    refs as missing, which under a confident ``--apply`` mass-duplicated the
    shared group library).

    Pure and OFFLINE: it only reads the in-memory ``lib_index`` — no network, no
    ``build_request``, no credential requirement. ``lib_index`` may be ``None``
    or partial (a degraded read degrades to empty/absent sub-maps); a missing
    sub-map simply yields no match for that identifier. Callers
    (:mod:`unify`, :mod:`citeconvert`) MUST route presence checks through here
    rather than re-deciding so the DOI→PMID→title precedence stays single-sourced.
    """
    if not lib_index:
        return None
    doi_idx = lib_index.get("doi") or {}
    pmid_idx = lib_index.get("pmid") or {}
    title_idx = lib_index.get("title") or {}

    if doi:
        norm = citecheck._normalise_doi(doi)
        key = doi_idx.get(norm)
        if key:
            return key
    if pmid:
        key = pmid_idx.get(str(pmid).strip())
        if key:
            return key
    if title:
        nt = _normalize_title(title)
        if nt:
            key = title_idx.get(nt)
            if key:
                return key
    return None


def _item_key_of(item: Any) -> Optional[str]:
    """Best-effort recover the Zotero *itemKey* from an item the API returned.

    Zotero responses identify an item differently per ``format``:

      * ``format=csljson`` carries a CSL ``id`` shaped ``"<libraryID>/<itemKey>"``
        (e.g. ``"2504198/X8ISWWQ2"``); some exports use the bare itemKey.
      * ``format=json`` (used by :func:`formatted_citations`) carries a top-level
        ``"key"`` and/or a nested ``"data"."key"``.

    Returns the itemKey (the part after the last ``/`` of a CSL ``id``), or
    ``None`` if no key can be recovered — callers must treat ``None`` as
    "cannot bind by key" rather than guessing a position.
    """
    if not isinstance(item, dict):
        return None
    # Explicit Zotero key fields win (format=json and full item objects).
    key = item.get("key")
    if isinstance(key, str) and key:
        return key
    data = item.get("data")
    if isinstance(data, dict):
        dkey = data.get("key")
        if isinstance(dkey, str) and dkey:
            return dkey
    # CSL-JSON: id is "<libraryID>/<itemKey>" or, rarely, the bare itemKey.
    cid = item.get("id")
    if isinstance(cid, str) and cid:
        return cid.rsplit("/", 1)[-1]
    if isinstance(cid, int):
        return str(cid)
    return None


def _reorder_by_keys(
    item_keys: list[str], items: list[Any], *, what: str
) -> list[Any]:
    """Reorder ``items`` (an API response in Zotero's natural/library order) to
    match the REQUESTED ``item_keys`` order — binding each requested key to the
    returned item whose own key matches it.

    Zotero serves a multi-``itemKey`` request in the library's natural sort order,
    NOT the order the keys were requested. Trusting response order silently pairs
    requested key *i* with a DIFFERENT work's metadata when the orders diverge —
    the CRIT-3 mis-binding. This function makes the binding explicit by key.

    Items whose own key cannot be recovered (``None``) are used positionally ONLY
    when the ENTIRE response is keyless — the legacy/stub shape where response
    order is the only signal we have. If ANY item's key was recovered, a keyless
    item is an unrelated object (a returned child note/attachment, or a malformed
    ``id``) and is NEVER bound to a requested key: binding it would embed the
    wrong work's metadata into that citation (a CRIT-3 mis-bind). A requested key
    with no keyed match then yields ``None`` at that position (the caller decides
    whether to placeholder or drop it) and a surfaced :class:`UserWarning` — a
    missing item must never shift, nor be impersonated by, the others.
    """
    by_key: dict[str, Any] = {}
    unkeyed: list[Any] = []
    for it in items:
        k = _item_key_of(it)
        if k is not None and k not in by_key:
            by_key[k] = it
        elif k is None:
            unkeyed.append(it)
    ordered: list[Any] = []
    missing: list[str] = []
    # Positional fallback is safe ONLY when no key was recoverable at all. With a
    # partially-keyed response, an unkeyed item is an unrelated object and binding
    # it to a missing requested key would mis-attribute another work's metadata.
    allow_positional = not by_key
    unkeyed_iter = iter(unkeyed)
    for key in item_keys:
        if key in by_key:
            ordered.append(by_key[key])
            continue
        stub = next(unkeyed_iter, None) if allow_positional else None
        if stub is not None:
            ordered.append(stub)
        else:
            ordered.append(None)
            missing.append(key)
    if missing:
        warnings.warn(
            f"Zotero {what} omitted {len(missing)} requested item(s) "
            f"({', '.join(missing)}); their slots are left unbound rather than "
            f"shifting the remaining items.",
            UserWarning,
            stacklevel=2,
        )
    return ordered


def formatted_citations(
    item_keys: list[str], style: str = "vancouver", kind: str = "bib", strip: bool = True
) -> list[str]:
    """Server-side-rendered citations for ``item_keys`` in CSL ``style``.

    Zotero renders each entry using the named CSL style, so a shared group gets
    identical output regardless of who runs it. ``style`` may be any CSL id
    Zotero knows — e.g. ``"vancouver"``, ``"ieee"``, ``"apa"``, ``"nature"``.

    ``kind`` selects what Zotero renders:
        ``"bib"``      full bibliography entry (default — a usable reference)
        ``"citation"`` the in-text citation marker (e.g. "(1)")

    HTML wrappers Zotero adds are stripped. Returns the list in item order.
    """
    if not item_keys:
        return []
    if kind not in ("bib", "citation"):
        raise ValueError("kind must be 'bib' or 'citation'")
    req = build_request(
        "items",
        {
            "itemKey": ",".join(item_keys),
            "include": kind,
            "style": style,
            "format": "json",
        },
    )
    result = _get_json(req)
    items = result if isinstance(result, list) else []
    # Bind each requested key to ITS OWN rendered entry: Zotero returns the list
    # in library order, not request order (see _reorder_by_keys / CRIT-3). A key
    # Zotero omits yields a None slot, which we drop from the rendered output.
    ordered = _reorder_by_keys(item_keys, items, what=f"include={kind}")
    vals = [
        item[kind]
        for item in ordered
        if isinstance(item, dict) and kind in item
    ]
    return [strip_html(v) for v in vals] if strip else vals


def item_uri(key: str) -> str:
    """Canonical Zotero URI for an item in the configured library, e.g.
    ``http://zotero.org/groups/2504198/items/X8ISWWQ2`` — this is what binds an
    inserted citation field to your shared group library."""
    cfg = zotero_config()
    if not cfg:
        raise RuntimeError("Zotero credentials not configured; set env var(s): "
                           + ", ".join(_missing_env()))
    prefix = "groups" if cfg["library_type"] == "group" else "users"
    return f"http://zotero.org/{prefix}/{cfg['library_id']}/items/{key}"


def item_uri_offline_safe(key: str) -> str:
    """Canonical item URI for ``key``, NEVER raising on a degraded read.

    ``item_uri`` needs only the library id/type (no network) but RAISES when
    credentials are entirely unset. On a DEGRADED read (offline match, creds
    missing) we cannot form the canonical URI — return ``""`` rather than crash.
    The written field still carries the matched item KEY, so Word/Zotero rebind
    the full URI/itemData on the first Refresh. SINGLE OWNER of this fallback —
    citeconvert + unify call here instead of each re-deciding (avoids drift)."""
    try:
        return item_uri(key)
    except (RuntimeError, OSError):
        return ""


def csljson(item_keys: list[str]) -> list[dict]:
    """CSL-JSON metadata for ``item_keys`` (Zotero ``format=csljson``).

    This is exactly the ``itemData`` Zotero embeds inside a Word citation field,
    so a refresh in Word renders identically to the desktop app.
    """
    if not item_keys:
        return []
    data = _get_json(build_request("items", {"itemKey": ",".join(item_keys), "format": "csljson"}))
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        items = data["items"]
    elif isinstance(data, list):
        items = data
    else:
        items = []
    # CRITICAL: Zotero returns multi-itemKey responses in the library's natural
    # sort order, NOT request order. Bind each requested key to ITS OWN CSL-JSON
    # by key (see _reorder_by_keys) so a downstream positional zip with
    # keys/uris can never embed a different work's metadata (CRIT-3). For a key
    # Zotero omits, emit a minimal placeholder carrying that key's own CSL ``id``
    # so the slot stays aligned with keys/uris (the URI binding stays correct)
    # without ever shifting — or mis-binding — the items Zotero DID return.
    ordered = _reorder_by_keys(item_keys, items, what="format=csljson")
    return [
        item if item is not None else {"id": _csl_id_for(key)}
        for key, item in zip(item_keys, ordered)
    ]


def _csl_id_for(key: str) -> str:
    """CSL ``id`` (``"<libraryID>/<itemKey>"``) for ``key`` in the configured
    library, matching Zotero's ``format=csljson`` ``id`` shape; falls back to the
    bare ``key`` if credentials are not configured."""
    cfg = zotero_config()
    if cfg and cfg.get("library_id"):
        return f"{cfg['library_id']}/{key}"
    return key


def _creators_to_authors(creators: list[dict]) -> list[str]:
    """Map Zotero ``creators`` to "Family II" author strings (authors only).

    Initials are derived from the first name's tokens. A single-field
    ``name`` creator (organisation/collective) is passed through verbatim.
    """
    authors: list[str] = []
    for creator in creators or []:
        if not isinstance(creator, dict):
            continue
        if creator.get("creatorType") not in (None, "author"):
            continue
        last = (creator.get("lastName") or "").strip()
        first = (creator.get("firstName") or "").strip()
        if last:
            initials = "".join(tok[0].upper() for tok in first.split() if tok)
            authors.append(f"{last} {initials}".strip())
        elif creator.get("name"):
            authors.append(str(creator["name"]).strip())
    return authors


def _year_from_date(date: str) -> Optional[str]:
    """Pull a 4-digit year from a Zotero ``date`` string (e.g. ``"2021-12"``).

    Returns ``None`` when no 4-digit run is present. A non-numeric date such as
    ``"in press"`` / ``"n.d."`` / ``"forthcoming"`` (or a stray short run like
    ``"21"``) must NOT leak as the "year": it would corrupt the rendered
    reference (e.g. ``in press;12(3):1-9``) and the author-year sort key. Matches
    the sibling extractors ``cite._year_from`` / ``zoterolocal._year_from_date``.
    """
    date = (date or "").strip()
    run = ""
    for ch in date:
        if ch.isdigit():
            run += ch
            if len(run) == 4:
                return run
        else:
            run = ""
    return None


def to_reference(item: dict) -> "cite.Reference":
    """Map a Zotero item's ``data`` block to a :class:`cite.Reference`.

    Tolerates items passed either as ``{"data": {...}}`` or as the bare data
    dict. Page field is Zotero's ``pages``; DOI is ``DOI``.
    """
    data = item.get("data", item) if isinstance(item, dict) else {}
    return cite.Reference(
        authors=_creators_to_authors(data.get("creators", [])),
        title=(data.get("title") or "").strip(),
        journal=(data.get("publicationTitle") or data.get("journalAbbreviation") or "").strip(),
        year=_year_from_date(data.get("date", "")),
        volume=(data.get("volume") or "").strip(),
        issue=(data.get("issue") or "").strip(),
        pages=(data.get("pages") or "").strip(),
        doi=(data.get("DOI") or data.get("doi") or "").strip(),
    )


# ---------------------------------------------------------------------------
# Write capability (Zotero Web API v3)
# ---------------------------------------------------------------------------

# CSL type → Zotero itemType mapping (extend as needed)
_CSL_TYPE_MAP: dict[str, str] = {
    "article-journal": "journalArticle",
    "article": "journalArticle",
    "paper-conference": "conferencePaper",
    "book": "book",
    "chapter": "bookSection",
}


def _post_json(
    path: str,
    body: Any,
    *,
    extra_headers: Optional[dict[str, str]] = None,
    timeout: float = 15.0,
) -> Any:
    """POST ``body`` (serialised to JSON) to a library path; return parsed JSON.

    Builds the URL the same way :func:`build_request` does, adds standard auth
    headers, then adds ``extra_headers`` (e.g. ``Zotero-Write-Token``).
    The API key appears only in the ``Zotero-API-Key`` header — never in the URL
    or any exception message.
    """
    cfg = zotero_config()
    if not cfg:
        missing = ", ".join(_missing_env())
        raise RuntimeError(
            f"Zotero credentials not configured; set env var(s): {missing}"
        )

    prefix = "groups" if cfg["library_type"] == "group" else "users"
    url = f"{API_BASE}/{prefix}/{cfg['library_id']}/{path.lstrip('/')}"

    data = json.dumps(body).encode("utf-8")
    headers: dict[str, str] = {
        "Zotero-API-Key": cfg["api_key"],
        "Zotero-API-Version": API_VERSION,
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)

    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    # One bounded retry on a transient 429/5xx.  Safe for writes here: every
    # create POST carries a unique ``Zotero-Write-Token`` (set by the caller),
    # which Zotero treats as an idempotency key — replaying the identical request
    # cannot double-create.  Honours Retry-After/Backoff via the shared helper.
    return json.loads(_urlopen_retrying(req, timeout=timeout).decode("utf-8"))


# Tri-state write-access result.  ``True``/``False`` are definitive answers from
# Zotero; ``WRITE_ACCESS_UNKNOWN`` means we COULD NOT VERIFY (network/parse error)
# — distinct from a verified "no write access".  It is deliberately falsy so the
# legacy ``if not key_can_write_status()`` fail-closed gate still refuses the
# write on an unknown; callers that want a truthful message check identity
# against this sentinel (see ``unify``/``endnote``).
class _WriteAccessUnknown:
    __slots__ = ()

    def __bool__(self) -> bool:  # falsy → fail-closed at any boolean gate
        return False

    def __repr__(self) -> str:
        return "WRITE_ACCESS_UNKNOWN"


WRITE_ACCESS_UNKNOWN = _WriteAccessUnknown()


def key_can_write_status():
    """Tri-state write-access check: ``True`` / ``False`` / ``WRITE_ACCESS_UNKNOWN``.

    GETs ``https://api.zotero.org/keys/<key>`` and inspects
    ``access.groups.all.write`` (or the per-group grant):

    * ``True``  — the key definitively HAS group write access.
    * ``False`` — Zotero answered and the key definitively does NOT (and no
      credentials configured also returns ``False`` — there is nothing to write
      *with*, a definitive no).
    * :data:`WRITE_ACCESS_UNKNOWN` — we could not reach Zotero / parse the answer
      (network error, timeout, 5xx, bad JSON).  This is NOT "no access"; it is
      "could not verify".  It is falsy, so a boolean gate still fails closed and
      refuses the write — but a caller can detect it and tell the user to RETRY
      rather than asserting (wrongly) that the key lacks write access.

    The key value is sent only in the ``Zotero-API-Key`` header; it never
    appears in URLs, exceptions, or return values.
    """
    cfg = zotero_config()
    if not cfg:
        return False  # nothing to write with — a definitive no, not "unknown"

    # Use a direct URL to the /keys/ endpoint (not the library-scoped base)
    url = f"{API_BASE}/keys/{cfg['api_key']}"
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Zotero-API-Key": cfg["api_key"],
            "Zotero-API-Version": API_VERSION,
        },
    )
    try:
        # One bounded retry on a transient 429/5xx (shared helper); a persistent
        # failure (or any other error) means we cannot verify → UNKNOWN.
        data = json.loads(_urlopen_retrying(req, timeout=15.0).decode("utf-8"))
    except Exception:  # noqa: BLE001 — could not verify; never assert "no access"
        return WRITE_ACCESS_UNKNOWN

    if not isinstance(data, dict):
        return WRITE_ACCESS_UNKNOWN

    access = data.get("access", {})
    # Check broad group write first, then per-group
    groups_access = access.get("groups", {})
    if groups_access.get("all", {}).get("write"):
        return True
    cfg_id = cfg.get("library_id", "")
    if groups_access.get(cfg_id, {}).get("write"):
        return True
    return False


def key_can_write() -> bool:
    """Boolean write-access gate (back-compat wrapper over
    :func:`key_can_write_status`).

    Returns ``True`` only on a definitive yes; ``False`` on a definitive no AND
    on an unverifiable result — preserving the original fail-closed contract
    (and the original "``False`` on any error" behaviour callers/tests rely on).
    Callers that need to distinguish "no access" from "could not verify" (to
    emit a *retry* hint instead of a wrong "key lacks write access" message)
    should call :func:`key_can_write_status` and compare against
    :data:`WRITE_ACCESS_UNKNOWN`.
    """
    return key_can_write_status() is True


def csljson_to_zotero_item(
    meta: dict,
    *,
    item_type: str = "journalArticle",
    collections: Optional[list[str]] = None,
    tags: Optional[list[str]] = None,
) -> dict:
    """Map a CSL-JSON-ish metadata dict to a Zotero item object.

    ``meta`` is the shape that ``refresolve``/Crossref produce::

        {
          "doi":     "10.xxxx/...",
          "title":   "...",
          "authors": [{"family": "Smith", "given": "John"}],
          "year":    "2024",
          "journal": "Nature",
          "volume":  "612",
          "issue":   "7",
          "pages":   "1-10",
          "type":    "article-journal",   # CSL type — overrides item_type arg
        }

    The ``type`` key (CSL type) is mapped to a Zotero ``itemType`` via
    :data:`_CSL_TYPE_MAP`; unknown types fall back to ``item_type``.

    Authors are mapped from ``{"family", "given"}`` → Zotero
    ``{"creatorType": "author", "lastName", "firstName"}``.
    ``year`` → ``date``, ``journal`` → ``publicationTitle``, ``doi`` → ``DOI``.

    ``collections`` is a list of Zotero collection keys (not names).
    ``tags`` is a list of tag strings.
    """
    # Resolve item type: CSL "type" field wins if recognised
    csl_type = (meta.get("type") or "").strip()
    zotero_type = _CSL_TYPE_MAP.get(csl_type, item_type)

    # Authors
    creators: list[dict] = []
    for author in meta.get("authors") or []:
        if not isinstance(author, dict):
            continue
        last = (author.get("family") or author.get("lastName") or "").strip()
        first = (author.get("given") or author.get("firstName") or "").strip()
        if last or first:
            creators.append({
                "creatorType": "author",
                "firstName": first,
                "lastName": last,
            })

    item: dict[str, Any] = {
        "itemType": zotero_type,
        "title": (meta.get("title") or "").strip(),
        "creators": creators,
        "date": str(meta["year"]).strip() if meta.get("year") else "",
        "DOI": (meta.get("doi") or "").strip(),
        "publicationTitle": (meta.get("journal") or "").strip(),
        "volume": str(meta.get("volume") or "").strip(),
        "issue": str(meta.get("issue") or "").strip(),
        "pages": (meta.get("pages") or "").strip(),
        "collections": list(collections) if collections else [],
        "tags": [{"tag": t} for t in (tags or [])],
    }
    return item


def ensure_collection(name: str) -> str:
    """Return the collection key for ``name`` in the group library, creating it if absent.

    GETs ``groups/<gid>/collections`` (paginated), matches on name
    (case-insensitive), and returns the key.  If no match, POSTs a new
    collection and returns the new key.

    Raises :class:`RuntimeError` if credentials are not configured.
    """
    name_norm = name.strip()

    # Paginate through all collections
    start = 0
    while True:
        params: dict[str, Any] = {"format": "json", "limit": "100", "start": str(start)}
        data, headers = _get_json_headers(build_request("collections", params))
        if not isinstance(data, list):
            break
        for coll in data:
            cdata = coll.get("data", {}) if isinstance(coll, dict) else {}
            if (cdata.get("name") or "").strip().lower() == name_norm.lower():
                return coll["key"]
        start += len(data)
        if not data or start >= int(headers.get("Total-Results", start)):
            break

    # Not found — create it
    result = _post_json("collections", [{"name": name_norm}])
    # Response: {"successful": {"0": {"key": "...", ...}}, ...}
    successful = result.get("successful", {})
    if successful:
        entry = next(iter(successful.values()))
        return entry["key"]
    failed = result.get("failed", {})
    if failed:
        reason = next(iter(failed.values()), {}).get("message", "unknown error")
        raise RuntimeError(f"Failed to create Zotero collection '{name_norm}': {reason}")
    raise RuntimeError(f"Unexpected response creating collection '{name_norm}': {result!r}")


def _normalize_title(title: str) -> str:
    """Lowercase, strip accents, collapse whitespace — for fuzzy title dedup."""
    nfkd = unicodedata.normalize("NFKD", title or "")
    ascii_approx = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", ascii_approx).strip().lower()


def _title_exists_in_library(title: str) -> Optional[str]:
    """Search library for ``title``; return the item key of a match or ``None``.

    Uses Zotero quick-search, then checks normalized titles to avoid false
    positives from substring matches.
    """
    target = _normalize_title(title)
    if not target:
        return None
    for item in search_items(title):
        data = item.get("data", {}) if isinstance(item, dict) else {}
        if _normalize_title(data.get("title", "")) == target:
            return item.get("key")
    return None


def create_items(
    metas: list[dict],
    *,
    collection: Optional[str] = None,
    tags: Optional[list[str]] = None,
    dedup: bool = True,
    doi_index: Optional[dict[str, str]] = None,
    pmid_index: Optional[dict[str, str]] = None,
    attach_pdfs: bool = False,
    index_degraded: bool = False,
) -> dict:
    """Batch-create Zotero items from CSL-JSON-ish metadata dicts.

    Parameters
    ----------
    metas:
        List of metadata dicts in the shape :func:`csljson_to_zotero_item` accepts.
    collection:
        Collection *name* (not key). Resolved (or created) via
        :func:`ensure_collection` and attached to every created item.
    tags:
        List of tag strings attached to every created item.
    dedup:
        When ``True`` (default), items whose DOI or normalised title already
        exist in the library are skipped (recorded in ``skipped_existing``).
    doi_index:
        Optional pre-built ``{normalised_doi: item_key}`` map (from
        :func:`library_doi_index`). When supplied, the DOI dedup check answers
        from it in O(1) per item instead of a full-library ``fetch_all`` scan
        per item — passed straight to :func:`get_item_by_doi`. Callers that
        already built the index (e.g. ``unify.apply_unification``) SHOULD thread
        it in to avoid the O(N·M) re-scan (F3).
    pmid_index:
        Optional pre-built ``{pmid: item_key}`` map (from
        :func:`library_index`). Defense-in-depth dedup: when a meta carries a
        ``pmid`` that is present in this map, the item is recorded
        ``skipped_existing`` even if its DOI/title did not match — so a ref that
        was false-flagged "missing" upstream (present only by PMID) is never
        duplicated into the shared group.
    attach_pdfs:
        When ``True`` (OPT-IN; default ``False`` — existing behaviour unchanged),
        after the items are created, try to discover an open-access PDF for each
        created item (via :func:`zoterocite.oapdf.find_oa_pdf_url` using its DOI /
        PMID / arXiv id) and attach it through :func:`attach_pdf_to_item`.  The
        attach reuses THIS call's write gate (no second permission check) and is
        best-effort: a fetch/upload failure is recorded under ``pdf_skipped`` and
        never demotes a successful create.
    index_degraded:
        Fail-closed dedup safety (default ``False`` — existing behaviour
        unchanged).  When ``True`` AND ``dedup`` is on, the pre-built index the
        caller passed came from a DEGRADED/unverifiable library read, so an
        ``absent`` verdict cannot be trusted: an item the index reports as absent
        is NOT created — it is routed to ``skipped_degraded_read`` instead of
        being POSTed, so we never risk a duplicate write to the shared group on
        an uncertain read.  Items the index POSITIVELY matches (by DOI/PMID) still
        go to ``skipped_existing``.  Belt-and-suspenders with the caller's own
        degraded-read guard (``unify.apply_unification`` already fails closed on
        the ``LibraryUnavailableError`` raise).

    Returns
    -------
    dict with keys:
        ``"created"``               list of ``{title, key, doi}``
        ``"skipped_existing"``      list of ``{title, existing_key}``
        ``"failed"``                list of ``{title, reason}``
        ``"skipped_degraded_read"`` list of ``{title, reason}`` — items refused
            because the library index was degraded/unverifiable and could not
            confirm the item is absent (only ever non-empty when
            ``index_degraded=True``).

    When ``attach_pdfs=True``, two more keys are present:
        ``"pdf_attached"``     list of ``{key, source_url}``
        ``"pdf_skipped"``      list of ``{key, reason}``

    A single-item failure never raises — it is recorded in ``"failed"``.
    A :data:`False` result from :func:`key_can_write` causes the entire call to
    return with all items in ``"failed"`` (no POST attempted).
    """
    result: dict[str, list] = {
        "created": [], "skipped_existing": [], "failed": [],
        "skipped_degraded_read": [],
    }

    if not metas:
        return result

    # Drop None / non-dict entries: a None metadata can't become an item, so skip
    # it rather than crash on ``meta.get(...)`` downstream (a refresolve miss can
    # leave a None in the list). Defensive — never abort the whole batch over one
    # bad entry.
    metas = [m for m in metas if isinstance(m, dict)]
    if not metas:
        return result

    # Gate on write permission.  Fail closed on BOTH a definitive "no access"
    # and an unverifiable result, but record the truthful reason per item so a
    # caller surfaces "retry — could not verify" instead of asserting no-write.
    _write_status = key_can_write_status()
    if not _write_status:
        if _write_status is WRITE_ACCESS_UNKNOWN:
            _reason = ("could not reach Zotero to verify the API key's write "
                       "access — refused (fail-closed); retry when reachable")
        else:
            _reason = "API key does not have write access to this library"
        for meta in metas:
            result["failed"].append({
                "title": (meta.get("title") or ""),
                "reason": _reason,
            })
        return result

    # Resolve collection key once (if requested)
    coll_key: Optional[str] = None
    if collection:
        try:
            coll_key = ensure_collection(collection)
        except Exception as exc:
            for meta in metas:
                result["failed"].append({
                    "title": (meta.get("title") or ""),
                    "reason": f"Collection error: {exc}",
                })
            return result

    # Split into to-create vs skipped
    to_create: list[dict] = []   # (meta, zotero_item)
    for meta in metas:
        title = (meta.get("title") or "").strip()
        doi = (meta.get("doi") or "").strip()
        pmid = str(meta.get("pmid") or "").strip()

        # The dedup lookups (get_item_by_doi / _title_exists_in_library) hit the
        # network. Wrap them in the SAME per-item guard as the conversion below
        # so a transient read failure (timeout/5xx) mid-dedup degrades THAT item
        # to "failed" rather than raising and aborting the whole batch after
        # earlier items were already written (F3). An item we cannot dedup is
        # never blindly created — fail closed for that one item.
        try:
            if dedup:
                existing_key: Optional[str] = None
                if doi:
                    # Seed/cap with the pre-built index when supplied: O(1) per
                    # item, no full-library fetch_all re-scan per DOI.
                    existing = get_item_by_doi(doi, doi_index=doi_index)
                    if existing:
                        existing_key = existing.get("key")
                # Defense-in-depth: a PMID hit in the pre-built index means the
                # item IS present even if its DOI/title did not match — catches a
                # ref upstream false-flagged "missing" (present only by PMID).
                if existing_key is None and pmid and pmid_index:
                    existing_key = pmid_index.get(pmid)
                # Fail-closed on a DEGRADED index read: we can trust a POSITIVE
                # match above (the item is present) but NOT an absence — and the
                # live ``_title_exists_in_library`` search below is itself a
                # (degraded) read whose "absent" answer is equally untrustworthy.
                # Refuse to create rather than risk a duplicate write to the
                # shared group; the user re-runs once the library is readable.
                if existing_key is None and index_degraded:
                    result["skipped_degraded_read"].append({
                        "title": title,
                        "reason": ("library index degraded/unverifiable — could not "
                                   "confirm this item is absent; refused to create "
                                   "(fail-closed) to avoid a duplicate write."),
                    })
                    continue
                if existing_key is None:
                    existing_key = _title_exists_in_library(title)

                if existing_key is not None:
                    result["skipped_existing"].append({"title": title, "existing_key": existing_key})
                    continue

            zitem = csljson_to_zotero_item(
                meta,
                collections=[coll_key] if coll_key else None,
                tags=tags,
            )
        except Exception as exc:
            result["failed"].append({"title": title, "reason": f"Item conversion error: {exc}"})
            continue

        to_create.append((meta, zitem))

    # POST in batches of ≤50, each with a unique Zotero-Write-Token
    BATCH_SIZE = 50
    for batch_start in range(0, len(to_create), BATCH_SIZE):
        batch = to_create[batch_start: batch_start + BATCH_SIZE]
        batch_metas = [m for m, _ in batch]
        batch_items = [z for _, z in batch]

        write_token = secrets.token_hex(16)  # unique idempotency token per request
        try:
            resp = _post_json(
                "items",
                batch_items,
                extra_headers={"Zotero-Write-Token": write_token},
            )
        except Exception as exc:
            for meta in batch_metas:
                result["failed"].append({
                    "title": (meta.get("title") or ""),
                    "reason": f"POST error: {exc}",
                })
            continue

        successful = resp.get("successful", {})
        unchanged = resp.get("unchanged", {})
        failed = resp.get("failed", {})

        for idx_str, item_resp in successful.items():
            idx = int(idx_str)
            meta = batch_metas[idx]
            result["created"].append({
                "title": (meta.get("title") or ""),
                "key": item_resp.get("key", ""),
                "doi": (meta.get("doi") or ""),
            })

        for idx_str, item_resp in unchanged.items():
            idx = int(idx_str)
            meta = batch_metas[idx]
            # Treat unchanged as skipped (already existed at write time)
            result["skipped_existing"].append({
                "title": (meta.get("title") or ""),
                "existing_key": item_resp.get("key", ""),
            })

        for idx_str, err in failed.items():
            idx = int(idx_str)
            meta = batch_metas[idx]
            result["failed"].append({
                "title": (meta.get("title") or ""),
                "reason": err.get("message", "unknown error"),
            })

    # Opt-in open-access PDF attach.  Runs only after the items are created and
    # only inside this already-write-gated function, so the SAME write-permission
    # gate (checked above) covers the attach.  Each attach is best-effort: a
    # fetch/upload failure never demotes a successful create — the PDF outcome is
    # recorded separately under ``result["pdf_attached"]`` / ``result["pdf_skipped"]``.
    if attach_pdfs and result["created"]:
        _attach_oa_pdfs_to_created(result, to_create)

    return result


# ---------------------------------------------------------------------------
# Open-access PDF attach  (opt-in; wired by create_items(attach_pdfs=True))
# ---------------------------------------------------------------------------

def _attach_oa_pdfs_to_created(result: dict, to_create: list) -> None:
    """For each newly-created item, try to find + attach an OA PDF (best-effort).

    Imported lazily so the OA/SSRF machinery (and its network) is only pulled in
    when a caller actually opts in.  Records outcomes on ``result`` under
    ``"pdf_attached"`` (list of ``{key, source_url}``) and ``"pdf_skipped"``
    (list of ``{key, reason}``); never raises, never touches ``result["created"]``.
    """
    from . import oapdf  # lazy: only imported on the opt-in path

    result.setdefault("pdf_attached", [])
    result.setdefault("pdf_skipped", [])

    # Map created records back to their source meta so we can pull pmid/arxiv too
    # (the create record only carries title/key/doi).  to_create is [(meta, item)].
    meta_by_doi: dict[str, dict] = {}
    meta_by_title: dict[str, dict] = {}
    for meta, _zitem in to_create:
        d = (meta.get("doi") or "").strip().lower()
        if d:
            meta_by_doi[d] = meta
        t = (meta.get("title") or "").strip().lower()
        if t:
            meta_by_title[t] = meta

    for created in result["created"]:
        key = created.get("key")
        if not key:
            continue
        doi = (created.get("doi") or "").strip() or None
        meta = (
            meta_by_doi.get((doi or "").lower())
            or meta_by_title.get((created.get("title") or "").strip().lower())
            or {}
        )
        pmid = (str(meta.get("pmid")).strip() if meta.get("pmid") else None)
        arxiv = (str(meta.get("arxiv")).strip() if meta.get("arxiv") else None)

        if not (doi or pmid or arxiv):
            result["pdf_skipped"].append({"key": key, "reason": "no DOI/PMID/arXiv identifier"})
            continue

        try:
            pdf_url = oapdf.find_oa_pdf_url(doi=doi, pmid=pmid, arxiv=arxiv)
        except Exception:  # noqa: BLE001 — never demote a successful create
            pdf_url = None
        if not pdf_url:
            result["pdf_skipped"].append({"key": key, "reason": "no open-access PDF found"})
            continue

        try:
            pdf_bytes = oapdf.fetch_pdf_guarded(pdf_url)
        except Exception:  # noqa: BLE001
            pdf_bytes = None
        if not pdf_bytes:
            result["pdf_skipped"].append({
                "key": key,
                "reason": f"OA URL found but no valid PDF downloaded: {pdf_url}",
            })
            continue

        filename = _pdf_filename_for(doi, pmid, arxiv, key)
        ok = attach_pdf_to_item(key, pdf_bytes, filename)
        if ok:
            result["pdf_attached"].append({"key": key, "source_url": pdf_url})
        else:
            result["pdf_skipped"].append({"key": key, "reason": "attach/upload failed"})


def _pdf_filename_for(
    doi: Optional[str], pmid: Optional[str], arxiv: Optional[str], key: str
) -> str:
    """A filesystem-safe ``*.pdf`` name derived from the best available id."""
    stem = doi or (f"PMID{pmid}" if pmid else None) or (f"arXiv{arxiv}" if arxiv else None) or key
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("_") or key
    return f"{safe}.pdf"


def _post_form(
    path: str,
    fields: dict[str, str],
    *,
    extra_headers: Optional[dict[str, str]] = None,
    timeout: float = 60.0,
) -> Any:
    """POST a ``application/x-www-form-urlencoded`` body to a library path.

    Used by the Zotero file-upload handshake (the upload-authorization and
    upload-registration steps both take form bodies, not JSON).  Same auth +
    URL construction as :func:`_post_json`; the API key is sent only in the
    ``Zotero-API-Key`` header.  Returns parsed JSON.
    """
    cfg = zotero_config()
    if not cfg:
        missing = ", ".join(_missing_env())
        raise RuntimeError(f"Zotero credentials not configured; set env var(s): {missing}")

    prefix = "groups" if cfg["library_type"] == "group" else "users"
    url = f"{API_BASE}/{prefix}/{cfg['library_id']}/{path.lstrip('/')}"
    data = urllib.parse.urlencode(fields).encode("utf-8")
    headers: dict[str, str] = {
        "Zotero-API-Key": cfg["api_key"],
        "Zotero-API-Version": API_VERSION,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    return json.loads(_urlopen_retrying(req, timeout=timeout).decode("utf-8"))


def attach_pdf_to_item(
    parent_key: str,
    pdf_bytes: bytes,
    filename: str,
    *,
    timeout: float = 120.0,
) -> bool:
    """Attach ``pdf_bytes`` as an imported-file child of the item ``parent_key``.

    zotero.py is the authoritative Zotero WRITER, so the full Web-API file-upload
    handshake lives here:

    1. Create an ``imported_file`` attachment item (POST ``items``) carrying the
       parent key, filename, content-type, and the file's md5.
    2. Request upload authorization (POST ``items/<key>/file`` with the md5 /
       filename / filesize / mtime; ``If-None-Match: *``).  Zotero answers with
       either ``{"exists": 1}`` (the file is already stored — done) or a
       presigned ``{"url", "params"/"prefix"+"suffix", "uploadKey"}``.
    3. PUT the bytes to the presigned URL (prefix + bytes + suffix, full-upload
       form).  This URL is Zotero's own S3 endpoint, not attacker-influenced.
    4. Register the upload (POST ``items/<key>/file`` with ``upload=<uploadKey>``;
       ``If-None-Match: *``).

    Write permission is NOT re-checked here — the only caller is
    :func:`create_items`, which has already gated on
    :func:`key_can_write_status`.  Returns ``True`` on a stored file, ``False``
    on any failure (never raises): a failed attach must not break the add path.
    """
    if not pdf_bytes:
        return False
    try:
        md5 = hashlib.md5(pdf_bytes).hexdigest()  # noqa: S324 — Zotero's required content key, not a security digest
        filesize = len(pdf_bytes)
        mtime = int(time.time() * 1000)

        # 1) Create the attachment item.
        attach_item = {
            "itemType": "attachment",
            "linkMode": "imported_file",
            "parentItem": parent_key,
            "title": filename,
            "filename": filename,
            "contentType": "application/pdf",
            "charset": "",
            "tags": [],
            "relations": {},
            "note": "",
        }
        token = secrets.token_hex(16)
        create_resp = _post_json(
            "items", [attach_item], extra_headers={"Zotero-Write-Token": token}
        )
        successful = (create_resp or {}).get("successful", {})
        if not successful:
            return False
        attach_key = successful.get("0", {}).get("key")
        if not attach_key:
            return False

        # 2) Request upload authorization.
        auth = _post_form(
            f"items/{attach_key}/file",
            {
                "md5": md5,
                "filename": filename,
                "filesize": str(filesize),
                "mtime": str(mtime),
            },
            extra_headers={"If-None-Match": "*"},
        )
        if not isinstance(auth, dict):
            return False
        if auth.get("exists"):
            return True  # identical file already stored — nothing to upload

        upload_url = auth.get("url")
        upload_key = auth.get("uploadKey")
        if not upload_url or not upload_key:
            return False

        # 3) PUT the bytes (full-upload: prefix + content + suffix or multipart
        # params).  Modern Zotero returns prefix/suffix for a single-part PUT.
        prefix = (auth.get("prefix") or "").encode("utf-8")
        suffix = (auth.get("suffix") or "").encode("utf-8")
        content_type = auth.get("contentType", "application/pdf")
        if prefix or suffix:
            put_body = prefix + pdf_bytes + suffix
            put_req = urllib.request.Request(
                upload_url, data=put_body, method="PUT",
                headers={"Content-Type": content_type},
            )
        else:
            # Older/multipart-params form: post the params then the file.
            params = auth.get("params") or {}
            boundary = "----zoterocite" + secrets.token_hex(8)
            parts: list[bytes] = []
            for fk, fv in params.items():
                parts.append(f"--{boundary}\r\n".encode())
                parts.append(
                    f'Content-Disposition: form-data; name="{fk}"\r\n\r\n'.encode()
                )
                parts.append(f"{fv}\r\n".encode())
            parts.append(f"--{boundary}\r\n".encode())
            parts.append(
                f'Content-Disposition: form-data; name="file"; '
                f'filename="{filename}"\r\n'.encode()
            )
            parts.append(b"Content-Type: application/pdf\r\n\r\n")
            parts.append(pdf_bytes)
            parts.append(f"\r\n--{boundary}--\r\n".encode())
            put_body = b"".join(parts)
            put_req = urllib.request.Request(
                upload_url, data=put_body, method="POST",
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            )
        with urllib.request.urlopen(put_req, timeout=_effective_timeout(timeout)):  # noqa: S310 — Zotero storage endpoint
            pass

        # 4) Register the upload.  Zotero answers this step with an empty 204,
        # so DON'T route through _post_form (it json.loads the body and would
        # raise on ""); issue the form POST directly and treat any 2xx as success.
        cfg = zotero_config()
        prefix_seg = "groups" if cfg["library_type"] == "group" else "users"
        reg_url = f"{API_BASE}/{prefix_seg}/{cfg['library_id']}/items/{attach_key}/file"
        reg_req = urllib.request.Request(
            reg_url,
            data=urllib.parse.urlencode({"upload": upload_key}).encode("utf-8"),
            method="POST",
            headers={
                "Zotero-API-Key": cfg["api_key"],
                "Zotero-API-Version": API_VERSION,
                "Content-Type": "application/x-www-form-urlencoded",
                "If-None-Match": "*",
            },
        )
        with urllib.request.urlopen(reg_req, timeout=_effective_timeout(timeout)) as reg_resp:  # noqa: S310
            return 200 <= (getattr(reg_resp, "status", None) or reg_resp.getcode()) < 300
    except Exception:  # noqa: BLE001 — never break the add path on an attach failure
        return False


# ---------------------------------------------------------------------------
# Version-checked writes (PATCH / trash) — shared-library-safe update primitives
# ---------------------------------------------------------------------------
#
# The CREATE path above (``create_items`` → ``_post_json``) makes concurrency
# safe with a per-request ``Zotero-Write-Token`` (an idempotency key: replaying a
# create cannot double-create).  The UPDATE primitives below instead guard with a
# VERSION PRECONDITION: every write carries ``If-Unmodified-Since-Version:
# <version>`` so the server refuses (HTTP 412) the write if the object changed
# since we read it.  A shared group can be edited by a collaborator between our
# read and our write, and silently clobbering that edit is exactly what a
# version-checked write prevents.  A 409 ("target library is locked" — a sync in
# progress) is treated the same way.  Both surface as :class:`ZoteroWriteConflict`
# so a caller ABORTS that object rather than retry-and-clobber.
#
# These back a relation-preserving library de-duplication merge (the ``dedup``
# module), whose contract is to reproduce Zotero's native "Duplicate Items" merge
# over the Web API — recording ``relations["dc:replaces"]`` on a surviving master
# and TRASHING (never purging) the retired duplicates.


class ZoteroWriteConflict(RuntimeError):
    """A version-checked write was refused because the target changed first.

    Raised on HTTP 412 (an ``If-Unmodified-Since-Version`` precondition failed —
    the item was modified since we read its version) or HTTP 409 (the target
    library is locked by another sync).  Either way the caller must ABORT that
    write rather than retry-and-clobber a concurrent edit in the shared group.
    The item path is named in the message; the API key never is.
    """


def _urlopen_write_with_headers(
    req: urllib.request.Request,
    *,
    timeout: float,
    retries: int = 1,
    _sleep=None,
) -> tuple[bytes, dict]:
    """Like :func:`_urlopen_retrying` but returns ``(raw_body, headers)``.

    A version-checked write needs the RESPONSE HEADERS: Zotero answers a
    successful single-object PATCH with ``204 No Content`` (empty body) and the
    object's new version in ``Last-Modified-Version``.  A thin delegate over the
    shared :func:`_urlopen_with_headers_retrying` retry loop (which the header-
    reading read path also uses), so the retry policy lives in ONE place.  A
    retried PATCH is SAFE: it still carries the original
    ``If-Unmodified-Since-Version``, so if the first attempt actually applied (and
    only its response was lost) the retry hits a 412 and fails CLOSED rather than
    double-applying.  Non-transient errors (incl. 412/409) propagate to the caller
    for classification.  The request headers (which carry the API key) are never
    logged or surfaced here.
    """
    return _urlopen_with_headers_retrying(
        req, timeout=timeout, retries=retries, _sleep=_sleep
    )


def _version_from_headers(headers, *, fallback: Optional[int] = None) -> Optional[int]:
    """Parse ``Last-Modified-Version`` from a write response's headers.

    Returns the int version Zotero reports for the just-written object, or
    ``fallback`` when the header is absent/unparseable (some proxies strip it).
    """
    if not headers:
        return fallback
    try:
        raw = headers.get("Last-Modified-Version")
    except Exception:  # noqa: BLE001 — header access must never raise here
        raw = None
    if raw is None:
        return fallback
    try:
        return int(raw)
    except (TypeError, ValueError):
        return fallback


def _patch_json(
    path: str,
    body: Any,
    *,
    if_unmodified_since_version: int,
    extra_headers: Optional[dict[str, str]] = None,
    timeout: float = 15.0,
) -> Optional[int]:
    """PATCH ``body`` (a PARTIAL item update) to a library path, version-checked.

    Zotero supports partial item updates via PATCH: only the fields present in
    ``body`` are changed (the rest of the item is untouched).  The request carries
    ``If-Unmodified-Since-Version: <version>`` so Zotero rejects the write with
    HTTP 412 if the item was modified since that version — never silently
    clobbering a collaborator's concurrent edit in the shared group.  Returns the
    object's NEW version (from the ``Last-Modified-Version`` response header; a
    successful single-object PATCH is a 204 with an empty body), or ``None`` if
    the header was stripped.  Raises :class:`ZoteroWriteConflict` on 412/409 and
    propagates any other HTTP error.  Builds the URL and auth headers exactly like
    :func:`_post_json`; the API key appears only in the ``Zotero-API-Key`` header,
    never in the URL or an exception message.
    """
    cfg = zotero_config()
    if not cfg:
        missing = ", ".join(_missing_env())
        raise RuntimeError(
            f"Zotero credentials not configured; set env var(s): {missing}"
        )

    prefix = "groups" if cfg["library_type"] == "group" else "users"
    url = f"{API_BASE}/{prefix}/{cfg['library_id']}/{path.lstrip('/')}"

    data = json.dumps(body).encode("utf-8")
    headers: dict[str, str] = {
        "Zotero-API-Key": cfg["api_key"],
        "Zotero-API-Version": API_VERSION,
        "Content-Type": "application/json",
        "If-Unmodified-Since-Version": str(int(if_unmodified_since_version)),
    }
    if extra_headers:
        headers.update(extra_headers)

    req = urllib.request.Request(url, data=data, method="PATCH", headers=headers)
    try:
        _body, resp_headers = _urlopen_write_with_headers(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        status = getattr(exc, "code", None)
        if status in (412, 409):
            # Do NOT chain the original error (its message could echo the URL);
            # raise a clean, key-free conflict the caller aborts on.
            raise ZoteroWriteConflict(
                f"version-checked write to {path!r} refused (HTTP {status}): the "
                "target changed since it was read, or the library is locked; "
                "aborting rather than overwriting a concurrent edit."
            ) from None
        raise
    return _version_from_headers(resp_headers, fallback=None)


def item_version(item: Any) -> Optional[int]:
    """Best-effort read of an item's library *version* from a Zotero API object.

    ``format=json`` items carry a top-level ``version``; some shapes nest it under
    ``data.version``.  Returns the int version or ``None``.  Used to seed the
    ``If-Unmodified-Since-Version`` precondition on a subsequent write.
    """
    if not isinstance(item, dict):
        return None
    for candidate in (item.get("version"), (item.get("data") or {}).get("version")):
        if candidate is not None:
            try:
                return int(candidate)
            except (TypeError, ValueError):
                return None
    return None


def get_item_children(key: str) -> list[dict]:
    """ALL child items (notes + attachments) of ``key`` — GET ``items/<key>/children``.

    Returns the raw Zotero item objects (each carrying its own ``version`` and
    ``data``), or ``[]`` if the item has none.  Used by the ``dedup`` merge to
    REPARENT a loser's children onto the surviving master before the loser is
    trashed, so nothing is orphaned — which makes COMPLETENESS load-bearing: a
    child left unfetched would be silently destroyed when the loser is trashed
    (then purged).  So this PAGINATES (``limit=100`` + ``start`` until
    ``Total-Results`` is exhausted) exactly like :func:`fetch_all`, and FAILS
    CLOSED — raising :class:`LibraryUnavailableError` (the same degraded-read
    signal, which the merge treats as "children unreadable → do NOT trash this
    loser") — when the ``Total-Results`` header is missing/unparseable or a page
    truncates mid-pagination, rather than returning a partial child list that
    would orphan the rest.  A 404 (the item is already gone) yields ``[]``; any
    other HTTP error propagates so the merge fails closed.
    """
    out: list[dict] = []
    start = 0
    expected_total: Optional[int] = None
    while True:
        params: dict[str, Any] = {
            "format": "json", "limit": "100", "start": str(start),
        }
        try:
            data, headers = _get_json_headers(
                build_request(f"items/{key}/children", params)
            )
        except urllib.error.HTTPError as exc:
            if getattr(exc, "code", None) == 404:
                return []  # item already gone — it has no children to reparent
            raise

        raw_total = headers.get("Total-Results")
        header_ok = False
        if raw_total is not None:
            try:
                expected_total = int(raw_total)
                header_ok = True
            except (TypeError, ValueError):
                header_ok = False

        if not isinstance(data, list):
            raise LibraryUnavailableError(
                "Zotero returned a non-list children response; treating as a "
                "degraded read (fail-closed) rather than a complete child set."
            )
        if not data:
            break
        out.extend(data)
        start += len(data)

        if not header_ok:
            # No authoritative count → cannot confirm every child was fetched.
            # Fail closed rather than reparent-and-trash on a partial child set.
            raise LibraryUnavailableError(
                "Zotero children response is missing/invalid the Total-Results "
                "header; cannot confirm all children were fetched. Treating as a "
                "degraded read (fail-closed) so no child is orphaned on trash."
            )
        if start >= expected_total:
            break

    if expected_total is None:
        # First page empty AND no valid Total-Results — a degraded read, not a
        # genuinely child-less item (which reports ``Total-Results: 0``).
        raise LibraryUnavailableError(
            "Zotero children response is missing the Total-Results header; "
            "cannot confirm the child set (fail-closed)."
        )
    if len(out) != expected_total:
        # A page returned short mid-pagination — we hold fewer children than the
        # item has. Fail closed so the loser is not trashed with children left
        # behind (which a trash-purge would destroy).
        raise LibraryUnavailableError(
            f"Zotero children fetch is incomplete: got {len(out)} of "
            f"{expected_total} child item(s). Treating as a degraded read "
            "(fail-closed) so no child is orphaned on trash."
        )
    return out


def patch_item_fields(
    key: str, fields: dict, *, version: int, timeout: float = 15.0
) -> Optional[int]:
    """Version-checked PARTIAL update of item ``key`` with ``fields``.

    Thin wrapper over :func:`_patch_json` (single-item PATCH).  ``fields`` is a
    partial item-data dict — only the keys present are changed.  ``version`` is
    the item's current version (from :func:`get_item` via :func:`item_version`);
    the write carries it as ``If-Unmodified-Since-Version`` and raises
    :class:`ZoteroWriteConflict` if the item changed first.  Returns the new
    version (or ``None`` if the response header was stripped).
    """
    return _patch_json(
        f"items/{key}", fields, if_unmodified_since_version=version, timeout=timeout
    )


def trash_item(key: str, *, version: int, timeout: float = 15.0) -> Optional[int]:
    """Move item ``key`` to the trash (``deleted: 1``) — REVERSIBLE, not purged.

    A version-checked PATCH, NOT a hard ``DELETE``: the item stays recoverable
    from Zotero's trash and its URI stays valid (so a ``dc:replaces`` relation
    pointing at it keeps resolving a merged-away citation to the master).  Raises
    :class:`ZoteroWriteConflict` on a concurrent edit.  This is how the ``dedup``
    merge retires a duplicate without destroying it.
    """
    return patch_item_fields(key, {"deleted": 1}, version=version, timeout=timeout)


def reparent_item(
    child_key: str, new_parent_key: str, *, version: int, timeout: float = 15.0
) -> Optional[int]:
    """Re-attach child item ``child_key`` under a new parent (``parentItem``).

    Version-checked PATCH used by the ``dedup`` merge to move a loser's
    notes/attachments onto the surviving master BEFORE the loser is trashed, so
    nothing is orphaned.  Raises :class:`ZoteroWriteConflict` on a concurrent
    edit.
    """
    return patch_item_fields(
        child_key, {"parentItem": new_parent_key}, version=version, timeout=timeout
    )
