# Fictional process document corpus

The generation plan and validator define the authored-document contract below.
Pass 3 adds scanned-page rendering and local OCR. Retrieval and retrieval
evaluation belong to later passes; validator success is not evidence of them.

## Provenance and scope

Author all later text independently from this plan and the original fictional
records described in `docs/DATA_CONTRACT.md` and `config/synthetic_spec.json`.
Never use legacy or real operational documents, screenshots, schemas, records,
or code as source material. Do not copy an existing organization's paperwork.

The corpus covers issue queues, triage, ownership, scheduling, escalation,
handover context, and process discussions only. Never include repair
instructions, inspection procedures, technical judgments about aircraft,
maintenance sign-offs, or certifications. Never include real organizations,
regulators, manufacturers, airlines, people, places, URLs, email addresses, or
phone numbers. Fictional people and teams keep their existing `Demo Worker`
and `Demo Team` names. Use only the configured `DEMO-STATION-*` and
`DEMO-PROGRAM-*` labels for locations and programs.

The record vocabulary includes `inspection` and `rectification` as task-kind
labels; the glossary may explain their use as tracking categories, never how to
perform physical work. A completed task does not automatically close its issue;
queue status never implies a technical judgment about an aircraft. Do not infer
historical status from a current-state CSV row or invent numeric claims.

## Deterministic plan

`spec.json` has version `documents-2026-11-08.1`, seed `20261108`, a vocabulary
copied from the original configuration, and 60 slots in fixed order:

| Kind | Count | Document IDs | Body words (inclusive) |
| --- | ---: | --- | --- |
| policy | 8 | DOC-0001–DOC-0008 | 180–500 |
| glossary | 2 | DOC-0009–DOC-0010 | 300–600 |
| handover | 14 | DOC-0011–DOC-0024 | 180–500 |
| memo | 10 | DOC-0025–DOC-0034 | 180–500 |
| coordination | 14 | DOC-0035–DOC-0048 | 180–500 |
| retrospective | 8 | DOC-0049–DOC-0056 | 180–500 |
| form | 4 | DOC-0057–DOC-0060 | 120–250 |

Regenerate from the repository root, using the standard library only:

```sh
.venv/bin/python -B scripts/document_spec.py
```

Options: `--seed INTEGER`, `--data CSV_DIRECTORY`, `--config JSON_FILE`, and
`--output JSON_FILE`. The same seed, CSV inputs, configuration, and generator
produce byte-identical UTF-8 JSON, including a trailing newline. There is no
wall-clock timestamp or model call. This script writes only the plan.

Sampling uses sorted CSV identifiers and a local seeded random generator. Each
slot draws an issue, its aircraft, one to three tasks belonging to that issue,
and the assigned team of one sampled task: four to six unique related IDs,
within the permitted one-to-six limit. The author is sampled from that team's
people. A slot's date is on or after the creation dates of all its related
records and on or before the earlier of the last CSV timestamp date and the
configured history end. The current records end on 2026-10-27. Dates express
fictional paperwork timing, not proof that the author worked a particular shift.

Handover, coordination, and form slots carry the related aircraft's station
and program. Memos and retrospectives carry its program. Policies and glossaries
have global scope, with their related records serving as permitted examples.
Working titles are distinct prompts for future authors and may be refined;
the final document titles must remain unique.

## Markdown file contract

Create one UTF-8 file per slot at `data/documents/DOC-0001.md` through
`data/documents/DOC-0060.md` in the next pass. The very first line is `---`.
Follow it with one simple `key: value` line per field, then a closing `---` line.
This is a deliberately small front-matter format, not general YAML: no nested
values, continuation lines, duplicate keys, comments, quoting rules, or extra
keys. Values are literal strings; do not add YAML quotes. Dates use `YYYY-MM-DD`.

| Field | Requirement |
| --- | --- |
| `doc_id` | Slot ID; also matches the filename |
| `kind` | Exact slot kind |
| `title` | Nonempty final title; start from the slot's working title |
| `date` | Exact slot date |
| `station` | Exact slot station when present; otherwise omit |
| `program` | Exact slot program when present; otherwise omit |
| `author` | Exact slot person ID |
| `team` | Exact slot team ID; must match the author's CSV team |
| `related_ids` | Slot IDs in the same order, comma-separated; spaces around commas optional |
| `authored_by` | who prepared the fictional document set |
| `spec_version` | `documents-2026-11-08.1` |

