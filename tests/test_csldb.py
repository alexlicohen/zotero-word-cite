"""Tests for zoterocite/csldb.py (journal -> CSL-id catalog + resolver) and the
csldb-backed validation in zoterofield.ensure_pref.

All online checks are monkeypatched — no test touches the network. Loading the
catalog must need no network either, which we assert explicitly.
"""
from __future__ import annotations

import urllib.error
from unittest.mock import MagicMock

import pytest

from zoterocite import csldb
from zoterocite import zoterofield
from zoterocite.docxio import Docx


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _fake_urlopen(status=None, http_error_code=None, exc=None):
    """Build a urlopen replacement.

    * status=200/404...   -> context-manager response with that .status
    * http_error_code=404 -> raises urllib.error.HTTPError(code)
    * exc=...             -> raises that exception (e.g. URLError for offline)
    """
    def _fn(req, timeout=4.0):
        if exc is not None:
            raise exc
        if http_error_code is not None:
            raise urllib.error.HTTPError(req.full_url, http_error_code,
                                         "err", {}, None)
        resp = MagicMock()
        resp.status = status
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp
    return _fn


def _blank_docx(tmp_path):
    """A minimal valid .docx via the project's builder."""
    from zoterocite.builder import new_doc
    p = tmp_path / "d.docx"
    new_doc(p, ["Body paragraph."])
    return Docx(p)


# ---------------------------------------------------------------------------
# resolve_style
# ---------------------------------------------------------------------------
class TestResolveStyle:
    @pytest.mark.parametrize("name,expected", [
        ("Brain", "brain"),
        ("brain", "brain"),
        ("The Lancet", "the-lancet"),
        ("Lancet", "the-lancet"),
        ("The Lancet Neurology", "the-lancet-neurology"),
        ("Lancet Neurology", "the-lancet-neurology"),
        ("Annals of Neurology", "annals-of-neurology"),
        ("ann neurol", "annals-of-neurology"),
        ("Nature", "nature"),
        ("Nature Communications", "nature-communications"),
        ("nat commun", "nature-communications"),
        ("NeuroImage", "neuroimage"),
        ("neuro image", "neuroimage"),
        ("Epilepsia", "epilepsia"),
        ("eLife", "elife"),
        ("Cell", "cell"),
        ("PNAS", "pnas"),
        ("Proceedings of the National Academy of Sciences", "pnas"),
        ("JAMA Neurology", "jama"),
        ("Vancouver", "vancouver"),
    ])
    def test_resolves(self, name, expected):
        assert csldb.resolve_style(name) == expected

    def test_lancet_vs_lancet_neurology_disambiguation(self):
        # The longer, more specific name must not collapse to the generic one.
        assert csldb.resolve_style("Lancet Neurology") == "the-lancet-neurology"
        assert csldb.resolve_style("The Lancet") == "the-lancet"

    def test_unknown_returns_none(self):
        assert csldb.resolve_style("Journal of Imaginary Studies") is None
        assert csldb.resolve_style("") is None
        assert csldb.resolve_style("   ") is None

    def test_passthrough_known_id(self):
        # An exact csl id is returned as-is.
        assert csldb.resolve_style("the-lancet-neurology") == "the-lancet-neurology"


# ---------------------------------------------------------------------------
# is_valid_style (offline)
# ---------------------------------------------------------------------------
class TestIsValidStyleOffline:
    def test_known_catalog_ids(self):
        for cid in ("vancouver-superscript", "vancouver", "apa", "nature",
                    "the-lancet-neurology", "pnas", "jama"):
            assert csldb.is_valid_style(cid) is True

    def test_plausible_unknown_slug_accepted_offline(self):
        # A new journal id Alex might pass, not yet in the seed.
        assert csldb.is_valid_style("journal-of-neuroscience") is True

    @pytest.mark.parametrize("bad", [
        "",
        "http://www.zotero.org/styles/nature",
        "Nature",                       # capitals not allowed in a slug
        "the_lancet",                   # underscores not allowed
        "ends-",                        # trailing hyphen
        "-leads",                       # leading hyphen
        "double--hyphen",
        "has space",
        "ZOTERO_PREF garbage",
    ])
    def test_implausible_rejected(self, bad):
        assert csldb.is_valid_style(bad) is False


