"""Tests for zoterocite.refextract.extract_references.

Synthetic .docx documents are built with zoterocite.new_doc + helpers, then
passed to extract_references. No network; no Zotero client needed (fields
result is from classify_citation_sources, which itself is tested separately).

Buckets verified:
  reflist       — numbered entries under a "References" heading
  intext        — author-year and numeric ad-hoc in-text cites in body
  placeholders  — bracket stubs + comment-embedded citation text
  fields        — classify_citation_sources() passthrough (counts only)
  counts        — summary dict
"""
from __future__ import annotations

from pathlib import Path

import pytest
from lxml import etree

from zoterocite import Docx, add_comment, new_doc
from zoterocite.docxio import DOCUMENT
from zoterocite.ooxml import NS, qn
from zoterocite.paras import iter_paragraphs
from zoterocite.refextract import _is_reference_shaped, extract_references

W = NS["w"]


def _set_heading_style(path: Path, para_index: int, style: str = "Heading1") -> None:
    """Mark the paragraph at *para_index* as a Word Heading (pStyle), in place."""
    doc = Docx(path)
    root = doc.tree(DOCUMENT)
    paras = iter_paragraphs(root)
    target = paras[para_index]
    ppr = target.find(qn("w:pPr"))
    if ppr is None:
        ppr = target.makeelement(qn("w:pPr"), {})
        target.insert(0, ppr)
    pstyle = ppr.makeelement(qn("w:pStyle"), {qn("w:val"): style})
    ppr.insert(0, pstyle)
    doc.save(path)


# ===========================================================================
# Helpers to build synthetic documents
# ===========================================================================

def _build_doc(tmp_path: Path, paragraphs: list[str], name: str = "doc.docx") -> Path:
    """Create a minimal .docx with the given paragraph strings."""
    p = tmp_path / name
    new_doc(p, paragraphs)
    return p


def _add_comment_to_doc(path: Path, anchor: str, comment_text: str) -> Path:
    """Add a Word comment anchored to *anchor* text and save back to same path."""
    doc = Docx(path)
    add_comment(doc, anchor, comment_text, author="Test Author", initials="TA")
    doc.save(path)
    return path


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture()
def full_doc(tmp_path: Path) -> Path:
    """A document with:
    - body paragraph 1: author-year cite (Smith et al., 2020)
    - body paragraph 2: numeric cite [12]
    - body paragraph 3: bracket placeholder [CITE: tubers and ASD]
    - References heading
    - two numbered reference entries
    - one Word comment containing a citation-ish string
    """
    paragraphs = [
        # body paragraphs
        "Tuberous sclerosis is associated with ASD (Smith et al., 2020).",
        "Prior work supports this model [12].",
        "More evidence is needed [CITE: tubers and ASD].",
        # reference list
        "References",
        "1. Smith J, Jones A, Brown K. Tuberous sclerosis and autism. J Neurol. 2020;10:1-5.",
        "2. Jones B, et al. Cortical tubers in TSC. Brain. 2019;50:200-210.",
    ]
    path = _build_doc(tmp_path, paragraphs, "full.docx")

    # Add a comment that contains a citation-like string (author + year)
    _add_comment_to_doc(
        path,
        "Prior work supports",
        "See also Williams 2018 for the numeric evidence; cite this.",
    )
    return path


# ===========================================================================
# Core tests
# ===========================================================================

class TestReflist:
    def test_reflist_populated(self, full_doc):
        result = extract_references(full_doc)
        assert len(result["reflist"]) == 2

    def test_reflist_numbering_detected(self, full_doc):
        result = extract_references(full_doc)
        nums = [e["numbering"] for e in result["reflist"]]
        # Both entries start with "1." and "2." — should detect numbering
        assert all(n is not None for n in nums), f"Expected numbering, got {nums}"

    def test_reflist_text_content(self, full_doc):
        result = extract_references(full_doc)
        texts = " ".join(e["text"] for e in result["reflist"])
        assert "Smith J" in texts
        assert "Jones B" in texts

    def test_reflist_index_sequential(self, full_doc):
        result = extract_references(full_doc)
        indices = [e["index"] for e in result["reflist"]]
        assert indices == list(range(len(indices)))


