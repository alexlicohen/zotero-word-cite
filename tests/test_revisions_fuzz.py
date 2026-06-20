"""Property/fuzz tests for the cited-paragraph tracked-edit engine
(:func:`zoterocite.revisions._apply_inplace_plain_edits` via the public
:func:`tracked_replace_paragraphs`).

WHY THIS EXISTS
---------------
A tracked edit on a paragraph carrying a LIVE Zotero citation field must split
only the *plain* runs the edit touches and leave the complex field byte-identical.
The hard case is an insertion whose plain-text offset lands on a run **seam** —
especially the prose|field boundary. A 2026-06 review found two successive bugs
here that hand-picked example tests missed:

  1. both runs at a seam deferred ownership -> the ``<w:ins>`` was silently dropped
     (reported as applied -> data loss);
  2. the seam was then owned by the run STARTING at the offset -> a word inserted
     just before a citation landed AFTER it with a doubled space.

The fix: the seam is owned by the run **ENDING** at the offset (the global-start
offset 0, which no run ends at, is owned by the first run). Because empty plain
runs are filtered out, the half-open intervals ``(sᵢ, eᵢ]`` partition ``(0, L]``
and ``{0}`` is handled separately, so **exactly one** run owns every offset.

These randomized tests are the guard that caught (2) and that keeps the invariant
honest under refactoring. Invariants checked per trial:

  * accepted-view == the target text         (the edit applied correctly)
  * rejected-view == the original text        (reject restores the original)
  * the document validates (well-formed OOXML)
  * every field's begin/separate/end fldChars + instrText survive
  * a *refused* edit (skipped_field_paras) leaves the paragraph byte-unchanged
    (all-or-nothing rollback — a refusal is loud and safe, never silent loss)
"""
import random

from lxml import etree

from zoterocite import Docx, new_doc, read_views, validate
from zoterocite.docxio import DOCUMENT
from zoterocite.ooxml import qn
from zoterocite.revisions import tracked_replace_paragraphs
from zoterocite.zoterofield import _field_runs, _plain_run, _runs

AC = "Cohen, Alexander (Neurology)"
WORDS = ["alpha", "beta", "gamma", "delta", "cohort", "finding", "model",
         "result", "prior", "novel", "the", "a", "of", "in", "we", "show"]


# ---------------------------------------------------------------------------
# Random cited-paragraph generator
# ---------------------------------------------------------------------------
def _random_tokens(rnd):
    """A sequence of word / citation tokens with >=1 word and >=1 citation."""
    toks, nw, nc = [], 0, 0
    for _ in range(rnd.randint(2, 7)):
        if rnd.random() < 0.4:
            toks.append(("c", f"({rnd.randint(1, 99)})")); nc += 1
        else:
            toks.append(("w", rnd.choice(WORDS))); nw += 1
    if nc == 0:
        toks.insert(rnd.randrange(len(toks) + 1), ("c", "(7)"))
    if nw == 0:
        toks.insert(rnd.randrange(len(toks) + 1), ("w", "tail"))
    return toks


def _to_pieces(toks):
    """Render tokens joined by single spaces into (full_text, [(kind, text), ...]).
    The space separating tokens is folded into the PLAIN run preceding a field, so
    fields are always whitespace-bounded (like a real '...finding (6) across...')
    and never share a word-token with prose."""
    pieces, cur = [], ""
    for i, (k, v) in enumerate(toks):
        sep = "" if i == 0 else " "
        if k == "w":
            cur += sep + v
        else:
            cur += sep
            if cur:
                pieces.append(("plain", cur)); cur = ""
            pieces.append(("field", v))
    if cur:
        pieces.append(("plain", cur))
    return "".join(v for _, v in pieces), pieces


