-- Fictional document metadata and local bge-small-en-v1.5 passage embeddings.
-- Apply through load_documents.py after the base schema and records are loaded.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS preflight.documents (
    doc_id text PRIMARY KEY CHECK (doc_id ~ '^DOC-[0-9]{4}$'),
    kind text NOT NULL CHECK (kind IN ('policy', 'glossary', 'handover', 'memo', 'coordination', 'retrospective', 'form')),
    title text NOT NULL,
    doc_date date NOT NULL,
    station text CHECK (station IN ('DEMO-STATION-01', 'DEMO-STATION-02', 'DEMO-STATION-03')),
    program text CHECK (program IN ('DEMO-PROGRAM-ALPHA', 'DEMO-PROGRAM-BETA')),
    author_id text NOT NULL REFERENCES preflight.people,
    team_id text NOT NULL REFERENCES preflight.teams,
    related_ids text[] NOT NULL
);

CREATE TABLE IF NOT EXISTS preflight.passages (
    passage_id text PRIMARY KEY CHECK (passage_id ~ '^DOC-[0-9]{4}#[0-9]+$'),
    doc_id text NOT NULL REFERENCES preflight.documents,
    ordinal int NOT NULL CHECK (ordinal > 0),
    UNIQUE (doc_id, ordinal),
    text text NOT NULL,
    text_search tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    embedding vector(384) NOT NULL
);

CREATE INDEX IF NOT EXISTS passages_text_search_idx ON preflight.passages USING gin (text_search);
CREATE INDEX IF NOT EXISTS passages_embedding_idx ON preflight.passages USING hnsw (embedding vector_cosine_ops);
GRANT SELECT ON preflight.documents, preflight.passages TO preflight_reader;
