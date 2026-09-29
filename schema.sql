CREATE TABLE memories (
  id         bigserial PRIMARY KEY,
  text       text NOT NULL,
  tags       text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now(),
  search     tsvector GENERATED ALWAYS AS (to_tsvector('english', text || ' ' || tags)) STORED
);
CREATE INDEX memories_search_idx ON memories USING gin (search);

-- Week 2: search by meaning (pgvector).
-- The column has no fixed size so the embedding model can change later;
-- a personal memory store is small enough to search without an index.
CREATE EXTENSION IF NOT EXISTS vector;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS embedding vector;
