"""Native threaded comment replies: read_comments + add_comment_reply.

Covers the OOXML comment-threading model implemented in zoterocite.comments:
  * comments.xml  -- reply is a flat sibling <w:comment> with its own paraId
  * document.xml  -- reply gets a commentReference at the parent's anchor (no
                     second range)
  * commentsExtended.xml -- the thread link (w15:paraIdParent)
  * commentsIds.xml      -- durable ids for round-trip fidelity

The threading test carries a mutation self-check (see test_threading_has_teeth)
to prove the parent-link assertion actually fails when the linkage is broken.
"""
from lxml import etree

from zoterocite.comments import add_comment, add_comment_reply, read_comments
from zoterocite.docxio import Docx, DOCUMENT
from zoterocite.ooxml import qn
from zoterocite.builder import new_doc

AUTHOR = "Cohen, Alexander (Neurology)"
COAUTHOR = "Reviewer Two"
ANCHOR = "We thank the reviewers for their thorough and constructive critiques"

COMMENTS = "word/comments.xml"
EXTENDED = "word/commentsExtended.xml"
IDS = "word/commentsIds.xml"


# -- helpers -----------------------------------------------------------------
def _make_base_doc(tmp_path, name="base.docx"):
    """Build a minimal in-memory doc whose first paragraph contains ANCHOR."""
    src = tmp_path / name
    new_doc(src, [
        "We thank the reviewers for their thorough and constructive critiques and respond point by point below.",
        "Cortical tubers in TSC are present from birth and evolve over development.",
    ])
    return src


def _seed_with_reply(tmp_path, reply_text="I agree, will tighten this."):
    """Flat parent comment + one threaded reply; saved + reloaded. Returns the
    reloaded Docx plus (parent_id, reply_id)."""
    src = _make_base_doc(tmp_path, "seed_base.docx")
    doc = Docx(src)
    parent_id = add_comment(doc, ANCHOR, "Please clarify the framing.",
                            author=AUTHOR, initials="AC")
    reply_id = add_comment_reply(doc, parent_id, reply_text,
                                 author=COAUTHOR, initials="R2")
    out = tmp_path / "threaded.docx"
    doc.save(out)
    return Docx(out), parent_id, reply_id


def _commentex_for(doc, para_id):
    root = doc.read_tree(EXTENDED)
    for e in root.findall(qn("w15:commentEx")):
        if e.get(qn("w15:paraId")) == para_id:
            return e
    return None


def _comment_paraid(doc, cid):
    root = doc.read_tree(COMMENTS)
    for c in root.findall(qn("w:comment")):
        if c.get(qn("w:id")) == str(cid):
            return c.find(qn("w:p")).get(qn("w14:paraId"))
    return None


# -- read_comments robustness ------------------------------------------------
def test_read_comments_empty_when_no_part(tmp_path):
    src = _make_base_doc(tmp_path)
    assert read_comments(Docx(src)) == []


def test_read_comments_flat_comment_has_no_parent(tmp_path):
    src = _make_base_doc(tmp_path)
    doc = Docx(src)
    cid = add_comment(doc, ANCHOR, "A flat note.", author=AUTHOR, initials="AC")
    out = tmp_path / "flat.docx"
    doc.save(out)

    rows = read_comments(Docx(out))
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == cid
    assert row["author"] == AUTHOR
    assert row["initials"] == "AC"
    assert row["text"] == "A flat note."
    assert row["parent_id"] is None
    assert row["is_reply"] is False
    assert row["done"] is False
    # anchor resolution via the refextract-style reverse lookup
    assert row["anchor_text"] is not None
    assert ANCHOR in row["anchor_text"]
    assert isinstance(row["paragraph_index"], int)


def test_read_comments_multi_author(tmp_path):
    src = _make_base_doc(tmp_path)
    doc = Docx(src)
    add_comment(doc, ANCHOR, "From AC.", author=AUTHOR, initials="AC")
    add_comment(doc, "Cortical tubers in TSC are present from birth",
                "From R2.", author=COAUTHOR, initials="R2")
    out = tmp_path / "multi.docx"
    doc.save(out)
    rows = read_comments(Docx(out))
    assert {r["author"] for r in rows} == {AUTHOR, COAUTHOR}
    assert all(r["parent_id"] is None for r in rows)