The body uses plain Markdown paragraphs, short lists, optional headings, and at
most one simple pipe table. Forms use `Field: value` lines. Avoid images, HTML,
code blocks, links, nested tables, and layout tricks so later rendering and OCR
have a small, predictable input format. Layout and prose quality need author
review; the validator does not parse a full Markdown grammar or enforce table
count or form layout.

The body may mention only IDs in `related_ids`, plus the slot's author and team.
This applies to headings and table cells too. Even the document's own ID is not
allowed in the body unless permitted by that rule; keep it in front matter.
Event, shift, and document IDs are recognized, but cannot be put in `related_ids`
in this version, so they cannot be cited in bodies. IDs are case-sensitive;
malformed or lowercase ID-shaped tokens are errors. Station and program labels
are scope labels, not record IDs in that list; use the slot's scope when present.

Word counts exclude front matter and count Unicode word tokens using
`\b\w+(?:[-']\w+)*\b` after NFKC normalization. Internal ASCII hyphens,
apostrophes, and underscores stay within a token: `DEMO-AIR-0001` and
`awaiting_review` each count once. Headings, field labels, list text, and table
cells count; Markdown punctuation does not. Limits are inclusive. Final titles
are compared after NFKC normalization, case folding, and whitespace collapsing.

## Validation and diagnostics

From the repository root:

```sh
.venv/bin/python -B scripts/validate_documents.py --allow-missing
.venv/bin/python -B scripts/validate_documents.py
.venv/bin/python -B -W error -m unittest discover -s tests -p test_documents.py
```

The first command supports work in progress. It skips only missing-document
checks: it still validates the full 60-slot spec, rejects unexpected files, and
checks every present document. With no documents in pass 1 it prints
`Validated 0 documents (60 missing slots allowed).` The strict command requires
all 60 files and deliberately fails until they exist.

Options `--documents DIRECTORY`, `--spec JSON_FILE`, and `--data CSV_DIRECTORY`
support isolated validation and temporary test fixtures. Validation checks the
plan against the CSV identities, author/team relationships, scope, related
issue/aircraft and task/issue relationships, dates, kind counts, and word ranges
before reading bodies. Symlink document files are rejected without reading them.

Success exits 0 with one summary line. Failure exits 1 and prints one rule per
line, such as `DOC-0007: banned maintenance`. Diagnostics never echo body text,
metadata values, titles, regex matches, or underlying exception messages.
Malformed filenames use `DOC-????`; unreadable or structurally invalid input
plans use `SPEC`. Repeated violations of the same rule in one document collapse
to one line. Duplicate-title errors identify every affected document.

## Scans and OCR

From the repository root, with `requirements-ingest.txt` installed in `.venv`
and Tesseract plus its English language data installed locally:

```sh
.venv/bin/python -B scripts/render_scans.py --documents data/documents --synthetic data/synthetic --output build/scans
.venv/bin/python -B scripts/ocr_scans.py --scans build/scans --documents data/documents
.venv/bin/python -B scripts/ocr_scans.py --scans build/scans --documents data/documents --report
```

Each command accepts `--only DOC-0001,DOC-0011` for a subset. Rendering reads
`DOCUMENTS/spec.json`, verifies the vendored font SHA-256 values, and writes
1275 × 1650 JPEGs at 150 dpi. `--synthetic` defaults to `data/synthetic` under
the repository: `people.csv` and `teams.csv` are read at runtime for the author's
display name and team. Missing identities or a mismatched team fail before
rendering that document. The CSVs are never baked into the ingestion image.

