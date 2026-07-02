"""Shared stdlib HTTP GET helper (network-resilient, never-raise contract).

ONE GET primitive for every read-only public-API client in zotero-word-cite
(:mod:`entrez`, :mod:`orcid_api`, :mod:`mybib`, :mod:`icite`, :mod:`contested`).
Before this module those clients each carried a near-identical private
``_http_get`` that had **drifted** — different timeouts (10/15/30 s), different
headers — and *none* retried or honoured ``Retry-After``.  They now all call
:func:`http_get`, so the timeout/headers are an explicit per-call argument and
the retry policy lives in exactly one place.

NOT for :mod:`zotero` (auth + write-token GETs — a different contract) or for
POST/authenticated endpoints.

Design contract — preserved from the modules this replaces
----------------------------------------------------------
**This function never raises to the caller.**  Every URLError / HTTPError /
timeout / OS error is swallowed and ``None`` is returned, because every caller
must degrade gracefully (to citation-string-only ranking, an empty works list,
etc.) when the network is unavailable.  ``except Exception`` is deliberate and
already spares ``KeyboardInterrupt`` / ``SystemExit`` (those derive from
``BaseException``, not ``Exception``).

The URL is never placed in an emitted message or exception — it may carry an
NCBI ``api_key`` query param.  On failure we just return ``None``.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional


def _timeout_ceiling() -> Optional[float]:
    """A process-wide hard ceiling (seconds) on every GET, or ``None``.

    Read from ``ZOTERO_WORD_CITE_HTTP_TIMEOUT`` at call time.  This is the retry-saver
    for unreachable hosts: a deployment can guarantee no single read blocks a CLI
    for more than N seconds regardless of the per-call ``timeout`` a caller
    passes.  Absent / malformed / non-positive → no ceiling (the caller's own
    timeout stands), so default behaviour and the network-mock tests are
    unchanged.
    """
    raw = os.environ.get("ZOTERO_WORD_CITE_HTTP_TIMEOUT")
    if not raw:
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    return val if val > 0 else None

_TOOL = "zotero-word-cite"
# The polite-pool contact email is sourced from the environment first so the
# toolkit can be used by anyone without editing code; the literal below is just
# a neutral placeholder.  Set ZOTERO_WORD_CITE_CONTACT_EMAIL (or NCBI_EMAIL) to
# your own address so Crossref/NCBI can reach you (the "polite pool").  This
# module is the SINGLE OWNER of the contact email + User-Agent — every
# public-API client routes through :func:`contact_email` / :func:`user_agent`
# rather than carrying its own copy.
_DEFAULT_EMAIL = "zotero-word-cite@example.com"
# Default UA mirrors the polite-pool string the per-module clients used; a
# caller may override it (or add an ``Accept`` header) via ``headers``.  Kept as
# a module constant for the no-env case (and referenced by tests); the live
# default is computed by :func:`user_agent` so a per-process env override
# applies even to callers that pass no UA header.
_DEFAULT_UA = f"{_TOOL}/1.0 (mailto:{_DEFAULT_EMAIL})"


def contact_email() -> str:
    """Return the polite-pool contact email, read at CALL time.

    Precedence: ``ZOTERO_WORD_CITE_CONTACT_EMAIL`` (the multi-user override) →
    ``NCBI_EMAIL`` (the long-standing NCBI override, kept so existing setups and
    NCBI-specific precedence keep working) → :data:`_DEFAULT_EMAIL` (this repo's
    fallback).  Read from the environment on every call so a process that sets
    the env var after import is still honoured; with NO env var set the result
    is byte-identical to the historical hard-coded value.
    """
    return (
        os.environ.get("ZOTERO_WORD_CITE_CONTACT_EMAIL")
        or os.environ.get("NCBI_EMAIL")
        or _DEFAULT_EMAIL
    )


def user_agent(tool: str = _TOOL) -> str:
    """Return the polite-pool ``User-Agent`` string for *tool*, read at CALL time.

    Composes :func:`contact_email` into the ``tool/1.0 (mailto:...)`` form every
    client used to build by hand.  With no env var set this equals
    :data:`_DEFAULT_UA` for the default tool, so existing network-mock tests stay
    byte-identical.
    """
    return f"{tool}/1.0 (mailto:{contact_email()})"

# Cap on how long we will honour a server-sent ``Retry-After`` before our one
# bounded retry.  Public read APIs (NCBI, ORCID, iCite, Crossref) return small
# Retry-After values; we never want to block a CLI on a multi-minute sleep, so
# clamp hard.  A malformed/absent header falls back to ``_DEFAULT_RETRY_WAIT``.
_MAX_RETRY_WAIT = 3.0
_DEFAULT_RETRY_WAIT = 1.0

# Statuses worth one retry: 429 (rate limit) + transient 5xx.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


def _parse_retry_after(value: Optional[str]) -> float:
    """Seconds to wait per a ``Retry-After`` header value, clamped to a few s.

    ``Retry-After`` may be an integer number of seconds (the common case for
    these APIs) or an HTTP-date.  We only honour the integer form — an
    HTTP-date would require parsing + clock-skew handling for no real benefit
    here — and clamp everything to ``[0, _MAX_RETRY_WAIT]``.  Anything missing
    or unparseable yields the small default so we still back off once.
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


