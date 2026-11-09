#!/usr/bin/env python3
"""Evaluate passage retrieval against a fixed gold set, without generating answers.

System mode uses the MCP service's database and ranking code. Offline mode compares
stored OCR embeddings with clean Markdown chunks using the pinned local BGE model;
optional Titan calls cache vectors by model/configuration and exact text hash.
Reports and console output contain metrics and case IDs, never source text.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import hashlib
import importlib
import json
from pathlib import Path
import sys
import tempfile
from typing import Any, Protocol

import numpy as np
from numpy.typing import NDArray

# python - has __file__ == '<stdin>'; the container already has the service
# packages on its import path. No scripts/ package is needed in system mode.
ROOT = Path(__file__).resolve().parents[1] if __file__ != '<stdin>' else Path.cwd()
sys.path.insert(0, str(ROOT))

from mcp_service.embeddings import Embedder, get_embedder  # noqa: E402
from mcp_service.repository import Repository  # noqa: E402

Row = dict[str, Any]
Rankings = Mapping[str, Mapping[str, Sequence[Row]]]
TITAN_MODEL = 'amazon.titan-embed-text-v2:0'
TITAN_DIMENSIONS = 1024


class TitanError(RuntimeError):
    """Controlled diagnostics that never contain SDK request or response text."""


class QueryEmbedder(Protocol):
    def embed_query(self, text: str) -> NDArray[np.float32]: ...

    def embed_passages(self, texts: Sequence[str]) -> NDArray[np.float32]: ...


def normalise(text: str) -> str:
    return ' '.join(text.casefold().split())


def is_relevant(case: Row, passage: Row) -> bool:
    """A gold document AND at least one normalised phrase substring in this text."""
    if passage['doc_id'] not in case['doc_ids']:
        return False
    text = normalise(passage['text'])
    return any(normalise(phrase) in text for phrase in case['phrases'])


def make_report(gold: Row, rankings: Rankings, *, mode: str) -> Row:
    """One metrics core for both modes. Ranks are one-based, capped at ten.

    Null means no relevant passage in the first ten. Tied scores retain the input
    ranking's order; this function never assigns shared or fractional ranks.
    """
    cases = gold['cases']
    ids = [case['id'] for case in cases]
    if not cases or len(set(ids)) != len(ids):
        raise ValueError('Gold cases must be nonempty with unique IDs')
    if any(set(by_case) != set(ids) for by_case in rankings.values()):
        raise ValueError('Rankings must cover exactly the gold cases')
    variants: Row = {}
    results: list[Row] = [{'id': ident, 'first_relevant_rank': {}} for ident in ids]
    for variant, by_case in rankings.items():
        passage_hits = document_hits = 0
        reciprocals: list[float] = []
        for case, result in zip(cases, results, strict=True):
            rows = by_case[case['id']][:10]
            rank = next((position for position, row in enumerate(rows, 1) if is_relevant(case, row)), None)
            passage_hits += rank is not None and rank <= 5
            document_hits += any(row['doc_id'] in case['doc_ids'] for row in rows[:5])
            reciprocals.append(1 / rank if rank is not None else 0.0)
            result['first_relevant_rank'][variant] = rank
        variants[variant] = {
            'cases': len(cases),
            'passage_recall@5': passage_hits / len(cases),
            'document_recall@5': document_hits / len(cases),
            'mrr@10': sum(reciprocals) / len(cases),
        }
    return {'version': gold['version'], 'mode': mode, 'variants': variants, 'cases': results}


def cosine_rank(passages: Sequence[Row], vectors: NDArray[Any], query: NDArray[Any], *, limit: int = 10) -> list[Row]:
    """Exact cosine ranking; ties use passage ID, independent of input order."""
    matrix = np.asarray(vectors, dtype=np.float64)
    query = np.asarray(query, dtype=np.float64)
    if matrix.ndim != 2 or query.ndim != 1 or matrix.shape != (len(passages), len(query)):
        raise ValueError('Embedding dimensions do not match passages and query')
    norms = np.linalg.norm(matrix, axis=1)
    query_norm = np.linalg.norm(query)
    if not np.isfinite(matrix).all() or not np.isfinite(query).all() or (norms == 0).any() or query_norm == 0:
        raise ValueError('Embeddings must be finite and nonzero')
    scores = (matrix / norms[:, None]) @ (query / query_norm)
    order = sorted(range(len(passages)), key=lambda i: (-float(scores[i]), passages[i]['passage_id']))
    return [passages[i] for i in order[:limit]]


def clean_passages(documents: Path) -> list[Row]:
    # Import only offline: the MCP image does not contain scripts/ or documents/.
    sys.path.insert(0, str(ROOT / 'scripts'))
    from build_passages import chunk_text, read_document

    rows: list[Row] = []
    for source in sorted(documents.glob('DOC-*.md')):
        metadata, body = read_document(source)
        for ordinal, text in enumerate(chunk_text(body), 1):
            rows.append({'passage_id': f'{metadata["doc_id"]}#{ordinal}', 'doc_id': metadata['doc_id'], 'text': text})
    if not rows:
        raise ValueError('No clean passages found')
    return rows


def evaluate_offline(gold: Row, documents: Path, embedder: QueryEmbedder, *, titan: QueryEmbedder | None = None) -> Row:
    ocr = [json.loads(line) for line in (documents / 'passages.jsonl').read_text(encoding='utf-8').splitlines()]
    if not ocr:
        raise ValueError('No OCR passages found')
    clean = clean_passages(documents)
    rankings: dict[str, dict[str, list[Row]]] = {}
    for name, model in [('bge', embedder), *([('titan', titan)] if titan is not None else [])]:
        ocr_vectors = (
            np.asarray([row['embedding'] for row in ocr], dtype=np.float32)
            if name == 'bge'
            else model.embed_passages([row['text'] for row in ocr])
        )
        clean_vectors = model.embed_passages([row['text'] for row in clean])
        queries = {case['id']: model.embed_query(case['question']) for case in gold['cases']}
        for source, passages, vectors in [('ocr', ocr, ocr_vectors), ('clean', clean, clean_vectors)]:
            rankings[f'{name}_{source}'] = {
                case['id']: cosine_rank(passages, vectors, queries[case['id']]) for case in gold['cases']
            }
    return make_report(gold, rankings, mode='offline')


def evaluate_system(gold: Row, repository: Repository, embedder: QueryEmbedder) -> Row:
    rankings: dict[str, dict[str, list[Row]]] = {variant: {} for variant in ('keyword', 'vector', 'hybrid')}
    for case in gold['cases']:
        ident, question = case['id'], case['question']
        rankings['keyword'][ident] = repository.keyword_candidates(question, None, None)
        rankings['vector'][ident] = repository.vector_candidates(embedder.embed_query(question), None, None)
        rankings['hybrid'][ident] = repository.search_documents(question, 10, None, None)['results']
    return make_report(gold, rankings, mode='system')


def titan_cache_key(model: str, text: str) -> str:
    identity = {
        'model': model,
        'text_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
        'dimensions': TITAN_DIMENSIONS,
        'normalize': True,
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()


class TitanEmbedder:
    """Only cache misses import boto3 and create a client via its default credential chain."""

    def __init__(self, cache_dir: Path, *, region: str = 'ca-central-1') -> None:
        self.cache_dir = cache_dir
        self.region = region
        self.client: Any = None

    @staticmethod
    def validate(vector: Any) -> NDArray[np.float32]:
        values = np.asarray(vector, dtype=np.float32)
        if values.shape != (TITAN_DIMENSIONS,) or not np.isfinite(values).all() or np.linalg.norm(values) == 0:
            raise ValueError('Invalid Titan embedding')
        return values

    def embed_query(self, text: str) -> NDArray[np.float32]:
        # Titan receives the original question, without the BGE-specific prefix.
        path = self.cache_dir / (titan_cache_key(TITAN_MODEL, text) + '.json')
        if path.exists():
            return self.validate(json.loads(path.read_text(encoding='utf-8')))
        if self.client is None:
            try:
                boto3 = importlib.import_module('boto3')
            except ImportError:
                raise TitanError(
                    'boto3 is required for --titan cache misses; install it separately or omit --titan'
                ) from None
            try:
                self.client = boto3.client('bedrock-runtime', region_name=self.region)
            except Exception:
                raise TitanError(
                    'Titan client setup failed; check the default AWS credential chain and region'
                ) from None
        try:
            response = self.client.invoke_model(
                modelId=TITAN_MODEL,
                body=json.dumps({'inputText': text, 'dimensions': TITAN_DIMENSIONS, 'normalize': True}),
                contentType='application/json',
                accept='application/json',
            )
            with response['body'] as body:
                payload = json.loads(body.read())
        except Exception:
            # SDK errors may contain request text; do not print them or chain them.
            raise TitanError('Titan embedding request failed; check AWS access and model availability') from None
        vector = self.validate(payload.get('embedding'))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.cache_dir, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(json.dumps(vector.tolist(), allow_nan=False) + '\n')
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return vector

    def embed_passages(self, texts: Sequence[str]) -> NDArray[np.float32]:
        return np.asarray([self.embed_query(text) for text in texts], dtype=np.float32).reshape(-1, TITAN_DIMENSIONS)


def load_gold(path: Path) -> Row:
    gold = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(gold, dict) or not isinstance(gold.get('version'), str):
        raise ValueError('Gold set needs a version')
    cases = gold.get('cases')
    if not isinstance(cases, list) or not cases:
        raise ValueError('Gold set needs nonempty cases')
    for case in cases:
        if not isinstance(case, dict) or any(
            not isinstance(case.get(field), str) or not case[field].strip() for field in ('id', 'question')
        ):
            raise ValueError('Each gold case needs an ID and question')
        for field in ('doc_ids', 'phrases'):
            values = case.get(field)
            if (
                not isinstance(values, list)
                or not values
                or any(not isinstance(v, str) or not v.strip() for v in values)
            ):
                raise ValueError('Each gold case needs nonempty doc_ids and phrases')
    if len({case['id'] for case in cases}) != len(cases):
        raise ValueError('Gold cases must have unique IDs')
    return gold


def table(report: Row) -> str:
    lines = [f'{"variant":<14} {"cases":>5} {"passage R@5":>12} {"document R@5":>13} {"MRR@10":>8}']
    for variant, metrics in report['variants'].items():
        lines.append(
            f'{variant:<14} {metrics["cases"]:>5} {metrics["passage_recall@5"]:>12.4f} '
            f'{metrics["document_recall@5"]:>13.4f} {metrics["mrr@10"]:>8.4f}'
        )
    return '\n'.join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('system', 'offline'), required=True)
    parser.add_argument(
        '--gold', type=Path, default=ROOT / 'evals/retrieval.json', help='Gold JSON file; never reads stdin'
    )
    parser.add_argument('--documents', type=Path, default=ROOT / 'data/documents', help='Offline corpus directory')
    parser.add_argument('--model-dir', type=Path, help='Required in offline mode; checksum-verified local BGE model')
    parser.add_argument(
        '--titan', action='store_true', help='Also compare Titan in offline mode (AWS calls on cache misses)'
    )
    parser.add_argument('--region', default='ca-central-1')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.mode == 'offline' and args.model_dir is None:
        parser.error('--mode offline requires --model-dir')
    if args.mode == 'system' and (args.titan or args.model_dir is not None):
        parser.error('--titan and --model-dir are offline options; system uses the MCP model configuration')
    try:
        gold = load_gold(args.gold)
        if args.mode == 'offline':
            titan = TitanEmbedder(ROOT / 'build/retrieval-cache', region=args.region) if args.titan else None
            report = evaluate_offline(gold, args.documents, Embedder(args.model_dir), titan=titan)
        else:
            import psycopg

            from preflight_core.database import open_database

            with open_database(application_name='preflight-oss-retrieval-eval') as connection:
                connection.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
                report = evaluate_system(gold, Repository(connection), get_embedder())
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + '\n', encoding='utf-8')
    except TitanError as error:
        # Only controlled, text-free Titan messages reach here.
        print(str(error), file=sys.stderr)
        return 2
    except Exception as error:
        # Database/JSON/model errors can include source text or connection details.
        print(
            f'Retrieval evaluation failed ({type(error).__name__}); check inputs and mode configuration',
            file=sys.stderr,
        )
        return 2
    print(table(report))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