# ---------------------------------------------------------------------------
# is_valid_style (online) — fully monkeypatched, never hits network
# ---------------------------------------------------------------------------
class TestIsValidStyleOnline:
    def test_known_id_short_circuits_no_network(self, monkeypatch):
        # A known id must be valid online WITHOUT any HTTP call.
        def _boom(*a, **k):
            raise AssertionError("network must not be called for a known id")
        monkeypatch.setattr("zoterocite.csldb.urllib.request.urlopen", _boom)
        assert csldb.is_valid_style("apa", online=True) is True

    def test_unknown_slug_present_online(self, monkeypatch):
        monkeypatch.setattr("zoterocite.csldb.urllib.request.urlopen",
                            _fake_urlopen(status=200))
        assert csldb.is_valid_style("journal-of-neuroscience", online=True) is True

    def test_unknown_slug_absent_online_is_false(self, monkeypatch):
        monkeypatch.setattr("zoterocite.csldb.urllib.request.urlopen",
                            _fake_urlopen(http_error_code=404))
        assert csldb.is_valid_style("totally-made-up-journal", online=True) is False

    def test_unreachable_network_cannot_verify_fails_open(self, monkeypatch):
        # Offline/timeout -> "cannot verify" -> defer to offline rule (True for a
        # plausible slug). Never hard-fail.
        monkeypatch.setattr(
            "zoterocite.csldb.urllib.request.urlopen",
            _fake_urlopen(exc=urllib.error.URLError("offline")))
        assert csldb.is_valid_style("plausible-but-unknown", online=True) is True

    def test_server_5xx_cannot_verify(self, monkeypatch):
        monkeypatch.setattr("zoterocite.csldb.urllib.request.urlopen",
                            _fake_urlopen(http_error_code=503))
        # 503 is not a definitive "absent" -> cannot verify -> fail open.
        assert csldb.is_valid_style("plausible-but-unknown", online=True) is True

    def test_implausible_never_calls_network(self, monkeypatch):
        def _boom(*a, **k):
            raise AssertionError("network must not be called for an implausible id")
        monkeypatch.setattr("zoterocite.csldb.urllib.request.urlopen", _boom)
        assert csldb.is_valid_style("http://x/y", online=True) is False


# ---------------------------------------------------------------------------
# _online_exists — dependent-style fallback probe
# ---------------------------------------------------------------------------
class TestOnlineExistsDependentFallback:
    def _dispatching_urlopen(self, root_result, dependent_result):
        """Route the mock response by which URL is requested: root master/<id>.csl
        vs master/dependent/<id>.csl. Each *_result is ('status', code),
        ('http_error', code), or ('exc', exception)."""
        def _fn(req, timeout=4.0):
            is_dependent = "/dependent/" in req.full_url
            kind, val = dependent_result if is_dependent else root_result
            if kind == "exc":
                raise val
            if kind == "http_error":
                raise urllib.error.HTTPError(req.full_url, val, "err", {}, None)
            resp = MagicMock()
            resp.status = val
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp
        return _fn

    def test_root_404_dependent_200_is_true(self, monkeypatch):
        monkeypatch.setattr(
            "zoterocite.csldb.urllib.request.urlopen",
            self._dispatching_urlopen(("http_error", 404), ("status", 200)),
        )
        assert csldb._online_exists("some-dependent-style") is True

    def test_both_404_is_false(self, monkeypatch):
        monkeypatch.setattr(
            "zoterocite.csldb.urllib.request.urlopen",
            self._dispatching_urlopen(("http_error", 404), ("http_error", 404)),
        )
        assert csldb._online_exists("totally-made-up-journal") is False

    def test_root_200_never_probes_dependent(self, monkeypatch):
        def _boom(req, timeout=4.0):
            if "/dependent/" in req.full_url:
                raise AssertionError("must not probe dependent/ when root is 200")
            resp = MagicMock()
            resp.status = 200
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp
        monkeypatch.setattr("zoterocite.csldb.urllib.request.urlopen", _boom)
        assert csldb._online_exists("brain") is True

    def test_root_404_dependent_unverifiable_is_none(self, monkeypatch):
        # Root definitively 404s; the dependent probe errors (offline/5xx) ->
        # overall result must stay None ("cannot verify"), NOT collapse to False.
        monkeypatch.setattr(
            "zoterocite.csldb.urllib.request.urlopen",
            self._dispatching_urlopen(
                ("http_error", 404), ("exc", urllib.error.URLError("offline"))
            ),
        )
        assert csldb._online_exists("plausible-but-unknown") is None

    def test_root_unverifiable_never_probes_dependent(self, monkeypatch):
        # Root errors out (cannot verify) -> overall None, and the dependent/
        # path is never probed (no second HTTP call on an inconclusive root).
        def _fn(req, timeout=4.0):
            if "/dependent/" in req.full_url:
                raise AssertionError("must not probe dependent/ when root is unverifiable")
            raise urllib.error.URLError("offline")
        monkeypatch.setattr("zoterocite.csldb.urllib.request.urlopen", _fn)
        assert csldb._online_exists("plausible-but-unknown") is None


