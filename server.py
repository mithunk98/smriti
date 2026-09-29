import os
import re
from contextlib import contextmanager

import psycopg
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.responses import PlainTextResponse

DATABASE_URL = os.environ["DATABASE_URL"]
TOKEN = os.environ["SMRITI_TOKEN"]

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
    try:
        with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
            yield conn
    except psycopg.Error as e:
        # Surface the real cause to Claude instead of a generic "Error executing tool".
        detail = " ".join(str(e).split())[:500] or "no details"
        hint = ""
        if DATABASE_URL.split("://", 1)[-1].count("@") > 1:
            hint = " Hint: the database password contains '@'; use a password with only letters and numbers."
        raise ToolError(f"Database error ({type(e).__name__}): {_redact(detail)}{hint}") from e


@mcp.tool()
def remember(text: str, tags: str = "") -> str:
    """Save a fact, decision, idea or note about the user's life to long-term memory."""
    with db() as conn:
        conn.execute("INSERT INTO memories (text, tags) VALUES (%s, %s)", (text, tags))
    return "Saved."


@mcp.tool()
def recall(query: str, limit: int = 10) -> str:
    """Search long-term memory. Use before answering anything about the user's past, plans or preferences."""
    with db() as conn:
        rows = conn.execute(
            """WITH q AS (  -- match ANY word, best matches first
                 SELECT to_tsquery('english', replace(plainto_tsquery('english', %s)::text, '&', '|')) AS q)
               SELECT created_at::date, text, tags FROM memories, q
               WHERE search @@ q.q
               ORDER BY ts_rank(search, q.q) DESC, created_at DESC
               LIMIT %s""",
            (query, limit),
        ).fetchall()
    if not rows:
        return "No matching memories."
    return "\n".join(f"[{d}] {t}" + (f"  (tags: {g})" if g else "") for d, t, g in rows)


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
