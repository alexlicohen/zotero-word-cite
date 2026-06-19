"""Command-line surface for zotero-word-cite. Thin wrappers over the tested
citation/Zotero engine so a Claude Code session can invoke deterministic
operations via Bash.

Invoke as ``python -m zoterocite.cli <subcommand>`` (or via the ``zwc`` wrapper):

    zwc init                                  # show required/optional env-var status
    zwc cite (--record JSON | --bibliography FILE) [--from-pubmed] [--style ..]
    zwc zotero (--doi DOI | --search QUERY | --all) [--count] [--style ..]
    zwc cite-into FILE --anchor A (--key K | --keys K,.. | --doi DOI) -o OUT
    zwc zotero-scan FILE [--check-links]
    zwc csl (--list | --resolve JOURNAL | --check CSL_ID [--online])
    zwc convert-to-zotero FILE [-o OUT] [--managers ..] [--track] [--json]
    zwc unify-refs FILE [--no-fetch|--offline] [--json] | --apply --decisions @d.json -o OUT
    zwc cite-check FILE [--rw-csv CSV] [--refresh|--no-refresh] [--check-existence]
    zwc endnote-migrate DOC --library LIB [--apply] [--collection NAME] [-o OUT]

Default CSL style is ``vancouver-superscript`` (= NIH numbered superscript).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .docxio import DocxLoadError
from .findings import format_findings, findings_to_dicts


def _require_out(args):
    if not args.out:
        sys.exit("error: this command modifies the document; pass -o/--out OUTPUT.docx")
    return Path(args.out)


def _json_arg(val):
    """Parse a JSON CLI arg.

    A leading '@' means read the JSON from that file. As a footgun guard, a bare
    path with no '@' that (a) is not obviously inline JSON and (b) names an
    existing file is also read as the JSON source — so ``--decisions /tmp/d.json``
    works like ``--decisions @/tmp/d.json`` instead of failing with a confusing
    JSONDecodeError on the path string. Inline JSON ('{...}', '[...]', '"..."')
    is unaffected because it is parsed directly.
    """
    if not val:
        return None
    if val.startswith("@"):
        val = Path(val[1:]).read_text(encoding="utf-8")
    elif val.lstrip()[:1] not in ("{", "[", '"') and Path(val).is_file():
        val = Path(val).read_text(encoding="utf-8")
    return json.loads(val)


def cmd_init(a):
    """zotero-word-cite init — show whether the Zotero / NCBI / Crossref env vars are set."""
    import os
    lines = [
        "zotero-word-cite environment status",
        "(set these in your shell or a .env so the engine can reach Zotero / NCBI / Crossref)",
        "",
    ]
    env_vars = [
        ("ZOTERO_API_KEY", "Zotero API key"),
        ("ZOTERO_GROUP_ID", "Zotero group library ID"),
        ("ZOTERO_USER_ID", "Zotero user library ID (fallback)"),
        ("NCBI_API_KEY", "optional — NCBI/Entrez API key"),
        ("NCBI_EMAIL", "optional — NCBI/Entrez polite-pool email"),
        ("ZOTERO_WORD_CITE_CONTACT_EMAIL", "optional — Crossref/NCBI polite-pool email"),
    ]
    for var, desc in env_vars:
        status = "set" if os.environ.get(var) else "MISSING"
        lines.append(f"  {var}: {status}  ({desc})")
    print("\n".join(lines))


def cmd_cite(a):
    from .cite import Reference, format_reference, format_bibliography, from_pubmed_record
    mk = (lambda d: from_pubmed_record(d)) if a.from_pubmed else (lambda d: Reference(**d))
    if a.record:
        print(format_reference(mk(json.loads(a.record)), style=a.style))
    elif a.bibliography:
        recs = json.loads(Path(a.bibliography).read_text())
        print(format_bibliography([mk(d) for d in recs], style=a.style))
    else:
        sys.exit("error: pass --record JSON or --bibliography FILE")


def cmd_zotero(a):
    from . import zotero
    from .cite import format_reference
    try:
        if a.count:
            print(zotero.count_items(a.search or None)); return
        if getattr(a, "all"):
            items = zotero.fetch_all(a.search or None, max_items=(a.limit or None))
            print(f"# {len(items)} item(s)")
            for it in items:
                print(it.get("key"), "-", it.get("data", {}).get("title", ""))
            return
        if a.doi:
            item = zotero.get_item_by_doi(a.doi)
            if not item:
                sys.exit("no Zotero item with that DOI")
            if a.format == "reference":      # local formatting from item metadata
                cstyle = "vancouver" if a.style in ("vancouver", "nih", "ama") else "author_year"
                print(format_reference(zotero.to_reference(item), style=cstyle))
            else:                            # server-rendered: bib (full) or intext marker
                kind = "citation" if a.format == "intext" else "bib"
                out = zotero.formatted_citations([item["key"]], style=a.style, kind=kind)
                print(out[0] if out else "(no citation returned)")
        elif a.search:
            total = zotero.count_items(a.search)
            items = zotero.search_items(a.search)
            print(f"# {total} match(es); showing {min(a.limit, len(items))}")
            for it in items[: a.limit]:
                print(it.get("key"), "-", it.get("data", {}).get("title", ""))
        else:
            sys.exit("error: pass --doi DOI, --search QUERY, or --all [--count]")
    except RuntimeError as exc:
        sys.exit(f"zotero: {exc}")


def cmd_cite_into(a):
    out = _require_out(a)
    from .zoterofield import cite_into
    items = json.loads(a.items) if a.items else None
    keys = a.keys.split(",") if a.keys else None
    if not items and a.key and a.locator:
        items = [{"key": a.key, "locator": a.locator, "label": a.label or "page"}]
    elif not items and a.key:
        keys = [a.key]
    try:
        cite_into(a.file, a.anchor, keys=keys, items=items, doi=a.doi, style=a.style,
                  add_bibliography=a.bibliography, out=out)
    except (RuntimeError, ValueError) as exc:
        sys.exit(f"cite-into: {exc}")
    print(f"inserted live Zotero citation field -> {out}")


def cmd_zotero_scan(a):
    from .zoterofield import scan_citations, check_links
    if a.check_links:
        rows = check_links(a.file)
        for r in rows:
            print(f"[{r['status'].upper():8}] {r['key']} - {(r['title'] or '')[:70]}")
        nbroken = sum(1 for r in rows if r["status"] == "broken")
        print(f"# {len(rows)} cited item(s); {nbroken} broken, "
              f"{sum(1 for r in rows if r['status']=='external')} external")
        sys.exit(1 if nbroken else 0)
    cits = scan_citations(a.file)
    for i, c in enumerate(cits, 1):
        label = ", ".join(it["key"] + (f" @{it['locator']}" if it.get("locator") else "")
                          for it in c["items"])
        print(f"{i}. [{len(c['items'])} item(s)] {label}")
        for it in c["items"]:
            print(f"     - {it['key']}: {(it['title'] or '')[:70]}")
    print(f"# {len(cits)} citation field(s)")


def cmd_csl(a):
    """zotero-word-cite csl --list | --resolve "Brain" | --check <id> [--online]

    Journal citation-style catalog: resolve a journal name to its CSL id (the id
    Zotero renders with), list the catalog, or validate an id. Drudgery/Green-tier
    — picking a journal's mandated style is pure data lookup."""
    from . import csldb
    if a.list:
        for s in csldb.list_styles():
            flags = ",".join(f for f, on in (("numbered", s.get("numbered")),
                                             ("superscript", s.get("superscript"))) if on)
            print(f"{s['csl_id']:<28} {s['name']}" + (f"  [{flags}]" if flags else ""))
        return
    if a.resolve:
        cid = csldb.resolve_style(a.resolve)
        if cid:
            print(cid)
        else:
            near = ", ".join(f"{s.name} ({s.csl_id})" for s in csldb.nearest_styles(a.resolve))
            print(f"no match for {a.resolve!r}" + (f" — did you mean: {near}" if near else ""))
            sys.exit(1)
        return
    if a.check:
        ok = csldb.is_valid_style(a.check, online=a.online)
        print(f"{a.check}: {'valid' if ok else 'unknown'}" + (" (checked online)" if a.online else ""))
        sys.exit(0 if ok else 1)
    print("error: pass one of --list, --resolve JOURNAL, or --check CSL_ID", file=sys.stderr)
    sys.exit(2)


