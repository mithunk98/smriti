# Smriti

*Smriti* (स्मृति) is Sanskrit for "memory". It is my personal AI memory: a small cloud MCP server that gives Claude long-term memory shared across all my devices.

```
Laptop 1 (Claude Code) ─┐
                        ├── HTTPS + secret token ──▶  Smriti (Render)  ──▶  Postgres (Supabase)
Laptop 2 (Claude Code) ─┘
```

## Tools

| Tool | What it does |
|---|---|
| `remember(text, tags)` | Save a fact, decision or plan |
| `recall(query)` | Search memories by meaning (with a Voyage key) or keywords, best matches first |
| `recent(days)` | List everything saved in the last N days |
| `add_task(title, due, notes)` | Add a to-do, optionally with a due date |
| `list_tasks()` | Open tasks, soonest first, with overdue / due-today flags |
| `complete_task(id)` | Mark a task as done |

## Setup

### 1. Database (Supabase)
1. Create a project at [supabase.com](https://supabase.com).
2. In **SQL Editor**, run [`schema.sql`](schema.sql).
3. Click **Connect** and copy the **Session pooler** connection string (IPv4, which Render needs), with your DB password filled in.

### 2. Secret token
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 3. Deploy (Render)
- New **Web Service**, then connect this repo
- Build: `pip install -r requirements.txt`
- Start: `uvicorn server:app --host 0.0.0.0 --port $PORT`
- Environment: `DATABASE_URL` (from step 1), `SMRITI_TOKEN` (from step 2)

The free tier sleeps after about 15 minutes idle, so the first request after that takes around a minute.

### 4. Optional: search by meaning
1. In the Supabase **SQL Editor**, run [`schema-semantic.sql`](schema-semantic.sql).
2. Create an API key at [voyageai.com](https://www.voyageai.com) and add it on Render as `VOYAGE_API_KEY`.

Existing memories are embedded automatically on the next `remember`/`recall`. If Voyage is unreachable, memories still save and search falls back to keywords. Set `VOYAGE_MODEL` to use a model other than `voyage-3.5-lite`.

### 5. Tasks
In the Supabase **SQL Editor**, run [`schema-tasks.sql`](schema-tasks.sql). Due dates use your local day; set `SMRITI_TIMEZONE` (default `Asia/Kolkata`) if you are elsewhere.

### 6. Connect Claude Code (on each laptop)
On Windows, paste [`setup-laptop.ps1`](setup-laptop.ps1) into PowerShell: it adds the server and the `CLAUDE.md` instructions below. Elsewhere:
```bash
claude mcp add --transport http --scope user smriti \
  https://YOUR-APP.onrender.com/mcp \
  --header "Authorization: Bearer YOUR_TOKEN"
```

Then add to `~/.claude/CLAUDE.md`:
```markdown
## Smriti (my personal memory)
- Before answering questions about my life, plans, decisions or preferences, call `recall`.
- When I share a decision, deadline, goal or important fact, save it with `remember` (add short tags).
- When I say "weekly review", call `recent` with days=7 and summarize what I did vs. what I planned.

## Smriti tasks
- When I mention something I have to do, add it with `add_task` (convert dates like "next Friday" to YYYY-MM-DD).
- When I ask what to do, plan my day or week, or start a work session, call `list_tasks` and point out anything overdue or due soon.
- When I say I finished something, mark it with `complete_task`.
```

## Run locally
```bash
pip install -r requirements.txt
export DATABASE_URL="postgresql://..." SMRITI_TOKEN="dev-token"
uvicorn server:app --port 8000
```

## Roadmap
- [x] Week 1: cloud memory (`remember` / `recall` / `recent`)
- [x] Week 2: semantic search (pgvector + Voyage embeddings)
- [x] Week 3: tasks with due dates
- [ ] Later: calendar and Spotify focus playlists
- [ ] Week 4: proactive morning brief and weekly review
