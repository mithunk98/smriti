import json
import logging
import os
import re
import urllib.request
from contextlib import contextmanager

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

log = logging.getLogger("smriti")

mcp = MCPServer("smriti", instructions="Personal memory for the user. Save important facts, decisions and plans with remember; look things up with recall before answering personal questions.")


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
    return "\n".join(f"[{d}] {t}" + (f"  (tags: {g})" if g else "") for d, t, g in rows)


@mcp.tool()
def remember(text: str, tags: str = "") -> str:
    """Save a fact, decision, idea or note about the user's life to long-term memory."""
    with db() as conn:
        conn.execute("INSERT INTO memories (text, tags) VALUES (%s, %s)", (text, tags))
        if VOYAGE_API_KEY:
            _backfill(conn)
    return "Saved."


@mcp.tool()
def recall(query: str, limit: int = 10) -> str:
    """Search long-term memory. Use before answering anything about the user's past, plans or preferences."""
    with db() as conn:
        if VOYAGE_API_KEY:
            _backfill(conn)
            q = _embed([query], "query")
            if q:
                v = _vec(q[0])
                rows = conn.execute(
                    """SELECT created_at::date, text, tags FROM memories
                       WHERE embedding IS NOT NULL AND vector_dims(embedding) = vector_dims(%s::vector)
                       ORDER BY embedding <=> %s::vector, created_at DESC
                       LIMIT %s""",
                    (v, v, limit),
                ).fetchall()
                if rows:
                    return "Closest memories by meaning (best first):\n" + _fmt(rows)

        rows = conn.execute(
            """WITH q AS (  -- match ANY word, best matches first
                 SELECT to_tsquery('english', replace(plainto_tsquery('english', %s)::text, '&', '|')) AS q)
               SELECT created_at::date, text, tags FROM memories, q
               WHERE search @@ q.q
               ORDER BY ts_rank(search, q.q) DESC, created_at DESC
               LIMIT %s""",
            (query, limit),
        ).fetchall()
        if rows:
            return _fmt(rows)

        # Keyword search misses paraphrases ("build" vs "built"), so give Claude the
        # latest memories to reason over instead of nothing.
        rows = conn.execute(
            "SELECT created_at::date, text, tags FROM memories ORDER BY created_at DESC LIMIT %s",
            (limit,),
        ).fetchall()
    if not rows:
        return "Memory is empty."
    return "No keyword match. Most recent memories:\n" + _fmt(rows)


@mcp.tool()
def recent(days: int = 7) -> str:
    """List everything remembered in the last N days (good for weekly reviews)."""
    with db() as conn:
        rows = conn.execute(
            "SELECT created_at::date, text FROM memories WHERE created_at > now() - make_interval(days => %s) ORDER BY created_at",
            (days,),
        ).fetchall()
    return "\n".join(f"[{d}] {t}" for d, t in rows) or "Nothing in that period."


class BearerAuth:
    """Rejects any HTTP request that doesn't carry the secret token."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            auth = dict(scope["headers"]).get(b"authorization", b"").decode()
            if auth != f"Bearer {TOKEN}":
                return await PlainTextResponse("Unauthorized", status_code=401)(scope, receive, send)
        await self.app(scope, receive, send)


app = BearerAuth(mcp.streamable_http_app(stateless_http=True, host="0.0.0.0"))