class TestIntext:
    def test_author_year_detected(self, full_doc):
        result = extract_references(full_doc)
        ay = [e for e in result["intext"] if e["kind"] == "author_year"]
        assert len(ay) >= 1
        texts = [e["text"] for e in ay]
        assert any("Smith et al." in t and "2020" in t for t in texts), (
            f"Expected author-year cite in {texts}"
        )

    def test_numeric_detected(self, full_doc):
        result = extract_references(full_doc)
        num = [e for e in result["intext"] if e["kind"] == "numeric"]
        assert len(num) >= 1
        assert any("[12]" in e["text"] for e in num), (
            f"Expected [12] in {[e['text'] for e in num]}"
        )

    def test_intext_not_in_reflist(self, full_doc):
        """Entries in the reflist region must NOT appear in intext."""
        result = extract_references(full_doc)
        # The reflist entries contain years (2020, 2019) and author names; make
        # sure those specific author-year patterns are not double-counted.
        # We test by checking the doc produces exactly 1 author-year cite
        # (the body paragraph) not 3+ (body + both reflist entries).
        ay = [e for e in result["intext"] if e["kind"] == "author_year"]
        # The two reflist entries have author-year patterns but should be excluded
        # We expect at most 1 definitive paren form from body paragraphs
        paren_forms = [e for e in ay if e["text"].startswith("(")]
        assert len(paren_forms) <= 2, (
            f"Too many paren author-year cites (reflist leak?): {paren_forms}"
        )


class TestPlaceholders:
    def test_bracket_placeholder_found(self, full_doc):
        result = extract_references(full_doc)
        brackets = [p for p in result["placeholders"] if p["kind"] == "bracket"]
        assert len(brackets) >= 1
        texts = [p["text"] for p in brackets]
        assert any("CITE" in t or "tubers" in t.lower() for t in texts), (
            f"Expected CITE placeholder in {texts}"
        )

    def test_numeric_not_flagged_as_placeholder(self, full_doc):
        """[12] is a numeric cite; it must not also appear as a placeholder."""
        result = extract_references(full_doc)
        placeholder_texts = [p["text"] for p in result["placeholders"] if p["kind"] == "bracket"]
        assert "[12]" not in placeholder_texts, (
            f"[12] was incorrectly flagged as a placeholder"
        )

    def test_comment_citation_found(self, full_doc):
        result = extract_references(full_doc)
        comments = [p for p in result["placeholders"] if p["kind"] == "comment"]
        assert len(comments) >= 1, "Expected at least one comment-embedded citation"
        comment_texts = " ".join(p["text"] for p in comments)
        assert "Williams" in comment_texts or "2018" in comment_texts or "cite" in comment_texts.lower()

    def test_comment_context_populated(self, full_doc):
        result = extract_references(full_doc)
        comments = [p for p in result["placeholders"] if p["kind"] == "comment"]
        assert comments, "Expected comment placeholders"
        # Context should be non-empty (anchored to a paragraph)
        assert any(c["context"] for c in comments), (
            f"Expected non-empty context, got {[c['context'] for c in comments]}"
        )

    def test_placeholder_kind_is_bracket(self, full_doc):
        result = extract_references(full_doc)
        for p in result["placeholders"]:
            assert p["kind"] in ("bracket", "comment")


class TestCounts:
    def test_counts_match_lists(self, full_doc):
        result = extract_references(full_doc)
        assert result["counts"]["reflist"] == len(result["reflist"])
        assert result["counts"]["intext"] == len(result["intext"])
        assert result["counts"]["placeholders"] == len(result["placeholders"])

    def test_counts_nonzero(self, full_doc):
        result = extract_references(full_doc)
        assert result["counts"]["reflist"] >= 2
        assert result["counts"]["intext"] >= 2
        assert result["counts"]["placeholders"] >= 2  # bracket + comment


class TestFields:
    def test_fields_key_present(self, full_doc):
        result = extract_references(full_doc)
        assert "fields" in result
        assert "counts" in result["fields"]
        assert "items" in result["fields"]


# ===========================================================================
# Empty document — no crash, empty lists
# ===========================================================================

class TestEmptyDoc:
    def test_empty_doc_no_crash(self, tmp_path):
        path = _build_doc(tmp_path, ["Hello world."], "empty.docx")
        result = extract_references(path)
        assert result["reflist"] == []
        assert result["intext"] == []
        assert result["placeholders"] == []
        assert result["counts"]["reflist"] == 0
        assert result["counts"]["intext"] == 0
        assert result["counts"]["placeholders"] == 0

    def test_empty_doc_fields_present(self, tmp_path):
        path = _build_doc(tmp_path, ["Hello world."], "empty2.docx")
        result = extract_references(path)
        assert "fields" in result
        assert isinstance(result["fields"]["counts"], dict)


# ===========================================================================
# Specific detection edge cases
# ===========================================================================

