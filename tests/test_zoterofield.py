import pytest

from zoterocite import (
    Docx, read_views, validate, insert_citation, scan_citations, check_links, new_doc,
    cite_into,
)
from zoterocite import zotero
from zoterocite import zoterofield
from zoterocite.builder import new_doc
from zoterocite.ooxml import qn

URI = "http://zotero.org/groups/2504198/items/X8ISWWQ2"
ITEMDATA = {"id": "2504198/X8ISWWQ2", "type": "article-journal",
            "title": "Convolutional neural networks for automatic tuber segmentation",
            "container-title": "Epilepsia", "issued": {"date-parts": [[2026, 2]]}}


def test_insert_live_zotero_field(tmp_path):
    src = tmp_path / "src.docx"
    new_doc(src, ["This claim about tuber segmentation needs a citation here."])
    doc = Docx(src)
    insert_citation(doc, "needs a citation", ["X8ISWWQ2"],
                    itemdata=[ITEMDATA], uris=[URI], rendered="(1)",
                    style="vancouver", add_bibliography=True,
                    bib_rendered="1. Sánchez Fernández I, et al. Epilepsia. 2026;67(2):830-845.")
    out = tmp_path / "out.docx"; doc.save(out)

    re = Docx(out)
    body = re.raw("word/document.xml").decode()
    # the live Zotero field code, the group-library URI, and the embedded CSL-JSON
    assert "ADDIN ZOTERO_ITEM CSL_CITATION" in body
    assert URI in body
    assert "article-journal" in body          # itemData embedded
    assert "ZOTERO_BIBL" in body
    # prefs live in custom document properties (where Zotero looks), NOT a field
    assert "ZOTERO_PREF" not in body
    custom = re.raw("docProps/custom.xml").decode()
    assert "ZOTERO_PREF_1" in custom and "styles/vancouver" in custom and 'value="Field"' in custom
    # custom.xml is registered so Word/Zotero can find it
    assert "custom-properties" in re.raw("[Content_Types].xml").decode()
    assert "docProps/custom.xml" in re.raw("_rels/.rels").decode()
    # round-trips cleanly (no Word repair) and the placeholder text is visible
    assert validate(out).ok
    assert "(1)" in read_views(out)["accepted"]


def test_existing_renderings_reports_live_field_text(tmp_path):
    # existing_renderings backs the unify-refs guard that must NOT re-cite a marker
    # already produced by a live Zotero field (which would clobber it with a raw key).
    src = tmp_path / "src.docx"
    new_doc(src, ["This sentence ends with a citation here."])
    doc = Docx(src)
    insert_citation(doc, "citation here", ["X8ISWWQ2"],
                    itemdata=[ITEMDATA], uris=[URI], rendered="(1,2)", style="vancouver")
    out = tmp_path / "out.docx"; doc.save(out)
    root = Docx(out).read_tree(zoterofield.DOCUMENT)
    assert "(1,2)" in zoterofield.existing_renderings(root)
    # a doc with no Zotero fields yields an empty set (nothing to protect)
    bare = tmp_path / "bare.docx"; new_doc(bare, ["No citations at all here."])
    assert zoterofield.existing_renderings(Docx(bare).read_tree(zoterofield.DOCUMENT)) == set()


def test_pref_added_only_once(tmp_path):
    src = tmp_path / "s.docx"
    new_doc(src, ["First spot here.", "Second spot here."])
    doc = Docx(src)
    insert_citation(doc, "First spot", ["AAA"], itemdata=[{"id": "1"}],
                    uris=["http://zotero.org/groups/1/items/AAA"], rendered="(1)")
    insert_citation(doc, "Second spot", ["BBB"], itemdata=[{"id": "2"}],
                    uris=["http://zotero.org/groups/1/items/BBB"], rendered="(2)")
    out = tmp_path / "o.docx"; doc.save(out)
    re = Docx(out)
    custom = re.raw("docProps/custom.xml").decode()
    assert custom.count('name="ZOTERO_PREF_1"') == 1   # prefs set once, not duplicated
    assert re.raw("word/document.xml").decode().count("ADDIN ZOTERO_ITEM CSL_CITATION") == 2


def test_multi_item_citation_with_locator(tmp_path):
    src = tmp_path / "m.docx"
    new_doc(src, ["A combined claim citing two papers here."])
    doc = Docx(src)
    insert_citation(doc, "two papers", ["AAA", "BBB"],
                    itemdata=[{"id": "1", "title": "First"}, {"id": "2", "title": "Second"}],
                    uris=["http://zotero.org/groups/2504198/items/AAA",
                          "http://zotero.org/groups/2504198/items/BBB"],
                    extras=[{"locator": "5", "label": "page"}, {}], rendered="(1, 2)")
    out = tmp_path / "m_out.docx"; doc.save(out)
    body = Docx(out).raw("word/document.xml").decode()
    assert '"locator": "5"' in body and '"label": "page"' in body
    # one field grouping both items
    assert body.count("ADDIN ZOTERO_ITEM CSL_CITATION") == 1
    cits = scan_citations(out)
    assert len(cits) == 1 and len(cits[0]["items"]) == 2
    assert cits[0]["items"][0]["key"] == "AAA" and cits[0]["items"][0]["locator"] == "5"


