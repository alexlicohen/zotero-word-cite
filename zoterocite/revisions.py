"""Tracked changes (w:ins / w:del) under a named author — surgical and run-split-safe.

Mirrors the OOXML shape Word produces, including the subtle case of *deleting text
that is itself a pending insertion* (`<w:ins><w:del><w:r><w:delText>`), which is
exactly the case that broke naive regex editing.

Also: accept_all / reject_all (text-level) and spread_timestamps.
"""
from __future__ import annotations

import copy
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import List, Optional

from lxml import etree

from .docxio import DOCUMENT, Docx
from .ooxml import DATE_FMT, NS, now_iso, qn
from .paras import find_paragraph, get_body, iter_paragraphs, paragraph_text

W = NS["w"]
# Retained for backward compatibility / explicit callers. NOT used as a default
# any more — unset dates resolve to the real current time (see now_iso) so Word
# no longer shows every edit as 1/1/26. Spread a set of edits across a realistic
# session window with spread_timestamps().
DEFAULT_DATE = "2026-01-01T00:00:00Z"

# Word-level redline (see _redline_para_text): split on words, keeping each word's
# trailing whitespace glued so a deletion boundary never lands mid-word.
_WS_SPLIT = re.compile(r"\S+\s*")
# An unchanged span this many characters or longer is kept untouched (its own
# clean run); shorter unchanged spans wedged *between* edits are absorbed into the
# surrounding change, so a heavily-edited paragraph shows a few clean edit spans
# with the unchanged sentences left intact — instead of fragmenting around
# incidental common words or collapsing to a whole-paragraph replace.
_REDLINE_KEEP_MIN_CHARS = 8


# -- id allocation -----------------------------------------------------------
def _max_rev_id(root) -> int:
    mx = 0
    for el in root.iter():
        if el.tag in (qn("w:ins"), qn("w:del")):
            try:
                mx = max(mx, int(el.get(qn("w:id"), 0)))
            except ValueError:
                pass
    return mx


class _Ids:
    def __init__(self, start: int):
        self.n = start

    def next(self) -> str:
        self.n += 1
        return str(self.n)


# -- run building ------------------------------------------------------------
def _first_rpr(p) -> Optional[etree._Element]:
    r = p.find(".//" + qn("w:r"))
    if r is not None:
        rpr = r.find(qn("w:rPr"))
        if rpr is not None:
            return copy.deepcopy(rpr)
    return None


def _make_run(text: str, rpr: Optional[etree._Element]):
    r = etree.Element(qn("w:r"))
    if rpr is not None:
        r.append(copy.deepcopy(rpr))
    t = etree.SubElement(r, qn("w:t"))
    t.set(qn("xml:space"), "preserve")
    t.text = text
    return r


def _ins(ids, author, date, *children):
    el = etree.Element(qn("w:ins"))
    el.set(qn("w:id"), ids.next())
    el.set(qn("w:author"), author)
    el.set(qn("w:date"), date)
    for c in children:
        el.append(c)
    return el


def _wrap_run_as_deletion(r, ids, author, date) -> None:
    """In place: <w:r>…<w:t>  ->  <w:del><w:r>…<w:delText></w:del> (skip if already deleted)."""
    anc = r.getparent()
    while anc is not None:
        if anc.tag == qn("w:del"):
            return  # already inside a deletion
        anc = anc.getparent()
    parent = r.getparent()
    idx = parent.index(r)
    d = etree.Element(qn("w:del"))
    d.set(qn("w:id"), ids.next())
    d.set(qn("w:author"), author)
    d.set(qn("w:date"), date)
    parent.remove(r)
    d.append(r)
    parent.insert(idx, d)
    for t in r.findall(qn("w:t")):
        t.tag = qn("w:delText")


# -- private batch helper ----------------------------------------------------
def _replace_para_text(p, new_text: str, ids: _Ids, author: str, date: str,
                       rpr: Optional[etree._Element] = None) -> None:
    """Core del+ins on a paragraph element (in-place, no doc/root needed).

    Wraps each text-bearing run as a tracked deletion, then appends a tracked
    insertion carrying `new_text` with the preserved first-run rPr.
    """
    keep_rpr = rpr if rpr is not None else _first_rpr(p)
    for r in list(p.iter(qn("w:r"))):
        if r.find(qn("w:t")) is not None:
            _wrap_run_as_deletion(r, ids, author, date)
    p.append(_ins(ids, author, date, _make_run(new_text, keep_rpr)))


# -- word-level redline ------------------------------------------------------
def _word_tokens(text: str) -> List[str]:
    """Split into word tokens, each carrying its trailing whitespace, so the join
    is lossless and split boundaries fall on whitespace gaps (never mid-word)."""
    if not text:
        return []
    toks = _WS_SPLIT.findall(text)
    if not toks:                      # all-whitespace
        return [text]
    lead = text[: len(text) - len(text.lstrip())]
    if lead:                          # \S+ dropped any leading whitespace; restore it
        toks[0] = lead + toks[0]
    return toks


# Text-less run children that carry no author content and are safe to drop when a
# run is rebuilt from its w:t (Word regenerates lastRenderedPageBreak on the next
# layout; a soft hyphen is discretionary). Real paginated manuscripts are full of
# lastRenderedPageBreak, so NOT tolerating it sent most paragraphs to the
# whole-paragraph fallback. Flow-affecting children (w:br/w:tab/w:cr/w:drawing/…)
# are deliberately excluded — dropping them would change the document, so such
# paragraphs still fall back.
_REDLINE_DROPPABLE_RUN_KIDS = frozenset({"w:lastRenderedPageBreak", "w:softHyphen"})


def _redline_eligible(p) -> bool:
    """Granular redline is safe only on a plain prose paragraph: no pre-existing
    tracked changes, and only pPr + plain text runs (whose children are rPr/t plus
    text-less layout hints we can safely drop). Hyperlinks, bookmarks, comment
    markers, tabs/breaks, drawings, etc. fall back to whole-paragraph."""
    if p.find(".//" + qn("w:ins")) is not None or p.find(".//" + qn("w:del")) is not None:
        return False
    allowed = {qn("w:rPr"), qn("w:t")} | {qn(t) for t in _REDLINE_DROPPABLE_RUN_KIDS}
    for ch in p:
        if ch.tag == qn("w:pPr"):
            continue
        if ch.tag == qn("w:r"):
            if any(rc.tag not in allowed for rc in ch):
                return False
            continue
        return False
    return True


def _del_wrap(run, ids, author, date):
    """Wrap a freshly built run as a tracked deletion (<w:del><w:r><w:delText>)."""
    d = etree.Element(qn("w:del"))
    d.set(qn("w:id"), ids.next())
    d.set(qn("w:author"), author)
    d.set(qn("w:date"), date)
    for t in run.findall(qn("w:t")):
        t.tag = qn("w:delText")
    d.append(run)
    return d