Letterhead documents use Libre Baskerville; notes use Courier Prime. Forms
have ruled boxes, bold printed labels and Caveat values with seeded baseline
and size variation (at most 3 pixels vertically and 2 font-size units).
Any value containing a record ID is printed in type. Empty fields stay empty;
values are never randomly erased. The signature abbreviates the looked-up
display name, with the full printed name and team beneath it. Seeded marginalia
use only `seen`, `file`, `copy to desk lead`, or `ok`. Stamps may cross a form
box edge. An existing title in the opening paper header is printed once, and
each sheet includes `Page N of M`. Globally scoped documents omit the scope
line instead of inventing a station or program.

The document seed is the specification seed plus its numeric document ID.
Renderer `scans-2026-11-09.2` places a smaller paper silhouette above a dark-grey
scanner bed with a soft shadow. Rotation magnitude is 0.4–1.2 degrees with a
random sign, scale is 0.972–0.98, and offsets are capped at 8 pixels at full
resolution. Edge irregularity is capped at 1.8 pixels. Paper texture and fine
grain cover the sheet; word-scale toner density can lighten or darken ink.
Fine grain is capped at ±4 intensity levels, density gain at 0.78–1.24, ink
bleed blend at 0.16–0.24, Gaussian blur at 0.20–0.34 pixels, and JPEG quality
at 60–78. Toner specks and small dust marks are seeded; a horizontal streak,
when selected, is only 1–2 pixels tall and 5–10 intensity levels darker.
Grayscale copies have slightly crushed blacks and light background noise;
color copies retain warm paper. Approximately one third use grayscale.

Each effect is a pure RGB-array transformation with a local seeded RNG.
Byte identity requires the same inputs, CSV identities, fonts, renderer and
Pillow/NumPy/native-library environment. `build/` is ignored. Each sidecar JSON
records the renderer version, seeds, source checksum, display names, page
parameters and image checksums. Its per-page `printed_reference` comes from
the exact drawing plan: letterhead, scope, document/date line, title and typed
body, printed author/team, and page-number line. Handwriting and stamps are
excluded. `excluded_regions` gives annotation rectangles in final scan pixel
coordinates after the same scale, rotation and offset as the image.

OCR verifies image checksums and writes `data/documents/ocr/DOC-NNNN.json` with
the actual Tesseract version, full page text, `printed_text`, scan checksum,
and `mean_word_confidence`. Confidence averages scored level-5 TSV words,
including zero and decimal scores, ignoring -1; it is `null` only when no
scored words exist. Printed text omits words whose TSV bounding-box centers
fall inside annotation regions. This geometric exclusion may also omit an
OCR word that spans printed and handwritten content; it does not claim to
separate overlapping ink perfectly.

`--report` reads saved OCR and scan sidecars without invoking Tesseract. It
compares each page's `printed_text` with that page's `printed_reference`.
CER is character edit distance after whitespace normalization; case and
punctuation still count. Page edits and reference lengths are summed for each
document, then aggregated by kind. Headers, printed names and page numbers
are part of the reference; handwriting and stamps are excluded from both
comparison sides. Per-page confidence is retained in `ocr/report.json`.
Missing OCR is listed; stale sources, changed render versions/checksums and
legacy results lacking printed references require rendering and OCR again.
Keep sidecars with their images. The scripts do not validate provenance;
run the corpus validator separately.

The separate ingestion image uses the root allow-list build context. Building
requires package access; the runtime commands can run offline with mounted data:

```sh
docker build -f tools/ingest/Dockerfile -t preflight-oss-ingest .
mkdir -p build/scans data/documents/ocr
docker run --rm --network none --user "$(id -u):$(id -g)" \
  -v "$PWD/data/documents:/app/data/documents:ro" \
  -v "$PWD/data/synthetic:/app/data/synthetic:ro" -v "$PWD/build:/app/build" \
  preflight-oss-ingest scripts/render_scans.py --synthetic data/synthetic --output build/scans
docker run --rm --network none --user "$(id -u):$(id -g)" \
  -v "$PWD/data/documents:/app/data/documents:ro" \
  -v "$PWD/data/documents/ocr:/app/data/documents/ocr" -v "$PWD/build:/app/build:ro" \
  preflight-oss-ingest scripts/ocr_scans.py --scans build/scans --documents data/documents
docker run --rm --network none --user "$(id -u):$(id -g)" \
  -v "$PWD/data/documents:/app/data/documents:ro" \
  -v "$PWD/data/documents/ocr:/app/data/documents/ocr" -v "$PWD/build:/app/build:ro" \
  preflight-oss-ingest scripts/ocr_scans.py --scans build/scans --documents data/documents --report
```