def test_superscript_render_from_html(tmp_path):
    src = tmp_path / "s.docx"; new_doc(src, ["A claim needing a superscript cite here."])
    doc = Docx(src)
    insert_citation(doc, "needing a superscript", ["X8ISWWQ2"],
                    itemdata=[{"id": "1", "title": "t"}],
                    uris=["http://zotero.org/groups/2504198/items/X8ISWWQ2"],
                    rendered_html="<sup>1</sup>")
    out = tmp_path / "s_out.docx"; doc.save(out)
    body = Docx(out).raw("word/document.xml").decode()
    assert '<w:vertAlign w:val="superscript"/>' in body   # shows superscript before any refresh
    assert '"plainCitation": "1"' in body                 # plain text Zotero compares against
    assert validate(out).ok


def test_check_links_flags_broken_and_external(tmp_path, monkeypatch):
    src = tmp_path / "c.docx"
    new_doc(src, ["Good cite here.", "Dead cite here.", "Foreign cite here."])
    doc = Docx(src)
    G = "http://zotero.org/groups/2504198/items/"
    insert_citation(doc, "Good cite", ["GOODKEY"], itemdata=[{"id": "1", "title": "Good"}],
                    uris=[G + "GOODKEY"], rendered="(1)")
    insert_citation(doc, "Dead cite", ["DEADKEY"], itemdata=[{"id": "2", "title": "Dead"}],
                    uris=[G + "DEADKEY"], rendered="(2)")
    insert_citation(doc, "Foreign cite", ["XKEY"], itemdata=[{"id": "3", "title": "Foreign"}],
                    uris=["http://zotero.org/groups/999999/items/XKEY"], rendered="(3)")
    out = tmp_path / "c_out.docx"; doc.save(out)

    monkeypatch.setattr(zotero, "zotero_config",
                        lambda: {"library_type": "group", "library_id": "2504198", "api_key": "k"})
    monkeypatch.setattr(zotero, "item_exists", lambda key: key == "GOODKEY")
    status = {r["key"]: r["status"] for r in check_links(out)}
    assert status["GOODKEY"] == "ok"
    assert status["DEADKEY"] == "broken"
    assert status["XKEY"] == "external"


def test_check_links_degrades_when_credentials_unconfigured(tmp_path, monkeypatch):
    # With no Zotero credentials, check_links must NOT raise — it cannot verify a
    # key against the library, so each keyed item degrades to "unverified" instead
    # of leaking a RuntimeError to the caller (read-only QC must stay non-fatal).
    src = tmp_path / "c.docx"
    new_doc(src, ["A claim here with a cite here."])
    doc = Docx(src)
    G = "http://zotero.org/groups/2504198/items/"
    insert_citation(doc, "cite here", ["KEY1"], itemdata=[{"id": "1", "title": "T"}],
                    uris=[G + "KEY1"], rendered="(1)")
    out = tmp_path / "c_out.docx"; doc.save(out)

    # No credentials configured: zotero_config returns {} and item_exists would raise.
    monkeypatch.setattr(zotero, "zotero_config", lambda: {})

    def _boom(key):  # must never be reached once we early-out on no-cred
        raise AssertionError("item_exists must not be called without credentials")
    monkeypatch.setattr(zotero, "item_exists", _boom)

    rows = check_links(out)  # must not raise
    assert len(rows) == 1
    assert rows[0]["key"] == "KEY1"
    assert rows[0]["status"] == "unverified"


# --------------------------------------------------------------------------- #
# CRIT-3 / F1 end-to-end: cite_into must bind each citationItem's embedded
# itemData/id to ITS OWN uri/key even when Zotero returns csljson in a different
# order than requested. The embedded title (from itemData) decoded against the
# key (from the uri) is the cross-check that the two are not swapped.
# --------------------------------------------------------------------------- #
GROUP = "2504198"


def _group_creds(monkeypatch):
    monkeypatch.setattr(
        zotero, "zotero_config",
        lambda: {"library_type": "group", "library_id": GROUP, "api_key": "k"},
    )


