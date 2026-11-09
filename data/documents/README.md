# Fictional process document corpus — pass 1

This pass contains a generation plan and validator, **no authored documents**.
Page-image rendering, OCR, retrieval, and retrieval evaluation belong to later
passes. Passing this validator is not evidence of any of those capabilities.

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
| `authored_by` | Actual authoring tool and model as `tool / model`; both required |
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
