"""Shared canned-``urlopen`` mock helpers for the Zotero tests.

Both ``test_zotero_write.py`` (the write primitives) and ``test_dedup.py`` (the
library-wide merge) mock ``urllib.request.urlopen`` with the SAME context-manager
fake.  This module is the single source of that fake so the two suites cannot
silently diverge (they previously kept independent copy-pasted definitions).

Imported by top-level name (``from _zotero_http_helpers import fake_response``);
pytest puts the ``tests/`` directory on ``sys.path`` for its test modules, so no
package/``__init__`` is needed.  NO real network is ever performed here.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock
import urllib.error


def fake_response(body: Any, headers: dict | None = None, status: int = 200):
    """A context-manager mock mimicking ``urllib.request.urlopen``.

    ``body`` is JSON-encoded for ``.read()``; ``headers`` is exposed as the plain
    ``.headers`` mapping (Zotero's ``Total-Results`` / ``Last-Modified-Version``
    live here).  Usable as ``with urlopen(...) as resp:`` via ``__enter__`` /
    ``__exit__``.  ``status`` is accepted for call-site clarity (the fake does not
    branch on it).
    """
    raw = json.dumps(body).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = raw
    mock_resp.headers = headers or {}
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def http_error(code: int) -> urllib.error.HTTPError:
    """An ``HTTPError`` with ``code`` for use as a ``urlopen`` ``side_effect``."""
    return urllib.error.HTTPError("https://api.zotero.org/x", code, "err", {}, None)
