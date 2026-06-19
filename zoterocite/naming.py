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


def rename_to_convention(path: str | Path, *, apply: bool = False, **fields) -> Path:
    path = Path(path)
    fields.setdefault("ext", path.suffix or ".docx")
    new = path.with_name(convention_name(**fields))
    if apply and new != path:
        path.rename(new)
    return new