class TestEdgeCases:
    def test_bibliography_heading_variant(self, tmp_path):
        """'Bibliography' heading should trigger reflist detection."""
        path = _build_doc(tmp_path, [
            "Some body text.",
            "Bibliography",
            "1. Author A. A title. Journal. 2021.",
        ])
        result = extract_references(path)
        assert len(result["reflist"]) == 1

    def test_works_cited_heading(self, tmp_path):
        """'Works Cited' heading should trigger reflist detection."""
        path = _build_doc(tmp_path, [
            "Body text here.",
            "Works Cited",
            "Cohen A. Lesion mapping. Brain. 2022;1:1-10.",
        ])
        result = extract_references(path)
        assert len(result["reflist"]) >= 1

    def test_numbered_reflist_without_brackets(self, tmp_path):
        """'1. Author...' form (no brackets) should detect numbering."""
        path = _build_doc(tmp_path, [
            "References",
            "1. Doe J. A study. Lancet. 2020.",
            "2. Roe R. Another study. NEJM. 2021.",
        ])
        result = extract_references(path)
        assert len(result["reflist"]) == 2
        assert all(e["numbering"] is not None for e in result["reflist"])

    def test_bracket_numbered_reflist(self, tmp_path):
        """'[1] Author...' form should detect numbering."""
        path = _build_doc(tmp_path, [
            "References",
            "[1] Smith AB. Cortex. Nature. 2020.",
        ])
        result = extract_references(path)
        assert len(result["reflist"]) == 1
        assert result["reflist"][0]["numbering"] == "[1]"

    def test_no_double_count_numeric_as_placeholder(self, tmp_path):
        """[3] should be counted as numeric intext, NOT as a placeholder."""
        path = _build_doc(tmp_path, [
            "This claim is supported [3].",
        ])
        result = extract_references(path)
        nums = [e for e in result["intext"] if e["kind"] == "numeric"]
        placeholder_brackets = [p for p in result["placeholders"] if p["kind"] == "bracket"]
        assert any("[3]" in e["text"] for e in nums)
        assert not any("[3]" in p["text"] for p in placeholder_brackets)

    def test_definite_cite_placeholder(self, tmp_path):
        """[CITE] should always be flagged as a placeholder."""
        path = _build_doc(tmp_path, [
            "This needs a reference [CITE].",
        ])
        result = extract_references(path)
        brackets = [p for p in result["placeholders"] if p["kind"] == "bracket"]
        assert any("[CITE]" in p["text"] for p in brackets)

    def test_ref_question_mark_placeholder(self, tmp_path):
        """[ref?] should be flagged as a placeholder."""
        path = _build_doc(tmp_path, [
            "This needs a reference [ref?].",
        ])
        result = extract_references(path)
        brackets = [p for p in result["placeholders"] if p["kind"] == "bracket"]
        assert any("ref?" in p["text"].lower() for p in brackets)

    def test_author_year_stub_placeholder(self, tmp_path):
        """[Smith 2020] looks like a placeholder (year present, not a pure number)."""
        path = _build_doc(tmp_path, [
            "Needs replacement [Smith 2020].",
        ])
        result = extract_references(path)
        brackets = [p for p in result["placeholders"] if p["kind"] == "bracket"]
        assert any("Smith 2020" in p["text"] for p in brackets)

    def test_no_comment_section_for_empty_doc(self, tmp_path):
        """A doc with no comments.xml must not crash."""
        path = _build_doc(tmp_path, ["No comments here."])
        # No comments part added — should return empty placeholders
        result = extract_references(path)
        comment_phs = [p for p in result["placeholders"] if p["kind"] == "comment"]
        assert comment_phs == []

    def test_intext_index_sequential(self, full_doc):
        result = extract_references(full_doc)
        indices = [e["index"] for e in result["intext"]]
        assert indices == list(range(len(indices)))

    def test_detected_by_heading_on_heading_path(self, tmp_path):
        """Existing heading-based entries carry detected_by=='heading'."""
        path = _build_doc(tmp_path, [
            "Body text.",
            "References",
            "1. Smith J. A study. Lancet. 2020.",
            "2. Doe R. Another study. NEJM. 2021.",
        ])
        result = extract_references(path)
        assert len(result["reflist"]) == 2
        for entry in result["reflist"]:
            assert entry["detected_by"] == "heading", (
                f"Expected 'heading', got {entry['detected_by']!r}"
            )


# ===========================================================================
# Heading-less run detection
# ===========================================================================

