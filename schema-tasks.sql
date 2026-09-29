-- Week 3: tasks. Run once in the Supabase SQL Editor.
CREATE TABLE IF NOT EXISTS tasks (
  id         bigserial PRIMARY KEY,
  title      text NOT NULL,
  due        date,
  notes      text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now(),
  done_at    timestamptz
);
