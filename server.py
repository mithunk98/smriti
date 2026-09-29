import json
import logging
import os
import re
import urllib.request
from contextlib import contextmanager
from datetime import date, datetime
from zoneinfo import ZoneInfo

import psycopg
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.responses import PlainTextResponse

DATABASE_URL = os.environ["DATABASE_URL"]
TOKEN = os.environ["SMRITI_TOKEN"]
# Optional: with a Voyage AI key, recall searches by meaning instead of keywords.
VOYAGE_API_KEY = os.environ.get("VOYAGE_API_KEY", "").strip()
VOYAGE_MODEL = os.environ.get("VOYAGE_MODEL", "voyage-3.5-lite")
VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"
# "Today" for task due dates is the user's local day, not the server's (UTC).
TIMEZONE = ZoneInfo(os.environ.get("SMRITI_TIMEZONE", "Asia/Kolkata"))

log = logging.getLogger("smriti")

mcp = MCPServer(
    "smriti",
    instructions=(
        "Personal memory and task list for the user. Save important facts, decisions and plans with remember; "
        "look things up with recall before answering personal questions. Fix wrong memories with update_memory "
        "or forget rather than saving corrections. Track things the user has to do "
        "with add_task / list_tasks / complete_task."
    ),
)


def _redact(msg: str) -> str:
    """Hide the database password/URL so errors are safe to show."""
    msg = msg.replace(DATABASE_URL, "***")
    try:
        password = psycopg.conninfo.conninfo_to_dict(DATABASE_URL).get("password")
    except psycopg.Error:
        password = None
    secrets = [password] if password else []
    # A password containing @ or / is mis-split by URL parsing, so also hide
    # every fragment of the password exactly as written in the URL.
    userinfo = DATABASE_URL.split("://", 1)[-1].rsplit("@", 1)[0]
    if ":" in userinfo:
        secrets += [p for p in re.split(r"[@/:]", userinfo.split(":", 1)[1]) if len(p) >= 2]
    for secret in sorted(secrets, key=len, reverse=True):
        msg = msg.replace(str(secret), "***")
    return re.sub(r"://\S*@", "://***@", msg)


@contextmanager
def db():
    # A non-URL value is parsed as key=value text and its errors quote raw fragments
    # (possibly the password), so reject it before connecting.
    if not DATABASE_URL.strip().startswith(("postgresql://", "postgres://")):
        raise ToolError(
            "Database error: DATABASE_URL on the server does not start with postgresql:// . "
            "Paste the full Session pooler connection string as the value (no quotes or spaces)."
        )
    try:
        with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
            yield conn
    except psycopg.Error as e:
        # Surface the real cause to Claude instead of a generic "Error executing tool".
        detail = " ".join(str(e).split())[:500] or "no details"
        hint = ""
        if DATABASE_URL.split("://", 1)[-1].count("@") > 1:
            hint = " Hint: the database password contains '@'; use a password with only letters and numbers."
        elif isinstance(e, psycopg.errors.UndefinedColumn) and "embedding" in str(e):
            hint = " Hint: run schema-semantic.sql in the Supabase SQL Editor to enable search by meaning."
        elif isinstance(e, psycopg.errors.UndefinedTable) and "tasks" in str(e):
            hint = " Hint: run schema-tasks.sql in the Supabase SQL Editor to enable tasks."
        raise ToolError(f"Database error ({type(e).__name__}): {_redact(detail)}{hint}") from e