def _build(tmp_path, pieces, idx):
    """Build a one-paragraph doc from a pieces layout; return (path, n_fields)."""
    src = tmp_path / f"f{idx}.docx"
    new_doc(src, [{"runs": [{"text": ""}]}])
    doc = Docx(src)
    p = list(doc.tree(DOCUMENT).iter(qn("w:p")))[0]
    for r in list(p):
        if r.tag == qn("w:r"):
            p.remove(r)
    nf = 0
    for kind, val in pieces:
        if kind == "plain":
            r = etree.SubElement(p, qn("w:r"))
            t = etree.SubElement(r, qn("w:t"))
            t.set(qn("xml:space"), "preserve"); t.text = val
        else:
            nf += 1
            instr = f'ADDIN ZOTERO_ITEM CSL_CITATION {{"citationID":"F{nf}"}}'
            for r in _runs(_field_runs(instr, _plain_run(val))):
                p.append(r)
    doc.save(src)
    return src, nf


def _mask(pieces):
    m = []
    for kind, val in pieces:
        m.extend([kind == "plain"] * len(val))
    return m


def _words(full, mask):
    """Maximal runs [a,b) of plain, non-space chars (whole words)."""
    out, i, L = [], 0, len(full)
    while i < L:
        if mask[i] and full[i] != " ":
            j = i
            while j < L and mask[j] and full[j] != " ":
                j += 1
            out.append((i, j)); i = j
        else:
            i += 1
    return out


def _field_ok(path, nf):
    body = Docx(path).raw(DOCUMENT).decode()
    return body.count("<w:fldChar") == 3 * nf and body.count("<w:instrText") == nf


def _single_edit(full, mask, rnd):
    """One insert / delete / replace confined to plain text (insert anchored where
    the char to the LEFT is plain -> covers word-starts, the prose|field seam, and
    end-of-text)."""
    L = len(full)
    op = rnd.choice(["insert", "insert", "insert", "delete", "replace"])
    if op == "insert":
        cands = [k for k in range(1, L + 1) if mask[k - 1] and (k == L or full[k] != " ")]
        if not cands:
            return None
        k = rnd.choice(cands)
        marker = "XINS" if k == L else "XINS "
        return full[:k] + marker + full[k:]
    ws = _words(full, mask)
    if not ws:
        return None
    a, b = rnd.choice(ws)
    if op == "replace":
        return full[:a] + "ZED" + full[b:]
    if b < L and full[b] == " " and mask[b]:
        s, e = a, b + 1
    elif a > 0 and full[a - 1] == " " and mask[a - 1]:
        s, e = a - 1, b
    else:
        s, e = a, b
    return full[:s] + full[e:]


def _multi_edit(full, mask, rnd):
    """2-3 simultaneous edits at distinct, non-adjacent positions (two seam
    insertions + an optional distant word replace), spliced right-to-left."""
    L = len(full)
    seams = [k for k in range(1, L + 1) if mask[k - 1] and (k == L or full[k] != " ")]
    ws = _words(full, mask)
    if len(seams) < 2 or not ws:
        return None
    rnd.shuffle(seams)
    chosen, used = [], set()
    for k in seams:
        if all(abs(k - u) > 1 for u in used):
            chosen.append(("ins", k)); used.add(k)
        if len(chosen) == 2:
            break
    if len(chosen) < 2:
        return None
    for (a, b) in rnd.sample(ws, len(ws)):
        if all(not (a - 1 <= u <= b + 1) for u in used):
            chosen.append(("rep", a, b)); break
    chosen.sort(key=lambda e: e[1], reverse=True)
    s = full
    for e in chosen:
        if e[0] == "ins":
            k = e[1]
            s = s[:k] + ("YY" if k == L else "YY ") + s[k:]
        else:
            _, a, b = e
            s = s[:a] + "QQ" + s[b:]
    return s