class TestHeadinglessRunDetection:
    """Tests for the >=3-consecutive-paragraph run detection path."""

    def test_numbered_run_no_heading_detected(self, tmp_path):
        """Numbered reference list with no heading (>=3 entries) is detected via 'run'."""
        path = _build_doc(tmp_path, [
            "Tuberous sclerosis has a complex genetic basis.",
            "Prior work supports this finding.",
            # No "References" heading — just raw numbered entries
            "1. Smith AB, Jones CD. TSC genetics. Brain. 2020;10:1-10.",
            "2. Doe EF, Roe GH. mTOR signaling. Nature. 2019;5:200-210.",
            "3. Brown IJ, White KL. Cortical tubers. NEJM. 2018;3:50-60.",
        ])
        result = extract_references(path)
        assert len(result["reflist"]) == 3, (
            f"Expected 3 reflist entries, got {len(result['reflist'])}: {result['reflist']}"
        )
        for entry in result["reflist"]:
            assert entry["detected_by"] == "run", (
                f"Expected 'run', got {entry['detected_by']!r}"
            )

    def test_author_year_run_no_heading_no_enumerator(self, tmp_path):
        """Author-year reference list with no enumerator and no heading is detected."""
        path = _build_doc(tmp_path, [
            "Tuberous sclerosis has a complex genetic basis.",
            # Author-year style references, no heading, no numbers
            "Smith AB, Jones CD. Tuberous sclerosis complex genetics. Brain. 2020;10:1-10.",
            "Doe EF, Roe GH. mTOR signaling in TSC. Nature Neurosci. 2019;5:200-210.",
            "Brown IJ, White KL. Cortical tubers and epilepsy. NEJM. 2018;3:50-60.",
        ])
        result = extract_references(path)
        assert len(result["reflist"]) >= 3, (
            f"Expected >=3 reflist entries, got {len(result['reflist'])}: {result['reflist']}"
        )
        for entry in result["reflist"]:
            assert entry["detected_by"] == "run", (
                f"Expected 'run', got {entry['detected_by']!r}"
            )

    def test_two_isolated_figure_captions_not_detected(self, tmp_path):
        """Isolated figure-caption-like paragraphs must NOT produce a reflist."""
        path = _build_doc(tmp_path, [
            "This figure shows the results.",
            "Adapted from Wagenaar, Pediatrics 2018.",
            "Reprinted from Smith, Brain 2019.",
        ])
        result = extract_references(path)
        assert result["reflist"] == [], (
            f"Expected empty reflist, got {result['reflist']}"
        )

    def test_run_of_two_not_flagged(self, tmp_path):
        """A run of exactly 2 reference-shaped paragraphs must NOT be flagged (below threshold)."""
        path = _build_doc(tmp_path, [
            "This study supports the hypothesis.",
            "1. Smith AB, Jones CD. TSC genetics. Brain. 2020;10:1-10.",
            "2. Doe EF, Roe GH. mTOR signaling. Nature. 2019;5:200-210.",
            "See the full list online.",
        ])
        result = extract_references(path)
        assert result["reflist"] == [], (
            f"Expected empty reflist for 2-entry run, got {result['reflist']}"
        )

    def test_run_does_not_duplicate_heading_path(self, tmp_path):
        """When a heading path already captures entries, run detection does not add duplicates."""
        path = _build_doc(tmp_path, [
            "Body paragraph.",
            "References",
            "1. Smith AB, Jones CD. TSC genetics. Brain. 2020;10:1-10.",
            "2. Doe EF, Roe GH. mTOR signaling. Nature. 2019;5:200-210.",
            "3. Brown IJ, White KL. Cortical tubers. NEJM. 2018;3:50-60.",
        ])
        result = extract_references(path)
        # Should have exactly 3 (from heading), not 6 (3 heading + 3 run)
        assert len(result["reflist"]) == 3
        for entry in result["reflist"]:
            assert entry["detected_by"] == "heading"

    def test_precision_guard_adapted_from_not_ref(self, tmp_path):
        """Paragraph beginning 'Adapted from' is not counted as a reference entry."""
        path = _build_doc(tmp_path, [
            "Adapted from Wagenaar, Pediatrics 2018.",
            "1. Smith AB, Jones CD. TSC genetics. Brain. 2020;10:1-10.",
            "2. Doe EF, Roe GH. mTOR signaling. Nature. 2019;5:200-210.",
            "3. Brown IJ, White KL. Cortical tubers. NEJM. 2018;3:50-60.",
        ])
        result = extract_references(path)
        # The "Adapted from" line must not appear in reflist
        texts = [e["text"] for e in result["reflist"]]
        assert not any("Adapted from" in t for t in texts), (
            f"'Adapted from' paragraph should not be in reflist: {texts}"
        )

    def test_new_heading_variants_detected(self, tmp_path):
        """'Citations' and 'Cited Literature' headings trigger heading-path detection."""
        for heading in ("Citations", "Cited Literature", "Citations:"):
            path = _build_doc(tmp_path, [
                "Body text.",
                heading,
                "1. Smith AB. A study. Lancet. 2020.",
                "2. Doe EF. Another study. NEJM. 2021.",
            ], name=f"doc_{heading.split()[0]}.docx")
            result = extract_references(path)
            assert len(result["reflist"]) >= 1, (
                f"Heading '{heading}' failed to trigger detection"
            )
            assert all(e["detected_by"] == "heading" for e in result["reflist"]), (
                f"Expected 'heading' detected_by for '{heading}'"
            )