def test_cite_into_binds_correctly_when_zotero_reverses_order(tmp_path, monkeypatch):
    """Two keys; Zotero's csljson response is REVERSED vs the request. Each
    citationItem's embedded itemData/id AND uri must match its OWN key."""
    src = tmp_path / "s.docx"
    new_doc(src, ["A combined claim citing two papers here."])
    _group_creds(monkeypatch)

    # csljson HTTP returns the two items in REVERSED order (library order),
    # each carrying a title that uniquely identifies its OWN key.
    reversed_resp = {"items": [
        {"id": f"{GROUP}/BBB", "type": "article-journal", "title": "Beta paper"},
        {"id": f"{GROUP}/AAA", "type": "article-journal", "title": "Alpha paper"},
    ]}
    monkeypatch.setattr(zotero, "_get_json", lambda req: reversed_resp)
    # rendered marker is display-only here; stub it to avoid a second fetch path.
    monkeypatch.setattr(zotero, "formatted_citations",
                        lambda keys, **k: [f"({x})" for x in keys])

    out = tmp_path / "out.docx"
    cite_into(src, "two papers", keys=["AAA", "BBB"], out=out)

    cits = scan_citations(out)
    assert len(cits) == 1 and len(cits[0]["items"]) == 2
    by_key = {it["key"]: it for it in cits[0]["items"]}
    # uri decodes to the right key AND the embedded itemData title matches it.
    assert by_key["AAA"]["title"] == "Alpha paper"
    assert by_key["BBB"]["title"] == "Beta paper"
    assert by_key["AAA"]["uri"] == f"http://zotero.org/groups/{GROUP}/items/AAA"
    assert by_key["BBB"]["uri"] == f"http://zotero.org/groups/{GROUP}/items/BBB"

    # And the embedded CSL id is each item's OWN id (not the other's).
    body = Docx(out).raw("word/document.xml").decode()
    assert f'"id": "{GROUP}/AAA"' in body and f'"id": "{GROUP}/BBB"' in body
    assert validate(out).ok


def test_cite_into_omitted_key_still_binds_others(tmp_path, monkeypatch):
    """Zotero OMITS one requested key from csljson. The remaining keys must still
    bind correctly (no shift); the omitted key is surfaced, not silently
    mis-bound, and its slot carries only its OWN id (no other work's metadata)."""
    src = tmp_path / "s.docx"
    new_doc(src, ["A claim citing three papers here."])
    _group_creds(monkeypatch)

    # Request [AAA, BBB, CCC]; Zotero returns only CCC and AAA (BBB omitted),
    # and in reversed order to stress both bugs at once.
    resp = {"items": [
        {"id": f"{GROUP}/CCC", "title": "Gamma paper"},
        {"id": f"{GROUP}/AAA", "title": "Alpha paper"},
    ]}
    monkeypatch.setattr(zotero, "_get_json", lambda req: resp)
    monkeypatch.setattr(zotero, "formatted_citations",
                        lambda keys, **k: [f"({x})" for x in keys])

    out = tmp_path / "out.docx"
    with pytest.warns(UserWarning, match="BBB"):
        cite_into(src, "three papers", keys=["AAA", "BBB", "CCC"], out=out)

    cits = scan_citations(out)
    assert len(cits) == 1 and len(cits[0]["items"]) == 3
    by_key = {it["key"]: it for it in cits[0]["items"]}
    # The two returned items are still bound to their OWN metadata.
    assert by_key["AAA"]["title"] == "Alpha paper"
    assert by_key["CCC"]["title"] == "Gamma paper"
    # The omitted key keeps its OWN uri and an empty (placeholder) title — it did
    # NOT absorb Alpha's or Gamma's metadata.
    assert by_key["BBB"]["uri"] == f"http://zotero.org/groups/{GROUP}/items/BBB"
    assert by_key["BBB"]["title"] == ""
    body = Docx(out).raw("word/document.xml").decode()
    assert f'"id": "{GROUP}/BBB"' in body   # placeholder carries BBB's own id
    assert validate(out).ok


def test_cite_into_single_key_unchanged(tmp_path, monkeypatch):
    """Single-key citation through cite_into is unaffected by the reorder fix."""
    src = tmp_path / "s.docx"
    new_doc(src, ["A claim citing one paper here."])
    _group_creds(monkeypatch)
    monkeypatch.setattr(
        zotero, "_get_json",
        lambda req: {"items": [{"id": f"{GROUP}/SOLO", "title": "Solo paper"}]},
    )
    monkeypatch.setattr(zotero, "formatted_citations",
                        lambda keys, **k: [f"({x})" for x in keys])

    out = tmp_path / "out.docx"
    cite_into(src, "one paper", keys=["SOLO"], out=out)
    cits = scan_citations(out)
    assert len(cits) == 1 and len(cits[0]["items"]) == 1
    assert cits[0]["items"][0]["key"] == "SOLO"
    assert cits[0]["items"][0]["title"] == "Solo paper"
    assert validate(out).ok


