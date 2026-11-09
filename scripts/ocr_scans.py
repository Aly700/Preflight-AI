"""Read local English Tesseract TSV; compare typed OCR with each rendered page's reference."""

import argparse
from collections import defaultdict
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import re
import subprocess
from typing import Any

from render_scans import ROOT, read_document, select_documents, selected_ids, write_json


def parse_tsv(tsv: str, *, excluded_regions: list[list[int]] | None = None) -> tuple[str, float | None]:
    """Rebuild Tesseract's block/paragraph/line order; average scored words only."""
    reader = csv.DictReader(io.StringIO(tsv), delimiter='\t', quoting=csv.QUOTE_NONE)
    required = {'level', 'page_num', 'block_num', 'par_num', 'line_num', 'word_num', 'conf', 'text'}
    if excluded_regions:
        required |= {'left', 'top', 'width', 'height'}
    if not reader.fieldnames or not required.issubset(reader.fieldnames):
        raise ValueError('Invalid Tesseract TSV header')
    lines: dict[tuple[int, ...], list[tuple[int, str]]] = defaultdict(list)
    confidences = []
    try:
        for row in reader:
            if int(row['level']) != 5 or not row['text'].strip():
                continue
            if excluded_regions:
                x = int(row['left']) + int(row['width']) / 2
                y = int(row['top']) + int(row['height']) / 2
                if any(left <= x <= right and top <= y <= bottom for left, top, right, bottom in excluded_regions):
                    continue
            key = tuple(int(row[name]) for name in ('page_num', 'block_num', 'par_num', 'line_num'))
            lines[key].append((int(row['word_num']), row['text'].strip()))
            confidence = float(row['conf'])
            if not math.isfinite(confidence) or not -1 <= confidence <= 100:
                raise ValueError('Invalid confidence')
            if confidence >= 0:
                confidences.append(confidence)
    except (ValueError, TypeError, AttributeError, KeyError) as error:
        raise ValueError('Invalid Tesseract TSV word row') from error
    text = '\n'.join(' '.join(word for _, word in sorted(lines[key])) for key in sorted(lines))
    return text, sum(confidences) / len(confidences) if confidences else None


def run_tesseract(arguments: list[str]) -> str:
    try:
        result = subprocess.run(
            ['tesseract', *arguments],
            shell=False,
            check=True,
            capture_output=True,
            text=True,
            encoding='utf-8',
            timeout=120,
        )
    except FileNotFoundError:
        raise ValueError(
            'tesseract is not installed; use the tools/ingest image or install English Tesseract locally'
        ) from None
    except subprocess.TimeoutExpired:
        raise ValueError('tesseract exceeded the 120-second page timeout') from None
    except subprocess.CalledProcessError as error:
        raise ValueError(f'tesseract exited with status {error.returncode}') from None
    return result.stdout


def tesseract_version() -> str:
    lines = run_tesseract(['--version']).splitlines()
    if not lines or not lines[0].startswith('tesseract '):
        raise ValueError('Unrecognized tesseract version output')
    return lines[0].strip()


def ocr_page(path: Path, excluded_regions: list[list[int]] | None = None) -> dict[str, Any]:
    # An absolute path cannot be confused with an option, and argv is never a shell string.
    tsv = run_tesseract([str(path.resolve()), 'stdout', '-l', 'eng', '--dpi', '150', 'tsv'])
    text, confidence = parse_tsv(tsv)
    printed_text, _ = parse_tsv(tsv, excluded_regions=excluded_regions)
    return {'text': text, 'printed_text': printed_text, 'mean_word_confidence': confidence}


