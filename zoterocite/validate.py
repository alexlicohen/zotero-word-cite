"""Pre-delivery validation gate for grant .docx packages.

Codifies the manual "is this safe to send?" check into a single pass that
returns a structured :class:`Report`. The gate FAILS (``ok=False``) only on
*errors* -- things that would make Word show its "unreadable content / repair"
dialog or that prove the document is empty. *Warnings* are advisory (limits,
naming, leftover template guidance) and never fail the gate.

Errors:
    * malformed XML in any ``.xml``/``.rels`` part (root cause of repair dialogs)
    * empty accepted-view text (round-trip produced nothing)
    * a MEASURED whole-document page count over a configured ``max_pages``. This
      is the one limit that can fail the gate: it is a real, rendered page count
      (via :func:`zoterocite.pagefit.page_count`), and it is how the gate enforces
      a pack's ``max_pages`` / an explicit ``--page-limit`` as a HARD cap. It is an
      ERROR only when a page renderer (LibreOffice) is present to measure it;
      without one the count is unverifiable and degrades to a WARNING (below).

Warnings (advisory — never fail the gate):
    * accepted word count over a configured ``max_words``
    * unverifiable ``max_pages`` (no page renderer installed — count not measurable)
    * per-element word limits, when the section heading can be located
    * per-section ``<name>_max_pages`` (per-section page counts are not measurable)
    * filename not matching a naming convention regex
    * leftover template markers like ``[300 word maximum]``
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from lxml import etree

from .docxio import Docx, DocxLoadError
from .views import counts, read_views

# Parts that must be well-formed XML for Word to open the package cleanly.
_XML_SUFFIXES = (".xml", ".rels")

# Template guidance that should have been removed before delivery, e.g.
# "[300 word maximum]" or "[1,000 words maximum]".
_TEMPLATE_MARKER = re.compile(
    r"\[\s*[\d,]+\s+words?\s+maximum\s*\]", re.IGNORECASE
)


@dataclass
class Report:
    """Outcome of a pre-delivery validation pass.

    Attributes:
        ok: ``True`` iff no errors were recorded (warnings do not affect this).
        errors: blocking problems (each a human-readable string).
        warnings: advisory problems that do not fail the gate.
        info: structured facts gathered during validation (counts, part count,
            which checks ran, etc.).
    """

    ok: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    info: Dict[str, Any] = field(default_factory=dict)


def _is_xml_part(name: str) -> bool:
    """Does this part name need to be well-formed XML?"""
    return name.endswith(_XML_SUFFIXES)


def _check_well_formed(doc: Docx, errors: List[str]) -> int:
    """Parse every XML/rels part; append an ERROR per failure.

    Returns the number of XML parts inspected.
    """
    inspected = 0
    for part in doc.parts():
        if not _is_xml_part(part):
            continue
        inspected += 1
        try:
            etree.fromstring(doc.raw(part))
        except (etree.XMLSyntaxError, DocxLoadError) as exc:
            # Report malformed XML as a finding — never let it become a crash.
            # (We parse raw bytes here, so XMLSyntaxError is the live path;
            # DocxLoadError is caught too in case a part is routed through the
            # guarded Docx parser in future, keeping this the single seam that
            # turns a bad part into a VAL-ERR rather than a traceback.)
            errors.append(f"malformed XML in part {part}: {exc}")
    return inspected


def _find_section_text(accepted: str, heading: str) -> Optional[str]:
    """Return the accepted-view text following ``heading`` up to the next blank
    line / heading-like break, or ``None`` if the heading is not present.

    Headings in the accepted view appear on their own line (paragraphs are
    newline-separated by :func:`zoterocite.views.read_views`). We locate the
    heading line case-insensitively and collect following non-empty lines until
    the next blank line, which approximates a section boundary well enough for
    an advisory word-budget warning.
    """
    lines = accepted.splitlines()
    target = heading.strip().lower()
    for i, line in enumerate(lines):
        if line.strip().lower() == target:
            body: List[str] = []
            for following in lines[i + 1:]:
                if not following.strip():
                    if body:
                        break
                    continue
                body.append(following)
            return "\n".join(body)
    return None


def validate(
    path: str | Path,
    *,
    limits: Optional[Dict[str, Any]] = None,
    naming: Optional[str] = None,
    screen_retractions: bool = True,
) -> Report:
    """Run the pre-delivery gate over a .docx package.

    Args:
        path: path to the .docx to validate.
        limits: optional dict with any of the keys ``max_words`` (int),
            ``max_pages`` (int), ``element_limits`` (dict of heading -> max
            words). These produce WARNINGS, EXCEPT a ``max_pages`` overflow that
            can be MEASURED (a page renderer is installed): that is an ERROR and
            fails the gate (it is how a pack/`--page-limit` page cap is enforced).
            An unverifiable ``max_pages`` (no renderer) degrades to a WARNING.
        naming: optional regex string; a WARNING is raised if the filename does
            not match it.

    Returns:
        A :class:`Report`; ``ok`` is ``False`` iff any error was recorded.
    """
    path = Path(path)
    errors: List[str] = []
    warnings: List[str] = []
    info: Dict[str, Any] = {"path": str(path), "checks": []}

    # 1. Well-formedness ----------------------------------------------------
    info["checks"].append("well_formed")
    try:
        doc = Docx(path)
    except Exception as exc:  # unreadable zip => unrecoverable error
        errors.append(f"cannot open package: {exc}")
        return Report(ok=False, errors=errors, warnings=warnings, info=info)

    info["part_count"] = len(doc.parts())
    info["xml_parts_checked"] = _check_well_formed(doc, errors)

    # 2. Round-trip / non-empty accepted view -------------------------------
    info["checks"].append("round_trip")
    accepted = ""
    try:
        views = read_views(path)
        accepted = views["accepted"]
        info["counts"] = counts(accepted)
    except Exception as exc:
        errors.append(f"round-trip read failed: {exc}")

    if "counts" in info and info["counts"]["chars"] > 0 and not accepted.strip():
        # defensive: chars>0 but only whitespace
        accepted = ""
    if not accepted:
        errors.append("accepted view is empty (round-trip produced no text)")

    # 3. Limits -------------------------------------------------------------
    if limits:
        info["checks"].append("limits")
        word_count = info.get("counts", {}).get("words", 0)

        # Keys consumed by the standard checks below; anything else is flagged.
        _KNOWN_KEYS = {"max_words", "max_pages", "element_limits"}
        # Pattern keys: <name>_max_words / <name>_max_chars / <name>_max_pages
        _PATTERN_KEY = re.compile(
            r"^(?P<section>.+?)_(?P<metric>max_words|max_chars|max_pages)$"
        )

        max_words = limits.get("max_words")
        if max_words is not None and word_count > max_words:
            warnings.append(
                f"accepted view has {word_count} words, exceeds max_words={max_words}"
            )

        max_pages = limits.get("max_pages")
        if max_pages is not None:
            try:
                from .pagefit import page_count  # optional; not bundled here
                pages = page_count(path)
            except ImportError:
                pages = None  # no page renderer in this skill -> degrade gracefully
            if pages is not None:
                info["pages"] = pages
                if pages > max_pages:
                    # ERROR (blocking): a whole-document page overflow is a real,
                    # MEASURED, hard limit — it is the mechanism by which the gate
                    # (gate.run_gate folds these errors into VAL-ERR) enforces a
                    # pack's max_pages and an explicit --page-limit as a HARD cap
                    # that fails the gate. Caveat: this requires a page renderer
                    # (LibreOffice) to be installed; without one page_count returns
                    # None and the overflow can only be WARNED as unverifiable (the
                    # else branch) — you cannot block on a limit you cannot measure.
                    errors.append(
                        f"document is {pages} pages, exceeds max_pages={max_pages}"
                    )
            else:
                warnings.append(
                    f"page count not verifiable (no renderer); "
                    f"~{word_count} words on accepted view"
                )

        element_limits = limits.get("element_limits")
        if isinstance(element_limits, dict):
            for heading, max_w in element_limits.items():
                section = _find_section_text(accepted, heading)
                if section is None:
                    info.setdefault("element_limits_skipped", []).append(heading)
                    continue
                sec_words = counts(section)["words"]
                if max_w is not None and sec_words > max_w:
                    warnings.append(
                        f"section {heading!r} has {sec_words} words, "
                        f"exceeds limit {max_w}"
                    )

        # Pack-specific / named sub-limits: keys matching <name>_max_words etc.
        for key, limit_val in limits.items():
            if key in _KNOWN_KEYS:
                continue
            m = _PATTERN_KEY.match(key)
            if m:
                section_name = m.group("section")
                metric = m.group("metric")
                section_text = _find_section_text(accepted, section_name)
                if section_text is None:
                    warnings.append(
                        f"limit {key!r}={limit_val} declared but section "
                        f"{section_name!r} not found in accepted view — "
                        f"limit is unverifiable"
                    )
                else:
                    c = counts(section_text)
                    if metric == "max_words":
                        measured = c["words"]
                        unit = "words"
                    elif metric == "max_chars":
                        measured = c["chars_no_space"]
                        unit = "chars (no space)"
                    else:  # max_pages — cannot measure per-section pages
                        warnings.append(
                            f"limit {key!r}={limit_val} declared but per-section "
                            f"page counts are not verifiable"
                        )
                        continue
                    if limit_val is not None and measured > limit_val:
                        warnings.append(
                            f"section {section_name!r} has {measured} {unit}, "
                            f"exceeds {key}={limit_val}"
                        )
                    else:
                        info.setdefault("named_limits_checked", []).append(key)
            else:
                # Completely unrecognized key — surface it so it is never silent.
                warnings.append(
                    f"limit {key!r}={limit_val} declared but not checked "
                    f"(unrecognized limit key)"
                )

    # 4. Naming -------------------------------------------------------------
    if naming is not None:
        info["checks"].append("naming")
        if re.search(naming, path.name) is None:
            warnings.append(
                f"filename {path.name!r} does not match naming pattern {naming!r}"
            )

    # 5. Leftover template guidance -----------------------------------------
    info["checks"].append("template_markers")
    markers = _TEMPLATE_MARKER.findall(accepted)
    if markers:
        info["template_markers"] = markers
        warnings.append(
            f"accepted view contains {len(markers)} leftover template marker(s): "
            f"{', '.join(markers[:3])}"
            + ("..." if len(markers) > 3 else "")
        )

    # 6. Retraction screening (OFFLINE; cached Retraction Watch DB only) ------
    if screen_retractions:
        info["checks"].append("retractions")
        try:
            from .citecheck import (
                _extract_cited_dois,
                load_retraction_map,
                check_retractions,
            )
            dois = _extract_cited_dois(path)
            if dois:
                db = load_retraction_map(allow_network=False)
                if db:
                    for f in check_retractions(dois, db):
                        if getattr(f, "severity", "") == "ERROR":
                            errors.append(f.message)
                        else:
                            warnings.append(f.message)
                else:
                    warnings.append(
                        "retraction screening skipped — no cached Retraction "
                        "Watch DB; run `zoterocite cite-check --refresh` once online"
                    )
                    info["retraction_screening"] = "skipped-no-db"
        except Exception as exc:  # never let screening break the gate
            warnings.append(f"retraction screening errored (non-fatal): {exc}")

    return Report(ok=not errors, errors=errors, warnings=warnings, info=info)


def format_report(r: Report) -> str:
    """Render a :class:`Report` as a human-readable multi-line summary."""
    lines: List[str] = []
    status = "PASS" if r.ok else "FAIL"
    lines.append(f"Validation: {status}")

    path = r.info.get("path")
    if path:
        lines.append(f"  file: {path}")

    cnt = r.info.get("counts")
    if cnt:
        lines.append(
            f"  accepted: {cnt['words']} words, {cnt['chars']} chars "
            f"({cnt['chars_no_space']} non-space)"
        )

    if r.info.get("part_count") is not None:
        lines.append(
            f"  parts: {r.info['part_count']} "
            f"({r.info.get('xml_parts_checked', 0)} XML parts checked)"
        )

    if r.info.get("pages") is not None:
        lines.append(f"  pages: {r.info['pages']}")

    if r.info.get("checks"):
        lines.append(f"  checks run: {', '.join(r.info['checks'])}")

    if r.errors:
        lines.append(f"  errors ({len(r.errors)}):")
        lines.extend(f"    - {e}" for e in r.errors)
    else:
        lines.append("  errors: none")

    if r.warnings:
        lines.append(f"  warnings ({len(r.warnings)}):")
        lines.extend(f"    - {w}" for w in r.warnings)
    else:
        lines.append("  warnings: none")

    return "\n".join(lines)