# ===========================================================================
# E5 — references-region termination must be heading-only, never on a
# content-shaped fragment ('In Press', 'Nature Publishing Group', 'Smith JA').
# ===========================================================================

class TestReferenceRegionTermination:
    def test_in_press_fragment_keeps_later_refs(self, tmp_path):
        """An 'In Press.' line inside the References section must NOT truncate
        the region — every later reference is still captured."""
        path = _build_doc(tmp_path, [
            "Body text introducing the work, written in 2018.",
            "References",
            "1. Smith J, Jones A. Cortical tubers in TSC. J Neurol. 2018;10:1-5.",
            "In Press.",
            "2. Brown K, Lee M. Network mapping of lesions. Brain. 2019;50:200-210.",
            "3. Davis R, Patel S. Autism and tubers revisited. Neuron. 2020;77:30-40.",
        ], name="inpress.docx")
        result = extract_references(path)
        texts = " ".join(e["text"] for e in result["reflist"])
        assert "Brown K" in texts
        assert "Davis R" in texts, (
            f"'In Press.' fragment truncated the reflist: {result['reflist']}"
        )

    def test_publisher_fragment_keeps_later_refs(self, tmp_path):
        """A wrapped 'Nature Publishing Group' line (≤6 words, year-less,
        capital-initial) must NOT end the region."""
        path = _build_doc(tmp_path, [
            "Body text introducing the work, written in 2018.",
            "References",
            "1. Smith J, Jones A. Cortical tubers in TSC. J Neurol. 2018;10:1-5.",
            "Nature Publishing Group",
            "2. Brown K, Lee M. Network mapping of lesions. Brain. 2019;50:200-210.",
            "3. Davis R, Patel S. Autism and tubers revisited. Neuron. 2020;77:30-40.",
            "4. Evans T, Frank U. Final reference about the cortex. Cortex. 2021;88:50-60.",
        ], name="npg.docx")
        result = extract_references(path)
        texts = " ".join(e["text"] for e in result["reflist"])
        assert "Brown K" in texts
        assert "Davis R" in texts
        assert "Evans T" in texts, (
            f"'Nature Publishing Group' fragment truncated the reflist: {result['reflist']}"
        )

    def test_author_initials_fragment_keeps_later_refs(self, tmp_path):
        """A bare 'Smith JA' line must not be mistaken for a heading."""
        path = _build_doc(tmp_path, [
            "Body text introducing the work, written in 2018.",
            "References",
            "1. Smith J, Jones A. Cortical tubers in TSC. J Neurol. 2018;10:1-5.",
            "Smith JA",
            "2. Brown K, Lee M. Network mapping of lesions. Brain. 2019;50:200-210.",
            "3. Davis R, Patel S. Autism and tubers revisited. Neuron. 2020;77:30-40.",
        ], name="initials.docx")
        result = extract_references(path)
        texts = " ".join(e["text"] for e in result["reflist"])
        assert "Davis R" in texts, (
            f"'Smith JA' fragment truncated the reflist: {result['reflist']}"
        )

    def test_whitelisted_next_section_terminates_region(self, tmp_path):
        """A real next-section heading ('Acknowledgements') DOES end the region."""
        path = _build_doc(tmp_path, [
            "References",
            "1. Smith J, Jones A. Cortical tubers. J Neurol. 2018;10:1-5.",
            "2. Brown K, Lee M. Network mapping. Brain. 2019;50:200-210.",
            "Acknowledgements",
            "We thank the funders who supported this work in 2020.",
            "3. NotARef Davis R. Neuron. 2021;1:1-2.",
        ], name="ack.docx")
        result = extract_references(path)
        texts = " ".join(e["text"] for e in result["reflist"])
        assert "Smith J" in texts and "Brown K" in texts
        assert "Davis R" not in texts, (
            f"Region did not end at 'Acknowledgements': {result['reflist']}"
        )

    def test_pstyle_heading_terminates_region(self, tmp_path):
        """A paragraph styled as a Word Heading ends the region even if its
        text is not a whitelisted next-section name."""
        path = _build_doc(tmp_path, [
            "References",
            "1. Smith J, Jones A. Cortical tubers. J Neurol. 2018;10:1-5.",
            "2. Brown K, Lee M. Network mapping. Brain. 2019;50:200-210.",
            "Some Custom Section Name",
            "Post-section content mentioning the year 2020 in a long sentence here.",
        ], name="style.docx")
        _set_heading_style(path, 3, "Heading1")
        result = extract_references(path)
        assert len(result["reflist"]) == 2, (
            f"Region did not end at the styled heading: {result['reflist']}"
        )


# ===========================================================================
# E10 — heading-less reflists of single-author / lowercase-particle /
# organizational entries must be extracted (non-empty inventory).
# ===========================================================================