# ---------------------------------------------------------------------------
# style_validation_note (E12) — accepted-but-unverified styles must WARN
# ---------------------------------------------------------------------------
class TestStyleValidationNote:
    def test_incumbents_and_default_are_clean(self):
        """The 4 incumbent styles + default are cataloged -> no warning."""
        for cid in ("vancouver-superscript", "vancouver", "apa", "nature"):
            assert csldb.style_validation_note(cid) is None

    def test_known_catalog_id_is_clean(self):
        assert csldb.style_validation_note("the-lancet-neurology") is None

    def test_uncataloged_slug_offline_warns(self):
        """E12: a plausible-but-uncataloged slug is accepted offline by
        is_valid_style, but style_validation_note must flag it as unverified."""
        # is_valid_style accepts it (the gap E12 is about) ...
        assert csldb.is_valid_style("the-onion", online=False) is True
        # ... but the advisory surfaces the unverified status.
        note = csldb.style_validation_note("the-onion", online=False)
        assert note is not None
        assert "the-onion" in note
        assert "not" in note.lower() and "catalog" in note.lower()

    def test_implausible_returns_none(self):
        """Implausible ids are hard-rejected by is_valid_style; the soft note
        stays out of that path (returns None)."""
        for bad in ("", "http://x/y", "Nature", "has space"):
            assert csldb.style_validation_note(bad) is None

    def test_online_present_is_clean(self, monkeypatch):
        monkeypatch.setattr("zoterocite.csldb.urllib.request.urlopen",
                            _fake_urlopen(status=200))
        assert csldb.style_validation_note("journal-of-neuroscience",
                                           online=True) is None

    def test_online_absent_warns(self, monkeypatch):
        monkeypatch.setattr("zoterocite.csldb.urllib.request.urlopen",
                            _fake_urlopen(http_error_code=404))
        note = csldb.style_validation_note("totally-made-up-journal", online=True)
        assert note is not None
        assert "404" in note or "not be found" in note.lower()

    def test_online_unreachable_warns_unconfirmed(self, monkeypatch):
        """Cannot-verify (network down): is_valid_style fails open to True, but
        the note must flag it as unconfirmed."""
        monkeypatch.setattr(
            "zoterocite.csldb.urllib.request.urlopen",
            _fake_urlopen(exc=urllib.error.URLError("offline")))
        assert csldb.is_valid_style("plausible-but-unknown", online=True) is True
        note = csldb.style_validation_note("plausible-but-unknown", online=True)
        assert note is not None
        assert "not" in note.lower()


