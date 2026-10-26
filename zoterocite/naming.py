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
        name = short_name(name)
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


def short_name(name: str, *, max_len: int = MAX_NAME_LEN, taken=()) -> str:
    """Deterministically shorten *name* to <= *max_len* chars incl. extension.

    Keeps the extension and the END of the stem (the distinguishing suffix: doctype,
    draft number), disallowed characters become ``_``, and a 6-hex digest of the full
    original name is spliced in so two long names never collapse onto one. A name that
    already fits and is clean is returned unchanged. Names in *taken* are avoided
    (a further digest round, deterministic)."""
    import hashlib
    import os
    stem, ext = os.path.splitext(name)
    ext = "." + _SAFE_RE.sub("_", ext.lstrip(".")) if ext else ""
    clean = _SAFE_RE.sub("_", stem)
    cand = clean + ext
    if len(cand) <= max_len and clean == stem and cand not in taken:
        return cand
    salt = ""
    while True:
        h = hashlib.sha1((name + salt).encode("utf-8")).hexdigest()[:6]
        room = max_len - len(ext) - len(h) - 1
        tail = clean[-room:] if room > 0 else ""
        cand = f"{h}_{tail}{ext}" if tail else f"{h}{ext}"
        if cand not in taken:
            return cand
        salt += "+"