def _embed(texts: list[str], input_type: str) -> list[list[float]] | None:
    """Embed texts with Voyage AI; None if disabled or the call fails (callers fall back)."""
    if not VOYAGE_API_KEY or not texts:
        return None
    req = urllib.request.Request(
        VOYAGE_URL,
        data=json.dumps({"input": texts, "model": VOYAGE_MODEL, "input_type": input_type}).encode(),
        headers={"Authorization": f"Bearer {VOYAGE_API_KEY}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.load(resp)["data"]
        return [item["embedding"] for item in sorted(data, key=lambda item: item["index"])]
    except Exception as e:
        log.warning("Voyage embedding failed: %s", e)
        return None


def _vec(v: list[float]) -> str:
    return "[" + ",".join(map(str, v)) + "]"


def _backfill(conn, batch: int = 100) -> None:
    """Embed memories saved without an embedding (older ones, or when Voyage was down)."""
    rows = conn.execute(
        "SELECT id, text, tags FROM memories WHERE embedding IS NULL ORDER BY id LIMIT %s", (batch,)
    ).fetchall()
    vectors = _embed([f"{t} {g}".strip() for _, t, g in rows], "document")
    for (id_, _, _), v in zip(rows, vectors or []):
        conn.execute("UPDATE memories SET embedding = %s::vector WHERE id = %s", (_vec(v), id_))


def _fmt(rows) -> str:
    return "\n".join(f"#{i} [{d}] {t}" + (f"  (tags: {g})" if g else "") for i, d, t, g in rows)


@mcp.tool()
def remember(text: str, tags: str = "") -> str:
    """Save a fact, decision, idea or note about the user's life to long-term memory."""
    with db() as conn:
        conn.execute("INSERT INTO memories (text, tags) VALUES (%s, %s)", (text, tags))
        if VOYAGE_API_KEY:
            _backfill(conn)
    return "Saved."


@mcp.tool()
def recall(query: str, limit: int = 5) -> str:
    """Search long-term memory. Use before answering anything about the user's past, plans or preferences."""
    note = ""
    with db() as conn:
        if VOYAGE_API_KEY:
            _backfill(conn)
            q = _embed([query], "query")
            if q:
                v = _vec(q[0])
                rows = conn.execute(
                    """SELECT id, created_at::date AS day, text, tags, 1 - (embedding <=> %s::vector) AS score
                       FROM memories
                       WHERE embedding IS NOT NULL AND vector_dims(embedding) = vector_dims(%s::vector)
                       ORDER BY score DESC, created_at DESC
                       LIMIT %s""",
                    (v, v, limit),
                ).fetchall()
                if rows:
                    return "Closest memories by meaning (best first; ignore weak matches):\n" + "\n".join(
                        f"(match {score:.2f}) " + _fmt([(i, d, t, g)]) for i, d, t, g, score in rows
                    )
            else:
                note = "(Search by meaning is unavailable right now, e.g. rate-limited; keyword results below.)\n"

        rows = conn.execute(
            """WITH q AS (  -- match ANY word, best matches first; 'simple' so words aren't stemmed twice
                 SELECT to_tsquery('simple', replace(plainto_tsquery('english', %s)::text, '&', '|')) AS q)
               SELECT id, created_at::date AS day, text, tags FROM memories, q
               WHERE search @@ q.q
               ORDER BY ts_rank(search, q.q) DESC, created_at DESC
               LIMIT %s""",
            (query, limit),
        ).fetchall()
        if rows:
            return note + _fmt(rows)

        # Keyword search misses paraphrases ("build" vs "built"), so give Claude the
        # latest memories to reason over instead of nothing.
        rows = conn.execute(
            "SELECT id, created_at::date AS day, text, tags FROM memories ORDER BY created_at DESC LIMIT %s",
            (limit,),
        ).fetchall()
    if not rows:
        return "Memory is empty."
    return note + "No keyword match. Most recent memories (newest first):\n" + _fmt(rows)


@mcp.tool()
def recent(days: int = 7) -> str:
    """List everything remembered in the last N days (good for weekly reviews)."""
    with db() as conn:
        rows = conn.execute(
            "SELECT id, created_at::date AS day, text, tags FROM memories WHERE created_at > now() - make_interval(days => %s) ORDER BY created_at",
            (days,),
        ).fetchall()
    return _fmt(rows) or "Nothing in that period."


@mcp.tool()
def update_memory(memory_id: int, text: str, tags: str | None = None) -> str:
    """Correct a saved memory in place (use the #id from recall/recent) instead of saving a separate correction. Tags are kept unless given."""
    with db() as conn:
        # Clearing the embedding makes the next remember/recall re-embed the new text.
        row = conn.execute(
            "UPDATE memories SET text = %s, tags = COALESCE(%s, tags), embedding = NULL WHERE id = %s RETURNING id",
            (text, tags, memory_id),
        ).fetchone()
        if row and VOYAGE_API_KEY:
            _backfill(conn)
    if row is None:
        raise ToolError(f"No memory #{memory_id}. Use recall or recent to find the id.")
    return f"Updated memory #{memory_id}."


@mcp.tool()
def forget(memory_id: int) -> str:
    """Permanently delete a memory that is wrong or no longer wanted (use the #id from recall/recent). Confirm with the user first if unsure."""
    with db() as conn:
        row = conn.execute("DELETE FROM memories WHERE id = %s RETURNING text", (memory_id,)).fetchone()
    if row is None:
        raise ToolError(f"No memory #{memory_id}. Use recall or recent to find the id.")
    return f"Forgot memory #{memory_id}: {row[0]}"


def _today() -> date:
    return datetime.now(TIMEZONE).date()


def _parse_due(due: str) -> date | None:
    due = due.strip()
    if not due:
        return None
    try:
        return date.fromisoformat(due)
    except ValueError:
        raise ToolError(f"Invalid due date {due!r}: use YYYY-MM-DD (today is {_today()}).") from None


def _when(due: date | None, today: date) -> str:
    if due is None:
        return "no due date"
    days = (due - today).days
    if days < 0:
        return f"OVERDUE by {-days} day{'s' if days != -1 else ''}, was due {due}"
    if days == 0:
        return f"due TODAY, {due}"
    if days == 1:
        return f"due tomorrow, {due}"
    return f"due {due:%a %d %b}, in {days} days"


@mcp.tool()
def add_task(title: str, due: str = "", notes: str = "") -> str:
    """Add something the user has to do. `due` is a date as YYYY-MM-DD (convert "next Friday" etc. yourself; leave empty if there is no deadline)."""
    due_date = _parse_due(due)
    with db() as conn:
        (task_id,) = conn.execute(
            "INSERT INTO tasks (title, due, notes) VALUES (%s, %s, %s) RETURNING id", (title, due_date, notes)
        ).fetchone()
    return f"Added task #{task_id}: {title} ({_when(due_date, _today())})."


@mcp.tool()
def list_tasks(include_done: bool = False) -> str:
    """List the user's open tasks, soonest due first, with overdue ones flagged. Check this when the user asks what to do or plans their day/week."""
    with db() as conn:
        rows = conn.execute(
            """SELECT id, title, due, notes, done_at FROM tasks
               WHERE %s OR done_at IS NULL
               ORDER BY done_at IS NOT NULL, due NULLS LAST, id""",
            (include_done,),
        ).fetchall()
    if not rows:
        return "No open tasks."
    today = _today()
    lines = [f"Today is {today:%A %d %B %Y}."]
    for task_id, title, due, notes, done_at in rows:
        status = f"done {done_at.astimezone(TIMEZONE):%d %b}" if done_at else _when(due, today)
        lines.append(f"#{task_id} {title} ({status})" + (f"  notes: {notes}" if notes else ""))
    return "\n".join(lines)


@mcp.tool()
def complete_task(task_id: int) -> str:
    """Mark a task as done. Use the #id from list_tasks."""
    with db() as conn:
        row = conn.execute(
            "UPDATE tasks SET done_at = now() WHERE id = %s AND done_at IS NULL RETURNING title", (task_id,)
        ).fetchone()
    if row is None:
        raise ToolError(f"No open task #{task_id}. Call list_tasks to see the ids.")
    return f"Done: #{task_id} {row[0]}."


class BearerAuth:
    """Rejects any HTTP request that doesn't carry the secret token."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            if scope["path"] == "/health":
                # Public, data-free endpoint for uptime pings that keep the free instance awake.
                return await PlainTextResponse("ok")(scope, receive, send)
            auth = dict(scope["headers"]).get(b"authorization", b"").decode()
            if auth != f"Bearer {TOKEN}":
                return await PlainTextResponse("Unauthorized", status_code=401)(scope, receive, send)
        await self.app(scope, receive, send)


app = BearerAuth(mcp.streamable_http_app(stateless_http=True, host="0.0.0.0"))