def test_cite_into_grouped_marker_is_placeholder_not_fabricated_join(tmp_path, monkeypatch):
    """A grouped cite_into must NOT store a fabricated '(A); (B)' display marker:
    Zotero renders a group as ONE '(1,2)' marker, never a '; '-join of per-item
    ones, so joining corrupts the cached display + the existing_renderings dedup
    guard. A multi-key group stores the neutral '(citation)' placeholder (the
    live field carries the real citationItems; Word/Zotero regenerate the true
    grouped marker on refresh). Pre-fix this stored '(AAA); (BBB)'."""
    src = tmp_path / "s.docx"
    new_doc(src, ["A combined claim citing two papers here."])
    _group_creds(monkeypatch)
    monkeypatch.setattr(zotero, "_get_json", lambda req: {"items": [
        {"id": f"{GROUP}/AAA", "title": "Alpha paper"},
        {"id": f"{GROUP}/BBB", "title": "Beta paper"},
    ]})
    monkeypatch.setattr(zotero, "formatted_citations",
                        lambda keys, **k: [f"({x})" for x in keys])
    out = tmp_path / "out.docx"
    cite_into(src, "two papers", keys=["AAA", "BBB"], out=out)
    body = Docx(out).raw("word/document.xml").decode()
    assert "); (" not in body          # the fabricated per-item separator is gone
    assert "(citation)" in body         # neutral grouped placeholder stored instead
    assert validate(out).ok


# ---------------------------------------------------------------------------
# F11 — the citation-insert / field-write path is fully OFFLINE by default.
#
# ensure_pref runs on every insert_citation / cite_into / replace_* call via the
# style pref. A NON-catalog (but plausible) style must NOT trigger an online
# CSL-existence GET on this deterministic doc-edit path. Online validation is an
# explicit opt-in (validate_online=True), deferred to `csldb --check --online`.
# ---------------------------------------------------------------------------

def test_insert_citation_noncatalog_style_makes_no_network_call(tmp_path, monkeypatch):
    # Boom on ANY network access from the csldb online check.
    import zoterocite.csldb as csldb

    def _boom(*a, **k):
        raise AssertionError("citation insert hit the network (F11 regression)")
    monkeypatch.setattr(csldb.urllib.request, "urlopen", _boom)

    src = tmp_path / "s.docx"
    new_doc(src, ["A claim needing a citation here."])
    doc = Docx(src)
    # 'journal-of-neuroscience' is a plausible, NON-catalog slug: pre-F11 this
    # forced an online GET on the write path. Now it is accepted offline.
    insert_citation(doc, "needing a citation", ["AAA"], itemdata=[{"id": "1"}],
                    uris=["http://zotero.org/groups/1/items/AAA"], rendered="(1)",
                    style="journal-of-neuroscience")
    out = tmp_path / "o.docx"; doc.save(out)
    custom = Docx(out).raw("docProps/custom.xml").decode()
    assert "styles/journal-of-neuroscience" in custom


def test_ensure_pref_default_offline_even_when_repo_would_404(tmp_path, monkeypatch):
    # Simulate the repo answering 404: on the DEFAULT path it must never be asked
    # (no network), so a plausible slug is still accepted offline.
    import zoterocite.csldb as csldb
    monkeypatch.setattr(
        csldb.urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("network called on default path")))
    src = tmp_path / "s.docx"
    new_doc(src, ["x"])
    doc = Docx(src)
    zoterofield.ensure_pref(doc, "natuer-neuroscience")  # plausible, default offline
    out = tmp_path / "o.docx"; doc.save(out)
    assert "styles/natuer-neuroscience" in Docx(out).raw("docProps/custom.xml").decode()


def test_ensure_pref_optin_online_still_validates(tmp_path, monkeypatch):
    # The explicit opt-in DOES consult the repo and rejects a 404'd slug — the
    # deferred validation still works when asked for.
    import urllib.error
    import zoterocite.csldb as csldb

    def _404(*a, **k):
        raise urllib.error.HTTPError("u", 404, "nf", hdrs=None, fp=None)
    monkeypatch.setattr(csldb.urllib.request, "urlopen", _404)
    src = tmp_path / "s.docx"
    new_doc(src, ["x"])
    doc = Docx(src)
    with pytest.raises(ValueError):
        zoterofield.ensure_pref(doc, "natuer-neuroscience", validate_online=True)


def test_build_field_rejects_misaligned_lists():
    # zip() would silently truncate; mismatched key/itemdata/uri lengths must raise.
    import pytest as _pytest
    from zoterocite.zoterofield import _build_zotero_field_xml
    with _pytest.raises(ValueError):
        _build_zotero_field_xml(["s"], ["K1", "K2"], [{"id": "a"}], ["u1"])
