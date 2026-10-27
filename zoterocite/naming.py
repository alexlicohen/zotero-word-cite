"""File-naming convention. Pure string builder + an optional on-disk rename.

Default convention (overridable per pack/rules):
    {year}_{month}_{mechanism}_{pi}_{project}_{doctype}_draft_{n}.docx
e.g. 2026_July_R01-R_Cohen_TSC_Prediction_Budget_Justification_draft_1.docx
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

DEFAULT_TEMPLATE = "{year}_{month}_{mechanism}_{pi}_{project}_{doctype}_draft_{draft}{ext}"


def convention_name(
    *, year, month, mechanism, pi, project, doctype, draft=1, ext=".docx",
    template: str = DEFAULT_TEMPLATE,
) -> str:
    doctype = doctype.strip().replace(" ", "_")
    project = project.strip().replace(" ", "_")
    if not ext.startswith("."):
        ext = "." + ext
    return template.format(
        year=year, month=month, mechanism=mechanism, pi=pi,
        project=project, doctype=doctype, draft=draft, ext=ext,
    )


def rename_to_convention(path: str | Path, *, apply: bool = False,
                         short_names: bool = False, **fields) -> Path:
    """Return the convention-name destination path; rename on disk when *apply* is True.

    Raises :exc:`FileExistsError` if the destination exists and is not the same
    file as the source (same guard as ``cmd_rename`` — no silent overwrites).
    """
    path = Path(path)
    fields.setdefault("ext", path.suffix or ".docx")
    name = convention_name(**fields)
    if short_names:
        name = short_name(name, drop_date=True)
    new = path.with_name(name)
    if apply and new != path:
        if new.exists() and new.resolve() != path.resolve():
            raise FileExistsError(
                f"refusing to rename: target {new.name!r} already exists and "
                "renaming would overwrite it — move or remove it first"
            )
        path.rename(new)
    return new


MAX_NAME_LEN = 50
_SAFE_RE = __import__("re").compile(r"[^A-Za-z0-9_\-]")
_FILLER_RE = __import__("re").compile(r"^(draft|final|copy|version|ver|rev|v\d+)$", __import__("re").I)
_COUNTER_RE = __import__("re").compile(r"^\d{1,3}$")
_YEAR_RE = __import__("re").compile(r"^(?:19|20)\d{2}(?:\d{2}(?:\d{2})?)?$")
_MONTH_RE = __import__("re").compile(
    r"^(jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|june?|july?|aug(ust)?|"
    r"sep(t(ember)?)?|oct(ober)?|nov(ember)?|dec(ember)?)$", __import__("re").I)


def _date_prefix_len(tokens):
    """Number of leading tokens forming a date prefix (``2026_Oct``, ``2026_10_26``,
    ``202610``), else 0. Never consumes the whole name."""
    if not tokens or not _YEAR_RE.match(tokens[0]):
        return 0
    n = 1
    if len(tokens[0]) == 4 and len(tokens) > 1:
        if _MONTH_RE.match(tokens[1]):
            n = 2
        elif _COUNTER_RE.match(tokens[1]) and len(tokens[1]) <= 2:
            n = 2
            if len(tokens) > 2 and _COUNTER_RE.match(tokens[2]) and len(tokens[2]) <= 2:
                n = 3
    return n if n < len(tokens) else 0


def _filler_groups(tokens):
    """Index groups of version-ish tokens: a filler word plus the counter right after
    it, or a trailing bare counter (``draft_1``, ``v_3``, ``..._2``)."""
    groups, i = [], 0
    while i < len(tokens):
        if _FILLER_RE.match(tokens[i]):
            j = i + 1
            if j < len(tokens) and _COUNTER_RE.match(tokens[j]):
                j += 1
            groups.append(list(range(i, j)))
            i = j
        elif i == len(tokens) - 1 and i > 0 and _COUNTER_RE.match(tokens[i]):
            groups.append([i])
            i += 1
        else:
            i += 1
    return groups


def short_name(name: str, *, max_len: int = MAX_NAME_LEN, taken=(),
               drop_date: bool = False) -> str:
    """Deterministically shorten *name* to <= *max_len* chars incl. extension.

    Order: (1) a clean name that fits is returned unchanged; (2) disallowed chars
    become ``_``; (3) whole filler words go first, last to first (``draft``, ``final``,
    ``copy``, ``version``, ``v2``, plus the counter after them or a trailing one);
    (4) a leading date prefix (``2026_Oct_``, ``2026_10_26_``, ``202610_``) goes before any
    PI/project word; (5) then whole words from the MIDDLE of the stem (first and last words
    stay; after a date drop the first two words, mechanism and PI, stay and project words go
    left to right while more than 3 remain);
    (6) only if that cannot reach *max_len*, or the result is in *taken*, a 6-hex digest
    of the original name is prefixed to as many whole trailing words as fit (a word is
    cut only when the last word alone is too long). Extension kept.

    *drop_date* strips the leading date prefix AND the filler words/counters
    (``draft_1``) unconditionally, even when the name already fits (a submission set
    then shares one pattern); the word-dropping rules then treat the name as dated."""
    import hashlib
    import os
    stem, ext = os.path.splitext(name)
    ext = "." + _SAFE_RE.sub("_", ext.lstrip(".")) if ext else ""
    clean = _SAFE_RE.sub("_", stem)
    cand = clean + ext
    tokens = [t for t in clean.split("_") if t] or ["file"]
    dated = False
    if drop_date:
        k = _date_prefix_len(tokens)
        if k and len(tokens) > k:
            tokens = tokens[k:]
            dated = True
    if not dated and len(cand) <= max_len and clean == stem and cand not in taken:
        return cand

    def fits(toks):
        return len("_".join(toks)) + len(ext) <= max_len

    for group in reversed(_filler_groups(tokens)):
        # drop_date (rename --short-names) drops filler/counters unconditionally too,
        # so names that already fit share the set's pattern
        if fits(tokens) and not drop_date:
            break
        drop = set(group)
        if len(drop) < len(tokens):
            tokens = [t for k, t in enumerate(tokens) if k not in drop]
    if not dated and not fits(tokens):
        k = _date_prefix_len(tokens)
        if k:
            tokens = tokens[k:]
            dated = True
    while not fits(tokens) and len(tokens) > 2:
        if dated and len(tokens) > 3:
            # keep the first two words (mechanism + PI) and the last (component);
            # drop the earliest project words so the distinguishing tail words survive
            del tokens[2]
        else:
            del tokens[len(tokens) // 2]
    cand = "_".join(tokens) + ext
    if fits(tokens) and cand not in taken:
        return cand
    salt = ""
    while True:
        h = hashlib.sha1((name + salt).encode("utf-8")).hexdigest()[:6]
        room = max_len - len(ext) - len(h) - 1
        keep = []
        for t in reversed(tokens):
            if len("_".join([t] + keep)) <= room:
                keep.insert(0, t)
            else:
                break
        if not keep and room > 0:
            keep = [tokens[-1][-room:]]
        tail = "_".join(keep)
        cand = f"{h}_{tail}{ext}" if tail else f"{h}{ext}"
        if cand not in taken:
            return cand
        salt += "+"