def _run_fuzz(tmp_path, edit_fn, seeds, trials):
    """Drive the engine over random cited paragraphs; return a list of invariant
    violations (empty == all good).  read_views strips the paragraph's leading/
    trailing whitespace, so the target is compared end-stripped."""
    violations = []
    n = 0
    for seed in seeds:
        rnd = random.Random(seed)
        for _ in range(trials):
            n += 1
            toks = _random_tokens(rnd)
            full, pieces = _to_pieces(toks)
            src, nf = _build(tmp_path, pieces, n)
            old = read_views(src)["accepted"]
            if old != full:                 # builder sanity (whitespace model)
                violations.append((seed, "BUILD", repr(old), repr(full))); continue
            new = edit_fn(full, _mask(pieces), rnd)
            if not new or new == full:
                continue
            doc = Docx(src)
            try:
                tracked_replace_paragraphs(doc, {0: new}, author=AC, scope="word")
            except Exception as ex:  # noqa: BLE001
                violations.append((seed, f"RAISED {ex!r}", pieces, new)); continue
            out = tmp_path / f"o{n}.docx"
            doc.save(out)
            if doc.skipped_field_paras:
                # A refusal must be a clean no-op (all-or-nothing rollback).
                if (read_views(out)["accepted"] != old
                        or not validate(out).ok or not _field_ok(out, nf)):
                    violations.append((seed, "CORRUPTED SKIP", pieces, new))
                continue
            acc = read_views(out)["accepted"]
            rej = read_views(out)["rejected"]
            if acc != new.strip():
                violations.append((seed, "ACCEPTED!=new", repr(acc), repr(new.strip())))
            if rej != old:
                violations.append((seed, "REJECTED!=old", repr(rej), repr(old)))
            if not validate(out).ok:
                violations.append((seed, "INVALID", pieces, new))
            if not _field_ok(out, nf):
                violations.append((seed, "FIELD altered", pieces, new))
    return violations


def test_fuzz_single_edit_cited_paragraph(tmp_path):
    """Random single insert/delete/replace on random cited paragraphs (fields at
    start / middle / end, multiple adjacent fields, both seam orientations)."""
    viol = _run_fuzz(tmp_path, _single_edit, seeds=(1, 2, 3), trials=45)
    assert viol == [], viol[:10]


def test_fuzz_multi_edit_cited_paragraph(tmp_path):
    """Two+ simultaneous plain edits — stresses the per-run sorted-edit loop
    together with the seam-ownership rule."""
    viol = _run_fuzz(tmp_path, _multi_edit, seeds=(1, 2), trials=45)
    assert viol == [], viol[:10]


# ---------------------------------------------------------------------------
# Focused, deterministic seam cases (fast, explicit — complement the fuzz)
# ---------------------------------------------------------------------------
def _make_cited(tmp_path, prefix, rendered, suffix, name="src.docx"):
    src = tmp_path / name
    new_doc(src, [{"runs": [{"text": prefix}]}])
    doc = Docx(src)
    p = list(doc.tree(DOCUMENT).iter(qn("w:p")))[0]
    instr = 'ADDIN ZOTERO_ITEM CSL_CITATION {"citationID":"ABC"}'
    for r in _runs(_field_runs(instr, _plain_run(rendered))):
        p.append(r)
    sr = etree.SubElement(p, qn("w:r"))
    st = etree.SubElement(sr, qn("w:t"))
    st.set(qn("xml:space"), "preserve"); st.text = suffix
    doc.save(src)
    return src


def test_insert_at_prose_field_seam_lands_before_field(tmp_path):
    """The canonical regression: a word inserted at the prose|field seam lands
    BEFORE the citation (single-spaced), not after it."""
    src = _make_cited(tmp_path, "aa bb ", "(7)", " cc.")
    assert read_views(src)["accepted"] == "aa bb (7) cc."
    new = "aa bb X (7) cc."
    doc = Docx(src)
    tracked_replace_paragraphs(doc, {0: new}, author=AC, scope="word")
    assert doc.skipped_field_paras == []
    out = tmp_path / "out.docx"; doc.save(out)
    assert read_views(out)["accepted"] == new
    assert read_views(out)["rejected"] == "aa bb (7) cc."
    assert validate(out).ok


def test_insert_at_field_prose_seam_after_leading_field(tmp_path):
    """A word inserted just after a citation lands AFTER the field."""
    src = _make_cited(tmp_path, "aa ", "(7)", " bb cc.")
    assert read_views(src)["accepted"] == "aa (7) bb cc."
    new = "aa (7) X bb cc."
    doc = Docx(src)
    tracked_replace_paragraphs(doc, {0: new}, author=AC, scope="word")
    assert doc.skipped_field_paras == []
    out = tmp_path / "out.docx"; doc.save(out)
    assert read_views(out)["accepted"] == new
    assert read_views(out)["rejected"] == "aa (7) bb cc."
    assert validate(out).ok