def _redline_para_text(p, new_text: str, ids: _Ids, author: str, date: str,
                       *, rpr: Optional[etree._Element] = None) -> bool:
    """Word-token redline: strike + insert only the spans that actually changed,
    leaving unchanged spans as normal runs (so each edit is independently
    accept/rejectable and the reviewer sees exactly what changed).

    Anchors on the longest common subsequence (difflib opcodes) and keeps every
    *substantial* unchanged span untouched; short unchanged spans wedged between
    edits are absorbed into the surrounding change so the redline isn't
    fragmented by incidental common words. A near-total rewrite naturally falls
    out as one struck + one inserted region (no special-casing).

    Returns True if a granular diff was applied; False only when the paragraph is
    ineligible (pre-existing tracked changes / non-prose runs), to signal the
    caller to fall back to whole-paragraph. Per-run rPr is preserved; emitted
    markup is plain w:ins/w:del (delText), the shape accept_all/reject_all handle.
    """
    old = paragraph_text(p)
    if old == new_text:
        return True                                   # no-op
    if not _redline_eligible(p):
        return False
    old_toks, new_toks = _word_tokens(old), _word_tokens(new_text)
    opcodes = SequenceMatcher(None, old_toks, new_toks, autojunk=False).get_opcodes()

    # map each old character to its source run's rPr
    char_rpr_idx: List[int] = []
    run_rprs: List[Optional[etree._Element]] = []
    for r in p:
        if r.tag != qn("w:r") or r.find(qn("w:t")) is None:
            continue
        rpr_el = r.find(qn("w:rPr"))
        run_rprs.append(copy.deepcopy(rpr_el) if rpr_el is not None else None)
        idx = len(run_rprs) - 1
        txt = "".join(t.text or "" for t in r.findall(qn("w:t")))
        char_rpr_idx.extend([idx] * len(txt))
    if len(char_rpr_idx) != len(old):                 # defensive: text model mismatch
        return False

    keep_rpr = rpr if rpr is not None else (_first_rpr(p) if old else None)
    old_off = [0]
    for t in old_toks:
        old_off.append(old_off[-1] + len(t))

    def rpr_at(a: int):
        if a < len(old):
            return run_rprs[char_rpr_idx[a]]
        if old:
            return run_rprs[char_rpr_idx[len(old) - 1]]
        return keep_rpr

    def emit_span(a: int, b: int, deleted: bool):
        out = []
        k = a
        while k < b:
            idx = char_rpr_idx[k]
            j = k
            while j < b and char_rpr_idx[j] == idx:
                j += 1
            run = _make_run(old[k:j], run_rprs[idx])
            out.append(_del_wrap(run, ids, author, date) if deleted else run)
            k = j
        return out

    # Coalesce opcodes into regions. A 'kept' region is a substantial unchanged
    # span emitted untouched; a 'change' region is a contiguous edit emitted as
    # strike(old)+insert(new). A short unchanged span *between* two edits is
    # absorbed into the change so the redline stays compact; a short unchanged
    # span at a boundary (after a kept span or at the start) is kept.
    change_after = [False] * len(opcodes)             # is there an edit later than k?
    seen = False
    for k in range(len(opcodes) - 1, -1, -1):
        change_after[k] = seen
        if opcodes[k][0] != "equal":
            seen = True

    regions = []   # {"oi": (i1, i2), "nj": (j1, j2), "change": bool}
    for k, (tag, i1, i2, j1, j2) in enumerate(opcodes):
        if tag == "equal":
            substantial = len("".join(old_toks[i1:i2]).strip()) >= _REDLINE_KEEP_MIN_CHARS
            # absorb a short unchanged span only when wedged BETWEEN edits (an open
            # change region precedes it and another edit follows); keep it otherwise.
            if not substantial and regions and regions[-1]["change"] and change_after[k]:
                regions[-1]["oi"] = (regions[-1]["oi"][0], i2)
                regions[-1]["nj"] = (regions[-1]["nj"][0], j2)
            else:
                regions.append({"oi": (i1, i2), "nj": (j1, j2), "change": False})
        elif regions and regions[-1]["change"]:       # extend the open change region
            regions[-1]["oi"] = (regions[-1]["oi"][0], i2)
            regions[-1]["nj"] = (regions[-1]["nj"][0], j2)
        else:
            regions.append({"oi": (i1, i2), "nj": (j1, j2), "change": True})

    new_children = []
    for reg in regions:
        a, b = old_off[reg["oi"][0]], old_off[reg["oi"][1]]
        if not reg["change"]:
            new_children += emit_span(a, b, False)                    # untouched
            continue
        if b > a:
            new_children += emit_span(a, b, True)                     # struck
        new_sub = "".join(new_toks[reg["nj"][0]:reg["nj"][1]])
        if new_sub:
            new_children.append(_ins(ids, author, date, _make_run(new_sub, rpr_at(a))))

    for ch in [c for c in p if c.tag == qn("w:r")]:   # drop old runs, keep pPr
        p.remove(ch)
    for el in new_children:
        p.append(el)
    return True


# -- surgical (field-safe) single-run split ----------------------------------
def _minimal_diff(old: str, new: str):
    """The single contiguous span that differs between ``old`` and ``new`` (longest
    common prefix + suffix stripped) as ``(old_mid, new_mid)``, or ``None`` if equal.
    A paragraph with several edits collapses to the one span covering first-to-last
    change."""
    if old == new:
        return None
    plen = 0
    while plen < len(old) and plen < len(new) and old[plen] == new[plen]:
        plen += 1
    slen = 0
    while (slen < len(old) - plen and slen < len(new) - plen
           and old[len(old) - 1 - slen] == new[len(new) - 1 - slen]):
        slen += 1
    return old[plen:len(old) - slen], new[plen:len(new) - slen]


# -- complex-field awareness (Zotero citation safety) ------------------------
def _has_complex_field(p) -> bool:
    """True if paragraph ``p`` contains ANY complex-field marker (``<w:fldChar>``)
    — i.e. a Word complex field such as a live Zotero ``ZOTERO_ITEM`` citation. Such
    paragraphs must never reach the whole-paragraph del+ins path, which would strike
    the field's rendered result and bake it in as static literal text (silently
    killing the live field). Routed instead to the field-safe surgical path."""
    return p.find(".//" + qn("w:fldChar")) is not None


