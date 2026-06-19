"""zotero-word-cite: read and write real Zotero references in Word (.docx).

A self-contained extraction of the citation/Zotero/EndNote engine: scan and
verify Zotero citation fields, insert live ``ADDIN ZOTERO_ITEM`` fields, convert
EndNote / Mendeley / Word / manual references into the user's Zotero library
(with dedup, completeness, resolution, and retraction checks), and reformat in
any CSL style (default: ``vancouver-superscript`` = NIH numbered superscript).
"""
from .docxio import Docx, DocxLoadError, text_of, DOCUMENT
from .views import read_views, counts
from .naming import convention_name, rename_to_convention
from .revisions import (
    tracked_insert, tracked_replace_paragraph, tracked_replace_paragraphs,
    tracked_delete_paragraph_text,
    accept_all, reject_all, spread_timestamps,
)
from .comments import add_comment, add_comments_batch, list_comments, CommentItem
from .builder import new_doc, heading, body
from .validate import validate, format_report, Report
from .cite import Reference, format_reference, format_bibliography, in_text, from_pubmed_record
from .zotero import (
    zotero_config, search_items, get_item_by_doi, formatted_citations, to_reference,
    count_items, fetch_all, csljson, item_uri, get_item, item_exists,
)
from .zoterofield import insert_citation, cite_into, ensure_pref, scan_citations, check_links
from .findings import Finding, format_findings, findings_to_dicts
from .csldb import (
    resolve_style, is_valid_style, list_styles, nearest_styles,
    get_style, is_known_id, is_plausible_id, Style,
)
from .citecheck import (
    reconcile_citations, load_retraction_db, check_retractions,
    cite_check, default_rw_path, refresh_retraction_db, ensure_retraction_db,
    RETRACTION_WATCH_URL,
)
from .citeconvert import (
    classify_citation_sources, classification_findings, convert_to_zotero,
)
from .refextract import extract_references
from .refstyle import check_reference_style
from .refresolve import (
    extract_identifier, crossref_bibliographic, resolve_reference,
    is_preprint, check_preprint_status,
)
from .unify import plan_unification, apply_unification
from .endnote import (
    parse_endnote_library, plan_endnote_migration, apply_endnote_migration,
    validate_folder, WriteRefusedError,
)
from .mybib import fetch_mybib_works, fetch_mybib_works_status
from .orcid_api import fetch_orcid_works, fetch_orcid_works_status
from .icite import fetch_icite, fetch_icite_status

__all__ = [
    "Docx", "DocxLoadError", "text_of", "DOCUMENT",
    "read_views", "counts",
    "convention_name", "rename_to_convention",
    "tracked_insert", "tracked_replace_paragraph", "tracked_replace_paragraphs",
    "tracked_delete_paragraph_text",
    "accept_all", "reject_all", "spread_timestamps",
    "add_comment", "add_comments_batch", "list_comments", "CommentItem",
    "new_doc", "heading", "body",
    "validate", "format_report", "Report",
    "Reference", "format_reference", "format_bibliography", "in_text", "from_pubmed_record",
    "zotero_config", "search_items", "get_item_by_doi", "formatted_citations", "to_reference",
    "count_items", "fetch_all", "csljson", "item_uri", "get_item", "item_exists",
    "insert_citation", "cite_into", "ensure_pref", "scan_citations", "check_links",
    "Finding", "format_findings", "findings_to_dicts",
    "resolve_style", "is_valid_style", "list_styles", "nearest_styles",
    "get_style", "is_known_id", "is_plausible_id", "Style",
    "reconcile_citations", "load_retraction_db", "check_retractions",
    "cite_check", "default_rw_path", "refresh_retraction_db", "ensure_retraction_db",
    "RETRACTION_WATCH_URL",
    "classify_citation_sources", "classification_findings", "convert_to_zotero",
    "extract_references", "check_reference_style",
    "extract_identifier", "crossref_bibliographic", "resolve_reference",
    "is_preprint", "check_preprint_status",
    "plan_unification", "apply_unification",
    "parse_endnote_library", "plan_endnote_migration", "apply_endnote_migration",
    "validate_folder", "WriteRefusedError",
    "fetch_mybib_works", "fetch_mybib_works_status",
    "fetch_orcid_works", "fetch_orcid_works_status",
    "fetch_icite", "fetch_icite_status",
]
