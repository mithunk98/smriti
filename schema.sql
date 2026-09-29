CREATE TABLE memories (
  id         bigserial PRIMARY KEY,
  text       text NOT NULL,
  tags       text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now(),
  search     tsvector GENERATED ALWAYS AS (to_tsvector('english', text || ' ' || tags)) STORED
);
CREATE INDEX memories_search_idx ON memories USING gin (search);