# ---------------------------------------------------------------------------
# Opt-in disk-backed response cache (public read-only GETs only)
# ---------------------------------------------------------------------------
# A per-call, opt-in cache so a repeated resolution run does not re-fetch every
# reference from Crossref/PubMed/iCite. It is OFF by default (``cache_ttl=None``)
# → byte-identical historical behaviour. When a caller opts in, a successful body
# is stored keyed by a stable hash of the sanitized request; a later call within
# the TTL is served without touching the network.
#
# Two layers, mirroring the library-index caches: a process-level in-memory dict
# lazily seeded from a gitignored disk file. Disk read/write is best-effort —
# a corrupt or unwritable cache degrades to a live fetch, never raises.
#
# INVARIANTS
#   * Only a SUCCESSFUL body (non-None) is ever stored — a failure (None) must
#     NOT be cached, so a later retry re-attempts rather than serving a poisoned
#     empty. This is the key correctness invariant.
#   * The request may carry an ``api_key`` query param (NCBI). It is STRIPPED
#     before the key is hashed and is NEVER written to disk (only the hash and
#     the response body are stored, and the body does not echo the key).
#
# The cache path is a module attribute (tests redirect it to a tmp file).
_HTTP_CACHE_PATH = Path(__file__).parent.parent / "data" / "http_response_cache.json"

# Process-level in-memory layer. ``None`` = not yet loaded from disk (seeded
# lazily on first access); a dict maps ``key_hash -> {"body": text, "ts": unix}``.
_http_cache_mem: Optional[dict] = None


def _reset_http_cache() -> None:
    """Test hook: drop the in-memory layer so the next access reloads from disk.

    Does not touch the disk file (tests redirect ``_HTTP_CACHE_PATH`` to a tmp
    path). Monkeypatch-friendly: pair with a redirected ``_HTTP_CACHE_PATH`` to
    isolate a test from the real cache.
    """
    global _http_cache_mem
    _http_cache_mem = None


def _cache_enabled(cache_ttl) -> bool:
    """Whether an opt-in cache TTL is a usable positive number.

    A non-number / ``None`` / non-positive value means "no caching" and is
    swallowed here (never raises) so the never-raise contract of :func:`http_get`
    holds even for a malformed ``cache_ttl``.
    """
    try:
        return cache_ttl is not None and float(cache_ttl) > 0
    except (TypeError, ValueError):
        return False


def _sanitize_url_for_cache(url: str) -> Optional[str]:
    """Return *url* with any ``api_key`` query param removed, or ``None``.

    The api_key is stripped BEFORE the cache key is computed so (a) a rotated /
    absent key never busts the cache and (b) the secret never enters the key or
    the on-disk file. Returns ``None`` if the URL cannot be parsed cleanly — the
    caller then declines to cache rather than risk a lossy key.
    """
    try:
        parts = urllib.parse.urlsplit(url)
        pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    except Exception:  # noqa: BLE001 — unparseable URL: caller declines to cache
        return None
    kept = [(k, v) for (k, v) in pairs if k.lower() != "api_key"]
    clean_query = urllib.parse.urlencode(kept)
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, clean_query, parts.fragment)
    )


