-- Week 2: search by meaning. Run once in the Supabase SQL Editor.
-- The column has no fixed size so the embedding model can change later;
-- a personal memory store is small enough to search without an index.
CREATE EXTENSION IF NOT EXISTS vector;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS embedding vector;