# -- flat-comment output must stay byte-identical ----------------------------
def test_flat_comment_does_not_emit_extended_or_ids(tmp_path):
    """A plain add_comment must NOT start fabricating extended/ids parts."""
    src = _make_base_doc(tmp_path)
    doc = Docx(src)
    add_comment(doc, ANCHOR, "flat", author=AUTHOR, initials="AC")
    out = tmp_path / "flat2.docx"
    doc.save(out)
    re = Docx(out)
    assert re.has(COMMENTS)
    assert not re.has(EXTENDED)
    assert not re.has(IDS)


# -- core threading ----------------------------------------------------------
def test_reply_threads_under_parent(tmp_path):
    re, parent_id, reply_id = _seed_with_reply(tmp_path)
    rows = {r["id"]: r for r in read_comments(re)}
    assert set(rows) == {parent_id, reply_id}

    parent = rows[parent_id]
    reply = rows[reply_id]
    assert parent["parent_id"] is None
    assert parent["is_reply"] is False
    assert reply["parent_id"] == parent_id
    assert reply["is_reply"] is True
    assert reply["author"] == COAUTHOR
    assert reply["text"] == "I agree, will tighten this."


def test_reply_parts_well_formed(tmp_path):
    re, parent_id, reply_id = _seed_with_reply(tmp_path)

    # comments.xml: exactly two <w:comment>
    croot = re.read_tree(COMMENTS)
    assert len(croot.findall(qn("w:comment"))) == 2

    # commentsExtended.xml: reply entry links to the parent's paraId
    parent_pid = _comment_paraid(re, parent_id)
    reply_pid = _comment_paraid(re, reply_id)
    reply_ex = _commentex_for(re, reply_pid)
    assert reply_ex is not None
    assert reply_ex.get(qn("w15:paraIdParent")) == parent_pid
    assert reply_ex.get(qn("w15:done")) == "0"
    # parent entry exists and has NO parent link
    parent_ex = _commentex_for(re, parent_pid)
    assert parent_ex is not None
    assert parent_ex.get(qn("w15:paraIdParent")) is None

    # commentsIds.xml: a durable id for the reply paraId
    iroot = re.read_tree(IDS)
    reply_ids = [e for e in iroot.findall(qn("w16cid:commentId"))
                 if e.get(qn("w16cid:paraId")) == reply_pid]
    assert len(reply_ids) == 1
    durable = reply_ids[0].get(qn("w16cid:durableId"))
    assert durable is not None and len(durable) == 8
    int(durable, 16)  # valid hex


def test_reply_wires_content_types_and_reference(tmp_path):
    re, parent_id, reply_id = _seed_with_reply(tmp_path)

    # every XML part well-formed
    for part in re.parts():
        if part.endswith((".xml", ".rels")):
            etree.fromstring(re.raw(part))

    # [Content_Types].xml overrides for all three parts
    ct = re.raw("[Content_Types].xml").decode()
    assert "/word/comments.xml" in ct
    assert "/word/commentsExtended.xml" in ct
    assert "/word/commentsIds.xml" in ct
    assert "wordprocessingml.commentsExtended+xml" in ct
    assert "wordprocessingml.commentsIds+xml" in ct

    # document.xml has a commentReference for the reply id (and the parent's)
    droot = re.read_tree(DOCUMENT)
    ref_ids = {r.get(qn("w:id"))
               for r in droot.iter(qn("w:commentReference"))}
    assert str(reply_id) in ref_ids
    assert str(parent_id) in ref_ids

    # the reply did NOT create a second range (thread shares the parent's range)
    start_ids = [s.get(qn("w:id"))
                 for s in droot.iter(qn("w:commentRangeStart"))]
    assert start_ids.count(str(parent_id)) == 1
    assert str(reply_id) not in start_ids


def test_reply_validates_clean(tmp_path):
    """The saved package passes the pre-delivery validation gate."""
    from zoterocite.validate import validate
    re, _, _ = _seed_with_reply(tmp_path)
    out = tmp_path / "thread_validate.docx"
    re.save(out)
    report = validate(out, screen_retractions=False)
    assert report.ok, report.errors


def test_read_comments_roundtrip_stable(tmp_path):
    re, parent_id, reply_id = _seed_with_reply(tmp_path)
    first = read_comments(re)
    out2 = tmp_path / "thread_again.docx"
    re.save(out2)
    second = read_comments(Docx(out2))
    assert first == second