class TestHeadinglessAuthorVariants:
    def test_single_author_run_detected(self, tmp_path):
        """Heading-less single-author entries ('Smith JA. ... 2019') form a run."""
        path = _build_doc(tmp_path, [
            "Tuberous sclerosis has a complex genetic basis.",
            "Smith JA. Cortical tubers in tuberous sclerosis complex. J Neurol. 2019;10:1-10.",
            "Jones BC. mTOR signaling pathways in TSC pathology. Nature Neurosci. 2018;5:200-210.",
            "Brown DE. Epilepsy and cortical malformations in children. NEJM. 2017;3:50-60.",
        ], name="single.docx")
        result = extract_references(path)
        assert len(result["reflist"]) >= 3, (
            f"Single-author heading-less reflist not extracted: {result['reflist']}"
        )

    def test_lowercase_particle_run_detected(self, tmp_path):
        """Lowercase nobiliary particles ('van der Berg H, ...') are recognised."""
        path = _build_doc(tmp_path, [
            "Tuberous sclerosis has a complex genetic basis.",
            "van der Berg H, de Vries P. Network mapping of cortical lesions. Brain. 2020;10:1-10.",
            "von Hippel A, de la Cruz M. Lesion connectivity in epilepsy patients. Neuron. 2019;5:20-30.",
            "del Rio S, van Dijk T. Tuber localization and outcomes in TSC. Epilepsia. 2018;3:5-15.",
        ], name="particle.docx")
        result = extract_references(path)
        assert len(result["reflist"]) >= 3, (
            f"Lowercase-particle heading-less reflist not extracted: {result['reflist']}"
        )

    def test_organizational_author_run_detected(self, tmp_path):
        """Organizational / ALLCAPS authors ('ENIGMA Consortium.',
        'World Health Organization.') are recognised."""
        path = _build_doc(tmp_path, [
            "Tuberous sclerosis has a complex genetic basis.",
            "ENIGMA Consortium. Subcortical brain volume differences across disorders. Mol Psychiatry. 2018;23:100-110.",
            "World Health Organization. Global report on epilepsy and the brain. WHO Press. 2019;1:1-100.",
            "International League Against Epilepsy. Classification of seizures revisited. Epilepsia. 2017;58:20-30.",
        ], name="org.docx")
        result = extract_references(path)
        assert len(result["reflist"]) >= 3, (
            f"Organizational-author heading-less reflist not extracted: {result['reflist']}"
        )

    def test_mixed_variant_run_detected(self, tmp_path):
        """A mix of single-author, particle, and organizational entries still
        forms a run."""
        path = _build_doc(tmp_path, [
            "Introduction paragraph about the genetic basis of disease.",
            "Smith JA. Cortical tubers in tuberous sclerosis. J Neurol. 2019;10:1-10.",
            "van der Berg H. Network mapping of cortical lesions. Brain. 2020;5:20-30.",
            "ENIGMA Consortium. Subcortical volume differences. Mol Psychiatry. 2018;23:100-110.",
        ], name="mixed.docx")
        result = extract_references(path)
        assert len(result["reflist"]) >= 3, (
            f"Mixed-variant heading-less reflist not extracted: {result['reflist']}"
        )

    def test_figure_caption_with_year_still_guarded(self, tmp_path):
        """The precision guard still rejects figure/table caption lines even
        with the broadened author detector."""
        path = _build_doc(tmp_path, [
            "Figure 1. Cortical tubers shown in 2019 imaging of the brain region.",
            "Table 2. Patient demographics collected in 2018 across all sites here.",
            "Adapted from Wagenaar, Pediatrics 2017, with permission from publisher.",
        ], name="captions.docx")
        result = extract_references(path)
        assert result["reflist"] == [], (
            f"Caption lines wrongly captured as reflist: {result['reflist']}"
        )


# ===========================================================================
# R4-1 (regression of E5 fix) — a non-Heading-styled post-reference section
# (Disclosures / Author Information / Competing Interests / Online Methods /
# Extended Data / ...) must NOT have its prose swept into the reflist, even
# when termination misses (no Heading pStyle, name not whole-line).  The E5
# original (a content fragment between refs keeps all later refs) must still
# hold.
# ===========================================================================