def cmd_endnote_migrate(a):
    """zotero-word-cite endnote-migrate DOC --library LIB [--apply] [--collection NAME] [-o OUT] [--json]
    Migrate an EndNote library into a validated Zotero collection and re-cite the document.
    Default is a DRY-RUN plan (no writes); --apply performs the gated shared-library write."""
    from .endnote import (plan_endnote_migration, apply_endnote_migration,
                          WriteRefusedError)
    if a.apply:
        try:
            res = apply_endnote_migration(a.doc, a.library, collection=a.collection, out=a.out)
        except WriteRefusedError as e:
            print(f"refused: {e}", file=sys.stderr)
            print("The configured Zotero key is read-only; a write-enabled group key is required.",
                  file=sys.stderr)
            sys.exit(2)
        if a.json:
            print(json.dumps(res, indent=2, default=str)); return
        print(f"collection:   {res['collection']}")
        print(f"created:      {len(res['created'])}")
        print(f"matched:      {len(res['matched'])}")
        print(f"unmatched:    {len(res['unmatched'])}")
        print(f"skipped (retracted): {len(res.get('skipped_retracted', []))}")
        print(f"re-cited doc: {res['doc_out']}")
        return
    plan = plan_endnote_migration(a.doc, a.library, collection=a.collection)
    if a.json:
        print(json.dumps(plan, indent=2, default=str)); return
    print("# EndNote -> Zotero migration plan (dry run — no writes)")
    print(f"library records:            {len(plan['records'])}")
    print(f"  to create in Zotero:      {plan['to_create']}")
    print(f"  already in library:       {plan['to_match']}")
    print(f"document EndNote citations: {plan['doc_citations']}  (unmatched: {len(plan['unmatched'])})")
    print(f"collection would be:        {plan['collection_name']!r}")
    if plan.get("validation"):
        from .findings import format_findings
        print("\nvalidation:")
        print(format_findings(plan["validation"]))
    print("\nRe-run with --apply to create the collection + items and re-cite the document.")
    print("(--apply WRITES to the shared Zotero group library; requires a write-enabled key.)")