def _plain_text_mask(p):
    """Project paragraph ``p`` onto two parallel strings: its full accepted-view
    text and a per-character flag marking which characters live in a PLAIN prose run
    (outside any complex field) versus inside a field (the begin/instr/separate/end
    markers and the rendered citation result between ``separate`` and ``end``).

    Returns ``(full_text, plain_flags, plain_text)`` where ``plain_flags[i]`` is
    True iff ``full_text[i]`` came from a plain run that :func:`_surgical_replace_run`
    is allowed to split, and ``plain_text`` is the concatenation of just those
    characters (exactly the text :func:`_surgical_replace_run` searches). Returns
    ``None`` when the run-level reconstruction does NOT match :func:`paragraph_text`
    — an unusual structure (tracked changes already present, a tab/break/drawing
    INTERLEAVED inside a text run, content-controls (``w:sdt``), nested fields) that we
    refuse to touch surgically rather than risk corrupting. A STANDALONE ``<w:tab>``/
    ``<w:br>``/``<w:cr>`` or ``<w:drawing>`` run (its own run, no co-located text) IS
    modelled — transparent, marked non-plain, left byte-identical (a drawing emits no
    text; if it contains a nested textbox the reconstruction backstop still refuses).

    Only the plain-run + fldChar/instrText shape this package emits is modelled; a
    paragraph carrying pre-existing ``<w:ins>``/``<w:del>`` reconstructs differently
    (deleted text excluded from ``paragraph_text`` but present in the run walk) and
    is rejected here, falling through to a safe refusal upstream."""
    fld_tag, instr_tag, t_tag = qn("w:fldChar"), qn("w:instrText"), qn("w:t")
    tab_tag, br_tag, cr_tag = qn("w:tab"), qn("w:br"), qn("w:cr")
    draw_tag = qn("w:drawing")
    typ = qn("w:fldCharType")
    full_chars: List[str] = []
    plain_flags: List[bool] = []
    depth = 0
    for ch in p:
        if ch.tag != qn("w:r"):
            # pPr and zero-text STRUCTURAL markers (proof-error squiggles, bookmark
            # range markers) carry no author text and do not appear in paragraph_text,
            # so they are TRANSPARENT to the plain/field projection — skip them rather
            # than refusing. Real paginated manuscripts interleave w:proofErr between
            # runs (68 in the live TSC manuscript); refusing on them force-skipped
            # every paragraph that had one (bug W1-3). They are preserved in document
            # order untouched because the in-place splitter only ever mutates the
            # specific plain <w:r> runs it edits — it never reorders siblings.
            if ch.tag in (qn("w:pPr"), qn("w:proofErr"),
                          qn("w:bookmarkStart"), qn("w:bookmarkEnd")):
                continue
            # A text-bearing wrapper we can't model (pre-existing w:ins/w:del, a
            # hyperlink, smartTag) means this isn't the plain-prose-plus-field shape
            # we handle — refuse conservatively rather than risk corruption.
            return None
        r = ch
        fc = r.find(fld_tag)
        in_field = depth > 0
        if fc is not None:
            kind = fc.get(typ)
            if kind == "begin":
                depth += 1
            elif kind == "end" and depth > 0:
                depth -= 1
        is_field_run = fc is not None or r.find(instr_tag) is not None or in_field
        # Classify the run's children. Plain text (w:t) and the field markers are
        # modelled. A STANDALONE structural run (only w:tab / w:br / w:cr, no w:t) is
        # rendered to its \t / \n by paragraph_text, so it is TRANSPARENT here: emit
        # those chars marked NON-plain (an edit never splits across a tab/break) and
        # leave the run itself untouched — it carries no w:t, so _plain_run_map skips
        # it and the in-place splitter never rewrites it (byte-identical). A w:tab/br
        # INTERLEAVED with editable text in the SAME run is ordering-ambiguous for the
        # offset splitter (findall(w:t) loses the structural child's position), so it is
        # refused. A drawing / sdt / other unmodelled child still refuses.
        has_t = False
        has_struct = False
        has_draw = False
        for rc in r:
            if rc.tag == t_tag:
                has_t = True
                continue
            if rc.tag in (qn("w:rPr"), fld_tag, instr_tag):
                continue
            if rc.tag in (qn("w:lastRenderedPageBreak"), qn("w:softHyphen")):
                continue
            if rc.tag in (tab_tag, br_tag, cr_tag):
                has_struct = True
                continue
            if rc.tag == draw_tag:                    # an inline image / shape anchor
                has_draw = True
                continue
            return None                               # sdt / AlternateContent / unmodelled -> refuse
        # A text-bearing run may NOT also carry a structural break or a drawing: the
        # offset splitter rebuilds a plain run from findall(w:t) and would DROP the
        # non-text child, so such a run is refused (kept whole) rather than corrupted.
        if has_t and (has_struct or has_draw):
            return None
        if has_struct or has_draw:                    # standalone structural/drawing run
            # Transparent: a tab/break emits its \t/\n; a drawing renders to NO text in
            # paragraph_text so it emits nothing. All non-plain. The run carries no w:t,
            # so _plain_run_map skips it and the in-place splitter leaves it byte-
            # identical. A drawing that CONTAINS nested text (a textbox) makes this walk
            # diverge from paragraph_text's descendant iter -> caught by the backstop.
            for rc in r:
                if rc.tag == tab_tag:
                    full_chars.append("\t")
                    plain_flags.append(False)
                elif rc.tag in (br_tag, cr_tag):
                    full_chars.append("\n")
                    plain_flags.append(False)
            continue
        for t in r.findall(t_tag):
            s = t.text or ""
            full_chars.extend(s)
            plain_flags.extend([not is_field_run] * len(s))
    full_text = "".join(full_chars)
    if full_text != paragraph_text(p):       # reconstruction mismatch -> refuse
        return None
    plain_text = "".join(c for c, f in zip(full_text, plain_flags) if f)
    return full_text, plain_flags, plain_text


def _surgical_replace_run(p, old_substr: str, new_substr: str, ids: _Ids,
                          author: str, date: str) -> bool:
    """Surgically replace the FIRST plain-run occurrence of ``old_substr`` with
    ``new_substr`` as a tracked change IN PLACE, splitting only the single plain
    ``<w:t>`` run that wholly contains it into ``[pre, <w:del>old</w:del>,
    <w:ins>new</w:ins>, post]`` (each piece carrying a deepcopy of the run's rPr).

    FIELD-SAFE: every run that is part of a complex field — the begin/separate/end
    ``<w:fldChar>`` markers, the ``<w:instrText>``, AND the rendered citation text
    between separate and end — is skipped, so citation fields are left physically
    untouched. Returns ``False`` (no change) when ``old_substr`` is absent, spans
    runs, or appears only inside a field."""
    fld_tag, instr_tag, t_tag = qn("w:fldChar"), qn("w:instrText"), qn("w:t")
    typ = qn("w:fldCharType")
    depth = 0   # nesting level inside a complex field (begin..end); render text lives here
    for r in [ch for ch in p if ch.tag == qn("w:r")]:
        fc = r.find(fld_tag)
        in_field = depth > 0
        if fc is not None:                       # begin/separate/end marker run
            kind = fc.get(typ)
            if kind == "begin":
                depth += 1
            elif kind == "end" and depth > 0:
                depth -= 1
        # Skip every run that is part of a field: the begin/separate/end marker
        # runs, the instrText, AND the rendered citation text between separate and
        # end. Only plain prose runs OUTSIDE any field are eligible.
        if fc is not None or r.find(instr_tag) is not None or in_field:
            continue
        ts = r.findall(t_tag)
        if not ts:
            continue
        full = "".join(t.text or "" for t in ts)
        k = full.find(old_substr)
        if k < 0:
            continue
        rpr = r.find(qn("w:rPr"))   # deepcopy preserved onto each piece by _make_run

        pre = full[:k]
        post = full[k + len(old_substr):]
        pieces = []
        if pre:
            pieces.append(_make_run(pre, rpr))
        pieces.append(_del_wrap(_make_run(old_substr, rpr), ids, author, date))
        pieces.append(_ins(ids, author, date, _make_run(new_substr, rpr)))
        if post:
            pieces.append(_make_run(post, rpr))

        for el in pieces:
            r.addprevious(el)
        p.remove(r)
        return True
    return False


def _surgical_in_para(p, new_text: str, ids: _Ids, author: str, date: str) -> bool:
    """Granular, field-safe replacement of paragraph ``p``'s text with ``new_text``
    when the difference is a SINGLE contiguous span that occurs exactly once in a
    plain run. Computes the minimal diff and delegates to :func:`_surgical_replace_run`.

    Returns ``True`` if applied (including a no-op when the text already matches),
    ``False`` when the change is not a single uniquely-locatable plain-run span (so
    the caller falls back to a whole-paragraph replace)."""
    old = paragraph_text(p)
    if old == new_text:
        return True                                   # no-op
    md = _minimal_diff(old, new_text)
    if md is None:                                    # unreachable (old != new_text) but defensive
        return True
    old_mid, new_mid = md
    if not old_mid or old.count(old_mid) != 1:
        return False
    return _surgical_replace_run(p, old_mid, new_mid, ids, author, date)


