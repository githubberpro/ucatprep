"""
Data layer for the UCAT Prep app.

Dual backend, mirroring the rest of this repo: when DATABASE_URL is set (Neon /
any cloud PostgreSQL) and psycopg2 is available it talks to Postgres; otherwise
it falls back to a local SQLite file so the app runs with zero configuration.
"""

import os
import re
import json
from datetime import datetime, date, timedelta
from pathlib import Path

# ── Backend detection (lazy so env var can be injected before first use) ───────
try:
    import psycopg2
    import psycopg2.extras
    _HAS_PG = True
except ImportError:
    _HAS_PG = False

import sqlite3
DB_PATH = Path(__file__).parent / "ucat.db"

_USE_PG: bool | None = None
_DB_URL: str = ""
_BOOTSTRAPPED: bool = False
_CONN = None  # cached Postgres connection, reused for the life of the process


def _setup() -> bool:
    global _USE_PG, _DB_URL
    if _USE_PG is None:
        _DB_URL = os.environ.get("DATABASE_URL", "")
        _USE_PG = bool(_DB_URL and _HAS_PG)
    return _USE_PG


def _ph() -> str:
    return "%s" if _setup() else "?"


def _connect_pg():
    # Neon (and most cloud PostgreSQL) requires SSL — add sslmode=require if absent
    url = _DB_URL
    if "sslmode" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
    # Single long-lived connection: autocommit avoids a failed statement leaving
    # the connection in an aborted-transaction state that poisons later queries.
    conn.autocommit = True
    return conn


def get_conn():
    """Return a database connection.

    For Postgres/Neon the connection is cached and reused for the life of the
    process — opening a fresh SSL connection on every query was the main source
    of per-interaction latency. A closed/dropped connection is reopened on the
    next call. SQLite is a local file and cheap to open, so it stays per-call.
    """
    global _CONN
    if _setup():
        if _CONN is None or getattr(_CONN, "closed", 1):
            _CONN = _connect_pg()
        return _CONN
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _drop_conn():
    """Discard the cached Postgres connection so the next call reconnects."""
    global _CONN
    if _CONN is not None:
        try:
            _CONN.close()
        except Exception:
            pass
        _CONN = None


def _pg_call(run):
    """Run ``run(conn)`` on the cached Postgres connection, reconnecting and
    retrying once if it has been dropped (e.g. a Neon idle timeout)."""
    try:
        return run(get_conn())
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        _drop_conn()
        return run(get_conn())


def _n(sql: str) -> str:
    """Convert :name placeholders → %(name)s for psycopg2."""
    if _setup():
        return re.sub(r":(\w+)", r"%(\1)s", sql)
    return sql


def _q(conn, sql: str, params=()):
    """Execute and return all rows as dicts."""
    if _setup():
        def run(c):
            with c.cursor() as cur:
                cur.execute(sql, params or None)
                return [dict(r) for r in cur.fetchall()]
        return _pg_call(run)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _q1(conn, sql: str, params=()):
    """Execute and return first row as dict, or None."""
    if _setup():
        def run(c):
            with c.cursor() as cur:
                cur.execute(sql, params or None)
                row = cur.fetchone()
                return dict(row) if row else None
        return _pg_call(run)
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def _run(conn, sql: str, params=()):
    """Execute DML; returns lastrowid for INSERT statements."""
    if _setup():
        is_insert = sql.strip().upper().startswith("INSERT") and "RETURNING" not in sql.upper()
        exec_sql = (sql.rstrip(";") + " RETURNING id") if is_insert else sql
        def run(c):
            with c.cursor() as cur:
                cur.execute(exec_sql, params or None)
                if is_insert:
                    row = cur.fetchone()
                    return row["id"] if row else None
            return None
        return _pg_call(run)
    cur = conn.execute(sql, params)
    return cur.lastrowid


def _commit(conn):
    # Postgres runs in autocommit mode (see _connect_pg); commit() is a harmless
    # no-op there. SQLite still needs an explicit commit.
    if not _setup():
        conn.commit()


def _close(conn):
    # The Postgres connection is cached and reused, so closing it here would
    # defeat the purpose. SQLite connections are per-call and must be closed.
    if not _setup():
        conn.close()