def cmd_convert_to_zotero(a):
    """zotero-word-cite convert-to-zotero FILE [-o OUT] [--managers ...] [--track] [--json]

    Detects EndNote/Mendeley/Word-native/manual citations and converts the
    field-based foreign ones to live Zotero fields matched against the group
    library (dedup'd). Without -o it's a dry run (report only)."""
    from .citeconvert import convert_to_zotero
    managers = tuple(a.managers) if a.managers else ("endnote", "mendeley", "word")
    res = convert_to_zotero(a.file, out=a.out, managers=managers, track=a.track)
    if a.json:
        print(json.dumps(res, indent=2, default=str))
        return
    counts = res["classification"]["counts"]
    print("Citation sources: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    print(f"converted: {len(res['converted'])} | unmatched (not in library): "
          f"{len(res['unmatched'])} | manual (report-only): {len(res['manual_skipped'])} | "
          f"deduped: {res['deduped']}")
    for u in res["unmatched"]:
        print(f"  unmatched: {u.get('title') or u.get('doi') or u}")
    if res.get("out"):
        print(f"-> {res['out']}")
    else:
        print("(dry run — pass -o OUT.docx to write the converted document)")


def cmd_unify_refs(a):
    """zotero-word-cite unify-refs DOC [--no-fetch] [--offline] [--json] [-o plan.json]
       zotero-word-cite unify-refs DOC --apply --decisions @d.json -o OUT.docx [--no-add-missing] [--no-track]

    Default = dry-run PLAN (no writes, no doc changes): inventory + resolve + tier
    references. --apply consumes a decisions JSON to add confirmed missing refs to
    the Zotero group library ('Imported — review' collection) and rewrite the doc
    as tracked changes. Human-in-the-loop: review the plan before --apply.

    --offline is a strict kill-switch: zero network calls (no Crossref/PubMed, no
    Zotero library read — cache-only). Use for confidential documents."""
    from .unify import plan_unification, apply_unification
    offline = getattr(a, "offline", False)
    if a.apply:
        out = _require_out(a)
        decisions = _json_arg(a.decisions) if a.decisions else {}
        plan = (_json_arg(a.plan) if getattr(a, "plan", None)
                else plan_unification(a.file, fetch=not a.no_fetch, offline=offline))
        rep = apply_unification(a.file, plan, decisions, out=out,
                                add_missing=not a.no_add_missing, track=not a.no_track,
                                source_label=Path(a.file).name)
        print(json.dumps(rep, indent=2, default=str) if a.json else
              f"added {len(rep['added'])} to Zotero · matched {len(rep['matched'])} · "
              f"replaced {rep['replaced']} in doc · needs-input {len(rep['needs_input'])} · "
              f"retracted-flagged {len(rep['retracted_flagged'])} -> {rep['out']}")
        return
    plan = plan_unification(a.file, fetch=not a.no_fetch, offline=offline)
    if a.out:
        Path(a.out).write_text(json.dumps(plan, indent=2, default=str), encoding="utf-8")
    if a.json:
        print(json.dumps(plan, indent=2, default=str)); return
    s = plan["summary"]
    print("Reference inventory:", json.dumps(s, default=str))
    nc = plan["needs_confirmation"]
    print(f"auto (high-confidence): {len(plan['auto'])}  |  needs confirmation: "
          f"{len(nc.get('references', []))} refs + {len(nc.get('placeholders', []))} placeholders")
    for r in plan["references"][:12]:
        flag = " [RETRACTED]" if r.get("retracted") else (" [divergent]" if r.get("divergent") else "")
        inlib = "in-library" if r.get("in_library") else "MISSING"
        m = r.get("metadata") or {}
        print(f"  {r['tier']:<6} {inlib:<10} {r.get('confidence',''):<7}{flag}  {(m.get('title') or r['input'])[:72]}")


