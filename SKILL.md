---
name: zotero-word-cite
description: >
  Read and write real Zotero references in Microsoft Word (.docx) documents.
  Use to: scan/verify the Zotero citation fields in a document; insert live
  Zotero citations; convert EndNote, Mendeley, or Word/manual references into a
  user's Zotero library and re-cite the document with live Zotero fields; and
  reformat citations + bibliography in any CSL style (default
  vancouver-superscript = NIH numbered superscript). The flagship workflow takes
  a Word doc formatted with EndNote plus an exported EndNote library (RIS or
  EndNote XML), imports the references into Zotero with checks (dedup against the
  library, completeness, Crossref/PubMed resolution with confidence tiers, and
  Retraction Watch screening), then rewrites the document with live Zotero
  citations. Triggers on: "convert EndNote to Zotero", "import this EndNote
  library", "reformat references with Zotero", "cite this in Word", "check my
  citations / for retractions", "switch citation style to NIH/Vancouver/Nature",
  "scan the Zotero fields in this docx".
---

# zotero-word-cite

Deterministic mechanics for reading and writing **real Zotero reference fields**
in Word `.docx` files. This is a focused, standalone toolkit — it talks to the
Zotero Web API and edits the OOXML directly; it does not need Microsoft Word,
the Zotero desktop app, or any MCP server to run.

## When to use this skill

- The user wants to **migrate an EndNote-formatted document to Zotero** (the
  headline use case): they have a `.docx` whose citations are EndNote fields,
  plus an exported EndNote library (`.ris` or EndNote `.xml`).
- The user wants to **convert** Mendeley / Word-built-in / manual references in a
  document into live Zotero citations.
- The user wants to **insert** a Zotero citation at a spot in a document.
- The user wants to **scan / verify** the Zotero fields already in a document, or
  **check citation integrity** (orphans, uncited entries, retractions).
- The user wants to **change the citation style** (CSL).

## Prerequisites (check first)

Run `./scripts/zwc init` to see environment status. The user must have, in `.env`
(copied from `.env.example`):

- `ZOTERO_API_KEY` — a Zotero key with **read AND write** access (write is
  required to add references to the library).
- `ZOTERO_GROUP_ID` (preferred) **or** `ZOTERO_USER_ID` — which library to use.

If either is missing, point the user at the README's "Getting set up" section and
**ask them to provide the API key and group ID** — do not invent them.

## How to run

Every command is `./scripts/zwc <subcommand> ...` (the wrapper loads `.env` and
the virtualenv). Output documents are written to a new file via `-o`; the input
is never modified in place.

| Goal | Command |
|---|---|
| Show environment status | `zwc init` |
| List citation styles | `zwc csl --list` |
| Resolve a journal name to a CSL id | `zwc csl --resolve "Annals of Neurology"` |
| Search the Zotero library | `zwc zotero --search "tuber asd"` |
| Look up by DOI | `zwc zotero --doi 10.1234/abcd --format reference` |
| Insert a live citation at an anchor | `zwc cite-into FILE.docx --doi 10.1234/abcd --anchor "needs a cite here" -o OUT.docx` |
| List the Zotero fields in a doc | `zwc zotero-scan FILE.docx --check-links` |
| Citation integrity + retraction screen | `zwc cite-check FILE.docx` |
| Convert foreign citations to Zotero | `zwc convert-to-zotero FILE.docx --track -o OUT.docx` |
| Unify ad-hoc / manual references | `zwc unify-refs FILE.docx` (then `--apply`) |
| **EndNote → Zotero migration** | `zwc endnote-migrate FILE.docx LIBRARY.ris` (dry-run) then `--apply -o OUT.docx` |

The default `--style` everywhere is `vancouver-superscript` (NIH numbered
superscript). Pass `--style <csl-id>` to change it (e.g. `nature`, `apa`,
`annals-of-neurology`); see `zwc csl --list`.

## The flagship workflow: EndNote → Zotero

This is a **gated, dry-run-first** pipeline. Always preview, show the user the
plan, get confirmation, then apply.

1. **Dry run (read-only, no writes):**
   `zwc endnote-migrate paper.docx paper-library.ris`
   This parses the EndNote library, resolves each record (Crossref/PubMed),
   screens for retractions, dedups against the Zotero library, and reports what
   it *would* create / match / skip. Nothing is written to Zotero or the doc.

2. **Review the plan with the user.** Surface: how many references will be
   newly created vs. already-in-library, any low-confidence resolutions, any
   retracted references (these are flagged, never silently imported), and any
   document citations that could not be matched (reported, never invented).

3. **Apply (gated on a write-enabled key):**
   `zwc endnote-migrate paper.docx paper-library.ris --apply -o paper-zotero.docx`
   New references are added to the Zotero library (tagged `added-by:zotero-word-cite`,
   placed in an "Imported — review" collection), and the document's EndNote
   fields are replaced with **live Zotero citation fields**.

4. **Open `paper-zotero.docx` in Word** with the Zotero plugin and click
   **Refresh**. Citations render in NIH superscript and the bibliography
   regenerates. The fields are live — the user can keep editing them in Zotero.

## Safeguards (do not weaken these)

- **Dry-run first.** `endnote-migrate` and `unify-refs` never write to Zotero or
  the document unless explicitly given `--apply`.
- **Write gating.** Creating Zotero items requires a write-enabled API key; the
  engine refuses to write otherwise.
- **Dedup.** References are matched by normalized DOI, then normalized title,
  against the existing library before anything is created.
- **Retraction screening.** Cited/imported DOIs are checked against the
  Retraction Watch dataset (downloaded on demand; degrades gracefully offline).
- **No fabrication.** Document citations that cannot be matched to a library
  item are reported, never guessed.
- **Non-destructive.** Output goes to a new `-o` file; use `--track` to record
  changes as Word tracked changes for human review.

## Notes

- Citations are written as live `ADDIN ZOTERO_ITEM CSL_CITATION` Word fields plus
  Zotero document preferences, so the Zotero Word plugin recognizes and can
  re-edit them. CSL rendering is done by the Zotero Web API (server-side), so the
  rendered text appears after a Refresh in Word (or is produced by the engine
  when it has network access).
- This skill is a self-contained extraction of a larger grant-writing toolkit;
  it includes only the citation/Zotero/EndNote engine.