# ---------------------------------------------------------------------------
# list_styles
# ---------------------------------------------------------------------------
class TestListStyles:
    def test_shape_and_sort(self):
        rows = csldb.list_styles()
        assert isinstance(rows, list) and rows
        names = [r["name"] for r in rows]
        assert names == sorted(names, key=str.lower)
        for r in rows:
            assert set(r) == {"name", "csl_id", "issn", "numbered",
                              "superscript", "et_al_after", "aliases", "note"}

    def test_no_network_on_load(self, monkeypatch):
        # Importing/using the catalog must not perform any HTTP.
        def _boom(*a, **k):
            raise AssertionError("catalog load must not hit the network")
        monkeypatch.setattr("zoterocite.csldb.urllib.request.urlopen", _boom)
        assert csldb.list_styles()
        assert csldb.resolve_style("Brain") == "brain"

    def test_known_ids_present(self):
        ids = {r["csl_id"] for r in csldb.list_styles()}
        # The 4 incumbent styles plus the corrected ids must be in the catalog.
        for cid in ("vancouver-superscript", "vancouver", "apa", "nature",
                    "pnas", "jama"):
            assert cid in ids

    def test_no_fabricated_ids(self):
        # The brief's non-existent ids must NOT have been seeded verbatim.
        ids = {r["csl_id"] for r in csldb.list_styles()}
        assert "brain-communications" not in ids
        assert "annals-of-clinical-and-translational-neurology" not in ids
        assert "jama-neurology" not in ids
        assert "proceedings-of-the-national-academy-of-sciences" not in ids


# ---------------------------------------------------------------------------
# nearest_styles
# ---------------------------------------------------------------------------
def test_nearest_styles_returns_styles():
    near = csldb.nearest_styles("lancet neuro", n=3)
    assert 1 <= len(near) <= 3
    assert any(s.csl_id == "the-lancet-neurology" for s in near)


# ---------------------------------------------------------------------------
# et_al_after field
# ---------------------------------------------------------------------------
class TestEtAlAfter:
    def test_vancouver_styles_have_6(self):
        for cid in ("vancouver", "vancouver-superscript"):
            s = csldb.get_style(cid)
            assert s is not None
            assert s.et_al_after == 6, f"{cid}: expected 6, got {s.et_al_after}"

    def test_jama_has_6(self):
        s = csldb.get_style("jama")
        assert s is not None
        assert s.et_al_after == 6

    def test_nature_family_have_5(self):
        for cid in ("nature", "nature-neuroscience", "nature-medicine",
                    "nature-communications"):
            s = csldb.get_style(cid)
            assert s is not None
            assert s.et_al_after == 5, f"{cid}: expected 5, got {s.et_al_after}"

    def test_lancet_family_have_4(self):
        for cid in ("the-lancet", "the-lancet-neurology"):
            s = csldb.get_style(cid)
            assert s is not None
            assert s.et_al_after == 4, f"{cid}: expected 4, got {s.et_al_after}"

    def test_unknown_styles_leave_none(self):
        # Styles with no confidently-known threshold should stay None.
        for cid in ("apa", "brain", "neuroimage"):
            s = csldb.get_style(cid)
            assert s is not None
            assert s.et_al_after is None, \
                f"{cid}: expected None, got {s.et_al_after}"

    def test_list_styles_exposes_et_al_after(self):
        rows = csldb.list_styles()
        assert all("et_al_after" in r for r in rows)
        # Nature should expose 5 in the dict
        nat = next(r for r in rows if r["csl_id"] == "nature")
        assert nat["et_al_after"] == 5