def _cache_key(url: str, accept: Optional[str]) -> Optional[str]:
    """Stable sha256 hex key over ``(GET, sanitized_url, Accept)``, or ``None``.

    ``None`` when the URL cannot be sanitized — the caller then does not cache
    (the api_key must never leak into the key).
    """
    sanitized = _sanitize_url_for_cache(url)
    if sanitized is None:
        return None
    basis = "GET\n" + sanitized + "\n" + (accept or "")
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _load_disk_cache() -> dict:
    """Best-effort load of the disk cache into a dict; ``{}`` on any problem."""
    try:
        with open(_HTTP_CACHE_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except Exception:  # noqa: BLE001 — missing/corrupt cache: degrade to empty
        pass
    return {}


def _save_disk_cache(cache: dict) -> None:
    """Best-effort write of the whole cache dict; never raises."""
    try:
        _HTTP_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_HTTP_CACHE_PATH, "w", encoding="utf-8") as fh:
            json.dump(cache, fh)
    except Exception:  # noqa: BLE001 — unwritable cache: skip silently
        pass


def _fresh_entry_body(entry, now: float, ttl: float) -> Optional[bytes]:
    """Return the cached body bytes if *entry* is present and within *ttl*.

    ``None`` when the entry is malformed, has no body, or is stale — the caller
    then treats it as a miss and re-fetches (never-serve-stale on any doubt).
    """
    try:
        ts = float(entry.get("ts", 0))
        body = entry.get("body")
    except (AttributeError, TypeError, ValueError):
        return None
    if body is None:
        return None
    if now - ts >= ttl:
        return None
    try:
        return body.encode("utf-8")
    except (AttributeError, UnicodeEncodeError):
        return None


def _cache_lookup(key: str, ttl: float) -> Optional[bytes]:
    """Return a fresh cached body for *key*, or ``None`` (miss / stale)."""
    global _http_cache_mem
    if _http_cache_mem is None:
        _http_cache_mem = _load_disk_cache()
    entry = _http_cache_mem.get(key)
    if entry is None:
        return None
    return _fresh_entry_body(entry, time.time(), ttl)


def _cache_store(key: str, body) -> None:
    """Store a successful *body* under *key* (in memory + disk).

    Only a bytes body that round-trips cleanly as UTF-8 is cached (all the public
    APIs return UTF-8 text). A non-bytes body is stored as an empty string so a
    caller that (incorrectly) reaches here with a failure result produces an
    OBSERVABLE poisoned entry rather than a silent no-op — the never-cache-failure
    gate lives in :func:`http_get`, and this shape keeps that guard testable.
    """
    global _http_cache_mem
    if isinstance(body, (bytes, bytearray)):
        try:
            text = bytes(body).decode("utf-8")
        except UnicodeDecodeError:
            return  # non-UTF-8 body: cannot round-trip losslessly → do not cache
    else:
        text = ""
    if _http_cache_mem is None:
        _http_cache_mem = _load_disk_cache()
    _http_cache_mem[key] = {"body": text, "ts": time.time()}
    _save_disk_cache(_http_cache_mem)