def _plain_run_map(p):
    """Ordered model of paragraph ``p``'s PLAIN prose runs, in plain-text index space.

    Returns a list of ``(run_element, start, end, rpr)`` where ``[start, end)`` is the
    half-open span of the run's text within ``plain_text`` (the concatenation of all
    plain-run text, exactly the string :func:`_plain_text_mask` returns as its third
    value) and ``rpr`` is a deepcopy of the run's ``<w:rPr>`` (or ``None``). FIELD runs
    — the begin/separate/end ``<w:fldChar>`` markers, the ``<w:instrText>``, and the
    rendered citation result between separate and end — are NOT included: they carry no
    span here and are never touched.

    Mirrors the field-walk in :func:`_plain_text_mask`/:func:`_surgical_replace_run` so
    the plain-run partition is identical; the caller has already verified via
    :func:`_plain_text_mask` that this is the modelled plain-prose-plus-field shape, so
    the concatenated run spans reconstruct ``plain_text`` exactly."""
    fld_tag, instr_tag, t_tag = qn("w:fldChar"), qn("w:instrText"), qn("w:t")
    typ = qn("w:fldCharType")
    runs = []
    pos = 0
    depth = 0
    for r in [ch for ch in p if ch.tag == qn("w:r")]:
        fc = r.find(fld_tag)
        in_field = depth > 0
        if fc is not None:
            kind = fc.get(typ)
            if kind == "begin":
                depth += 1
            elif kind == "end" and depth > 0:
                depth -= 1
        if fc is not None or r.find(instr_tag) is not None or in_field:
            continue                                  # field run — skip, never split
        txt = "".join(t.text or "" for t in r.findall(t_tag))
        if not txt:
            continue                                  # text-less plain run (no w:t)
        rpr = r.find(qn("w:rPr"))
        rpr = copy.deepcopy(rpr) if rpr is not None else None
        runs.append((r, pos, pos + len(txt), rpr))
        pos += len(txt)
    return runs


def _apply_inplace_plain_edits(p, edits, plain_runs, ids, author, date) -> bool:
    """Apply position-anchored plain-text edits IN PLACE on a cited paragraph, splitting
    only the plain runs each edit touches and leaving every field run (and every
    untouched plain run) physically byte-identical.

    ``edits`` is a list of ``(pa, pb, new_sub, anchor_rpr)`` in plain-text offset space,
    NON-OVERLAPPING and sorted ascending by ``pa``:
      * a replacement/deletion strikes ``plain_text[pa:pb]`` (``pb > pa``) — each fully
        or partially covered plain run contributes a ``<w:del>`` preserving ITS OWN rPr,
        so an edit spanning a run boundary (e.g. a bold word mid-prose) keeps each
        struck piece's formatting — and inserts ``new_sub`` once as a ``<w:ins>``;
      * a pure insertion (``pa == pb``) inserts ``new_sub`` as a ``<w:ins>`` at that
        plain-text boundary, splitting the containing plain run if pa falls inside one.

    SINGLE-PASS, MAP-DRIVEN: every original plain run is rewritten EXACTLY ONCE from the
    (still-attached) ``plain_runs`` map, then spliced in. This is correct under multiple
    edits to the SAME run (each cut point is resolved against the original text), and it
    never re-references a run after mutating the tree (which detaches it). Edits are
    non-overlapping and sorted, so per-run cut points are monotonic. Returns ``True``."""
    W_R, W_DEL = qn("w:r"), qn("w:del")

    # Partition each plain run into ordered segments. A segment is either text that is
    # kept (emit a plain run), struck (emit <w:del>), or an insertion point (emit
    # <w:ins>). We walk each run's [s, e) and apply every edit that overlaps or anchors
    # within it, in plain-text order.
    rewrites = []   # (run_element, [replacement elements in order])
    n_runs = len(plain_runs)
    for ri, (r, s, e, rpr) in enumerate(plain_runs):
        full = "".join(t.text or "" for t in r.findall(qn("w:t")))
        pieces = []
        cursor = s                                    # plain offset within [s, e]
        # Edits that interact with THIS run: a replacement overlapping [s, e), or a
        # pure insertion whose boundary pa is strictly inside (s < pa < e) OR sits at
        # the run's start/end (assigned to the run on whose side it falls — start goes
        # to this run when this is the first run owning that boundary).
        for (pa, pb, new_sub, anchor_rpr) in edits:
            if pb > pa:                               # replacement/deletion
                if pa >= e or pb <= s:
                    continue                          # no overlap with this run
                la = max(pa, s)
                lb = min(pb, e)
                if la > cursor:                       # kept text before the strike
                    pieces.append(_make_run(full[cursor - s:la - s], rpr))
                if lb > la:
                    pieces.append(_del_wrap(
                        _make_run(full[la - s:lb - s], rpr), ids, author, date))
                # Insert the new text ONCE, attributed to the run that starts the
                # replacement (pa). For a run-spanning strike, only the first run emits
                # the <w:ins> — immediately after its <w:del> piece.
                if new_sub and pa >= s and pa < e:
                    pieces.append(_ins(ids, author, date,
                                       _make_run(new_sub, anchor_rpr)))
                cursor = lb
            else:                                     # pure insertion at boundary pa
                # Own the boundary on the run ENDING at pa (the previous run) plus
                # any strictly-interior point; the global-start boundary (pa == 0,
                # which no run ends at) is owned by the first run. Attaching to the
                # run that ENDS at pa places the inserted text at that run's end —
                # i.e. BEFORE any following complex field — so an insertion at a
                # prose|field seam lands before the citation, not after it. Exactly
                # one run owns each seam, so the <w:ins> is never dropped (the old
                # pair of deferral rules left an internal seam owned by neither).
                owns = (s < pa <= e) or (pa == s and ri == 0)
                if not owns:
                    continue
                if pa > cursor:
                    pieces.append(_make_run(full[cursor - s:pa - s], rpr))
                    cursor = pa
                pieces.append(_ins(ids, author, date, _make_run(new_sub, anchor_rpr)))
        if cursor < e:                                # trailing kept text
            pieces.append(_make_run(full[cursor - s:], rpr))
        # Only rewrite runs that actually changed (avoid needless re-serialization of
        # untouched runs — keeps them byte-identical).
        changed = not (len(pieces) == 1 and pieces[0].tag == W_R
                       and pieces[0].find(W_DEL) is None
                       and "".join(t.text or "" for t in pieces[0].findall(qn("w:t"))) == full
                       and pieces[0].find(qn("w:ins")) is None)
        if changed:
            rewrites.append((r, pieces))

    # Splice: replace each rewritten run with its pieces, in place.
    for r, pieces in rewrites:
        parent = r.getparent()
        if parent is None:
            return False                              # defensive: detached run
        idx = parent.index(r)
        parent.remove(r)
        for off, el in enumerate(pieces):
            parent.insert(idx + off, el)
    return True