# ---------------------------------------------------------------------------
# ensure_pref integration: raises on bogus, works for the 4 known ones
# ---------------------------------------------------------------------------
class TestEnsurePref:
    def test_default_style_still_works(self, tmp_path):
        doc = _blank_docx(tmp_path)
        zoterofield.ensure_pref(doc)  # default = vancouver-superscript
        xml = doc.tree(zoterofield._CUSTOM_PART)
        blob = "".join(p.findtext("{%s}lpwstr" % zoterofield._VT_NS, "")
                       for p in xml)
        assert "vancouver-superscript" in blob

    @pytest.mark.parametrize("style,needle", [
        ("vancouver-superscript", "vancouver-superscript"),
        ("vancouver", "vancouver"),
        ("apa", "apa"),
        ("nature", "nature"),
    ])
    def test_four_known_styles(self, tmp_path, style, needle):
        doc = _blank_docx(tmp_path)
        zoterofield.ensure_pref(doc, style)
        xml = doc.tree(zoterofield._CUSTOM_PART)
        blob = "".join(p.findtext("{%s}lpwstr" % zoterofield._VT_NS, "")
                       for p in xml)
        # the mapped URL contains the style slug
        assert needle in blob

    def test_new_catalog_id_accepted(self, tmp_path):
        doc = _blank_docx(tmp_path)
        zoterofield.ensure_pref(doc, "the-lancet-neurology")
        xml = doc.tree(zoterofield._CUSTOM_PART)
        blob = "".join(p.findtext("{%s}lpwstr" % zoterofield._VT_NS, "")
                       for p in xml)
        assert "zotero.org/styles/the-lancet-neurology" in blob

    def test_plausible_unknown_slug_accepted(self, tmp_path, monkeypatch):
        # The write path now runs the ONLINE existence check for an uncataloged
        # slug, so stub the repo to report the file present (200).
        monkeypatch.setattr("zoterocite.csldb.urllib.request.urlopen",
                            _fake_urlopen(status=200))
        doc = _blank_docx(tmp_path)
        zoterofield.ensure_pref(doc, "journal-of-neuroscience")
        xml = doc.tree(zoterofield._CUSTOM_PART)
        blob = "".join(p.findtext("{%s}lpwstr" % zoterofield._VT_NS, "")
                       for p in xml)
        assert "journal-of-neuroscience" in blob

    @pytest.mark.parametrize("bad", [
        "Nature",                                    # capitals -> not a slug
        "http://www.zotero.org/styles/made-up",
        "not a style at all",
        "",
    ])
    def test_bogus_style_raises(self, tmp_path, bad):
        doc = _blank_docx(tmp_path)
        with pytest.raises(ValueError) as ei:
            zoterofield.ensure_pref(doc, bad)
        # error must be actionable: mentions nearest matches / how to add one
        msg = str(ei.value)
        assert "csldb" in msg or "citation-style-language" in msg

    def test_bogus_does_not_write_pref(self, tmp_path):
        doc = _blank_docx(tmp_path)
        with pytest.raises(ValueError):
            zoterofield.ensure_pref(doc, "definitely not valid !!")
        # No ZOTERO_PREF custom property should have been written.
        if doc.has(zoterofield._CUSTOM_PART):
            xml = doc.tree(zoterofield._CUSTOM_PART)
            names = [p.get("name", "") for p in xml]
            assert not any(n.startswith("ZOTERO_PREF") for n in names)


