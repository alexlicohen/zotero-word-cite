"""Tests for the pre-delivery validation gate.

The document under test is built inline with ``new_doc`` (no fixture file), so
the suite is fully self-contained.
"""
from __future__ import annotations

from zoterocite.builder import new_doc
from zoterocite.docxio import Docx
from zoterocite.validate import Report, format_report, validate

_PARAS = [
    "Sample Document",
    "This is a generic, self-contained paragraph used only to exercise the "
    "pre-delivery validation gate. It contains well over ten words so the "
    "word-limit warning path can be checked deterministically.",
    "A second body paragraph keeps the accepted view comfortably non-empty.",
]


def _make_doc(tmp_path):
    out = tmp_path / "sample.docx"
    new_doc(out, _PARAS)
    return out


def test_clean_doc_passes(tmp_path):
    r = validate(_make_doc(tmp_path))
    assert isinstance(r, Report)
    assert r.ok is True
    assert r.errors == []
    assert r.info["counts"]["words"] > 0


def test_word_limit_warns_but_passes(tmp_path):
    r = validate(_make_doc(tmp_path), limits={"max_words": 10})
    # warnings must NOT fail the gate
    assert r.ok is True
    assert any("word" in w.lower() for w in r.warnings)


def test_corrupt_document_fails(tmp_path):
    doc = Docx(_make_doc(tmp_path))
    doc.set_part_bytes("word/document.xml", b"<w:bad><unclosed>")
    bad = tmp_path / "bad.docx"
    doc.save(bad)

    r = validate(bad)
    assert r.ok is False
    assert any("document.xml" in e for e in r.errors)


def test_format_report_nonempty(tmp_path):
    good = validate(_make_doc(tmp_path))
    out = format_report(good)
    assert isinstance(out, str) and out.strip()
    assert "PASS" in out

    doc = Docx(_make_doc(tmp_path))
    doc.set_part_bytes("word/document.xml", b"<w:bad><unclosed>")
    bad_path = tmp_path / "bad.docx"
    doc.save(bad_path)
    bad = validate(bad_path)
    bad_out = format_report(bad)
    assert isinstance(bad_out, str) and bad_out.strip()
    assert "FAIL" in bad_out


def test_retraction_screening_runs_offline(tmp_path):
    # validate() screens cited DOIs against the cached Retraction Watch DB
    # (offline only) — the check runs and never breaks the gate on a clean doc.
    r = validate(_make_doc(tmp_path))
    assert "retractions" in r.info["checks"]
    assert r.ok is True


def test_retraction_screening_can_be_disabled(tmp_path):
    r = validate(_make_doc(tmp_path), screen_retractions=False)
    assert "retractions" not in r.info["checks"]