def _surgical_in_cited_para(p, new_text: str, ids: _Ids, author: str, date: str) -> bool:
    """FIELD-SAFE replacement on a paragraph that carries a complex field (a live
    Zotero citation). Edits ONLY plain prose runs IN PLACE, leaving the field's
    begin/instr/separate/end markers and its rendered citation result physically
    byte-identical, and handles MULTIPLE non-adjacent changed spans (a normal
    multi-word voice rewrite) — including a changed span that crosses a plain-run
    boundary (bold mid-prose), which the old search-based path could not.

    POSITION/OFFSET-ANCHORED design (replaces the prior text-search + uniqueness gate,
    which refused a normal ``'using'->'under'`` edit because its LCS-stripped core
    ``'sing'`` is non-unique in real prose — bug W1-2, and borrowed a possibly-
    non-unique adjacent token for insertions — bug W1-4):

      * Project the paragraph onto its full accepted-view text and a plain-vs-field
        per-character mask (:func:`_plain_text_mask`). Refuse if the shape is unmodelled.
      * Build a plain-character -> source-run map (:func:`_plain_run_map`) over the
        SAME plain runs the mask flags, in plain-text index space.
      * Diff old (full) vs ``new_text`` with difflib word opcodes. For EVERY changed
        region: it must lie wholly inside plain text — if any char belongs to the field
        (its rendered citation result), the edit alters the citation render itself, which
        we REFUSE (return ``False``) rather than corrupt. Narrow each region to its
        minimal changed sub-span via :func:`_minimal_diff` (so only the truly-changed
        characters are struck — a minimal redline), convert that span from FULL-text to
        PLAIN-text offsets via the cumulative count of plain chars, and queue a
        position-anchored edit. NO text search, NO uniqueness gate.
      * Apply all queued edits in place (:func:`_apply_inplace_plain_edits`): split only
        the touched plain run(s) at the known offsets, wrapping each struck piece as
        ``<w:del>`` preserving its rPr and inserting the new text once as ``<w:ins>``;
        field runs and untouched plain runs are left exactly as-is.

    All-or-nothing: the paragraph's children are snapshotted first, so any refusal (a
    region touching the field, or an internal inconsistency) restores it byte-identical.

    Returns ``True`` if applied (or a no-op when unchanged), ``False`` to signal the
    caller to SKIP-AND-REPORT this paragraph — never to fall back to the
    whole-paragraph del+ins path, which would destroy the field. Refusing is correct:
    a citation edit we can't make safely is dropped loudly, never silently corrupted."""
    proj = _plain_text_mask(p)
    if proj is None:
        return False                                  # unmodelled shape -> refuse, don't corrupt
    full_text, plain_flags, plain_text = proj
    if full_text == new_text:
        return True                                   # no-op

    plain_runs = _plain_run_map(p)
    # Defensive: the plain-run partition must reconstruct plain_text exactly. If not,
    # the model and the mask disagree — refuse rather than risk a misplaced split.
    if "".join(
        ("".join(t.text or "" for t in r.findall(qn("w:t"))))
        for (r, _s, _e, _rpr) in plain_runs
    ) != plain_text:
        return False

    # Cumulative count of plain chars strictly before each FULL-text offset, so a
    # full-text span [a,b) (already verified plain-only) maps to plain offsets
    # [plain_before[a], plain_before[b]).
    plain_before = [0] * (len(full_text) + 1)
    acc = 0
    for i, f in enumerate(plain_flags):
        plain_before[i] = acc
        if f:
            acc += 1
    plain_before[len(full_text)] = acc

    def rpr_at_plain(po):
        """rPr of the plain run covering plain offset ``po`` (the run starting at ``po``
        for a boundary; the last run for end-of-text)."""
        for (r, s, e, rpr) in plain_runs:
            if s <= po < e:
                return rpr
        if plain_runs:
            return plain_runs[-1][3]
        return None

    old_toks, new_toks = _word_tokens(full_text), _word_tokens(new_text)
    opcodes = SequenceMatcher(None, old_toks, new_toks, autojunk=False).get_opcodes()
    old_off = [0]
    for t in old_toks:
        old_off.append(old_off[-1] + len(t))

    # Build every edit BEFORE mutating (all-or-nothing). Each entry is
    # (pa, pb, new_sub, anchor_rpr) in plain-text offset space.
    edits = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            continue
        a, b = old_off[i1], old_off[i2]               # FULL-text span of old side
        if any(not plain_flags[k2] for k2 in range(a, b)):
            return False                              # edit touches the citation render -> refuse
        old_seg = full_text[a:b]
        new_seg = "".join(new_toks[j1:j2])
        if tag == "insert":
            # Pure insertion: i1 == i2 so a == b. Anchor at the plain offset of the
            # boundary. No old side, no token-borrowing, no uniqueness needed (W1-4).
            pa = pb = plain_before[a]
            if new_seg:
                edits.append((pa, pb, new_seg, rpr_at_plain(pa)))
            continue
        # Replace/delete: narrow to the minimal changed sub-span so only the truly
        # changed characters are struck (preserves the minimal-redline shape the
        # existing field-safety tests assert, e.g. "(1)" plain "1"->"2" strikes just
        # "1"). The shared prefix/suffix stay untouched in their original runs.
        md = _minimal_diff(old_seg, new_seg)
        if md is None:
            continue
        old_mid, new_mid = md
        # offset of old_mid within old_seg = the shared-prefix length _minimal_diff
        # stripped (its prefix walk uses the same char-equality test).
        shared_pre = 0
        while (shared_pre < len(old_seg) and shared_pre < len(new_seg)
               and old_seg[shared_pre] == new_seg[shared_pre]):
            shared_pre += 1
        fa = a + shared_pre                           # FULL-text start of the changed span
        fb = fa + len(old_mid)                        # FULL-text end of the changed span
        pa = plain_before[fa]
        pb = plain_before[fb]
        edits.append((pa, pb, new_mid, rpr_at_plain(pa)))

    if not edits:
        return True

    # Snapshot for strict all-or-nothing rollback (field stays byte-identical on refusal).
    snapshot = [copy.deepcopy(ch) for ch in p]
    ok = _apply_inplace_plain_edits(p, edits, plain_runs, ids, author, date)
    if not ok:
        for ch in list(p):
            p.remove(ch)
        for ch in snapshot:
            p.append(ch)
        return False
    return True


def _apply_para_replacement(p, new_text: str, ids: _Ids, author: str, date: str,
                            *, scope: str = "word", rpr: Optional[etree._Element] = None) -> bool:
    """Single owner of the paragraph-edit strategy decision.

    Returns ``True`` if a tracked edit was applied (or it was a no-op), ``False`` if
    the paragraph was REFUSED and left unchanged (a cited paragraph whose edit could
    not be made field-safely — the caller must skip-and-report it, never corrupt).

    **Field-bearing paragraphs come FIRST and are routed to the field-safe path ONLY**
    (:func:`_surgical_in_cited_para`): a paragraph carrying a complex field (a live
    Zotero citation) must NEVER reach :func:`_replace_para_text`, which strikes the
    field's rendered result and re-inserts it as static literal text — silently
    killing the live field (it renumbers wrong on the next Zotero refresh). If the
    field-safe path cannot make the edit (the diff touches the rendered citation, or
    the change isn't uniquely locatable in plain runs), we REFUSE rather than fall
    back. ``scope`` is irrelevant for cited paragraphs — the whole-paragraph path is
    never field-safe, so ``scope="paragraph"`` cannot force it here.

    For NON-field paragraphs, ``scope == "word"`` dispatches in priority order:
      1. word-level **redline** (:func:`_redline_para_text`) — interleaved
         strike/insert preserving unchanged words BETWEEN changes as normal runs; the
         best result for any ELIGIBLE plain-prose paragraph. Returns True when applied
         (or a no-op); False ONLY when the paragraph is INELIGIBLE (pre-existing
         tracked changes / non-prose runs).
      2. **surgical** single-run split (:func:`_surgical_in_para`) — reached for an
         ineligible non-field paragraph; granular del+ins on the one changed span.
      3. whole-paragraph strike+insert (:func:`_replace_para_text`) — the final
         fallback for an ineligible non-field paragraph whose change is not a single
         uniquely-locatable plain-run span.

    Redline is tried BEFORE surgical on purpose: surgical collapses a multi-word edit
    into one big del+ins, so trying it first would degrade a multi-change redline on
    an eligible paragraph. ``scope == "paragraph"`` forces the whole-paragraph replace
    for non-field paragraphs only."""
    if _has_complex_field(p):
        # Field-safe path ONLY. Never fall back to the field-destroying whole-para
        # path — refuse (return False) so the caller skips-and-reports.
        return _surgical_in_cited_para(p, new_text, ids, author, date)
    if scope == "word":
        if _redline_para_text(p, new_text, ids, author, date, rpr=rpr):
            return True
        if _surgical_in_para(p, new_text, ids, author, date):
            return True
    _replace_para_text(p, new_text, ids, author, date, rpr=rpr)
    return True


