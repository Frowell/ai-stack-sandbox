-- Runs once on first container start. Sets up the hybrid-retrieval substrate:
-- a vector column (dense) and a generated tsvector column (sparse), each indexed.
-- The stored unit is a *chunk* (post layout-extraction / semantic chunking), not a
-- whole document -- hence doc_id / chunk_index / kind / meta alongside content.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
    id          BIGSERIAL PRIMARY KEY,
    source      TEXT,
    doc_id      TEXT,          -- logical document this chunk came from
    chunk_index INT,           -- position of the chunk within that document
    kind        TEXT,          -- body | heading | table | record | text | notes
    content     TEXT NOT NULL, -- chunk text; cited footnotes are merged in here
    meta        JSONB NOT NULL DEFAULT '{}'::jsonb,  -- section path, footnote ids, format
    embedding   VECTOR(1536),  -- text-embedding-3-small; change with EMBEDDING_DIM
    fts         tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED
);

-- Dense: approximate nearest neighbour over cosine distance.
CREATE INDEX IF NOT EXISTS documents_embedding_idx
    ON documents USING hnsw (embedding vector_cosine_ops);

-- Sparse: full-text keyword search.
CREATE INDEX IF NOT EXISTS documents_fts_idx
    ON documents USING gin (fts);

-- Optional: filter/inspect by chunk metadata (section, footnotes, format).
CREATE INDEX IF NOT EXISTS documents_meta_idx
    ON documents USING gin (meta);