def _do_fetch(
    url: str,
    timeout: float,
    req_headers: dict,
    retries: int,
    _sleep,
) -> Optional[bytes]:
    """The live network GET (retry/Retry-After loop). ``None`` on ANY failure.

    Extracted from :func:`http_get` so the cache read/write can wrap it without
    changing the historical fetch behaviour (all existing tests exercise this
    path unchanged).
    """
    attempts = max(0, int(retries)) + 1  # original + retries
    last_was_retryable = False
    for attempt in range(attempts):
        req = urllib.request.Request(url, method="GET", headers=req_headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                return resp.read()
        except urllib.error.HTTPError as exc:  # noqa: PERF203
            # Retry only transient statuses, and only if attempts remain.
            status = getattr(exc, "code", None)
            last_was_retryable = status in _RETRY_STATUSES
            if not last_was_retryable or attempt + 1 >= attempts:
                return None
            retry_after = None
            try:
                # HTTPError carries the response headers.
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
            except Exception:  # noqa: BLE001 — header access must never raise here
                retry_after = None
            wait = _parse_retry_after(retry_after)
            if wait > 0:
                _sleep(wait)
            continue
        except Exception:  # noqa: BLE001 — network resilience: never propagate
            # URLError, socket timeout, OSError, etc. — not retried (we can't
            # tell a transient blip from a hard failure, and a second blind
            # attempt rarely helps for these). Return None per the contract.
            return None
    return None


def http_get(
    url: str,
    *,
    timeout: float,
    headers: Optional[dict] = None,
    retries: int = 1,
    cache_ttl: Optional[float] = None,
    refresh: bool = False,
    _sleep=time.sleep,
) -> Optional[bytes]:
    """GET ``url`` and return the raw body, or ``None`` on ANY failure.

    Builds a ``urllib`` GET request carrying a default ``User-Agent`` (the
    caller may add or override headers — e.g. ``Accept: application/json``).
    On an HTTP 429 or transient 5xx it performs up to ``retries`` bounded
    retries (default one), honouring a ``Retry-After`` response header clamped
    to a few seconds.  Preserves the never-raise contract: URLError, HTTPError,
    timeouts, and OS errors are all swallowed and ``None`` is returned.

    Parameters
    ----------
    url:
        The full request URL (may already carry query params, incl. an
        ``api_key`` — which is therefore never logged or surfaced here).
    timeout:
        Per-request socket timeout in seconds (required — every caller passes
        its own deliberate value; reconciles the old per-module drift).
    headers:
        Extra request headers.  Merged over the default ``User-Agent`` so a
        caller can override the UA or add ``Accept``.
    retries:
        Maximum number of *retries* (not total attempts) on a 429 / 5xx.
        ``1`` (default) means: original attempt + at most one retry.  ``0``
        disables retrying.
    cache_ttl:
        OPT-IN response cache. ``None`` (default) or a non-positive value means
        NO caching — behaviour is byte-identical to before this parameter
        existed. A positive number of SECONDS enables a disk-backed cache: a
        successful body is stored keyed by a hash of the sanitized request, and
        a later call within ``cache_ttl`` seconds is served without any network
        call. Only successful (non-``None``) bodies are ever cached — a failure
        is never stored, so a retry re-attempts rather than serving a poisoned
        empty. Caching is public-read-only; never enable it for authenticated /
        write requests.
    refresh:
        When ``True`` (and caching is enabled), skip the cache READ and force a
        live fetch, then rewrite the cache with the fresh body. A stale/absent
        cache is untouched on failure.
    _sleep:
        Injection seam for the back-off sleep — tests pass a no-op so the
        retry path runs without really sleeping.  Not part of the public
        contract; callers in the codebase never set it.

    Returns
    -------
    The response body as ``bytes`` on success, or ``None`` on any failure
    (including retries exhausted).
    """
    # Compute the default UA at call time so a per-process env override
    # (ZOTERO_WORD_CITE_CONTACT_EMAIL / NCBI_EMAIL) applies even when the caller passes
    # no UA header.  With no env var set this is identical to ``_DEFAULT_UA``.
    req_headers = {"User-Agent": user_agent()}
    if headers:
        req_headers.update(headers)

    # Apply a process-wide timeout CEILING if one is configured, so an
    # unreachable host can never hang longer than the deployment allows even if a
    # caller passed a generous per-call timeout. With no env var set, the caller's
    # timeout stands unchanged (byte-identical default behaviour).
    _ceiling = _timeout_ceiling()
    if _ceiling is not None and _ceiling < timeout:
        timeout = _ceiling

    # Scheme allow-list (defense-in-depth): this is the shared fetch primitive,
    # so never let a caller-supplied URL make urlopen read a local file or other
    # non-web resource (``file://``, ``ftp://``, …) — an SSRF / local-file-read
    # vector. Only http(s) is fetched; anything else fails closed like any error.
    if urllib.parse.urlparse(url).scheme.lower() not in ("http", "https"):
        return None

    # Opt-in response cache. Disabled (the default) → straight to the live fetch,
    # byte-identical to the historical path. When enabled, the key is a hash of
    # the api_key-stripped request; a fresh hit short-circuits the network.
    caching = _cache_enabled(cache_ttl)
    cache_key: Optional[str] = None
    if caching:
        cache_key = _cache_key(url, req_headers.get("Accept"))
        if cache_key is None:
            caching = False  # unsanitizable URL: decline to cache this call
        elif not refresh:
            hit = _cache_lookup(cache_key, float(cache_ttl))
            if hit is not None:
                return hit

    body = _do_fetch(url, timeout, req_headers, retries, _sleep)

    # Cache ONLY a successful response. A ``None`` (4xx/5xx/timeout/OS error)
    # result is NEVER written, so a later retry re-attempts instead of serving a
    # poisoned empty. THIS IS THE KEY CORRECTNESS INVARIANT (guarded by test).
    if caching and body is not None:
        _cache_store(cache_key, body)
    return body