The image contains only its dependencies, scripts and fonts, not the fictional
records. Its default UID is 10001; the examples use the host UID/GID. English
Tesseract comes from Debian packages; its version is recorded, not pinned by
the image. Unit tests use fixed TSV fixtures and temporary documents without
invoking a real OCR binary.

## Complete banned-pattern list

Each expression below runs with `re.IGNORECASE` against the **entire document**,
including metadata, after Unicode NFKC normalization. Patterns are independent;
one input can violate several categories. No exemption is made for a negation,
quotation, or disclaimer. The labels are the diagnostic categories.

```text
maintenance: \btorqu\w*\b
maintenance: \brivet\w*\b
maintenance: \binspect\s+the\b
maintenance: \breplace\s+the\b
maintenance: \bremove\s+the\b
maintenance: \btighten\w*\b
maintenance: \blubricat\w*\b
maintenance: \bborescop\w*\b
maintenance: \bNDT\b
maintenance: \bpart\s+numbers?\b
maintenance: \bP\s*/\s*N\b
maintenance: \bservice\s+bulletins?\b
maintenance: \bairworthiness\s+directives?\b
readiness: \bairworthy\b
readiness: \bcleared\s+for\s+flight\b
readiness: \bsafe\s+to\s+fly\b
readiness: \bfit\s+for\s+flight\b
readiness: \breturn(?:ed|ing)?\s+to\s+service\b
certification: \bsign(?:ed|ing)?[\s-]+off\b
certification: \bcertif(?:y|ies|ied|ying|ication|ications)\b
certification: \breleas(?:e|ed|ing)\s+to\s+service\b
certification: \bapproved\s+for\s+flight\b
real-world: \b(?:FAA|EASA|Transport\s+Canada|Boeing|Airbus|Embraer|Bombardier)\b
real-world: \bATA\s+chapter\b
real-world: \b(?:Air\s+Canada|WestJet|Delta(?:\s+Air\s+Lines)?|United\s+Airlines|American\s+Airlines)\b
real-world: \b(?:Southwest\s+Airlines|British\s+Airways|Lufthansa|Emirates|Qatar\s+Airways)\b
real-world: \b(?:Singapore\s+Airlines|Air\s+France|KLM|Ryanair|easyJet|Qantas|JetBlue)\b
url: \b[a-z][a-z0-9+.-]*://\S+|\bwww\.\S+
url: (?<![\w@])(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}\b
email: (?<![\w.+-])[\w.+-]+@[\w.-]+\.[a-z]{2,63}\b
phone: (?<![\w-])(?:\+?1[ .-]?)?(?:\(\d{3}\)|\d{3})[ .-]?\d{3}[ .-]?\d{4}(?![\w-])
phone: (?<![\w-])\d{3}[ .-]\d{4}(?![\w-])
phone: (?<![\w-])\+\d{1,3}[ .-](?:\d[ .()-]?){7,14}\d(?![\w-])
```

These patterns are a reproducible screening rule, not an exhaustive detector of
real identities or prohibited meaning. Unknown organization/place/person names,
obfuscated contact details, and paraphrased technical procedures may escape
them. Future authors must follow the stricter scope above even when validation
passes; review provenance, factual consistency, language, and Markdown layout
before accepting the authored corpus. Do not describe a passing check as proof
that those reviews, OCR, or retrieval evaluation have happened.

On macOS, Docker Desktop's file sharing can briefly serve an older copy of a document edited just
before a container run. Each rendered page records the SHA-256 of its source document, and the OCR
report refuses a page rendered from a different source; re-render and OCR the documents it names.

Sample scans: [handover](../../docs/scans/handover-DOC-0011.jpg),
[form](../../docs/scans/form-DOC-0057.jpg) and [policy](../../docs/scans/policy-DOC-0004.jpg).