# -- public ops --------------------------------------------------------------
def tracked_insert(doc: Docx, anchor: str, text: str, *, author: str,
                   date: Optional[str] = None) -> Docx:
    """Append `text` to the anchored paragraph as a tracked insertion."""
    date = date or now_iso()
    root = doc.tree(DOCUMENT)
    ids = _Ids(_max_rev_id(root))
    p = find_paragraph(root, anchor)
    p.append(_ins(ids, author, date, _make_run(text, _first_rpr(p))))
    return doc


def tracked_replace_paragraph(doc: Docx, anchor: str, new_text: str, *, author: str,
                              date: Optional[str] = None,
                              rpr: Optional[etree._Element] = None,
                              scope: str = "word") -> Docx:
    """Replace the anchored paragraph's text as tracked changes.

    Default ``scope="word"`` emits a word-level redline — only the changed words
    are struck/inserted, so the reviewer sees what changed and can accept/reject
    independent edits. ``scope="paragraph"`` forces a single whole-paragraph
    strike+insert (also the automatic fallback for a near-total rewrite or a
    paragraph that already carries tracked changes). pPr is preserved."""
    date = date or now_iso()
    root = doc.tree(DOCUMENT)
    ids = _Ids(_max_rev_id(root))
    p = find_paragraph(root, anchor)
    applied = _apply_para_replacement(p, new_text, ids, author, date, scope=scope, rpr=rpr)
    # Surface a field-safe REFUSAL (a cited paragraph whose edit could not be applied
    # without corrupting the live citation field) so the caller can skip-and-report
    # rather than assume success. Additive attribute — signature unchanged.
    doc.last_replace_refused = not applied
    return doc


def tracked_replace_paragraph_el(doc: Docx, p_el: etree._Element, new_text: str, *,
                                 author: str, scope: str = "word",
                                 date: Optional[str] = None,
                                 rpr: Optional[etree._Element] = None) -> bool:
    """Replace a CACHED paragraph ELEMENT's text as tracked changes.

    Identical strategy to :func:`tracked_replace_paragraph` but the target
    paragraph is passed in directly (``p_el``) instead of being re-located by an
    anchor substring. Callers that already hold a stable handle to the paragraph
    (e.g. the section-feedback apply path, which resolves every anchor ONCE
    against the pristine tree and then mutates by element) MUST use this so a
    mid-apply mutation can never cause a later edit to re-resolve its anchor
    against the already-edited tree (which silently clobbers an earlier approved
    edit or skips a now-ambiguous one).

    ``p_el`` must be a ``<w:p>`` element belonging to ``doc``'s document tree (the
    SAME cached root that :meth:`Docx.read_tree`/:meth:`Docx.tree` return — they
    share one parsed tree per part, so an element captured during a read-only
    pre-scan is live for mutation here). The shared revision-id counter is seeded
    from ``_max_rev_id`` over the CURRENT tree on every call, so ids never collide
    across successive edits to different paragraphs.

    Returns ``True`` if a tracked edit was applied (or it was a no-op), ``False``
    if the edit was REFUSED and the paragraph left unchanged (a cited paragraph
    whose edit could not be made field-safely — the caller must skip-and-report,
    never corrupt). Also sets ``doc.last_replace_refused`` for parity with
    :func:`tracked_replace_paragraph`.

    Reuses :func:`_apply_para_replacement` (and thus its field-first dispatch and
    the surgical/redline/whole-paragraph strategy) verbatim — no logic duplicated.
    """
    date = date or now_iso()
    # Mark the document part dirty (we mutate a sub-element of its tree). Seed the
    # revision-id counter from the current tree so ids are unique across calls.
    root = doc.tree(DOCUMENT)
    ids = _Ids(_max_rev_id(root))
    applied = _apply_para_replacement(p_el, new_text, ids, author, date, scope=scope, rpr=rpr)
    doc.last_replace_refused = not applied
    return applied


def tracked_replace_paragraphs(doc: Docx, mapping: dict, *, author: str,
                                date: Optional[str] = None, scope: str = "word") -> Docx:
    """Apply tracked replacements to multiple paragraphs by body-paragraph index.

    Args:
        doc: the open Docx.
        mapping: ``dict[int, str]`` keyed by index into ``iter_paragraphs(root)``
            (the same ordering used everywhere in the package). Values are the
            proposed replacement texts.
        author: revision author string.
        date: ISO 8601 revision timestamp.

    Returns:
        ``doc`` (mutated in-place for chaining).

    Raises:
        IndexError: if any key is outside the range of paragraphs in the document.

    Paragraphs whose proposed text equals the current ``paragraph_text`` are
    silently skipped — no tracked change is written.
    """
    date = date or now_iso()
    root = doc.tree(DOCUMENT)
    paras = iter_paragraphs(root)
    ids = _Ids(_max_rev_id(root))   # one shared counter — no id collisions
    skipped: List[int] = []
    for idx, new_text in mapping.items():
        idx = int(idx)
        if idx < 0 or idx >= len(paras):
            raise IndexError(
                f"paragraph index {idx} is out of range (document has {len(paras)} paragraphs)"
            )
        p = paras[idx]
        if paragraph_text(p) == new_text:
            continue  # no-op: proposed text is identical to current text
        if not _apply_para_replacement(p, new_text, ids, author, date, scope=scope):
            # A cited paragraph whose edit couldn't be made field-safely was REFUSED
            # and left UNCHANGED — record it so the caller skips-and-reports instead
            # of silently corrupting the citation. (Additive attr; signature stable.)
            skipped.append(idx)
    doc.skipped_field_paras = skipped
    return doc


def tracked_insert_paragraphs(doc: Docx, anchor: str, blocks: list, *, author: str,
                              date: Optional[str] = None) -> Docx:
    """Insert NEW paragraphs immediately AFTER the anchored paragraph, each as a
    fully tracked insertion (Word shows them as inserted, attributable to
    ``author``, and they accept/reject cleanly).

    ``blocks`` is a list whose items are either a plain string (a body paragraph)
    or a dict ``{"style": <styleId or None>, "text": <str>}``. Each new ``<w:p>``
    marks BOTH its run text AND its paragraph mark as inserted — the latter via a
    ``<w:pPr><w:rPr><w:ins/></w:rPr></w:pPr>``, which is what makes Word treat the
    paragraph's newline as part of the insertion so a *reject* removes the whole
    paragraph instead of leaving an orphan empty one.

    Paragraphs are inserted in document order after the anchor (mirroring
    ``insert_styled``: ``anchor.addnext`` in reverse). Returns ``doc`` (not saved).
    """
    date = date or now_iso()
    root = doc.tree(DOCUMENT)
    ids = _Ids(_max_rev_id(root))
    anchor_p = find_paragraph(root, anchor)

    new_paras = []
    for block in blocks:
        if isinstance(block, dict):
            style = block.get("style")
            text = block.get("text", "")
        else:
            style, text = None, block
        p = etree.Element(qn("w:p"))
        ppr = etree.SubElement(p, qn("w:pPr"))
        if style:
            pstyle = etree.SubElement(ppr, qn("w:pStyle"))
            pstyle.set(qn("w:val"), style)
        # Mark the paragraph MARK (its newline) as an insertion.
        rpr = etree.SubElement(ppr, qn("w:rPr"))
        mark_ins = etree.SubElement(rpr, qn("w:ins"))
        mark_ins.set(qn("w:id"), ids.next())
        mark_ins.set(qn("w:author"), author)
        mark_ins.set(qn("w:date"), date)
        # The visible run text, wrapped as a tracked insertion.
        p.append(_ins(ids, author, date, _make_run(text, None)))
        new_paras.append(p)

    for el in reversed(new_paras):
        anchor_p.addnext(el)
    return doc


