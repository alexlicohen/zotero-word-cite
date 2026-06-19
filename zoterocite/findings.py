"""Shared Finding dataclass and formatting helpers for zoterocite review."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Finding:
    """A single lint/review finding.

    Attributes:
        check: identifier (e.g. "A1", "F2").
        severity: one of ERROR, WARN, INFO.
        message: human-readable description.
        location: optional string like "para 3" or "page 2, char 45".
        source: citation (NIH doc, spec section, etc.).
    """

    check: str
    severity: str  # ERROR | WARN | INFO
    message: str
    location: Optional[str] = None
    source: str = ""

    def __post_init__(self):
        if self.severity not in ("ERROR", "WARN", "INFO"):
            raise ValueError(f"Invalid severity: {self.severity!r}")


def format_findings(findings: List[Finding]) -> str:
    """Render findings grouped by severity as a human-readable string."""
    if not findings:
        return "review: no findings\n"

    groups: Dict[str, List[Finding]] = {"ERROR": [], "WARN": [], "INFO": []}
    for f in findings:
        groups.setdefault(f.severity, []).append(f)

    lines: List[str] = []
    for sev in ("ERROR", "WARN", "INFO"):
        bucket = groups.get(sev, [])
        if not bucket:
            continue
        lines.append(f"--- {sev} ({len(bucket)}) ---")
        for f in bucket:
            loc = f"  [{f.location}]" if f.location else ""
            src = f"  ({f.source})" if f.source else ""
            lines.append(f"  [{f.check}]{loc} {f.message}{src}")
    return "\n".join(lines) + "\n"


def findings_to_dicts(findings: List[Finding]) -> List[Dict[str, Any]]:
    """Serialize findings to a list of plain dicts for --json output."""
    out = []
    for f in findings:
        d: Dict[str, Any] = {
            "check": f.check,
            "severity": f.severity,
            "message": f.message,
        }
        if f.location is not None:
            d["location"] = f.location
        if f.source:
            d["source"] = f.source
        out.append(d)
    return out