def test_multiple_replies_same_parent(tmp_path):
    src = _make_base_doc(tmp_path, "multireply_base.docx")
    doc = Docx(src)
    parent_id = add_comment(doc, ANCHOR, "Root.", author=AUTHOR, initials="AC")
    r1 = add_comment_reply(doc, parent_id, "Reply one.",
                           author=COAUTHOR, initials="R2")
    r2 = add_comment_reply(doc, parent_id, "Reply two.",
                           author=AUTHOR, initials="AC")
    out = tmp_path / "multireply.docx"
    doc.save(out)

    assert r1 != r2
    re = Docx(out)
    rows = {r["id"]: r for r in read_comments(re)}
    assert rows[r1]["parent_id"] == parent_id
    assert rows[r2]["parent_id"] == parent_id

    parent_pid = _comment_paraid(re, parent_id)
    for rid in (r1, r2):
        pid = _comment_paraid(re, rid)
        assert _commentex_for(re, pid).get(qn("w15:paraIdParent")) == parent_pid
    # distinct reply paraIds
    assert _comment_paraid(re, r1) != _comment_paraid(re, r2)


def test_reply_to_missing_parent_raises(tmp_path):
    src = _make_base_doc(tmp_path)
    doc = Docx(src)
    add_comment(doc, ANCHOR, "Root.", author=AUTHOR, initials="AC")
    import pytest
    with pytest.raises(ValueError):
        add_comment_reply(doc, 9999, "orphan", author=COAUTHOR)


def _count_comments(doc):
    root = doc.read_tree(COMMENTS)
    return len(root.findall(qn("w:comment")))


def test_reply_no_doc_reference_is_atomic_no_orphan(tmp_path):
    """M2 regression: the parent comment EXISTS in comments.xml but its
    <w:commentReference> run was removed from document.xml. add_comment_reply
    must raise ValueError WITHOUT having appended the reply <w:comment> to
    comments.xml (no orphan comment -> no Word repair prompt for a caller that
    catches the error and saves).
    """
    import pytest
    src = _make_base_doc(tmp_path, "atomic_base.docx")
    doc = Docx(src)
    parent_id = add_comment(doc, ANCHOR, "Root.", author=AUTHOR, initials="AC")

    # Remove the parent's commentReference run from document.xml (live tree),
    # simulating a doc whose anchor run was stripped while the comment remains.
    doc_root = doc.tree(DOCUMENT)
    removed = 0
    for ref in list(doc_root.iter(qn("w:commentReference"))):
        if ref.get(qn("w:id")) == str(parent_id):
            run = ref.getparent()
            run.getparent().remove(run)
            removed += 1
    assert removed >= 1, "fixture precondition: parent reference run must exist to remove"

    before = _count_comments(doc)
    with pytest.raises(ValueError):
        add_comment_reply(doc, parent_id, "should not be appended",
                          author=COAUTHOR, initials="R2")
    after = _count_comments(doc)
    assert after == before, (
        f"orphan reply comment was appended despite the raise: {before} -> {after}"
    )


# -- TEETH: prove the threading assertion fails when the link is broken -------
def test_threading_has_teeth(tmp_path):
    """Mutation self-check: corrupt the reply's paraIdParent and confirm the
    parent_id/is_reply assertion in test_reply_threads_under_parent would FAIL.

    This proves the assertion has teeth -- it is sensitive to the actual thread
    linkage, not vacuously true."""
    re, parent_id, reply_id = _seed_with_reply(tmp_path)
    reply_pid = _comment_paraid(re, reply_id)

    # Baseline: correct linkage resolves parent_id.
    rows = {r["id"]: r for r in read_comments(re)}
    assert rows[reply_id]["parent_id"] == parent_id
    assert rows[reply_id]["is_reply"] is True

    # Mutate: point the reply's paraIdParent at a wrong/nonexistent paraId.
    eroot = re.tree(EXTENDED)
    for e in eroot.findall(qn("w15:commentEx")):
        if e.get(qn("w15:paraId")) == reply_pid:
            e.set(qn("w15:paraIdParent"), "DEADBEEF")
    broken = tmp_path / "broken.docx"
    re.save(broken)

    bad = {r["id"]: r for r in read_comments(Docx(broken))}
    # The assertion now FAILS: parent unresolved -> not a reply.
    assert bad[reply_id]["parent_id"] != parent_id
    assert bad[reply_id]["parent_id"] is None
    assert bad[reply_id]["is_reply"] is False