def tracked_replace_in_paragraph(doc: Docx, anchor: str, old_text: str, new_text: str,
                                 *, author: str, date: Optional[str] = None) -> bool:
    """Surgically replace the FIRST occurrence of ``old_text`` with ``new_text`` as
    a tracked change (``<w:del>old</w:del><w:ins>new</w:ins>``) IN PLACE, touching
    ONLY the single plain-text run that contains ``old_text``.

    This is the FIELD-SAFE alternative to ``tracked_replace_paragraph``: it never
    rebuilds the paragraph from reassembled text, so citation FIELDS (runs carrying
    ``<w:fldChar>``/``<w:instrText>``) and every other run are left physically
    untouched. ``old_text`` must lie wholly within one plain ``<w:t>`` run; if it
    spans runs, is absent, or appears only inside a field, NO change is made and
    ``False`` is returned (so the caller can fall back). Returns ``True`` on
    success.

    Thin public wrapper over the shared :func:`_surgical_replace_run` core — the
    same single-run split the unified word-edit dispatcher uses for
    ineligible/cited paragraphs; this entry point does an EXPLICIT caller-supplied
    old->new replace.
    """
    date = date or now_iso()
    root = doc.tree(DOCUMENT)
    ids = _Ids(_max_rev_id(root))
    p = find_paragraph(root, anchor)
    return _surgical_replace_run(p, old_text, new_text, ids, author, date)


def tracked_delete_paragraph_text(doc: Docx, anchor: str, *, author: str,
                                  date: Optional[str] = None) -> Docx:
    date = date or now_iso()
    root = doc.tree(DOCUMENT)
    ids = _Ids(_max_rev_id(root))
    p = find_paragraph(root, anchor)
    for r in list(p.iter(qn("w:r"))):
        if r.find(qn("w:t")) is not None:
            _wrap_run_as_deletion(r, ids, author, date)
    return doc


# -- accept / reject (text level) -------------------------------------------
def _unwrap(el) -> None:
    parent = el.getparent()
    idx = parent.index(el)
    for child in reversed(list(el)):
        parent.insert(idx, child)
    parent.remove(el)


# Tracked move range markers carry no content; they are removed when a move is
# resolved either way.
_MOVE_RANGE_MARKERS = (
    "w:moveFromRangeStart", "w:moveFromRangeEnd",
    "w:moveToRangeStart", "w:moveToRangeEnd",
)
# STRUCTURAL row/cell revisions that change table GEOMETRY (a row or cell is
# added, removed, or merged). Resolving these means rewriting the grid — dropping
# or restoring whole <w:tr>/<w:tc> elements and reconciling gridSpan/vMerge — which
# is risky enough that silently guessing would corrupt the table. We refuse loudly
# and leave them for Word. (Cell *text* edits, by contrast, are ordinary
# <w:ins>/<w:del> inside the cell and ARE resolved by the run-level path below.)
_UNHANDLED_TABLE_REVISIONS = (
    "w:cellIns", "w:cellDel", "w:cellMerge",
)

# PROPERTY-change marks: a *PrChange records that the table/row/cell/grid/section
# PROPERTIES (not geometry, not text) changed, embedding a snapshot of the PRIOR
# properties. These we DO resolve — accept drops the change record (current
# properties stand), reject restores the embedded prior-property snapshot. Each
# entry maps the change element to the properties element it lives inside and the
# nested snapshot it carries (both share that same element name). See
# _resolve_pr_changes.
_PR_CHANGE_MARKS = (
    ("w:trPrChange", "w:trPr"),
    ("w:tcPrChange", "w:tcPr"),
    ("w:tblPrChange", "w:tblPr"),
    ("w:tblGridChange", "w:tblGrid"),
    ("w:sectPrChange", "w:sectPr"),
)


def _guard_unhandled(root) -> None:
    """Raise if the document contains STRUCTURAL row/cell revisions that change
    table geometry (cellIns/cellDel/cellMerge), naming the offending element.
    Scans before any mutation so the tree is never left half-resolved.

    Cell-text edits (plain <w:ins>/<w:del> inside a cell) and property-change
    marks (trPrChange/tcPrChange/tblPrChange/tblGridChange/sectPrChange) are now
    resolved by accept_all/reject_all and are NOT guarded here."""
    for name in _UNHANDLED_TABLE_REVISIONS:
        if root.find(".//" + qn(name)) is not None:
            raise NotImplementedError(
                f"{name} (a structural row/cell add/delete/merge tracked revision) "
                f"is not yet handled by accept_all/reject_all because it changes "
                f"table geometry; resolve it in Word first. "
                f"(Cell-text edits and trPrChange/tcPrChange/tblPrChange/"
                f"tblGridChange/sectPrChange property changes ARE handled.)"
            )


def _resolve_pr_changes(root, *, accept: bool) -> None:
    """Resolve property-change marks (the *PrChange family in _PR_CHANGE_MARKS).

    A *PrChange element sits inside its properties element (e.g. <w:trPrChange>
    inside <w:trPr>) as a sibling of the CURRENT properties, and itself wraps a
    nested snapshot of the PRIOR properties (same element name as the parent):

        <w:trPr> ...current props...
          <w:trPrChange w:id w:author w:date>
            <w:trPr> ...prior props... </w:trPr>
          </w:trPrChange>
        </w:trPr>

    ACCEPT: drop the change record; the current properties already in place win.
    REJECT: replace the parent's current properties with the prior-properties
            snapshot (then drop the change record). pPr/rPr changes are handled
            separately by accept_all/reject_all and are intentionally untouched
            here."""
    for change_name, props_name in _PR_CHANGE_MARKS:
        for change in root.findall(".//" + qn(change_name)):
            parent = change.getparent()          # the props element (e.g. w:trPr)
            if parent is None:
                continue
            if not accept:
                snapshot = change.find(qn(props_name))   # nested prior-props
                # Wipe the parent's current props (everything except the change
                # record), then graft the prior props back in their place.
                for child in list(parent):
                    if child is not change:
                        parent.remove(child)
                if snapshot is not None:
                    idx = parent.index(change)
                    for prior in list(snapshot):
                        parent.insert(idx, prior)
                        idx += 1
            parent.remove(change)


def _remove_move_range_markers(root) -> None:
    for name in _MOVE_RANGE_MARKERS:
        for m in root.findall(".//" + qn(name)):
            m.getparent().remove(m)


def accept_all(doc: Docx) -> Docx:
    root = doc.tree(DOCUMENT)
    _guard_unhandled(root)
    # deletions and move-froms vanish (the deleted/original text is gone)
    for d in root.findall(".//" + qn("w:del")):
        d.getparent().remove(d)
    for mf in root.findall(".//" + qn("w:moveFrom")):
        mf.getparent().remove(mf)
    # insertions and move-tos are kept (unwrap the tracking wrapper)
    for ins in root.findall(".//" + qn("w:ins")):
        _unwrap(ins)
    for mt in root.findall(".//" + qn("w:moveTo")):
        _unwrap(mt)
    _remove_move_range_markers(root)
    for ch in ("w:rPrChange", "w:pPrChange"):
        for c in root.findall(".//" + qn(ch)):
            c.getparent().remove(c)
    _resolve_pr_changes(root, accept=True)
    return doc


