# Retrieval evaluation

How well `search_documents` finds the right passage, measured without a chat model.
The gold set is `evals/retrieval.json` (44 questions written from the documents and
phrased differently from them). A passage is relevant when it belongs to a gold
document and contains one of the case's key phrases. Commands are in
[evals/README.md](../evals/README.md#retrieval-evaluation).

- **Passage recall@5**: a relevant passage is in the top five.
- **Document recall@5**: any passage of a gold document is in the top five.
- **MRR@10**: the reciprocal rank of the first relevant passage, 0 if it is not in the top ten.

## The search the tool runs (November 9, 2026)

Measured inside the MCP service against PostgreSQL, over the OCR text of the scans,
with the local BAAI/bge-small-en-v1.5 model.

| Search | Passage recall@5 | Document recall@5 | MRR@10 |
|---|---:|---:|---:|
| Keyword (full-text, any term) | 0.955 | 1.000 | 0.924 |
| Vector (bge-small) | 0.955 | 1.000 | 0.886 |
| Hybrid, reciprocal rank fusion | **0.977** | 1.000 | **0.928** |

## Embedding model and OCR text

Exact cosine ranking in memory, vector search only.

| Embeddings | Text | Passage recall@5 | Document recall@5 | MRR@10 |
|---|---|---:|---:|---:|
| bge-small, local, 384 dimensions | OCR | 0.955 | 1.000 | 0.886 |
| bge-small | clean | 0.977 | 1.000 | 0.915 |
| Amazon Titan Text Embeddings V2, 1024 dimensions | OCR | 1.000 | 1.000 | 0.938 |
| Titan V2 | clean | 0.977 | 1.000 | 0.955 |

## What it shows

- Hybrid search beats either half on its own; the distinctive vocabulary of the
  documents makes keyword search strong, and vectors catch paraphrases.
- OCR noise costs the local model a few points against clean text. It also damages
  facts: one handover's "16:25" was read as "46:25", which no search can repair.
- The hosted Titan model ranks better than the small local model as a pure vector
  search, but hybrid search over the local model comes close while staying offline
  and free of per-query calls, so the service keeps the local model.
- Every question found its gold document in the top five under every method.