def run_ocr(scans: Path, output: Path, only: str | None) -> list[dict[str, Any]]:
    ids = selected_ids(only)
    manifests = [path for path in sorted(scans.glob('DOC-*.json')) if re.fullmatch(r'DOC-[0-9]{4}', path.stem)]
    if ids is not None:
        if ids - {path.stem for path in manifests}:
            raise ValueError('Selected scan manifest is missing; render the document first')
        manifests = [path for path in manifests if path.stem in ids]
    if not manifests:
        raise ValueError('No scan manifests found; run scripts/render_scans.py first')
    version = tesseract_version()
    output.mkdir(parents=True, exist_ok=True)
    results = []
    for path in manifests:
        if path.is_symlink():
            raise ValueError('Scan manifest must not be a symbolic link')
        manifest = json.loads(path.read_text(encoding='utf-8'))
        if manifest['doc_id'] != path.stem or not manifest['pages']:
            raise ValueError('Invalid scan manifest identity or empty pages')
        result = {key: manifest[key] for key in ('doc_id', 'kind', 'renderer_version', 'seed', 'source_sha256')}
        result['tesseract_version'] = version
        result['pages'] = []
        for number, page in enumerate(manifest['pages'], start=1):
            name = f'{path.stem}-p{number}.jpg'
            if page['page_number'] != number or page['file'] != name:
                raise ValueError('Scan pages must be consecutive and match the document ID')
            image_path = scans / name
            if image_path.is_symlink():
                raise ValueError('Scan image must not be a symbolic link')
            if hashlib.sha256(image_path.read_bytes()).hexdigest() != page['sha256']:
                raise ValueError(f'{path.stem}: scan checksum mismatch; render again before OCR')
            if not page.get('printed_reference') or 'excluded_regions' not in page:
                raise ValueError('Scan lacks a printed reference or annotation regions; render and OCR again')
            recognized = ocr_page(image_path, page['excluded_regions'])
            result['pages'].append({'page_number': number, 'scan_sha256': page['sha256'], **recognized})
        write_json(output / f'{path.stem}.json', result)
        results.append(result)
    return results


def normalize_whitespace(text: str) -> str:
    return ' '.join(text.split())


def levenshtein(reference: str, actual: str) -> int:
    """Exact character edit distance, with a row of memory and common ends removed."""
    start = 0
    while start < min(len(reference), len(actual)) and reference[start] == actual[start]:
        start += 1
    reference, actual = reference[start:], actual[start:]
    end = 0
    while end < min(len(reference), len(actual)) and reference[-end - 1] == actual[-end - 1]:
        end += 1
    if end:
        reference, actual = reference[:-end], actual[:-end]
    if len(reference) < len(actual):
        reference, actual = actual, reference
    if not actual:
        return len(reference)
    row = list(range(len(actual) + 1))
    for i, ref in enumerate(reference, start=1):
        next_row = [i]
        for j, char in enumerate(actual, start=1):
            next_row.append(min(next_row[-1] + 1, row[j] + 1, row[j - 1] + (ref != char)))
        row = next_row
    return row[-1]


def character_error_rate(reference: str, actual: str) -> float:
    reference, actual = normalize_whitespace(reference), normalize_whitespace(actual)
    if not reference:
        if actual:
            raise ValueError('CER is undefined for an empty reference with nonempty OCR')
        return 0.0
    return levenshtein(reference, actual) / len(reference)


def aggregate_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_kind: dict[str, list[dict[str, Any]]] = defaultdict(list)
    documents = []
    for row in sorted(rows, key=lambda row: row['doc_id']):
        item = {**row, 'cer': row['edits'] / row['reference_characters'] if row['reference_characters'] else 0.0}
        documents.append(item)
        by_kind[row['kind']].append(item)
    summaries = {}
    for kind, items in sorted(by_kind.items()):
        characters = sum(item['reference_characters'] for item in items)
        summaries[kind] = {
            'count': len(items),
            'cer': sum(item['edits'] for item in items) / characters if characters else 0.0,
            'mean_cer': sum(item['cer'] for item in items) / len(items),
        }
    return {
        'document_count': len(documents),
        'by_kind': summaries,
        'documents': documents,
        'worst_five': sorted(documents, key=lambda row: (-row['cer'], row['doc_id']))[:5],
    }