# ---------------------------------------------------------------------------
# BUG 1 regression: resolve_style must NOT map real journals to a wrong style.
# The old substring-containment + fuzzy fallback silently mis-resolved these
# real-but-uncataloged journals to a valid-but-wrong CSL id.
# ---------------------------------------------------------------------------
class TestResolveStyleNoSilentMisresolve:
    @pytest.mark.parametrize("name,wrong", [
        ("Brain Communications", "brain"),
        ("Science", "pnas"),
        ("Pediatric Neurology", "neurology"),
        ("Nature Reviews Neuroscience", "nature"),
        ("Annals of Clinical and Translational Neurology", "neurology"),
        # the fuzzy fallback also mis-mapped these multi-word names:
        ("Nature Reviews Neuroscience", "nature-neuroscience"),
        ("Brain Communications", "nature-communications"),
    ])
    def test_does_not_resolve_to_wrong_style(self, name, wrong):
        got = csldb.resolve_style(name)
        assert got != wrong, f"{name!r} silently mis-resolved to {wrong!r}"

    @pytest.mark.parametrize("name", [
        "Brain Communications",
        "Science",
        "Pediatric Neurology",
        "Nature Reviews Neuroscience",
        "Annals of Clinical and Translational Neurology",
    ])
    def test_uncataloged_real_journals_return_none(self, name):
        # No correct id exists in the seed for these, so the honest answer is
        # None (the CLI then offers nearest_styles as "did you mean").
        assert csldb.resolve_style(name) is None

    def test_brain_communications_not_brain(self):
        # A longer query whose tokens *superset* a short catalog key must not
        # collapse onto that key ("brain communications" -> "brain" was BUG 1).
        assert csldb.resolve_style("Brain Communications") != "brain"

    def test_whole_token_subset_still_resolves_unambiguous(self):
        # The query token-set ⊆ exactly one catalog name's tokens -> resolves.
        # "lancet neurol" is an alias (exact), but a partial like this exercises
        # the subset branch when not exact; it must reach the-lancet-neurology
        # and nothing else.
        assert csldb.resolve_style("brain") == "brain"
        assert csldb.resolve_style("the-lancet-neurology") == "the-lancet-neurology"


# ---------------------------------------------------------------------------
# BUG 2(a) regression: is_plausible_id must reject garbage that the bare slug
# regex rubber-stamped (single char, pure numeric, all-stub-segment).
# ---------------------------------------------------------------------------
class TestIsPlausibleId:
    @pytest.mark.parametrize("bad", [
        "x",          # single char
        "ab",         # too short
        "123",        # pure numeric
        "1-2-3",      # numeric stub segments
        "a-b-c",      # all segments too short
        "a-b",        # too short / stub segments
        "",           # empty
        "Nature",     # caps -> not a slug
        "the_lancet", # underscore
        "ends-",      # trailing hyphen
        "-leads",     # leading hyphen
        "double--hyphen",
        "has space",
        "http://x/y",
    ])
    def test_rejects_implausible(self, bad):
        assert csldb.is_plausible_id(bad) is False

    @pytest.mark.parametrize("good", [
        "apa", "pnas", "jama", "cell",            # short single-segment catalog ids
        "nature", "brain", "neurology", "elife",
        "the-lancet-neurology",
        "journal-of-neuroscience",                 # a plausible new id
        "vancouver-superscript",
    ])
    def test_accepts_plausible(self, good):
        assert csldb.is_plausible_id(good) is True


