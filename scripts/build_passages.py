#!/usr/bin/env python3
"""Build deterministic OCR passages with local, checksum-verified embeddings.

Words use the authored-document contract (Unicode words with internal hyphens
and apostrophes). Sentence boundaries are a punctuation heuristic. Every next
passage repeats exactly 20 words; a boundary must advance beyond that overlap.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Protocol
import unicodedata

import numpy as np
from numpy.typing import NDArray

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcp_service.embeddings import Embedder  # noqa: E402

WORD = re.compile(r"\b\w+(?:[-']\w+)*\b")
SENTENCE_END = re.compile(r"""[.!?]["'\u201d\u2019)]*\s*$""")
METADATA_FIELDS = ('doc_id', 'kind', 'title', 'date', 'station', 'program', 'author', 'team', 'related_ids')
HEADERS = {*METADATA_FIELDS, 'authored_by', 'spec_version'}


class PassageEmbedder(Protocol):
    def embed_passages(self, texts: Sequence[str]) -> NDArray[np.float32]: ...


def clean_ocr(text: str) -> str:
    """Drop scanner noise, join word hyphenation across lines, collapse whitespace."""
    lines = [line.strip() for line in text.splitlines() if sum(char.isalnum() for char in line) >= 3]
    joined = re.sub(r'(?<=\w)-\n(?=\w)', '', '\n'.join(lines))
    return ' '.join(unicodedata.normalize('NFKC', joined).split())


def chunk_text(text: str, max_words: int = 120, overlap: int = 20) -> list[str]:
    if not 0 <= overlap < max_words:
        raise ValueError('Chunk overlap must be nonnegative and smaller than max_words')
    text = ' '.join(unicodedata.normalize('NFKC', text).split())
    words = list(WORD.finditer(text))
    chunks = []
    start = 0
    while start < len(words):
        end = min(start + max_words, len(words))
        if end < len(words):
            for candidate in range(end, start + overlap, -1):
                between = text[words[candidate - 1].end() : words[candidate].start()]
                if SENTENCE_END.search(between):
                    end = candidate
                    break
        left = 0 if start == 0 else words[start].start()
        right = len(text) if end == len(words) else words[end].start()
        chunks.append(text[left:right].strip())
        if end == len(words):
            break
        start = end - overlap
    return chunks


def read_document(path: Path) -> tuple[dict[str, Any], str]:
    lines = path.read_text(encoding='utf-8').splitlines()
    if not lines or lines[0] != '---' or '---' not in lines[1:]:
        raise ValueError(f'{path.stem}: invalid front matter delimiters')
    end = lines.index('---', 1)
    fields: dict[str, str] = {}
    for line in lines[1:end]:
        key, separator, value = line.partition(':')
        if not separator or key not in HEADERS or key in fields or not value.strip():
            raise ValueError(f'{path.stem}: invalid front matter field')
        fields[key] = value.strip()
    if not (HEADERS - {'station', 'program'}) <= fields.keys() or fields['doc_id'] != path.stem:
        raise ValueError(f'{path.stem}: incomplete or mismatched front matter')
    metadata: dict[str, Any] = {key: fields.get(key) for key in METADATA_FIELDS}
    metadata['related_ids'] = [value.strip() for value in fields['related_ids'].split(',')]
    return metadata, '\n'.join(lines[end + 1 :])


def build_passages(documents: Path, output: Path, embedder: PassageEmbedder, *, clean: bool = False) -> int:
    """Read sources in ID order and atomically publish one UTF-8 JSONL artifact."""
    rows: list[dict[str, Any]] = []
    sources = sorted(documents.glob('DOC-*.md'))
    if not sources:
        raise ValueError('No document sources found')
    if clean and output.resolve() in {
        (documents / 'passages.jsonl').resolve(),
        (ROOT / 'data/documents/passages.jsonl'),
    }:
        raise ValueError('--clean requires a separate --output')
    for source in sources:
        if not re.fullmatch(r'DOC-[0-9]{4}', source.stem) or source.is_symlink():
            raise ValueError('Invalid document source path')
        metadata, body = read_document(source)
        if clean:
            text = body
        else:
            ocr = json.loads((documents / 'ocr' / f'{source.stem}.json').read_text(encoding='utf-8'))
            if ocr['doc_id'] != source.stem or ocr['source_sha256'] != hashlib.sha256(source.read_bytes()).hexdigest():
                raise ValueError(f'{source.stem}: OCR source mismatch')
            pages = sorted(ocr['pages'], key=lambda page: page['page_number'])
            if [page['page_number'] for page in pages] != list(range(1, len(pages) + 1)):
                raise ValueError(f'{source.stem}: invalid OCR page sequence')
            text = clean_ocr('\n'.join(page['text'] for page in pages))
        chunks = chunk_text(text)
        if not chunks:
            raise ValueError(f'{source.stem}: no passage text')
        vectors = embedder.embed_passages(chunks)
        if vectors.shape != (len(chunks), 384) or not np.isfinite(vectors).all():
            raise ValueError('Invalid passage embeddings')
        for ordinal, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True), 1):
            rows.append(
                {
                    'passage_id': f'{source.stem}#{ordinal}',
                    **metadata,
                    'ordinal': ordinal,
                    'text': chunk,
                    'embedding': [round(float(value), 6) for value in vector],
                }
            )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode='w', encoding='utf-8', newline='\n', dir=output.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n')
        temporary.replace(output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--documents', type=Path, default=ROOT / 'data/documents')
    parser.add_argument('--model-dir', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--clean', action='store_true', help='Use the Markdown body for evaluation only')
    args = parser.parse_args()
    output = args.output or args.documents / 'passages.jsonl'
    if args.clean and (
        args.output is None
        or output.resolve() in {(args.documents / 'passages.jsonl').resolve(), ROOT / 'data/documents/passages.jsonl'}
    ):
        parser.error('--clean requires a separate --output')
    try:
        count = build_passages(args.documents, output, Embedder(args.model_dir), clean=args.clean)
    except (OSError, ValueError, KeyError) as error:
        parser.error(str(error))
    print(json.dumps({'passages': count, 'source': 'clean' if args.clean else 'ocr'}, sort_keys=True))


if __name__ == '__main__':
    main()