def build_report(
    documents: Path,
    output: Path,
    only: str | None,
    *,
    scans: Path = ROOT / 'build/scans',
) -> dict[str, Any]:
    rows = []
    missing = []
    for path in select_documents(documents, only):
        document = read_document(path)
        ocr_path = output / f'{document.doc_id}.json'
        if not ocr_path.exists():
            missing.append(document.doc_id)
            continue
        if ocr_path.is_symlink():
            raise ValueError('OCR result must not be a symbolic link')
        result = json.loads(ocr_path.read_text(encoding='utf-8'))
        if result['doc_id'] != document.doc_id or result['source_sha256'] != document.source_sha256:
            raise ValueError(f'{document.doc_id}: source changed since rendering; render and OCR again')
        if not result['pages'] or [p['page_number'] for p in result['pages']] != list(
            range(1, len(result['pages']) + 1)
        ):
            raise ValueError('OCR pages must be nonempty and consecutively ordered')
        manifest_path = scans / f'{document.doc_id}.json'
        if manifest_path.is_symlink():
            raise ValueError('Scan manifest must not be a symbolic link')
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        if any(result.get(key) != manifest.get(key) for key in ('doc_id', 'source_sha256', 'renderer_version', 'seed')):
            raise ValueError(f'{document.doc_id}: render changed; render and OCR again')
        if len(result['pages']) != len(manifest['pages']):
            raise ValueError(f'{document.doc_id}: render changed; page count differs')
        pages: list[dict[str, Any]] = []
        for number, (recognized, rendered) in enumerate(zip(result['pages'], manifest['pages'], strict=True), 1):
            if not isinstance(rendered.get('printed_reference'), str) or not rendered['printed_reference'].strip():
                raise ValueError('Scan lacks a printed reference; render and OCR again')
            if recognized.get('scan_sha256') != rendered['sha256'] or rendered['page_number'] != number:
                raise ValueError(f'{document.doc_id}: render changed; render and OCR again')
            if not isinstance(recognized.get('printed_text'), str):
                raise ValueError('Saved OCR lacks printed text; run OCR again')
            confidence = recognized.get('mean_word_confidence')
            if confidence is not None and (
                not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0 <= confidence <= 100
            ):
                raise ValueError('Invalid saved page confidence; run OCR again')
            reference = normalize_whitespace(rendered['printed_reference'])
            actual = normalize_whitespace(recognized['printed_text'])
            edits = levenshtein(reference, actual)
            pages.append(
                {
                    'page_number': number,
                    'edits': edits,
                    'reference_characters': len(reference),
                    'cer': edits / len(reference),
                    'mean_word_confidence': confidence,
                }
            )
        rows.append(
            {
                'doc_id': document.doc_id,
                'kind': document.fields['kind'],
                'edits': sum(page['edits'] for page in pages),
                'reference_characters': sum(page['reference_characters'] for page in pages),
                'pages': pages,
            }
        )
    if not rows:
        raise ValueError('No saved OCR results for the selected documents')
    report = aggregate_report(rows)
    report['missing_ocr'] = missing
    report['comparison'] = (
        'Per-page typed OCR versus rendered printed reference, including headers and footers. '
        'Handwriting and stamps are excluded by scan-coordinate annotation regions; '
        'word centers inside those regions are omitted. Confidence is the mean of scored full-page TSV words.'
    )
    write_json(output / 'report.json', report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--documents', type=Path, default=ROOT / 'data/documents')
    parser.add_argument('--scans', type=Path, default=ROOT / 'build/scans')
    parser.add_argument('--output', type=Path, help='Defaults to DOCUMENTS/ocr')
    parser.add_argument('--only', help='Comma-separated document IDs')
    parser.add_argument('--report', action='store_true', help='Report saved OCR without invoking Tesseract')
    args = parser.parse_args()
    output = args.output or args.documents / 'ocr'
    try:
        if args.report:
            report = build_report(args.documents, output, args.only, scans=args.scans)
            for kind, summary in report['by_kind'].items():
                print(
                    f'{kind}: {summary["count"]} document(s), CER {summary["cer"]:.4f}, '
                    f'mean CER {summary["mean_cer"]:.4f}'
                )
            print('Worst five documents:')
            for row in report['worst_five']:
                print(f'  {row["doc_id"]} ({row["kind"]}): CER {row["cer"]:.4f}')
            print(f'Reported {report["document_count"]} document(s); {len(report["missing_ocr"])} missing OCR.')
        else:
            results = run_ocr(args.scans, output, args.only)
            for result in results:
                print(f'{result["doc_id"]}: OCR {len(result["pages"])} page(s)')
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as error:
        print(f'OCR failed: {error}')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
