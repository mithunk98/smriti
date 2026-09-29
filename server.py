import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import date, datetime, timedelta
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
TIMEZONE_NAME = os.environ.get("SMRITI_TIMEZONE", "Asia/Kolkata")
TIMEZONE = ZoneInfo(TIMEZONE_NAME)
# Optional: daily morning brief sent to Telegram at BRIEF_TIME (local, HH:MM).
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_URL = "https://api.telegram.org"
BRIEF_TIME = datetime.strptime(os.environ.get("BRIEF_TIME", "07:30"), "%H:%M").time()
# Optional: plain English and voice notes in Telegram, via Groq (free tier).
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_URL = "https://api.groq.com/openai/v1"
GROQ_CHAT_MODEL = os.environ.get("GROQ_CHAT_MODEL", "llama-3.3-70b-versatile")
GROQ_WHISPER_MODEL = os.environ.get("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo")

log = logging.getLogger("smriti")

mcp = MCPServer(
    "smriti",
    instructions=(
        "Personal memory and task list for the user. Save important facts, decisions and plans with remember; "
        "look things up with recall before answering personal questions. Fix wrong memories with update_memory "
        "or forget rather than saving corrections. Track things the user has to do "
        "with add_task / list_tasks / complete_task. morning_brief summarizes the day."
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
        elif isinstance(e, psycopg.errors.UndefinedTable) and "briefs" in str(e):
            hint = " Hint: run schema-brief.sql in the Supabase SQL Editor to enable the morning brief."
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


def _build_brief(conn, today: date) -> str:
    yesterday = today - timedelta(days=1)
    tasks = conn.execute(
        "SELECT title, due FROM tasks WHERE done_at IS NULL ORDER BY due NULLS LAST, id"
    ).fetchall()
    finished = conn.execute(
        "SELECT title FROM tasks WHERE (done_at AT TIME ZONE %s)::date = %s ORDER BY done_at",
        (TIMEZONE_NAME, yesterday),
    ).fetchall()
    saved = conn.execute(
        "SELECT text FROM memories WHERE (created_at AT TIME ZONE %s)::date = %s ORDER BY created_at",
        (TIMEZONE_NAME, yesterday),
    ).fetchall()

    overdue = [(t, d) for t, d in tasks if d and d < today]
    due_today = [t for t, d in tasks if d == today]
    week = [(t, d) for t, d in tasks if d and today < d <= today + timedelta(days=7)]
    later = [(t, d) for t, d in tasks if d is None or d > today + timedelta(days=7)]

    parts = [f"☀️ Good morning! {today:%A %d %B}"]
    if overdue:
        parts.append("⚠️ Overdue\n" + "\n".join(f"• {t} (was due {d:%d %b})" for t, d in overdue))
    if due_today:
        parts.append("📌 Today\n" + "\n".join(f"• {t}" for t in due_today))
    if week:
        parts.append("📅 Next 7 days\n" + "\n".join(
            f"• {d:%a %d %b}: {t} (in {(d - today).days} day{'s' if (d - today).days != 1 else ''})" for t, d in week
        ))
    if later:
        dated = [(t, d) for t, d in later if d]
        nxt = f" (next: {dated[0][0]}, {dated[0][1]:%a %d %b})" if dated else ""
        parts.append(f"🗓 Later: {len(later)} task{'s' if len(later) != 1 else ''}{nxt}")
    if not (overdue or due_today or week):
        parts.append("Nothing due this week. Enjoy the day!")
    if finished:
        parts.append("✅ Finished yesterday\n" + "\n".join(f"• {t}" for (t,) in finished))
    if saved:
        parts.append("🧠 Saved yesterday\n" + "\n".join(f"• {t}" for (t,) in saved))
    return "\n\n".join(parts)


def _telegram(method: str, payload: dict, timeout: float = 20):
    """Call the Telegram Bot API and return its result; errors never contain the bot token."""
    req = urllib.request.Request(
        f"{TELEGRAM_URL}/bot{TELEGRAM_BOT_TOKEN}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.load(resp)
        if not body.get("ok"):
            raise RuntimeError(f"Telegram did not accept the request: {body.get('description', '')}")
        return body.get("result")
    except Exception as e:
        detail = str(e)
        if isinstance(e, urllib.error.HTTPError):
            # Telegram explains rejections in the body, e.g. "Bad Request: chat not found".
            try:
                detail += f" ({json.load(e).get('description', '')})"
            except Exception:
                pass
        # Never let the bot token (part of the URL) reach logs or Claude.
        raise RuntimeError(f"Telegram {method} failed: {detail.replace(TELEGRAM_BOT_TOKEN, '***')}") from None


def _send_telegram(text: str) -> None:
    _telegram("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000]})


@mcp.tool()
def morning_brief(send_to_telegram: bool = False) -> str:
    """Today's brief: overdue, today's and upcoming tasks, plus what was finished and saved yesterday. Use when the user asks for a brief or to plan the day. send_to_telegram=True also sends it to their phone (to test delivery)."""
    with db() as conn:
        brief = _build_brief(conn, _today())
    if send_to_telegram:
        if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
            raise ToolError("Telegram is not set up: add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID on the server.")
        try:
            _send_telegram(brief)
        except RuntimeError as e:
            raise ToolError(str(e)) from None
        return "Sent to Telegram:\n\n" + brief
    return brief


def _send_daily_brief_if_due(now: datetime) -> bool:
    """Send today's brief once, at or after BRIEF_TIME. The briefs table makes it once per day across restarts."""
    if now.time() < BRIEF_TIME:
        return False
    with db() as conn:
        if conn.execute(
            "INSERT INTO briefs (day) VALUES (%s) ON CONFLICT DO NOTHING RETURNING day", (now.date(),)
        ).fetchone() is None:
            return False  # already sent today
        try:
            _send_telegram(_build_brief(conn, now.date()))
        except Exception:
            conn.execute("DELETE FROM briefs WHERE day = %s", (now.date(),))  # retry later
            raise
    return True


def _brief_scheduler() -> None:
    while True:
        try:
            if _send_daily_brief_if_due(datetime.now(TIMEZONE)):
                log.info("Morning brief sent")
        except Exception as e:
            log.warning("Morning brief failed, retrying in 15 minutes: %s", e)
            time.sleep(14 * 60)
        time.sleep(60)


WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

BOT_HELP = """Hi! I'm Smriti. Commands:
/task Submit lab record sunday: add a task (due: today, tomorrow, a weekday or YYYY-MM-DD)
/tasks: open tasks
/done 4: mark task #4 done
/remember Prof said chapters 1-6: save a memory
/recall exam syllabus: search memories
/recent 7: memories from the last N days
/forget 9: delete memory #9
/brief: today's brief"""
BOT_TIP = "\n\nTip: tap a command in the / menu and I'll ask for the details."


def _parse_day(word: str, today: date) -> date | None:
    """today / tomorrow / weekday (next one, never today) / YYYY-MM-DD, else None."""
    w = word.lower().strip(".,")
    if w == "today":
        return today
    if w in ("tomorrow", "tmrw"):
        return today + timedelta(days=1)
    for i, name in enumerate(WEEKDAYS):
        if w in (name, name[:3]):
            return today + timedelta(days=(i - today.weekday()) % 7 or 7)
    try:
        return date.fromisoformat(w)
    except ValueError:
        return None


# Tapping a command in Telegram's "/" menu sends it at once with no text, so the bot
# asks for the rest and treats the next plain message as the command's argument.
ASK_FOR = {
    "/task": "What's the task? Add a due day at the end, e.g. Submit lab record sunday",
    "/remember": "What should I remember?",
    "/recall": "What should I search for?",
    "/done": "Which task number is done?",
    "/forget": "Which memory number should I delete? (/recent shows the numbers)",
}
_pending_cmd: str | None = None


def _handle_bot_message(text: str) -> str:
    global _pending_cmd
    text = text.strip()
    if _pending_cmd and not text.startswith("/"):
        text = f"{_pending_cmd} {text}"
    _pending_cmd = None
    if not text.startswith("/"):
        if GROQ_API_KEY:
            return _ask_ai(text)
        return "I only understand commands (add GROQ_API_KEY on the server for plain English).\n\n" + BOT_HELP
    cmd, _, arg = text.partition(" ")
    cmd = cmd.lower().split("@")[0]  # "/tasks@my_bot" form
    arg = arg.strip()
    try:
        if cmd in ("/start", "/help"):
            extra = "\nOr just type or send a voice note in plain English, e.g. \"remind me to call mom on Sunday\"." if GROQ_API_KEY else ""
            return BOT_HELP + BOT_TIP + extra
        if cmd in ASK_FOR and not arg:
            _pending_cmd = cmd
            return ASK_FOR[cmd] + ("\n\n" + list_tasks() if cmd == "/done" else "")
        if cmd == "/task":
            words = arg.split()
            due = _parse_day(words[-1], _today()) if len(words) > 1 else None
            if due:
                words = words[:-1]
                if len(words) > 1 and words[-1].lower() in ("by", "on", "due"):
                    words = words[:-1]
            return add_task(" ".join(words), due.isoformat() if due else "")
        if cmd == "/tasks":
            return list_tasks()
        if cmd in ("/done", "/forget"):
            if not arg.lstrip("#").isdigit():
                return f"Usage: {cmd} 4 (the #id from {'/tasks' if cmd == '/done' else '/recall'})"
            n = int(arg.lstrip("#"))
            return complete_task(n) if cmd == "/done" else forget(n)
        if cmd == "/remember":
            return remember(arg)
        if cmd == "/recall":
            return recall(arg)
        if cmd == "/recent":
            return recent(int(arg) if arg.isdigit() else 7)
        if cmd == "/brief":
            return morning_brief()
        return "I don't know that command.\n\n" + BOT_HELP
    except ToolError as e:
        return f"⚠️ {e}"
    except Exception:
        log.exception("Telegram command failed")
        return "⚠️ Something went wrong on the server."


def _post(url: str, data: bytes, headers: dict, timeout: float = 60) -> dict:
    """POST to Groq; errors carry Groq's message but never the API key."""
    req = urllib.request.Request(url, data=data, headers={"Authorization": f"Bearer {GROQ_API_KEY}", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            detail = json.load(e).get("error", {}).get("message", "")
        except Exception:
            detail = ""
        raise RuntimeError(f"Groq error {e.code}: {detail}".replace(GROQ_API_KEY, "***")) from None
    except Exception as e:
        raise RuntimeError(f"Groq unreachable: {e}".replace(GROQ_API_KEY, "***")) from None


def _transcribe(audio: bytes, filename: str = "voice.ogg") -> str:
    boundary = "smriti" + os.urandom(8).hex()
    fields = {"model": GROQ_WHISPER_MODEL, "response_format": "json"}
    body = b"".join(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields.items()
    ) + (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + audio + f"\r\n--{boundary}--\r\n".encode()
    result = _post(f"{GROQ_URL}/audio/transcriptions", body, {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    return result.get("text", "").strip()


def _tg_download(file_id: str) -> bytes:
    path = _telegram("getFile", {"file_id": file_id})["file_path"]
    try:
        with urllib.request.urlopen(f"{TELEGRAM_URL}/file/bot{TELEGRAM_BOT_TOKEN}/{path}", timeout=60) as resp:
            return resp.read()
    except Exception as e:
        raise RuntimeError(f"Could not download the voice note: {str(e).replace(TELEGRAM_BOT_TOKEN, '***')}") from None


def _fn(name: str, description: str, **params) -> dict:
    required = [k for k, (_, _, req) in params.items() if req]
    props = {k: {"type": t, "description": d} for k, (t, d, _) in params.items()}
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props, "required": required},
    }}


# What the AI may do from Telegram. Deleting/editing memories stays with /forget,
# so a misheard voice note can never destroy data.
AI_TOOLS = [
    _fn("add_task", "Add something the user has to do.",
        title=("string", "Short task title", True),
        due=("string", "Due date YYYY-MM-DD, or empty if none", False),
        notes=("string", "Optional extra details", False)),
    _fn("list_tasks", "List open tasks with their #ids and due dates."),
    _fn("complete_task", "Mark a task done. Get its #id from list_tasks first.",
        task_id=("integer", "The task #id", True)),
    _fn("remember", "Save a fact, decision, idea or note about the user's life.",
        text=("string", "What to remember", True),
        tags=("string", "Short comma-separated tags", False)),
    _fn("recall", "Search saved memories by meaning.", query=("string", "What to look for", True)),
    _fn("recent", "List memories saved in the last N days.", days=("integer", "Number of days", False)),
    _fn("morning_brief", "Today's overview: overdue, today's and upcoming tasks."),
]
AI_FUNCS = {
    "add_task": add_task, "list_tasks": list_tasks, "complete_task": complete_task,
    "remember": remember, "recall": recall, "recent": recent,
    "morning_brief": lambda: morning_brief(),
}
_ai_history: list[dict] = []  # last few exchanges, so "mark it done" can follow "what's due?"


def _run_ai_tool(name: str, raw_args: str) -> str:
    try:
        args = json.loads(raw_args or "{}") or {}
        return str(AI_FUNCS[name](**args))
    except KeyError:
        return f"Unknown tool {name}"
    except ToolError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error: bad arguments for {name}: {e}"


def _ask_ai(text: str) -> str:
    today = _today()
    system = (
        "You are Smriti, the user's personal assistant, chatting in Telegram. "
        f"Today is {today:%A %Y-%m-%d} ({TIMEZONE_NAME}). Use the tools to act on requests: convert dates like "
        "'next Friday' to YYYY-MM-DD, call list_tasks to find a task's #id before completing it, and save "
        "things worth keeping with remember. Never invent tasks or memories. Reply briefly in plain text "
        "without markdown."
    )
    messages = [{"role": "system", "content": system}, *_ai_history, {"role": "user", "content": text}]
    try:
        _telegram("sendChatAction", {"chat_id": TELEGRAM_CHAT_ID, "action": "typing"})
    except Exception:
        pass
    try:
        for _ in range(6):
            reply = _post(f"{GROQ_URL}/chat/completions", json.dumps({
                "model": GROQ_CHAT_MODEL, "messages": messages, "tools": AI_TOOLS, "temperature": 0.2,
            }).encode(), {"Content-Type": "application/json"})["choices"][0]["message"]
            calls = reply.get("tool_calls") or []
            if not calls:
                answer = (reply.get("content") or "").strip() or "Done."
                _ai_history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": answer}])
                del _ai_history[:-6]
                return answer
            messages.append({"role": "assistant", "content": reply.get("content") or "", "tool_calls": calls})
            for call in calls:
                result = _run_ai_tool(call["function"]["name"], call["function"].get("arguments", ""))
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
        return "⚠️ That took too many steps. Try rephrasing, or use a command (/help)."
    except RuntimeError as e:
        return f"⚠️ {e}\nCommands still work: /help"


def _handle_voice(file_id: str) -> str:
    if not GROQ_API_KEY:
        return "Voice notes need GROQ_API_KEY on the server."
    try:
        text = _transcribe(_tg_download(file_id))
    except RuntimeError as e:
        return f"⚠️ {e}"
    if not text:
        return "🎙️ I couldn't hear anything in that voice note."
    return f"🎙️ \u201c{text}\u201d\n\n" + _handle_bot_message(text)


def _telegram_bot() -> None:
    """Long-poll Telegram and answer commands, but only from the owner's chat."""
    try:  # the "/" command menu in the Telegram app
        _telegram("setMyCommands", {"commands": [
            {"command": c.split(" ")[0].lstrip("/"), "description": d.strip()}
            for c, d in (line.split(": ", 1) for line in BOT_HELP.splitlines()[1:])
        ]})
    except Exception as e:
        log.warning("Could not set the Telegram command menu: %s", e)
    offset = None
    while True:
        try:
            payload = {"timeout": 50, "allowed_updates": ["message"]}
            if offset is not None:
                payload["offset"] = offset
            for update in _telegram("getUpdates", payload, timeout=65):
                offset = update["update_id"] + 1
                msg = update.get("message") or {}
                if str(msg.get("chat", {}).get("id")) != TELEGRAM_CHAT_ID:
                    continue  # a stranger found the bot: ignore
                voice = msg.get("voice") or msg.get("audio")
                if voice:
                    _send_telegram(_handle_voice(voice["file_id"]))
                elif msg.get("text"):
                    _send_telegram(_handle_bot_message(msg["text"]))
        except Exception as e:
            log.warning("Telegram bot polling failed, retrying in 30 seconds: %s", e)
            time.sleep(30)


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

if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID and os.environ.get("SMRITI_SCHEDULER", "1") == "1":
    threading.Thread(target=_brief_scheduler, name="morning-brief", daemon=True).start()
    threading.Thread(target=_telegram_bot, name="telegram-bot", daemon=True).start()