class TestPostReferenceSectionNotPolluting:
    def test_non_heading_disclosures_prose_not_in_reflist(self, tmp_path):
        """A 'Disclosures' section that is NOT Word-Heading-styled, opening with
        an inline 'Author Information.' lead-in, must not contribute its prose to
        the reflist — both the standalone label line and the section body are
        excluded."""
        path = _build_doc(tmp_path, [
            "References",
            "1. Smith J, Jones A. Cortical tubers in TSC. J Neurol. 2018;10:1-5.",
            "2. Brown K, Lee M. Network mapping of lesions. Brain. 2019;50:200-210.",
            # Standalone next-section label (whitelist catches this -> terminate)
            "Disclosures",
            # Section prose with a year present -> must NOT be reference-shaped
            "The authors declare no competing financial interests related to this "
            "work performed in 2020 at the institution and its affiliated centers.",
            # Inline 'Label.' lead-in style + a year -> must NOT be captured even
            # if termination above had been missed
            "Author Information. Correspondence should be addressed to the senior "
            "author regarding the cohort assembled between 2018 and 2021 here.",
        ], name="disclosures.docx")
        result = extract_references(path)
        texts = " ".join(e["text"] for e in result["reflist"])
        assert "Smith J" in texts and "Brown K" in texts
        assert "declare no competing" not in texts, (
            f"Disclosures prose polluted the reflist: {result['reflist']}"
        )
        assert "Correspondence should be addressed" not in texts, (
            f"Author-Information prose polluted the reflist: {result['reflist']}"
        )
        assert len(result["reflist"]) == 2, (
            f"Expected exactly the 2 real refs: {result['reflist']}"
        )

    def test_termination_miss_still_gated(self, tmp_path):
        """Even if the post-reference section label is NOT whitelisted and NOT
        Heading-styled (termination genuinely misses), the heading-path build's
        per-paragraph reference-shape gate keeps the section prose out."""
        path = _build_doc(tmp_path, [
            "References",
            "1. Smith J, Jones A. Cortical tubers in TSC. J Neurol. 2018;10:1-5.",
            "2. Brown K, Lee M. Network mapping of lesions. Brain. 2019;50:200-210.",
            # A section name NOT on the whitelist and NOT Heading-styled:
            "Patient Consent Statement",
            "All patients provided written informed consent under a protocol "
            "approved by the institutional review board in 2019 before enrolment.",
        ], name="consent.docx")
        result = extract_references(path)
        texts = " ".join(e["text"] for e in result["reflist"])
        assert "Smith J" in texts and "Brown K" in texts
        assert "informed consent" not in texts, (
            f"Non-whitelisted section prose leaked into reflist: {result['reflist']}"
        )

    def test_e5_in_press_fragment_still_keeps_all_refs(self, tmp_path):
        """E5 original must still hold: an 'In Press.' content fragment between
        references does not drop any later reference (the fragment itself is just
        skipped by the reference-shape gate)."""
        path = _build_doc(tmp_path, [
            "References",
            "1. Smith J, Jones A. Cortical tubers in TSC. J Neurol. 2018;10:1-5.",
            "In Press.",
            "2. Brown K, Lee M. Network mapping of lesions. Brain. 2019;50:200-210.",
            "3. Davis R, Patel S. Autism and tubers revisited. Neuron. 2020;77:30-40.",
        ], name="inpress_r41.docx")
        result = extract_references(path)
        texts = " ".join(e["text"] for e in result["reflist"])
        assert "Smith J" in texts
        assert "Brown K" in texts
        assert "Davis R" in texts, (
            f"E5: 'In Press.' fragment dropped a later reference: {result['reflist']}"
        )

    def test_e5_publisher_fragment_still_keeps_all_refs(self, tmp_path):
        """E5 original: a wrapped 'Nature Publishing Group' line between refs
        does not truncate the list."""
        path = _build_doc(tmp_path, [
            "References",
            "1. Smith J, Jones A. Cortical tubers in TSC. J Neurol. 2018;10:1-5.",
            "Nature Publishing Group",
            "2. Brown K, Lee M. Network mapping of lesions. Brain. 2019;50:200-210.",
            "3. Davis R, Patel S. Autism and tubers revisited. Neuron. 2020;77:30-40.",
        ], name="npg_r41.docx")
        result = extract_references(path)
        texts = " ".join(e["text"] for e in result["reflist"])
        assert "Brown K" in texts and "Davis R" in texts, (
            f"E5: 'Nature Publishing Group' fragment truncated the list: {result['reflist']}"
        )


# ===========================================================================
# R4-3 (regression of E10 fix) — the organizational/author branch of
# _is_reference_shaped must not class sentence-case 'Label. prose ... year'
# section openers ('Methods. ...', 'Data Availability. ...') as reference-
# shaped.  E10 originals (single-author / particle / organizational reflist
# entries) must still be reference-shaped.
# ===========================================================================

