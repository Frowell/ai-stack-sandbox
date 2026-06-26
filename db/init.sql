-- Runs once on first container start. Sets up the hybrid-retrieval substrate:
-- a vector column (dense) and a generated tsvector column (sparse), each indexed.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
    id        BIGSERIAL PRIMARY KEY,
    source    TEXT,
    content   TEXT NOT NULL,
    embedding VECTOR(1536),  -- text-embedding-3-small; change with EMBEDDING_DIM
    fts       tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED
);

-- Dense: approximate nearest neighbour over cosine distance.
CREATE INDEX IF NOT EXISTS documents_embedding_idx
    ON documents USING hnsw (embedding vector_cosine_ops);

-- Sparse: full-text keyword search.
CREATE INDEX IF NOT EXISTS documents_fts_idx
    ON documents USING gin (fts);