# ── Schema ─────────────────────────────────────────────────────────────────────

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS subjects (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    code      TEXT NOT NULL UNIQUE,
    name      TEXT NOT NULL,
    color     TEXT DEFAULT '#1f77b4',
    sort_order INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS topics (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    high_yield INTEGER DEFAULT 0,
    summary    TEXT,
    content    TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS questions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id  INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    topic_id    INTEGER REFERENCES topics(id) ON DELETE SET NULL,
    stem        TEXT NOT NULL,
    option_a    TEXT NOT NULL,
    option_b    TEXT NOT NULL,
    option_c    TEXT NOT NULL,
    option_d    TEXT NOT NULL,
    correct     TEXT NOT NULL CHECK(correct IN ('A','B','C','D')),
    explanation TEXT,
    difficulty  TEXT DEFAULT 'Medium' CHECK(difficulty IN ('Easy','Medium','Hard')),
    created_at  TEXT
);
CREATE TABLE IF NOT EXISTS attempts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    subject_id  INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    chosen      TEXT NOT NULL,
    is_correct  INTEGER NOT NULL,
    seconds     REAL DEFAULT 0,
    created_at  TEXT
);
CREATE TABLE IF NOT EXISTS flashcards (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id    INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    topic_id      INTEGER REFERENCES topics(id) ON DELETE SET NULL,
    front         TEXT NOT NULL,
    back          TEXT NOT NULL,
    ease          REAL DEFAULT 2.5,
    interval_days INTEGER DEFAULT 0,
    reps          INTEGER DEFAULT 0,
    due_date      TEXT,
    last_reviewed TEXT,
    created_at    TEXT
);
CREATE TABLE IF NOT EXISTS study_tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT NOT NULL,
    subject_id   INTEGER REFERENCES subjects(id) ON DELETE SET NULL,
    task_type    TEXT DEFAULT 'Review',
    due_date     TEXT,
    duration_min INTEGER DEFAULT 60,
    status       TEXT DEFAULT 'Todo' CHECK(status IN ('Todo','In Progress','Done')),
    notes        TEXT,
    created_at   TEXT
);
CREATE TABLE IF NOT EXISTS chat_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    role       TEXT NOT NULL CHECK(role IN ('user','assistant')),
    content    TEXT NOT NULL,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS app_context (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT
);
"""

_PG_TABLES = [
    """CREATE TABLE IF NOT EXISTS subjects (
        id         SERIAL PRIMARY KEY,
        code       TEXT NOT NULL UNIQUE,
        name       TEXT NOT NULL,
        color      TEXT DEFAULT '#1f77b4',
        sort_order INTEGER DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS topics (
        id         SERIAL PRIMARY KEY,
        subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
        name       TEXT NOT NULL,
        high_yield INTEGER DEFAULT 0,
        summary    TEXT,
        content    TEXT,
        created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS questions (
        id          SERIAL PRIMARY KEY,
        subject_id  INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
        topic_id    INTEGER REFERENCES topics(id) ON DELETE SET NULL,
        stem        TEXT NOT NULL,
        option_a    TEXT NOT NULL,
        option_b    TEXT NOT NULL,
        option_c    TEXT NOT NULL,
        option_d    TEXT NOT NULL,
        correct     TEXT NOT NULL CHECK(correct IN ('A','B','C','D')),
        explanation TEXT,
        difficulty  TEXT DEFAULT 'Medium' CHECK(difficulty IN ('Easy','Medium','Hard')),
        created_at  TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS attempts (
        id          SERIAL PRIMARY KEY,
        question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
        subject_id  INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
        chosen      TEXT NOT NULL,
        is_correct  INTEGER NOT NULL,
        seconds     DOUBLE PRECISION DEFAULT 0,
        created_at  TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS flashcards (
        id            SERIAL PRIMARY KEY,
        subject_id    INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
        topic_id      INTEGER REFERENCES topics(id) ON DELETE SET NULL,
        front         TEXT NOT NULL,
        back          TEXT NOT NULL,
        ease          DOUBLE PRECISION DEFAULT 2.5,
        interval_days INTEGER DEFAULT 0,
        reps          INTEGER DEFAULT 0,
        due_date      TEXT,
        last_reviewed TEXT,
        created_at    TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS study_tasks (
        id           SERIAL PRIMARY KEY,
        title        TEXT NOT NULL,
        subject_id   INTEGER REFERENCES subjects(id) ON DELETE SET NULL,
        task_type    TEXT DEFAULT 'Review',
        due_date     TEXT,
        duration_min INTEGER DEFAULT 60,
        status       TEXT DEFAULT 'Todo' CHECK(status IN ('Todo','In Progress','Done')),
        notes        TEXT,
        created_at   TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS chat_history (
        id         SERIAL PRIMARY KEY,
        role       TEXT NOT NULL CHECK(role IN ('user','assistant')),
        content    TEXT NOT NULL,
        created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS app_context (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL,
        updated_at TEXT
    )""",
]


def init_db():
    """Create tables, seed starter content on first run, and backfill any
    newer seed content into an already-populated database. The work runs once
    per process — Streamlit reruns this script on every interaction, so the
    guard keeps each rerun cheap."""
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return
    conn = get_conn()
    try:
        if _setup():
            with conn.cursor() as cur:
                for stmt in _PG_TABLES:
                    cur.execute(stmt)
        else:
            conn.executescript(_SQLITE_SCHEMA)
        _commit(conn)
    finally:
        _close(conn)
    seed_content()
    backfill_content()
    _BOOTSTRAPPED = True


# ── Subjects ───────────────────────────────────────────────────────────────────

def get_subjects():
    conn = get_conn()
    try:
        return _q(conn, "SELECT * FROM subjects ORDER BY sort_order, name")
    finally:
        _close(conn)


def get_subject_map():
    """Return {id: row} and {code: row} for quick lookups."""
    subs = get_subjects()
    return {s["id"]: s for s in subs}, {s["code"]: s for s in subs}


# ── Topics ─────────────────────────────────────────────────────────────────────

def get_topics(subject_id=None, high_yield_only=False):
    ph = _ph()
    sql = "SELECT t.*, s.name AS subject_name, s.color FROM topics t JOIN subjects s ON t.subject_id = s.id WHERE 1=1"
    params: list = []
    if subject_id:
        sql += f" AND t.subject_id = {ph}"
        params.append(subject_id)
    if high_yield_only:
        sql += " AND t.high_yield = 1"
    sql += " ORDER BY s.sort_order, t.name"
    conn = get_conn()
    try:
        return _q(conn, sql, tuple(params))
    finally:
        _close(conn)


def get_topic(topic_id):
    ph = _ph()
    conn = get_conn()
    try:
        return _q1(conn, f"SELECT * FROM topics WHERE id = {ph}", (topic_id,))
    finally:
        _close(conn)


def upsert_topic(data: dict):
    now = datetime.now().isoformat()
    data = dict(data)
    data.setdefault("high_yield", 0)
    data.setdefault("summary", "")
    data.setdefault("content", "")
    conn = get_conn()
    try:
        if data.get("id"):
            _run(conn, _n("""
                UPDATE topics SET subject_id=:subject_id, name=:name, high_yield=:high_yield,
                    summary=:summary, content=:content WHERE id=:id
            """), data)
        else:
            data["created_at"] = now
            data["id"] = _run(conn, _n("""
                INSERT INTO topics (subject_id, name, high_yield, summary, content, created_at)
                VALUES (:subject_id, :name, :high_yield, :summary, :content, :created_at)
            """), data)
        _commit(conn)
        return data["id"]
    finally:
        _close(conn)


def delete_topic(topic_id):
    ph = _ph()
    conn = get_conn()
    try:
        _run(conn, f"DELETE FROM topics WHERE id = {ph}", (topic_id,))
        _commit(conn)
    finally:
        _close(conn)


# ── Questions ──────────────────────────────────────────────────────────────────

def get_questions(subject_id=None, topic_id=None, difficulty=None, limit=None):
    ph = _ph()
    sql = """SELECT q.*, s.name AS subject_name, s.color, t.name AS topic_name
             FROM questions q JOIN subjects s ON q.subject_id = s.id
             LEFT JOIN topics t ON q.topic_id = t.id WHERE 1=1"""
    params: list = []
    if subject_id:
        sql += f" AND q.subject_id = {ph}"
        params.append(subject_id)
    if topic_id:
        sql += f" AND q.topic_id = {ph}"
        params.append(topic_id)
    if difficulty and difficulty != "All":
        sql += f" AND q.difficulty = {ph}"
        params.append(difficulty)
    sql += " ORDER BY q.id"
    if limit:
        sql += f" LIMIT {ph}"
        params.append(limit)
    conn = get_conn()
    try:
        return _q(conn, sql, tuple(params))
    finally:
        _close(conn)


def upsert_question(data: dict):
    now = datetime.now().isoformat()
    data = dict(data)
    data.setdefault("topic_id", None)
    data.setdefault("explanation", "")
    data.setdefault("difficulty", "Medium")
    conn = get_conn()
    try:
        if data.get("id"):
            _run(conn, _n("""
                UPDATE questions SET subject_id=:subject_id, topic_id=:topic_id, stem=:stem,
                    option_a=:option_a, option_b=:option_b, option_c=:option_c, option_d=:option_d,
                    correct=:correct, explanation=:explanation, difficulty=:difficulty WHERE id=:id
            """), data)
        else:
            data["created_at"] = now
            data["id"] = _run(conn, _n("""
                INSERT INTO questions (subject_id, topic_id, stem, option_a, option_b, option_c,
                    option_d, correct, explanation, difficulty, created_at)
                VALUES (:subject_id, :topic_id, :stem, :option_a, :option_b, :option_c,
                    :option_d, :correct, :explanation, :difficulty, :created_at)
            """), data)
        _commit(conn)
        return data["id"]
    finally:
        _close(conn)


def delete_question(qid):
    ph = _ph()
    conn = get_conn()
    try:
        _run(conn, f"DELETE FROM questions WHERE id = {ph}", (qid,))
        _commit(conn)
    finally:
        _close(conn)


def record_attempt(question_id, subject_id, chosen, is_correct, seconds=0):
    conn = get_conn()
    try:
        _run(conn, _n("""
            INSERT INTO attempts (question_id, subject_id, chosen, is_correct, seconds, created_at)
            VALUES (:question_id, :subject_id, :chosen, :is_correct, :seconds, :created_at)
        """), {"question_id": question_id, "subject_id": subject_id, "chosen": chosen,
               "is_correct": 1 if is_correct else 0, "seconds": seconds,
               "created_at": datetime.now().isoformat()})
        _commit(conn)
    finally:
        _close(conn)


# ── Flashcards (SM-2 lite spaced repetition) ───────────────────────────────────

def get_flashcards(subject_id=None, due_only=False):
    ph = _ph()
    sql = """SELECT f.*, s.name AS subject_name, s.color, t.name AS topic_name
             FROM flashcards f JOIN subjects s ON f.subject_id = s.id
             LEFT JOIN topics t ON f.topic_id = t.id WHERE 1=1"""
    params: list = []
    if subject_id:
        sql += f" AND f.subject_id = {ph}"
        params.append(subject_id)
    if due_only:
        today = date.today().isoformat()
        sql += f" AND (f.due_date IS NULL OR f.due_date <= {ph})"
        params.append(today)
    sql += " ORDER BY f.due_date NULLS FIRST, f.id" if _setup() else " ORDER BY f.due_date IS NOT NULL, f.due_date, f.id"
    conn = get_conn()
    try:
        return _q(conn, sql, tuple(params))
    finally:
        _close(conn)


def upsert_flashcard(data: dict):
    now = datetime.now().isoformat()
    data = dict(data)
    data.setdefault("topic_id", None)
    conn = get_conn()
    try:
        if data.get("id"):
            _run(conn, _n("""
                UPDATE flashcards SET subject_id=:subject_id, topic_id=:topic_id,
                    front=:front, back=:back WHERE id=:id
            """), data)
        else:
            data["created_at"] = now
            data["due_date"] = date.today().isoformat()
            data["id"] = _run(conn, _n("""
                INSERT INTO flashcards (subject_id, topic_id, front, back, due_date, created_at)
                VALUES (:subject_id, :topic_id, :front, :back, :due_date, :created_at)
            """), data)
        _commit(conn)
        return data["id"]
    finally:
        _close(conn)


def delete_flashcard(fid):
    ph = _ph()
    conn = get_conn()
    try:
        _run(conn, f"DELETE FROM flashcards WHERE id = {ph}", (fid,))
        _commit(conn)
    finally:
        _close(conn)


def review_flashcard(fid, quality: int):
    """Update a card with an SM-2-lite schedule. quality 0=Again,3=Hard,4=Good,5=Easy."""
    ph = _ph()
    conn = get_conn()
    try:
        card = _q1(conn, f"SELECT * FROM flashcards WHERE id = {ph}", (fid,))
        if not card:
            return
        ease = card.get("ease") or 2.5
        reps = card.get("reps") or 0
        interval = card.get("interval_days") or 0
        if quality < 3:
            reps = 0
            interval = 1
        else:
            reps += 1
            if reps == 1:
                interval = 1
            elif reps == 2:
                interval = 6
            else:
                interval = round(interval * ease)
            ease = max(1.3, ease + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02)))
        interval = max(1, int(interval))
        due = (date.today() + timedelta(days=interval)).isoformat()
        _run(conn, _n("""
            UPDATE flashcards SET ease=:ease, reps=:reps, interval_days=:interval,
                due_date=:due, last_reviewed=:now WHERE id=:id
        """), {"ease": round(ease, 2), "reps": reps, "interval": interval,
               "due": due, "now": datetime.now().isoformat(), "id": fid})
        _commit(conn)
    finally:
        _close(conn)


# ── Study tasks (scheduler) ────────────────────────────────────────────────────

def get_study_tasks(status=None):
    ph = _ph()
    sql = """SELECT st.*, s.name AS subject_name, s.color
             FROM study_tasks st LEFT JOIN subjects s ON st.subject_id = s.id WHERE 1=1"""
    params: list = []
    if status and status != "All":
        sql += f" AND st.status = {ph}"
        params.append(status)
    sql += " ORDER BY st.due_date IS NULL, st.due_date, st.id"
    conn = get_conn()
    try:
        return _q(conn, sql, tuple(params))
    finally:
        _close(conn)


def upsert_study_task(data: dict):
    now = datetime.now().isoformat()
    data = dict(data)
    data.setdefault("subject_id", None)
    data.setdefault("task_type", "Review")
    data.setdefault("duration_min", 60)
    data.setdefault("status", "Todo")
    data.setdefault("notes", "")
    conn = get_conn()
    try:
        if data.get("id"):
            _run(conn, _n("""
                UPDATE study_tasks SET title=:title, subject_id=:subject_id, task_type=:task_type,
                    due_date=:due_date, duration_min=:duration_min, status=:status, notes=:notes WHERE id=:id
            """), data)
        else:
            data["created_at"] = now
            data["id"] = _run(conn, _n("""
                INSERT INTO study_tasks (title, subject_id, task_type, due_date, duration_min, status, notes, created_at)
                VALUES (:title, :subject_id, :task_type, :due_date, :duration_min, :status, :notes, :created_at)
            """), data)
        _commit(conn)
        return data["id"]
    finally:
        _close(conn)


def set_task_status(task_id, status):
    ph = _ph()
    conn = get_conn()
    try:
        _run(conn, f"UPDATE study_tasks SET status = {ph} WHERE id = {ph}", (status, task_id))
        _commit(conn)
    finally:
        _close(conn)


def delete_study_task(task_id):
    ph = _ph()
    conn = get_conn()
    try:
        _run(conn, f"DELETE FROM study_tasks WHERE id = {ph}", (task_id,))
        _commit(conn)
    finally:
        _close(conn)


# ── Chat history (AI tutor) ────────────────────────────────────────────────────

def save_message(role: str, content: str):
    ph = _ph()
    conn = get_conn()
    try:
        _run(conn, f"INSERT INTO chat_history (role, content, created_at) VALUES ({ph}, {ph}, {ph})",
             (role, content, datetime.now().isoformat()))
        _commit(conn)
    finally:
        _close(conn)


def get_chat_history(limit=50):
    ph = _ph()
    conn = get_conn()
    try:
        rows = _q(conn, f"SELECT role, content FROM chat_history ORDER BY id DESC LIMIT {ph}", (limit,))
        return list(reversed(rows))
    finally:
        _close(conn)


def clear_chat_history():
    conn = get_conn()
    try:
        _run(conn, "DELETE FROM chat_history")
        _commit(conn)
    finally:
        _close(conn)


# ── App context (exam date etc.) ───────────────────────────────────────────────

def set_context(key: str, value: str):
    now = datetime.now().isoformat()
    conn = get_conn()
    try:
        if _setup():
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app_context (key, value, updated_at) VALUES (%s, %s, %s) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at",
                    (key, value, now))
        else:
            conn.execute("INSERT OR REPLACE INTO app_context (key, value, updated_at) VALUES (?, ?, ?)",
                         (key, value, now))
        _commit(conn)
    finally:
        _close(conn)


def get_context(key: str, default=None):
    ph = _ph()
    conn = get_conn()
    try:
        row = _q1(conn, f"SELECT value FROM app_context WHERE key = {ph}", (key,))
        return row["value"] if row else default
    finally:
        _close(conn)


# ── Analytics ──────────────────────────────────────────────────────────────────

def get_accuracy_by_subject():
    conn = get_conn()
    try:
        return _q(conn, """
            SELECT s.id AS subject_id, s.name AS subject_name, s.color,
                   COUNT(a.id) AS attempts,
                   SUM(a.is_correct) AS correct,
                   AVG(a.seconds) AS avg_seconds
            FROM subjects s LEFT JOIN attempts a ON a.subject_id = s.id
            GROUP BY s.id, s.name, s.color
            ORDER BY s.sort_order, s.name
        """)
    finally:
        _close(conn)


def get_attempts_over_time(days=30):
    ph = _ph()
    start = (date.today() - timedelta(days=days)).isoformat()
    conn = get_conn()
    try:
        return _q(conn, f"""
            SELECT substr(created_at, 1, 10) AS day,
                   COUNT(*) AS attempts,
                   SUM(is_correct) AS correct
            FROM attempts WHERE created_at >= {ph}
            GROUP BY substr(created_at, 1, 10)
            ORDER BY day
        """, (start,))
    finally:
        _close(conn)


def get_overall_stats():
    conn = get_conn()
    try:
        att = _q1(conn, "SELECT COUNT(*) AS n, SUM(is_correct) AS correct FROM attempts") or {}
        cards = _q1(conn, "SELECT COUNT(*) AS n FROM flashcards") or {}
        today = date.today().isoformat()
        ph = _ph()
        due = _q1(conn, f"SELECT COUNT(*) AS n FROM flashcards WHERE due_date IS NULL OR due_date <= {ph}", (today,)) or {}
        mastered = _q1(conn, "SELECT COUNT(*) AS n FROM flashcards WHERE reps >= 3") or {}
        tasks_done = _q1(conn, "SELECT COUNT(*) AS n FROM study_tasks WHERE status = 'Done'") or {}
        tasks_total = _q1(conn, "SELECT COUNT(*) AS n FROM study_tasks") or {}
        qs = _q1(conn, "SELECT COUNT(*) AS n FROM questions") or {}
        return {
            "attempts": att.get("n") or 0,
            "correct": att.get("correct") or 0,
            "cards": cards.get("n") or 0,
            "cards_due": due.get("n") or 0,
            "cards_mastered": mastered.get("n") or 0,
            "tasks_done": tasks_done.get("n") or 0,
            "tasks_total": tasks_total.get("n") or 0,
            "questions": qs.get("n") or 0,
        }
    finally:
        _close(conn)


# ── Seed content ───────────────────────────────────────────────────────────────

_SUBJECTS = [
    ("VR",  "Verbal Reasoning",       "#1f77b4", 1),
    ("DM",  "Decision Making",        "#2ca02c", 2),
    ("QR",  "Quantitative Reasoning", "#9467bd", 3),
    ("SJT", "Situational Judgement",  "#ff7f0e", 4),
]

# topics: (subject_code, name, high_yield, summary, content)
_TOPICS = [
    ("VR", "Reading for the Main Idea", 1,
     "Skim efficiently — you have only seconds per question.",
     "Verbal Reasoning gives **~44 questions in ~21 minutes**, so you cannot read every passage in full.\n\n- **Scan for keywords** from the question, then read only the sentence(s) around them.\n- Decide each item from the **passage alone** — never from outside knowledge.\n- The credited answer is the one the text best supports, not the most interesting one."),
    ("VR", "True / False / Can't Tell", 1,
     "The classic VR judgement: is the statement supported, contradicted, or neither?",
     "- **True** — the passage directly states or clearly implies it.\n- **False** — the passage contradicts it.\n- **Can't Tell** — there isn't enough information to decide. Use this whenever the passage is silent.\n\n**Trap:** absolute words ('all', 'never', 'always') make a statement easy to falsify — a single exception in the text makes it false."),
    ("VR", "Inference & Author Tone", 0,
     "Reading between the lines without over-reaching.",
     "Inference questions ask what *follows* from the passage. Stay close to the text:\n\n- A valid inference needs no extra assumptions.\n- Watch the author's **tone** (critical, neutral, enthusiastic) and **purpose**.\n- Eliminate options that are too strong, out of scope, or the opposite of the author's view."),
    ("DM", "Syllogisms & Logical Deduction", 1,
     "Decide what necessarily follows from the premises.",
     "A conclusion is **valid only if it must be true** given the premises.\n\n- 'All A are B' + 'Some B are C' does **not** prove anything about A and C → *no valid conclusion*.\n- Test options by looking for a **counterexample**; if one exists, the option is invalid.\n- Beware switching 'some' ↔ 'all' and reversing direction ('all A are B' ≠ 'all B are A')."),
    ("DM", "Venn Diagrams & Sets", 1,
     "Counting with overlapping groups.",
     "For two sets: **|A ∪ B| = |A| + |B| − |A ∩ B|**.\n\n- 'Neither' = Total − |A ∪ B|.\n- 'Only A' = |A| − |A ∩ B|.\n- Draw the circles, fill the **overlap first**, then work outward. For three sets, start from the central triple-overlap."),
    ("DM", "Probability & Statistics", 1,
     "Basic probability and expected value under time pressure.",
     "- **Probability** = favourable outcomes ÷ total outcomes (equally likely).\n- Independent events: multiply (AND); mutually exclusive: add (OR).\n- 'At least one' = 1 − P(none).\n- Know how to read **odds** ('2 to 3') and convert to a probability (2/5)."),
    ("DM", "Logic Puzzles & Arrangements", 0,
     "Ordering, matching, and conditional clues.",
     "Decision Making often gives a set of clues and asks who/what fits.\n\n- Translate clues into a quick **grid or ordering**.\n- Process the **most restrictive clue first**.\n- Eliminate options that violate any single clue rather than fully solving every case."),
    ("QR", "Percentages & Percentage Change", 1,
     "The most common QR skill — increases, decreases, and reverse percentages.",
     "- **Increase by x%:** multiply by (1 + x/100). A 25% rise on 80 → 80 × 1.25 = 100.\n- **Percentage change:** (change ÷ original) × 100.\n- **Reverse percentage:** if a price after +20% is 120, original = 120 ÷ 1.2 = 100.\n- Use the on-screen calculator sparingly — many can be done mentally."),
    ("QR", "Ratios & Proportion", 1,
     "Sharing quantities and scaling recipes/doses.",
     "- Split a total in ratio a:b → fractions a/(a+b) and b/(a+b).\n- Keep units consistent before dividing.\n- Direct proportion: y = kx. Inverse proportion: xy = k.\n- Dose/recipe scaling is just multiplying every part by the same factor."),
    ("QR", "Speed, Distance & Time", 0,
     "The classic rate triangle.",
     "**Speed = Distance ÷ Time** (and rearrangements). 150 km in 2.5 h → 60 km/h.\n\n- Convert units first (km↔m, hours↔minutes).\n- Average speed = total distance ÷ total time, *not* the mean of the speeds.\n- The same triangle works for any rate (flow, dosage per hour, etc.)."),
    ("QR", "Tables, Charts & Data", 1,
     "Extracting the right number quickly from a stimulus.",
     "Most QR items hang off a shared table or chart.\n\n- Read the **question first**, then hunt for only the figures you need.\n- Watch **units and footnotes** ('figures in thousands', '% of total').\n- Don't recompute the whole table — target the single cell or row required."),
    ("SJT", "Appropriateness Ratings", 1,
     "Rate how appropriate a response is on the UCAT 4-point scale.",
     "The scale is: **Very appropriate · Appropriate, but not ideal · Inappropriate, but not awful · Very inappropriate.**\n\n- Judge the response **as written**, in isolation — not against other options.\n- Anything that risks **patient safety**, breaches confidentiality, or is dishonest tends toward *very inappropriate*.\n- A reasonable action that is incomplete or slightly out of order is usually *appropriate, but not ideal*."),
    ("SJT", "Importance Ratings", 1,
     "Rate how important a consideration is when deciding what to do.",
     "Scale: **Very important · Important · Of minor importance · Not important at all.**\n\n- Considerations tied to **patient safety, professional duty, and the people directly affected** are usually very important.\n- Irrelevant, self-serving, or speculative considerations are *not important*.\n- Don't confuse 'true' with 'important' — a true but irrelevant fact can still be unimportant."),
    ("SJT", "Medical Ethics & Professionalism", 1,
     "The values the SJT rewards, anchored in GMC Good Medical Practice.",
     "Default to the **GMC 'Good Medical Practice'** principles:\n\n- **Patient safety first**, always.\n- **Confidentiality, honesty and integrity** (probity).\n- **Work within your competence** and seek senior help when unsure.\n- Raise concerns about colleagues **supportively but without delay** when patients could be at risk. SJT is scored in **Bands 1–4** (Band 1 = strongest), separately from the cognitive subtests."),
]

# questions: (subject_code, topic_name, stem, A, B, C, D, correct, explanation, difficulty)
_QUESTIONS = [
    ("VR", "Reading for the Main Idea",
     "Passage: \"Although caffeine is widely consumed, recent studies suggest its effect on long-term memory is negligible. Its impact on short-term alertness, however, is reliably positive.\" Which statement is best supported by the passage?",
     "Caffeine improves long-term memory",
     "Caffeine reliably improves short-term alertness",
     "Caffeine has no measurable effect on the body",
     "Caffeine consumption is declining", "B",
     "Only B is directly stated. A contradicts the passage, while C and D are not supported by anything in the text.", "Easy"),
    ("VR", "True / False / Can't Tell",
     "Passage: \"The clinic opens at 9 am on weekdays.\" Statement: \"The clinic opens at 9 am on Saturdays.\" Based only on the passage, this statement is:",
     "True", "False", "Can't tell", "Partly true", "C",
     "The passage only mentions weekdays and says nothing about Saturdays, so there isn't enough information to judge — the answer is 'Can't tell'.", "Medium"),
    ("VR", "Inference & Author Tone",
     "Passage: \"Every member of the debating society must pass an entry assessment. Priya is a member of the debating society.\" Which conclusion follows?",
     "Priya enjoys debating",
     "Priya must have passed (or must pass) the entry assessment",
     "Priya is the best debater",
     "Priya founded the society", "B",
     "If all members must pass the assessment and Priya is a member, it necessarily follows that the assessment applies to her. The others add information the passage never gives.", "Medium"),
    ("DM", "Syllogisms & Logical Deduction",
     "\"All cardiologists are doctors. Some doctors work night shifts.\" Which conclusion necessarily follows?",
     "All cardiologists work night shifts",
     "Some cardiologists work night shifts",
     "No valid conclusion can be drawn about cardiologists and night shifts",
     "All doctors are cardiologists", "C",
     "The night-shift doctors might be entirely non-cardiologists, so nothing is guaranteed about cardiologists. With a possible counterexample, no valid conclusion follows.", "Hard"),
    ("DM", "Venn Diagrams & Sets",
     "In a group of 100 students, 60 study biology, 45 study chemistry, and 30 study both. How many study neither subject?",
     "15", "25", "30", "40", "B",
     "Students studying at least one = 60 + 45 − 30 = 75. Neither = 100 − 75 = 25.", "Medium"),
    ("DM", "Probability & Statistics",
     "A bag contains 3 red and 2 blue counters. One counter is drawn at random. What is the probability it is blue?",
     "2/5", "3/5", "1/2", "2/3", "A",
     "Probability = favourable ÷ total = 2 blue ÷ 5 counters = 2/5.", "Easy"),
    ("QR", "Percentages & Percentage Change",
     "A medication costs £80. Its price increases by 25%. What is the new price?",
     "£85", "£100", "£105", "£120", "B",
     "An increase of 25% multiplies the price by 1.25: 80 × 1.25 = £100.", "Easy"),
    ("QR", "Percentages & Percentage Change",
     "A patient's weight falls from 90 kg to 81 kg. What is the percentage decrease?",
     "9%", "10%", "11%", "90%", "B",
     "Change = 9 kg. Percentage change = (9 ÷ 90) × 100 = 10%.", "Medium"),
    ("QR", "Speed, Distance & Time",
     "A car travels 150 km in 2.5 hours. What is its average speed?",
     "50 km/h", "60 km/h", "75 km/h", "375 km/h", "B",
     "Speed = distance ÷ time = 150 ÷ 2.5 = 60 km/h.", "Easy"),
    ("SJT", "Appropriateness Ratings",
     "A medical student notices that a fellow student has posted identifiable patient details on social media. How appropriate is it for the student to ask the colleague to remove the post immediately?",
     "A very appropriate thing to do",
     "Appropriate, but not ideal",
     "Inappropriate, but not awful",
     "A very inappropriate thing to do", "A",
     "Patient confidentiality is a core professional duty. Asking for the post to be taken down at once directly protects patients, so it is very appropriate (escalating to a senior may also be needed).", "Medium"),
    ("SJT", "Importance Ratings",
     "A junior colleague seems overwhelmed and has started making errors. When deciding how to respond, how important is it to consider patient safety?",
     "Very important", "Important", "Of minor importance", "Not important at all", "A",
     "Patient safety is the overriding concern in GMC Good Medical Practice, so it is a very important consideration in any clinical decision.", "Easy"),
    ("SJT", "Medical Ethics & Professionalism",
     "A patient asks a medical student whether they personally think the patient should refuse a treatment the doctor has recommended. What is the most appropriate response?",
     "Tell the patient to refuse the treatment",
     "Give the patient the student's own medical advice",
     "Encourage the patient to discuss their concerns with the responsible doctor",
     "Ignore the patient's question", "C",
     "A student should work within their competence and not give independent medical advice. Directing the patient back to the responsible doctor respects both patient autonomy and professional boundaries.", "Medium"),

    # ── Additional seeded questions ─────────────────────────────────────────
    ("VR", "Reading for the Main Idea",
     "Passage: \"Although many assume vaccination was a twentieth-century invention, deliberate inoculation against smallpox was practised in parts of Asia centuries earlier. What modern science added was not the idea but its safety and standardisation.\" Which statement is best supported?",
     "Vaccination was first invented in the twentieth century",
     "Inoculation against smallpox predates modern science",
     "Smallpox no longer exists anywhere in the world",
     "Modern medicine has made no real contribution to vaccines", "B",
     "The passage states inoculation was practised centuries before modern science, which is exactly option B. A is contradicted, and C and D go beyond anything in the text.", "Medium"),
    ("VR", "Reading for the Main Idea",
     "Passage: \"The report concludes that while remote consultations improve access for rural patients, they are no substitute for an in-person examination when physical signs must be assessed.\" Which statement is best supported?",
     "Remote consultations should replace all in-person visits",
     "Remote consultations can improve access but have clear limits",
     "Rural patients always prefer in-person appointments",
     "Physical examination is never necessary", "B",
     "The passage credits remote consultations with improving access while noting they cannot replace a physical examination — precisely option B. A and D are contradicted; C is never stated.", "Medium"),
    ("VR", "True / False / Can't Tell",
     "Passage: \"The new hospital wing was completed in 2019 and opened to patients in 2020.\" Statement: \"The new wing treated patients before 2020.\" Based only on the passage, this statement is:",
     "True", "False", "Can't tell", "Partly true", "B",
     "The passage says the wing opened to patients in 2020, so it did not treat patients before then. The statement is contradicted, making it False.", "Easy"),
    ("VR", "True / False / Can't Tell",
     "Passage: \"Dr Lee holds clinics on Mondays, Wednesdays and Fridays.\" Statement: \"Dr Lee never works at weekends.\" Based only on the passage, this statement is:",
     "True", "False", "Can't tell", "Partly true", "C",
     "The passage lists the weekdays Dr Lee holds clinics but says nothing about weekends, so there isn't enough information to judge the statement — the answer is 'Can't tell'.", "Medium"),
    ("VR", "Inference & Author Tone",
     "Passage: \"Yet another so-called breakthrough diet promises miracles while quietly ignoring the basic arithmetic of calories.\" The author's tone toward the diet is best described as:",
     "Enthusiastic", "Neutral and detached", "Sceptical", "Admiring", "C",
     "Phrases like 'so-called breakthrough' and 'promises miracles while ignoring the basic arithmetic' signal doubt and criticism, so the tone is sceptical.", "Medium"),

    ("DM", "Syllogisms & Logical Deduction",
     "\"No reptiles are mammals. All snakes are reptiles.\" Which conclusion necessarily follows?",
     "All snakes are mammals",
     "No snakes are mammals",
     "Some snakes are mammals",
     "Some mammals are snakes", "B",
     "If no reptiles are mammals and all snakes are reptiles, then snakes (being reptiles) cannot be mammals. So no snakes are mammals.", "Medium"),
    ("DM", "Venn Diagrams & Sets",
     "In a class of 40 students, 25 play football, 18 play tennis, and 5 play neither sport. How many play both?",
     "6", "7", "8", "9", "C",
     "Students playing at least one sport = 40 − 5 = 35. Using |F ∪ T| = |F| + |T| − both: 35 = 25 + 18 − both, so both = 8.", "Medium"),
    ("DM", "Probability & Statistics",
     "A fair six-sided die is rolled once. What is the probability of rolling a number greater than 4?",
     "1/6", "1/3", "1/2", "2/3", "B",
     "The outcomes greater than 4 are 5 and 6 — that is 2 of the 6 equally likely outcomes, giving 2/6 = 1/3.", "Easy"),
    ("DM", "Probability & Statistics",
     "A diagnostic test gives a positive result with probability 0.2. Two independent tests are run on different samples. What is the probability that both are positive?",
     "0.4", "0.2", "0.04", "0.004", "C",
     "For independent events you multiply: 0.2 × 0.2 = 0.04.", "Medium"),
    ("DM", "Logic Puzzles & Arrangements",
     "Four runners finish a race. Aki finishes before Ben, Ben finishes before Cara, and Dia finishes after Cara. Who finishes last?",
     "Aki", "Ben", "Cara", "Dia", "D",
     "The clues chain together as Aki → Ben → Cara → Dia, so Dia finishes last.", "Easy"),
    ("DM", "Logic Puzzles & Arrangements",
     "Three colleagues — X, Y and Z — sit in a row of three seats. X is not at either end, and Y sits immediately to the left of X. Who sits at the right-hand end?",
     "X", "Y", "Z", "It cannot be determined", "C",
     "X must be in the middle seat; Y is immediately to X's left, so Y takes the left end, leaving Z at the right-hand end.", "Medium"),

    ("QR", "Percentages & Percentage Change",
     "A jacket priced at £60 is reduced by 15% in a sale. What is the sale price?",
     "£45", "£51", "£54", "£55", "B",
     "A 15% reduction multiplies the price by 0.85: 60 × 0.85 = £51.", "Easy"),
    ("QR", "Percentages & Percentage Change",
     "After a 20% increase, a season ticket now costs £360. What was the original price?",
     "£288", "£300", "£320", "£340", "B",
     "Original × 1.2 = 360, so original = 360 ÷ 1.2 = £300. (Subtracting 20% of £360 is the classic trap.)", "Medium"),
    ("QR", "Ratios & Proportion",
     "A medicine is mixed with water in the ratio 2:5. To make 350 ml of the mixture, how much medicine is needed?",
     "70 ml", "100 ml", "140 ml", "175 ml", "B",
     "Medicine makes up 2 of every 7 parts, so 2/7 × 350 = 100 ml.", "Medium"),
    ("QR", "Ratios & Proportion",
     "A recipe for 4 people uses 600 g of flour. How much flour is needed to serve 6 people?",
     "750 g", "800 g", "900 g", "1000 g", "C",
     "Flour per person = 600 ÷ 4 = 150 g; for 6 people, 150 × 6 = 900 g.", "Easy"),
    ("QR", "Speed, Distance & Time",
     "A cyclist rides at a steady 18 km/h for 40 minutes. How far do they travel?",
     "7.2 km", "12 km", "13.5 km", "27 km", "B",
     "40 minutes is 2/3 of an hour, so distance = speed × time = 18 × 2/3 = 12 km.", "Medium"),
    ("QR", "Tables, Charts & Data",
     "A clinic saw 1,200 patients in total last year, of which 35% were seen during winter. How many patients were seen in winter?",
     "350", "420", "480", "600", "B",
     "35% of 1,200 = 0.35 × 1,200 = 420 patients.", "Easy"),

    ("SJT", "Appropriateness Ratings",
     "During a ward round a medical student realises they forgot to record a patient's drug allergy in the notes. How appropriate is it for the student to correct the notes and tell the supervising doctor straight away?",
     "A very appropriate thing to do",
     "Appropriate, but not ideal",
     "Inappropriate, but not awful",
     "A very inappropriate thing to do", "A",
     "An unrecorded allergy is a direct patient-safety risk. Correcting the record promptly and informing the supervisor is exactly what professional duty requires, so it is very appropriate.", "Medium"),
    ("SJT", "Appropriateness Ratings",
     "A friend asks a medical student to share photos taken inside the operating theatre on a social-media group chat. How appropriate is it for the student to refuse?",
     "A very appropriate thing to do",
     "Appropriate, but not ideal",
     "Inappropriate, but not awful",
     "A very inappropriate thing to do", "A",
     "Sharing theatre photos risks breaching patient confidentiality and professional standards. Refusing protects patients and is very appropriate.", "Easy"),
    ("SJT", "Importance Ratings",
     "A student doctor disagrees with a senior's management plan. When deciding how to act, how important is it to consider that the student's own view might be mistaken due to limited experience?",
     "Very important", "Important", "Of minor importance", "Not important at all", "A",
     "Recognising the limits of one's own competence is central to GMC guidance, so it is a very important consideration — while still raising any genuine safety concern through the proper channel.", "Medium"),
    ("SJT", "Importance Ratings",
     "A colleague offers to write up a procedure log entry for a clinical skill the student did not actually perform. When deciding how to respond, how important is honesty in record-keeping?",
     "Very important", "Important", "Of minor importance", "Not important at all", "A",
     "Probity — honesty and integrity — is a core GMC duty, and falsifying records is a serious breach. Honesty is therefore a very important consideration.", "Easy"),
    ("SJT", "Medical Ethics & Professionalism",
     "An adult patient with full capacity refuses a recommended blood transfusion on religious grounds. What is the most appropriate response from the team?",
     "Give the transfusion anyway to save their life",
     "Respect the patient's informed refusal while ensuring they understand the consequences",
     "Ask the patient's family to overrule the decision",
     "Discharge the patient immediately", "B",
     "A competent, informed adult has the right to refuse treatment. The team should respect the patient's autonomy while making sure the refusal is fully informed.", "Hard"),
    ("SJT", "Medical Ethics & Professionalism",
     "A junior doctor smells alcohol on a colleague who is about to start a clinical shift. What is the most appropriate first action?",
     "Ignore it and hope nothing goes wrong",
     "Announce it to all the other staff on the ward",
     "Raise the concern discreetly with a senior so patient safety is protected",
     "Post about it anonymously online", "C",
     "Patient safety comes first, and concerns about a colleague's fitness to practise must be raised promptly through the proper senior channel — discreetly, not publicly.", "Medium"),
]

# flashcards: (subject_code, topic_name, front, back)
_FLASHCARDS = [
    ("VR", "True / False / Can't Tell", "When should you choose 'Can't Tell' in Verbal Reasoning?", "When the passage doesn't give enough information to judge the statement true or false. Never use outside knowledge."),
    ("VR", "Reading for the Main Idea", "What's the recommended VR reading approach given the tight timing?", "Scan for keywords from the question and read only the relevant sentence(s) — you have roughly 20–30 seconds per question."),
    ("VR", "True / False / Can't Tell", "How do absolute words (all, never, always) affect a VR statement?", "They make it easy to disprove — a single exception in the passage makes the statement false."),
    ("DM", "Venn Diagrams & Sets", "How do you find 'neither' in a two-set Venn problem?", "Neither = Total − (|A| + |B| − |A∩B|). Add the two sets, subtract the overlap, subtract from the total."),
    ("DM", "Syllogisms & Logical Deduction", "When is a syllogism's conclusion valid?", "Only if it must be true given the premises. If a counterexample exists, choose 'no valid conclusion'."),
    ("DM", "Probability & Statistics", "Probability of an equally-likely event =", "Favourable outcomes ÷ total outcomes. 'At least one' = 1 − P(none)."),
    ("QR", "Percentages & Percentage Change", "How do you increase a value by x%?", "Multiply by (1 + x/100). A 25% rise on £80 → 80 × 1.25 = £100."),
    ("QR", "Percentages & Percentage Change", "Percentage change formula?", "(change ÷ original) × 100."),
    ("QR", "Speed, Distance & Time", "State the speed equation.", "Speed = distance ÷ time. Keep the units consistent first."),
    ("SJT", "Appropriateness Ratings", "Name the UCAT SJT appropriateness scale.", "Very appropriate · Appropriate but not ideal · Inappropriate but not awful · Very inappropriate."),
    ("SJT", "Medical Ethics & Professionalism", "What framework guides SJT answers?", "The GMC 'Good Medical Practice': patient safety, confidentiality, honesty/integrity, and working within your competence come first."),
    ("SJT", "Medical Ethics & Professionalism", "How is the SJT scored?", "In Bands 1–4 (Band 1 is strongest), reported separately from the cognitive scaled scores."),
    ("VR", "Inference & Author Tone", "What makes a valid inference in Verbal Reasoning?", "It follows from the passage with no extra assumptions. Reject options that are too strong, out of scope, or opposite to the author's view."),
    ("DM", "Logic Puzzles & Arrangements", "How should you start a logic puzzle with several clues?", "Translate the clues into a quick grid or ordering and process the most restrictive clue first, eliminating any option that breaks a single clue."),
    ("DM", "Probability & Statistics", "How do you combine independent events (AND)?", "Multiply their probabilities. For mutually exclusive events (OR), add them; and 'at least one' = 1 − P(none)."),
    ("QR", "Ratios & Proportion", "How do you split a total in the ratio a:b?", "Take fractions a/(a+b) and b/(a+b) of the total. Make sure the units are consistent first."),
    ("QR", "Tables, Charts & Data", "Best approach to a data or table question?", "Read the question first, then extract only the figures you need. Watch units and footnotes such as 'in thousands' or '% of total'."),
    ("SJT", "Importance Ratings", "Which considerations are usually 'very important' in the SJT?", "Those tied to patient safety, professional duty, and the people directly affected. Remember a fact can be true yet unimportant."),
]


def seed_content():
    """Idempotently load the starter MCAT content the first time the app runs."""
    existing = get_subjects()
    if existing:
        return  # already seeded
    conn = get_conn()
    try:
        # Subjects
        code_to_id = {}
        for code, name, color, order in _SUBJECTS:
            sid = _run(conn, _n("INSERT INTO subjects (code, name, color, sort_order) VALUES (:c,:n,:col,:o)"),
                       {"c": code, "n": name, "col": color, "o": order})
            code_to_id[code] = sid
        _commit(conn)
        # Topics
        now = datetime.now().isoformat()
        topic_key_to_id = {}
        for code, name, hy, summary, content in _TOPICS:
            tid = _run(conn, _n("""INSERT INTO topics (subject_id, name, high_yield, summary, content, created_at)
                       VALUES (:s,:n,:hy,:sum,:c,:ca)"""),
                       {"s": code_to_id[code], "n": name, "hy": hy, "sum": summary, "c": content, "ca": now})
            topic_key_to_id[(code, name)] = tid
        _commit(conn)
        # Questions
        for code, tname, stem, a, b, c, d, correct, expl, diff in _QUESTIONS:
            _run(conn, _n("""INSERT INTO questions (subject_id, topic_id, stem, option_a, option_b,
                       option_c, option_d, correct, explanation, difficulty, created_at)
                       VALUES (:s,:t,:stem,:a,:b,:c,:d,:cor,:e,:diff,:ca)"""),
                 {"s": code_to_id[code], "t": topic_key_to_id.get((code, tname)), "stem": stem,
                  "a": a, "b": b, "c": c, "d": d, "cor": correct, "e": expl, "diff": diff, "ca": now})
        # Flashcards
        today = date.today().isoformat()
        for code, tname, front, back in _FLASHCARDS:
            _run(conn, _n("""INSERT INTO flashcards (subject_id, topic_id, front, back, due_date, created_at)
                       VALUES (:s,:t,:f,:b,:due,:ca)"""),
                 {"s": code_to_id[code], "t": topic_key_to_id.get((code, tname)),
                  "f": front, "b": back, "due": today, "ca": now})
        _commit(conn)
    finally:
        _close(conn)


def backfill_content():
    """Idempotently add any seed topics/questions/flashcards that are missing.

    Unlike seed_content(), this runs even on an already-populated database, so
    expanded starter content reaches an existing deployment (e.g. Neon) without
    a manual reload. It only *adds* rows that are absent — matching questions by
    stem and flashcards by (front, back) — and never updates or deletes, so any
    user-edited or user-created content is left untouched.
    """
    subs = get_subjects()
    if not subs:
        return  # fresh database: seed_content() handles the initial load
    code_to_id = {s["code"]: s["id"] for s in subs}
    id_to_code = {v: k for k, v in code_to_id.items()}
    added_t = added_q = added_f = 0
    conn = get_conn()
    try:
        now = datetime.now().isoformat()
        today = date.today().isoformat()

        # Topics — map existing (code, name) → id, inserting any that are missing
        topic_key_to_id = {}
        for r in _q(conn, "SELECT id, subject_id, name FROM topics"):
            code = id_to_code.get(r["subject_id"])
            if code:
                topic_key_to_id[(code, r["name"])] = r["id"]
        for code, name, hy, summary, content in _TOPICS:
            if code not in code_to_id or (code, name) in topic_key_to_id:
                continue
            tid = _run(conn, _n("""INSERT INTO topics (subject_id, name, high_yield, summary, content, created_at)
                       VALUES (:s,:n,:hy,:sum,:c,:ca)"""),
                       {"s": code_to_id[code], "n": name, "hy": hy, "sum": summary, "c": content, "ca": now})
            topic_key_to_id[(code, name)] = tid
            added_t += 1
        _commit(conn)

        # Questions — insert any whose stem is not already present
        existing_stems = {r["stem"] for r in _q(conn, "SELECT stem FROM questions")}
        for code, tname, stem, a, b, c, d, correct, expl, diff in _QUESTIONS:
            if code not in code_to_id or stem in existing_stems:
                continue
            _run(conn, _n("""INSERT INTO questions (subject_id, topic_id, stem, option_a, option_b,
                       option_c, option_d, correct, explanation, difficulty, created_at)
                       VALUES (:s,:t,:stem,:a,:b,:c,:d,:cor,:e,:diff,:ca)"""),
                 {"s": code_to_id[code], "t": topic_key_to_id.get((code, tname)), "stem": stem,
                  "a": a, "b": b, "c": c, "d": d, "cor": correct, "e": expl, "diff": diff, "ca": now})
            existing_stems.add(stem)
            added_q += 1

        # Flashcards — insert any whose (front, back) pair is not already present
        existing_cards = {(r["front"], r["back"]) for r in _q(conn, "SELECT front, back FROM flashcards")}
        for code, tname, front, back in _FLASHCARDS:
            if code not in code_to_id or (front, back) in existing_cards:
                continue
            _run(conn, _n("""INSERT INTO flashcards (subject_id, topic_id, front, back, due_date, created_at)
                       VALUES (:s,:t,:f,:b,:due,:ca)"""),
                 {"s": code_to_id[code], "t": topic_key_to_id.get((code, tname)),
                  "f": front, "b": back, "due": today, "ca": now})
            existing_cards.add((front, back))
            added_f += 1
        _commit(conn)
    finally:
        _close(conn)
    return {"topics_added": added_t, "questions_added": added_q, "flashcards_added": added_f}
