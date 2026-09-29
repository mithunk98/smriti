-- Week 4: morning brief. Run once in the Supabase SQL Editor.
-- One row per day a brief was sent, so it is never sent twice.
CREATE TABLE IF NOT EXISTS briefs (
  day     date PRIMARY KEY,
  sent_at timestamptz NOT NULL DEFAULT now()
);
