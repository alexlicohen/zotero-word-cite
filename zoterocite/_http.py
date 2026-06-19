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

import os
import time
import urllib.error
import urllib.request
from typing import Optional

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


def http_get(
    url: str,
    *,
    timeout: float,
    headers: Optional[dict] = None,
    retries: int = 1,
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