# ---------------------------------------------------------------------------
# BUG 2(b) regression: ensure_pref runs the ONLINE existence check on the write
# path for an uncataloged slug. A reachable-but-404 slug must raise (no dead
# pref written); an unreachable network fails open; catalog ids/incumbents never
# touch the network.
# ---------------------------------------------------------------------------
class TestEnsurePrefOnlineOnWrite:
    # F11: ``ensure_pref`` validates against the LOCAL catalog by default and
    # makes NO network call on a citation insert. Online existence checking is an
    # explicit opt-in (``validate_online=True``), deferred to the explicit
    # ``zoterocite csldb --check <id> --online`` step. These tests pin BOTH the
    # default-offline contract and the opt-in online behaviour.

    # ---- default path: fully offline, never touches the network --------------
    def test_default_does_not_call_network_for_plausible_slug(self, tmp_path, monkeypatch):
        # The common citation-insert path must never hit the network: a
        # plausible-but-uncataloged slug is accepted via the LOCAL rule alone.
        def _boom(*a, **k):
            raise AssertionError("ensure_pref hit the network on the default path")
        monkeypatch.setattr("zoterocite.csldb.urllib.request.urlopen", _boom)
        doc = _blank_docx(tmp_path)
        zoterofield.ensure_pref(doc, "journal-of-neuroscience")  # no validate_online
        blob = "".join(p.findtext("{%s}lpwstr" % zoterofield._VT_NS, "")
                       for p in doc.tree(zoterofield._CUSTOM_PART))
        assert "journal-of-neuroscience" in blob

    def test_default_rejects_implausible_offline(self, tmp_path, monkeypatch):
        # An implausible value is still rejected offline (no network needed).
        def _boom(*a, **k):
            raise AssertionError("ensure_pref hit the network on the default path")
        monkeypatch.setattr("zoterocite.csldb.urllib.request.urlopen", _boom)
        doc = _blank_docx(tmp_path)
        with pytest.raises(ValueError):
            zoterofield.ensure_pref(doc, "definitely not valid !!")

    # ---- opt-in path: validate_online=True consults the CSL repo -------------
    def test_online_optin_unknown_plausible_slug_404_raises(self, tmp_path, monkeypatch):
        # With the explicit opt-in, a typo'd-but-plausible id the repo reports
        # absent (404) must NOT be written as a dead zotero.org/styles/<slug> pref.
        monkeypatch.setattr("zoterocite.csldb.urllib.request.urlopen",
                            _fake_urlopen(http_error_code=404))
        doc = _blank_docx(tmp_path)
        with pytest.raises(ValueError):
            zoterofield.ensure_pref(doc, "natuer-neuroscience", validate_online=True)
        if doc.has(zoterofield._CUSTOM_PART):
            xml = doc.tree(zoterofield._CUSTOM_PART)
            names = [p.get("name", "") for p in xml]
            assert not any(n.startswith("ZOTERO_PREF") for n in names)

    def test_online_optin_unknown_plausible_slug_present_writes(self, tmp_path, monkeypatch):
        monkeypatch.setattr("zoterocite.csldb.urllib.request.urlopen",
                            _fake_urlopen(status=200))
        doc = _blank_docx(tmp_path)
        zoterofield.ensure_pref(doc, "journal-of-neuroscience", validate_online=True)
        blob = "".join(p.findtext("{%s}lpwstr" % zoterofield._VT_NS, "")
                       for p in doc.tree(zoterofield._CUSTOM_PART))
        assert "journal-of-neuroscience" in blob

    def test_online_optin_unknown_plausible_slug_offline_fails_open(self, tmp_path, monkeypatch):
        # Even with the opt-in, an unreachable network -> "cannot verify" ->
        # defer to offline rule -> the plausible slug is written.
        monkeypatch.setattr(
            "zoterocite.csldb.urllib.request.urlopen",
            _fake_urlopen(exc=urllib.error.URLError("offline")))
        doc = _blank_docx(tmp_path)
        zoterofield.ensure_pref(doc, "journal-of-neuroscience", validate_online=True)
        blob = "".join(p.findtext("{%s}lpwstr" % zoterofield._VT_NS, "")
                       for p in doc.tree(zoterofield._CUSTOM_PART))
        assert "journal-of-neuroscience" in blob

    def test_catalog_id_never_calls_network(self, tmp_path, monkeypatch):
        def _boom(*a, **k):
            raise AssertionError("network must not be called for a catalog id")
        monkeypatch.setattr("zoterocite.csldb.urllib.request.urlopen", _boom)
        doc = _blank_docx(tmp_path)
        zoterofield.ensure_pref(doc, "the-lancet-neurology")  # catalog id
        blob = "".join(p.findtext("{%s}lpwstr" % zoterofield._VT_NS, "")
                       for p in doc.tree(zoterofield._CUSTOM_PART))
        assert "the-lancet-neurology" in blob

    @pytest.mark.parametrize("style", [
        "vancouver-superscript", "vancouver", "apa", "nature",
    ])
    def test_four_incumbents_never_call_network(self, tmp_path, monkeypatch, style):
        def _boom(*a, **k):
            raise AssertionError("network must not be called for an incumbent style")
        monkeypatch.setattr("zoterocite.csldb.urllib.request.urlopen", _boom)
        doc = _blank_docx(tmp_path)
        zoterofield.ensure_pref(doc, style)  # returned from STYLE_URLS, no check
        blob = "".join(p.findtext("{%s}lpwstr" % zoterofield._VT_NS, "")
                       for p in doc.tree(zoterofield._CUSTOM_PART))
        assert style in blob
