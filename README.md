# zotero-word-cite

Read and write **real Zotero references** in Microsoft Word (`.docx`) documents —
from the command line or by asking [Claude Code](https://claude.com/claude-code)
in plain English.

Its headline feature: hand it a Word document formatted with **EndNote** plus an
**exported EndNote library**, and it will import those references into your
**Zotero** library (with safety checks) and rewrite the document with live Zotero
citations — by default in **NIH numbered superscript** style.

It can also:

- **Scan and verify** the Zotero citation fields already in a document.
- **Insert** a Zotero citation at a chosen spot.
- **Convert** Mendeley / Word / manually-typed references into Zotero citations.
- **Check citation integrity** — find orphan citations, uncited references, and
  **retracted papers**.
- **Reformat** in any citation style (Vancouver, Nature, APA, journal-specific…).

It works by talking to the Zotero Web API and editing the Word file directly. You
do **not** need Microsoft Word or the Zotero desktop app for it to run — though
you'll want them (with the free [Zotero Word
plugin](https://www.zotero.org/support/word_processor_plugin_installation)) to
*see* the finished citations and keep editing them.

---

## Table of contents

1. [What you need](#1-what-you-need)
2. [Install](#2-install)
3. [Get your Zotero API key and library ID](#3-get-your-zotero-api-key-and-library-id)
4. [Configure](#4-configure)
5. [Use it with Claude Code (easiest)](#5-use-it-with-claude-code-easiest)
6. [Use it from the command line](#6-use-it-from-the-command-line)
7. [The flagship workflow: EndNote → Zotero](#7-the-flagship-workflow-endnote--zotero)
8. [Changing the citation style](#8-changing-the-citation-style)
9. [How your data is handled](#9-how-your-data-is-handled)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. What you need

- **Python 3.9 or newer** (`python3 --version` to check). macOS and Linux have it
  built in; on Windows use [python.org](https://www.python.org/downloads/) or WSL.
- **git** (to download this repository).
- A free **Zotero account** (https://www.zotero.org).
- *(Optional, to view results)* Microsoft Word with the
  [Zotero Word plugin](https://www.zotero.org/support/word_processor_plugin_installation).

No prior Claude or programming experience is required.

---

## 2. Install

Open a terminal and run:

```bash
git clone https://github.com/alexlicohen/zotero-word-cite.git
cd zotero-word-cite
./install.sh
```

`install.sh` creates a self-contained Python environment and installs the three
dependencies (`lxml`, `python-docx`, `pytest`). It does not touch the rest of
your system.

> **Tip — make it a Claude Code skill.** If you use Claude Code, clone (or move)
> this folder to `~/.claude/skills/zotero-word-cite`. Claude will then discover
> it automatically and you can just ask in plain English (see
> [section 5](#5-use-it-with-claude-code-easiest)).
> ```bash
> git clone https://github.com/alexlicohen/zotero-word-cite.git ~/.claude/skills/zotero-word-cite
> cd ~/.claude/skills/zotero-word-cite && ./install.sh
> ```

---

## 3. Get your Zotero API key and library ID

You need two things: an **API key** and a **library ID**.

### a) Create an API key (with write access)

1. Go to **https://www.zotero.org/settings/keys** (log in if needed).
2. Click **"Create new private key."**
3. Give it a name (e.g. *zotero-word-cite*).
4. **Important:** check **"Allow library access"** *and* **"Allow write
   access."** Write access is required so the tool can add imported EndNote
   references to your library.
5. If you'll use a **group** library, also tick that group (or "Read/Write" for
   all groups).
6. Click **Save Key** and copy the long key string — you won't see it again.

### b) Find your library ID

You can point the tool at a **group library** (shared, recommended for
collaboration) or your **personal library**.

- **Group library ID** — open your group on zotero.org. The number in the URL is
  the group ID:
  `https://www.zotero.org/groups/`**`2504198`**`/your-group-name`
  (also under *Group Settings → Library*). Use this as `ZOTERO_GROUP_ID`.
- **Personal library ID** — on the [API keys page](https://www.zotero.org/settings/keys)
  it says *"Your userID for use in API calls is `NNNNNN`."* Use that as
  `ZOTERO_USER_ID`. (Only used if you don't set a group ID.)

---

## 4. Configure

Copy the example config and fill in your values:

```bash
cp .env.example .env
```

Open `.env` in any text editor and set at least:

```ini
ZOTERO_API_KEY=<the key you created>
ZOTERO_GROUP_ID=<your group number>     # or leave blank and set ZOTERO_USER_ID
```

> `.env` holds your secret key. It is **gitignored** — it will never be committed
> or shared. Never paste your key into a document or a chat.

Verify everything is wired up:

```bash
./scripts/zwc init
```

You should see `set` next to `ZOTERO_API_KEY` and your library ID. Then try an
offline check that needs no network:

```bash
./scripts/zwc csl --list
```

This lists the citation styles, including `vancouver-superscript` (the NIH
default).

---

## 5. Use it with Claude Code (easiest)

If you installed this as a Claude Code skill (see the tip in
[section 2](#2-install)), just describe what you want. Examples:

- *"Convert the EndNote references in `~/Desktop/paper.docx` to Zotero using the
  exported library `~/Desktop/paper.ris`, and reformat in NIH style."*
- *"Scan `manuscript.docx` and tell me which citations aren't linked to my Zotero
  library."*
- *"Check `grant.docx` for retracted references."*
- *"Switch the citations in `paper.docx` to Nature style."*

Claude will run the right commands, show you a preview before writing anything to
your library, and explain the results. You stay in control — nothing is added to
Zotero until you confirm.

---

## 6. Use it from the command line

Every command is `./scripts/zwc <subcommand>`. Run `./scripts/zwc <subcommand>
--help` for full options. The input document is never changed in place — results
go to a new file you name with `-o`.

```bash
# Environment + styles
./scripts/zwc init
./scripts/zwc csl --list
./scripts/zwc csl --resolve "Annals of Neurology"

# Look things up in your Zotero library
./scripts/zwc zotero --search "tuber asd"
./scripts/zwc zotero --doi 10.1212/WNL.0000000000012345 --format reference

# Work with a document
./scripts/zwc zotero-scan manuscript.docx --check-links
./scripts/zwc cite-check manuscript.docx
./scripts/zwc cite-into manuscript.docx --doi 10.1212/WNL.0000000000012345 \
    --anchor "as shown previously" -o manuscript-cited.docx
./scripts/zwc convert-to-zotero manuscript.docx --track -o manuscript-zotero.docx
```

---

## 7. The flagship workflow: EndNote → Zotero

You have `paper.docx` (citations are EndNote fields) and an EndNote library
exported as **RIS** (`File → Export → RIS`) or **EndNote XML** (`File → Export →
XML`), say `paper.ris`.

**Step 1 — dry run (nothing is written):**

```bash
./scripts/zwc endnote-migrate paper.docx paper.ris
```

This prints a plan: which references would be **newly created** in Zotero, which
already **exist** in your library (deduplicated by DOI/title), which resolved
with **low confidence**, and any flagged as **retracted**. Review it.

**Step 2 — apply (writes to Zotero + produces a new document):**

```bash
./scripts/zwc endnote-migrate paper.docx paper.ris --apply -o paper-zotero.docx
```

New references are added to your Zotero library — tagged `added-by:zotero-word-cite`
and placed in an **"Imported — review"** collection so you can eyeball them — and
the document's EndNote citations are replaced with **live Zotero fields**.

**Step 3 — see the result:**

Open `paper-zotero.docx` in Microsoft Word with the Zotero plugin installed and
click **Zotero → Refresh**. The in-text citations render in NIH numbered
superscript and the bibliography regenerates. The citations are *live* — you can
add/remove/edit them with Zotero from here on.

> Add `--track` to record the changes as Word tracked changes (accept/reject in
> Word) for review.

---

## 8. Changing the citation style

NIH numbered superscript (`vancouver-superscript`) is the default. To use another
style, pass `--style` with a CSL id from `./scripts/zwc csl --list`:

```bash
./scripts/zwc endnote-migrate paper.docx paper.ris --apply \
    --style nature -o paper-nature.docx
```

Common ids: `vancouver-superscript` (NIH), `vancouver`, `nature`, `apa`,
`annals-of-neurology`, `brain`, `neurology`, `the-lancet-neurology`. You can also
change the style later in Word from the Zotero plugin (Document Preferences).

---

## 9. How your data is handled

- Your Zotero API key lives only in `.env`, which is **gitignored**. It is sent
  only to Zotero's official API over HTTPS.
- The tool **reads** your Zotero library and, only when you pass `--apply`,
  **adds** new references to it. It never deletes or overwrites existing Zotero
  items.
- Documents are processed locally. To resolve/verify references it queries public
  services (Zotero, Crossref, PubMed, Retraction Watch); only DOIs/titles/PMIDs
  are sent, never document text.
- Optional: set `ZOTERO_WORD_CITE_CONTACT_EMAIL` to join the API "polite pool."

---

## 10. Troubleshooting

| Symptom | Fix |
|---|---|
| `zwc: virtualenv not found` | Run `./install.sh` from the project folder first. |
| `init` shows `ZOTERO_API_KEY: MISSING` | You haven't created/edited `.env` — see [section 4](#4-configure). |
| "write access" / refuses to add references | Your API key lacks **write** permission — recreate it ([section 3a](#a-create-an-api-key-with-write-access)). |
| Citations show as `(1)` placeholders / don't render in Word | Open in Word **with the Zotero plugin** and click **Refresh** — rendering happens there. |
| Retraction check seems skipped | The Retraction Watch database downloads on first use and needs network access; it degrades gracefully offline. Run `./scripts/zwc cite-check FILE.docx` while online. |
| A reference wasn't matched | The tool reports unmatched references rather than guessing. Check the DOI/title in your library, or import it first. |

Run the test suite any time to confirm your install is healthy:

```bash
./.venv/bin/python -m pytest tests/ -q
```

---

## License

[MIT](LICENSE). This project is a standalone extraction of the citation/Zotero
engine from a larger grant-writing toolkit.