def cmd_cite_check(a):
    """zotero-word-cite cite-check FILE [--rw-csv CSV] [--refresh|--no-refresh] [--check-existence] [--json]

    The Retraction Watch DB auto-refreshes when missing or >7 days stale.
    --refresh forces an immediate re-download; --no-refresh uses only the
    existing cache (no network)."""
    from .citecheck import cite_check, refresh_retraction_db
    if getattr(a, "refresh", False):
        dest = refresh_retraction_db()
        print(f"refreshed Retraction Watch db -> {dest}", file=sys.stderr)
    findings = cite_check(
        a.file, rw_csv=a.rw_csv, check_existence=a.check_existence,
        auto_refresh=not getattr(a, "no_refresh", False),
    )
    if a.json:
        print(json.dumps(findings_to_dicts(findings), indent=2))
    else:
        print(format_findings(findings))
    sys.exit(1 if any(f.severity == "ERROR" for f in findings) else 0)


def build_parser():
    p = argparse.ArgumentParser(prog="zoterocite")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="show whether the Zotero / NCBI / Crossref env vars are set")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("cite")
    s.add_argument("--record"); s.add_argument("--bibliography")
    s.add_argument("--from-pubmed", action="store_true", dest="from_pubmed")
    s.add_argument("--style", default="vancouver", choices=["vancouver", "author_year"])
    s.set_defaults(fn=cmd_cite)

    s = sub.add_parser("zotero")
    s.add_argument("--doi"); s.add_argument("--search")
    s.add_argument("--style", default="vancouver-superscript")
    s.add_argument("--format", default="bib", choices=["bib", "intext", "reference"])
    s.add_argument("--count", action="store_true", help="print the total count only")
    s.add_argument("--all", action="store_true", help="paginate the whole library/search")
    s.add_argument("--limit", type=int, default=10); s.set_defaults(fn=cmd_zotero)

    s = sub.add_parser("cite-into"); s.add_argument("file")
    s.add_argument("--anchor", required=True)
    s.add_argument("--key"); s.add_argument("--keys"); s.add_argument("--doi")
    s.add_argument("--items", help='JSON list of {key,locator?,label?,prefix?,suffix?}')
    s.add_argument("--locator"); s.add_argument("--label", help="page|chapter|figure|… (with --locator)")
    s.add_argument("--style", default="vancouver-superscript")
    s.add_argument("--bibliography", action="store_true")
    s.add_argument("-o", "--out"); s.set_defaults(fn=cmd_cite_into)

    s = sub.add_parser("zotero-scan"); s.add_argument("file")
    s.add_argument("--check-links", action="store_true", dest="check_links")
    s.set_defaults(fn=cmd_zotero_scan)

    s = sub.add_parser("csl", help="journal citation-style (CSL) catalog: resolve/list/check")
    s.add_argument("--list", action="store_true", help="list the catalog")
    s.add_argument("--resolve", metavar="JOURNAL", help="resolve a journal name to its CSL id")
    s.add_argument("--check", metavar="CSL_ID", help="validate a CSL id (catalog/slug)")
    s.add_argument("--online", action="store_true", help="with --check, verify against the CSL repo")
    s.set_defaults(fn=cmd_csl)

    s = sub.add_parser("convert-to-zotero"); s.add_argument("file")
    s.add_argument("--managers", nargs="*",
                   help="subset of: endnote mendeley word (default: all three)")
    s.add_argument("--track", action="store_true", help="make conversions tracked changes")
    s.add_argument("--json", action="store_true", default=False)
    s.add_argument("-o", "--out", help="write converted doc (omit for a dry run)")
    s.set_defaults(fn=cmd_convert_to_zotero)

    s = sub.add_parser("unify-refs"); s.add_argument("file")
    s.add_argument("--no-fetch", action="store_true", dest="no_fetch", help="inventory only; no Crossref/PubMed resolution")
    s.add_argument("--offline", action="store_true", dest="offline",
                   help="strict offline kill-switch: zero network (no Crossref/PubMed AND no Zotero library read; cache-only). "
                        "Use for confidential documents. Implies --no-fetch.")
    s.add_argument("--apply", action="store_true", help="apply (add to Zotero + rewrite doc); needs --decisions")
    s.add_argument("--decisions", help="confirmations JSON (or @file) for --apply")
    s.add_argument("--plan", help="a prior plan JSON (or @file) to apply against")
    s.add_argument("--no-add-missing", action="store_true", dest="no_add_missing", help="don't write to Zotero")
    s.add_argument("--no-track", action="store_true", dest="no_track", help="rewrite without tracked changes")
    s.add_argument("--json", action="store_true", default=False)
    s.add_argument("-o", "--out")
    s.set_defaults(fn=cmd_unify_refs)

    s = sub.add_parser("cite-check"); s.add_argument("file")
    s.add_argument("--rw-csv", dest="rw_csv", help="Retraction Watch CSV (defaults to the cached copy if present)")
    s.add_argument("--refresh", action="store_true",
                   help="force an immediate Retraction Watch re-download before checking")
    s.add_argument("--no-refresh", action="store_true", dest="no_refresh",
                   help="never hit the network; use only the cached Retraction Watch db")
    s.add_argument("--check-existence", action="store_true", dest="check_existence",
                   help="verify each cited DOI exists via Crossref (network)")
    s.add_argument("--json", action="store_true", default=False)
    s.set_defaults(fn=cmd_cite_check)

    s = sub.add_parser("endnote-migrate",
                       help="EndNote library -> validated Zotero collection + re-cite the doc (dry-run unless --apply)")
    s.add_argument("doc", help="the document that uses the EndNote citations")
    s.add_argument("--library", required=True, help="EndNote export to migrate (.xml or .ris)")
    s.add_argument("--apply", action="store_true", default=False,
                   help="perform the gated shared-library write + re-cite (default: dry-run plan)")
    s.add_argument("--collection", default=None, help="Zotero collection name (default: 'Imported — review')")
    s.add_argument("-o", "--out", default=None, help="output docx for the re-cited doc (default: <doc>.zotero.docx)")
    s.add_argument("--json", action="store_true", default=False)
    s.set_defaults(fn=cmd_endnote_migrate)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except (DocxLoadError, FileNotFoundError, json.JSONDecodeError,
            UnicodeDecodeError) as e:
        # Input-boundary failures: a dirty/wrong/missing file or undecodable
        # input. Surface a clean one-line message instead of a raw traceback.
        # NOTE: SystemExit and KeyboardInterrupt are deliberately NOT caught —
        # argparse and the commands' own sys.exit(...) rely on SystemExit
        # propagating; a blanket `except Exception` would also swallow genuine
        # logic bugs that must still show a traceback.
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