def _inserted_para_mark(p) -> bool:
    """True if paragraph ``p``'s MARK (its ¶ / newline) is tracked-inserted, i.e.
    it carries ``<w:pPr><w:rPr><w:ins/></w:rPr></w:pPr>``. Such a mark means the
    paragraph BREAK after ``p`` was inserted: accepting keeps the break, rejecting
    removes it (``p`` merges into the following paragraph)."""
    ppr = p.find(qn("w:pPr"))
    if ppr is None:
        return False
    rpr = ppr.find(qn("w:rPr"))
    return rpr is not None and rpr.find(qn("w:ins")) is not None


def _reject_inserted_para_marks(root, marked) -> None:
    """Reject paragraph-mark insertions: the inserted ¶ is removed, so each marked
    paragraph merges into the FOLLOWING one (its surviving run content — whatever
    is left after the normal reject pass — is prepended to the next paragraph) and
    the now-spurious ``<w:p>`` is deleted. Mirrors how Word collapses a rejected
    inserted paragraph; without it the empty ``<w:p>`` would be left as an orphan.

    A marked paragraph with no following sibling paragraph (e.g. the very last in
    its parent) cannot merge forward; its emptied shell is removed in place."""
    p_tag = qn("w:p")
    for p in marked:
        parent = p.getparent()
        if parent is None:
            continue
        nxt = p.getnext()
        while nxt is not None and nxt.tag != p_tag:
            nxt = nxt.getnext()
        if nxt is None:
            parent.remove(p)
            continue
        # Move p's surviving runs (everything except its own pPr) to the start of
        # the next paragraph, after that paragraph's pPr if present.
        movers = [ch for ch in p if ch.tag != qn("w:pPr")]
        nxt_ppr = nxt.find(qn("w:pPr"))
        anchor_idx = (list(nxt).index(nxt_ppr) + 1) if nxt_ppr is not None else 0
        for off, ch in enumerate(movers):
            nxt.insert(anchor_idx + off, ch)
        parent.remove(p)


def reject_all(doc: Docx) -> Docx:
    root = doc.tree(DOCUMENT)
    _guard_unhandled(root)
    # Capture paragraphs whose MARK is inserted BEFORE the generic ins sweep wipes
    # the signal; their forward-merge happens after the normal reject passes.
    marked = [p for p in root.iter(qn("w:p")) if _inserted_para_mark(p)]
    # insertions and move-tos vanish (the new/destination text is gone)
    for ins in root.findall(".//" + qn("w:ins")):
        ins.getparent().remove(ins)
    for mt in root.findall(".//" + qn("w:moveTo")):
        mt.getparent().remove(mt)
    # deletions are restored: <w:delText> -> <w:t>, then unwrap
    for d in root.findall(".//" + qn("w:del")):
        for dt in d.findall(".//" + qn("w:delText")):
            dt.tag = qn("w:t")
        _unwrap(d)
    # move-froms are restored (content already uses normal <w:t>): just unwrap
    for mf in root.findall(".//" + qn("w:moveFrom")):
        _unwrap(mf)
    _remove_move_range_markers(root)
    for ch in ("w:rPrChange", "w:pPrChange"):
        for c in root.findall(".//" + qn(ch)):
            c.getparent().remove(c)
    _resolve_pr_changes(root, accept=False)
    _reject_inserted_para_marks(root, marked)
    return doc


# -- timestamps --------------------------------------------------------------
def _at_fraction(start: str, end: str, frac: float) -> str:
    """The instant ``frac`` of the way from ``start`` to ``end`` (ISO seconds)."""
    from datetime import datetime
    a = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ")
    b = datetime.strptime(end, "%Y-%m-%dT%H:%M:%SZ")
    return (a + (b - a) * frac).strftime("%Y-%m-%dT%H:%M:%SZ")


def _jittered_fractions(n: int) -> List[float]:
    """``n`` monotonically-increasing fractions in [0, 1] — first at 0, last at 1,
    but with NON-uniform gaps (deterministic jitter) so spread timestamps read like
    a human editing pass rather than an evenly-ticking clock. A lone event lands at
    1.0 ("now")."""
    import hashlib
    if n <= 0:
        return []
    if n == 1:
        return [1.0]
    weights = []
    for i in range(n - 1):
        h = int(hashlib.sha1(f"gf-spread-{i}".encode()).hexdigest()[:8], 16)
        weights.append(0.55 + 0.9 * ((h % 10_000) / 10_000.0))   # gap weight in [0.55, 1.45]
    total = sum(weights)
    fracs, acc = [0.0], 0.0
    for w in weights:
        acc += w / total
        fracs.append(acc)
    fracs[-1] = 1.0
    return fracs


def spread_timestamps(doc: Docx, author: str, start: Optional[str] = None,
                      end: Optional[str] = None, *, min_minutes: int = 15) -> int:
    """Spread one author's comment + tracked-change timestamps across a window.

    Rewrites the ``w:date`` of every dated mark by ``author`` — comments
    (``word/comments.xml``) and every tracked-change element in ``document.xml``
    (ins/del, moveFrom/moveTo, …) — distributed across the window in document order.

    Marks are grouped into **events**: all of one paragraph's tracked marks (the
    ins/del of a single edit) share ONE timestamp, while each comment is its own
    event. So a single word-level redline isn't smeared across many times, but
    distinct edits/comments get distinct, ordered slots. Gaps are jittered so the
    result reads like a human pass, not a clock tick.

    Window defaults: ``end`` -> now; ``start`` -> ``end - min_minutes`` (>= 15 min
    wide). Other authors' marks are left untouched. Returns the number of *marks*
    (not events) updated.
    """
    from datetime import datetime, timedelta
    end_dt = datetime.strptime(end, DATE_FMT) if end else datetime.strptime(now_iso(), DATE_FMT)
    start_dt = datetime.strptime(start, DATE_FMT) if start else end_dt - timedelta(minutes=min_minutes)
    if start_dt > end_dt:
        start_dt, end_dt = end_dt, start_dt
    if (end_dt - start_dt) < timedelta(minutes=min_minutes):   # enforce the minimum span
        start_dt = end_dt - timedelta(minutes=min_minutes)
    start_s, end_s = start_dt.strftime(DATE_FMT), end_dt.strftime(DATE_FMT)

    aq, dq = qn("w:author"), qn("w:date")
    root = doc.tree(DOCUMENT)

    # Build events in document order: each comment anchor is one event; all of a
    # paragraph's dated tracked marks by this author are ONE event (a single edit).
    events = []                        # (order_index, [elements_to_stamp])
    comment_order = {}                 # comment id -> order index
    order = 0
    for p in root.iter(qn("w:p")):
        for cs in p.iter(qn("w:commentRangeStart")):
            cid = cs.get(qn("w:id"))
            if cid is not None and cid not in comment_order:
                comment_order[cid] = order
                order += 1
        marks = [el for el in p.iter() if el.get(aq) == author and el.get(dq) is not None]
        if marks:
            events.append((order, marks))
            order += 1

    if doc.has("word/comments.xml"):
        croot = doc.tree("word/comments.xml")
        for c in croot.iter(qn("w:comment")):
            if c.get(aq) != author:
                continue
            cid = c.get(qn("w:id"))
            o = comment_order.get(cid)
            if o is None:                       # comment with no range marker in the body
                o = order
                order += 1
            events.append((o, [c]))

    events.sort(key=lambda t: t[0])
    fracs = _jittered_fractions(len(events))
    updated = 0
    for frac, (_, els) in zip(fracs, events):
        stamp = _at_fraction(start_s, end_s, frac)
        for el in els:
            el.set(dq, stamp)
            updated += 1
    return updated


# -- path convenience --------------------------------------------------------
def open_doc(path: str | Path) -> Docx:
    return Docx(path)