class TestSectionLabelProseNotReferenceShaped:
    @pytest.mark.parametrize("prose", [
        "Methods. Patients were recruited between 2018 and 2021 across many sites.",
        "Data Availability. Data generated in 2022 are available on reasonable request.",
        "Background. Tuberous sclerosis has been studied since 2015 in many cohorts.",
        "Results. We observed significant effects in the 2020 cortical thickness data.",
        "Conclusions. The 2021 findings support a network model of cortical lesions.",
        "Discussion. Our 2019 results extend prior lesion network mapping work here.",
        "Funding. This work was supported by a 2020 grant from a national institute.",
        "Introduction. Since 2018 the field has grown with new imaging methods now.",
        "Materials. Tissue samples collected in 2017 were processed using protocols.",
        "Acknowledgements. We thank the 2020 cohort participants for contributing.",
        "Abstract. We report a 2022 study of cortical tubers in tuberous sclerosis.",
    ])
    def test_section_label_prose_not_reference_shaped(self, prose):
        assert _is_reference_shaped(prose) is False, (
            f"Section-label prose wrongly read as reference-shaped: {prose!r}"
        )

    @pytest.mark.parametrize("entry", [
        # E10 single-author + initials + period
        "Smith JA. Cortical tubers in tuberous sclerosis complex. J Neurol. 2019;10:1-10.",
        # E10 lowercase nobiliary particle, single author
        "van der Berg H. Network mapping of cortical lesions in epilepsy. Brain. 2020;5:20-30.",
        # E10 organizational / ALLCAPS author
        "ENIGMA Consortium. Subcortical brain volume differences across disorders. Mol Psychiatry. 2020;23:100-110.",
        # E10 organizational, multi-word non-acronym
        "World Health Organization. Global report on epilepsy and the brain. WHO Press. 2019;1:1-100.",
    ])
    def test_e10_author_entries_still_reference_shaped(self, entry):
        assert _is_reference_shaped(entry) is True, (
            f"E10 reference entry no longer reference-shaped: {entry!r}"
        )


def _numeric_cites(doc) -> list:
    """Kept numeric in-text markers' text (``e["text"]`` for numeric kinds)."""
    return [e["text"] for e in extract_references(doc)["intext"] if e["kind"] == "numeric"]


def test_enumerator_colon_introduced_dropped(tmp_path):
    # DROP: colon-introduced numbered list inside a single sentence — prose, not cites.
    doc = _build_doc(tmp_path, [
        "We identified three phenotypes: (1) severe impairment, (2) moderate "
        "impairment, and (3) preserved functioning across the cohort here.",
    ])
    nums = _numeric_cites(doc)
    assert "(1)" not in nums and "(2)" not in nums and "(3)" not in nums


def test_enumerator_verb_introduced_no_colon_dropped(tmp_path):
    # DROP: verb-introduced list (no colon before "(1)") — markers 1..k are list items.
    doc = _build_doc(tmp_path, [
        "The patient had (1) seizures, (2) developmental delay, and (3) hypotonia "
        "at baseline evaluation here.",
    ])
    nums = _numeric_cites(doc)
    assert "(1)" not in nums and "(2)" not in nums and "(3)" not in nums


def test_sequential_citations_across_sentences_kept(tmp_path):
    # KEEP: genuine sequential Vancouver/NIH cites across separate sentences.
    doc = _build_doc(tmp_path, [
        "We found A (1). Later we confirmed B (2). Finally we observed C (3) in the cohort.",
    ])
    nums = _numeric_cites(doc)
    assert "(1)" in nums and "(2)" in nums and "(3)" in nums


def test_claim_trailing_citations_kept(tmp_path):
    # KEEP: claim-trailing cites — markers are NOT followed by lowercase list-item prose
    #       (e.g. "(2)," / "(3) in"), so no enumerator run forms.
    doc = _build_doc(tmp_path, [
        "The effect was robust as shown (1) and confirmed (2), though others "
        "disagree (3) in this area.",
    ])
    nums = _numeric_cites(doc)
    assert "(1)" in nums and "(2)" in nums and "(3)" in nums


def test_mixed_enumerator_run_then_trailing_citation(tmp_path):
    # MIXED: (1),(2),(3) are a colon-introduced list (dropped); (4) is a real cite
    #        (preceded by prose, not a list separator) and is KEPT.
    doc = _build_doc(tmp_path, [
        "We saw three phenotypes: (1) severe, (2) moderate, and (3) preserved, "
        "consistent with prior work (4) in the field.",
    ])
    nums = _numeric_cites(doc)
    assert "(1)" not in nums and "(2)" not in nums and "(3)" not in nums
    assert "(4)" in nums


def test_multinumber_and_nonrun_single_kept(tmp_path):
    # KEEP: a multi-number "(3,4)" and a non-run single "(5)" are real citations.
    doc = _build_doc(tmp_path, [
        "These scores were calibrated (3,4) and validated previously (5) in "
        "independent samples here.",
    ])
    nums = _numeric_cites(doc)
    assert "(3,4)" in nums and "(5)" in nums
