import json
import os
import sqlite3
import threading
import uuid

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "content_desk.db")

_lock = threading.Lock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row


def init_db():
    with _lock:
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS notes (
                id TEXT PRIMARY KEY,
                source TEXT,              -- 'telegram' | 'export'
                source_ref TEXT UNIQUE,   -- chat_id:message_id, dedupes re-imports
                captured_at TEXT,
                text TEXT,
                extra_context TEXT,       -- Meera's answers to follow-up questions
                verdict TEXT,             -- NULL (untriaged) | develop | needs_detail | not_now
                score INTEGER,
                triage_json TEXT,
                status TEXT DEFAULT 'inbox',  -- inbox | drafted | used | archived
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS drafts (
                id TEXT PRIMARY KEY,
                note_id TEXT,
                body TEXT,
                angle_json TEXT,
                lint_json TEXT,
                status TEXT DEFAULT 'ready',  -- ready | approved | posted | rejected
                feedback TEXT,
                final_body TEXT,              -- what she actually posted, feeds voice examples
                created_at TEXT DEFAULT (datetime('now')),
                reviewed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);

            -- One row per pipeline run: every stage's reasoning is kept so the page can show why.
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                note_id TEXT,
                input_text TEXT,
                score INTEGER,
                stage1_json TEXT,
                thesis TEXT,
                searches_json TEXT,       -- [{query, results, usable, reason}]
                outcome TEXT,             -- feedback | draft | error
                draft_id TEXT,
                message TEXT,             -- exactly what went (or would go) to Telegram
                delivered INTEGER DEFAULT 0,
                error TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            """
        )
        run_cols = {r["name"] for r in _conn.execute("PRAGMA table_info(runs)").fetchall()}
        if "post_grade_json" not in run_cols:
            _conn.execute("ALTER TABLE runs ADD COLUMN post_grade_json TEXT")
        cols = {r["name"] for r in _conn.execute("PRAGMA table_info(notes)").fetchall()}
        if "question_msg_id" not in cols:  # additive migration for databases from before the bot asked questions
            _conn.execute("ALTER TABLE notes ADD COLUMN question_msg_id INTEGER")
        _conn.commit()


def _rows(sql, args=()):
    with _lock:
        return [dict(r) for r in _conn.execute(sql, args).fetchall()]


def _exec(sql, args=()):
    with _lock:
        cur = _conn.execute(sql, args)
        _conn.commit()
        return cur.rowcount


# ---- kv ----

def kv_get(k, default=None):
    rows = _rows("SELECT v FROM kv WHERE k=?", (k,))
    return rows[0]["v"] if rows else default


def kv_set(k, v):
    _exec("INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


# ---- notes ----

def add_note(source, source_ref, captured_at, text):
    """Returns the new note's id, or None if it was already stored."""
    note_id = uuid.uuid4().hex[:12]
    inserted = _exec(
        "INSERT OR IGNORE INTO notes(id, source, source_ref, captured_at, text) VALUES(?, ?, ?, ?, ?)",
        (note_id, source, source_ref, captured_at, text),
    )
    return note_id if inserted else None


def get_note(note_id):
    rows = _rows("SELECT * FROM notes WHERE id=?", (note_id,))
    return _hydrate_note(rows[0]) if rows else None


def untriaged_notes():
    return [_hydrate_note(r) for r in _rows("SELECT * FROM notes WHERE verdict IS NULL ORDER BY captured_at")]


def notes_by_verdict(verdict, status="inbox"):
    return [
        _hydrate_note(r)
        for r in _rows(
            "SELECT * FROM notes WHERE verdict=? AND status=? ORDER BY score DESC, captured_at DESC",
            (verdict, status),
        )
    ]


def all_notes():
    return [_hydrate_note(r) for r in _rows("SELECT * FROM notes ORDER BY captured_at DESC")]


def save_triage(note_id, verdict, score, triage):
    _exec(
        "UPDATE notes SET verdict=?, score=?, triage_json=? WHERE id=?",
        (verdict, score, json.dumps(triage), note_id),
    )


def reset_triage(note_id):
    _exec(
        "UPDATE notes SET verdict=NULL, score=NULL, triage_json=NULL, question_msg_id=NULL WHERE id=?",
        (note_id,),
    )


def set_question_msg(note_id, msg_id):
    _exec("UPDATE notes SET question_msg_id=? WHERE id=?", (msg_id, note_id))


def note_by_question_msg(msg_id):
    rows = _rows("SELECT * FROM notes WHERE question_msg_id=?", (msg_id,))
    return _hydrate_note(rows[0]) if rows else None


def add_context(note_id, answer):
    note = get_note(note_id)
    combined = ((note.get("extra_context") or "") + "\n" + answer).strip()
    _exec("UPDATE notes SET extra_context=? WHERE id=?", (combined, note_id))


def set_note_verdict(note_id, verdict):
    _exec("UPDATE notes SET verdict=? WHERE id=?", (verdict, note_id))


def set_note_status(note_id, status):
    _exec("UPDATE notes SET status=? WHERE id=?", (status, note_id))


def _hydrate_note(r):
    r["triage"] = json.loads(r["triage_json"]) if r.get("triage_json") else None
    return r


# ---- drafts ----

def add_draft(note_id, body, angle, lint):
    draft_id = uuid.uuid4().hex[:12]
    _exec(
        "INSERT INTO drafts(id, note_id, body, angle_json, lint_json) VALUES(?, ?, ?, ?, ?)",
        (draft_id, note_id, body, json.dumps(angle), json.dumps(lint)),
    )
    return draft_id


def update_draft_body(draft_id, body, lint):
    _exec("UPDATE drafts SET body=?, lint_json=? WHERE id=?", (body, json.dumps(lint), draft_id))


def get_draft(draft_id):
    rows = _rows("SELECT * FROM drafts WHERE id=?", (draft_id,))
    return _hydrate_draft(rows[0]) if rows else None


def drafts_by_status(status):
    return [
        _hydrate_draft(r)
        for r in _rows("SELECT * FROM drafts WHERE status=? ORDER BY created_at DESC", (status,))
    ]


def set_draft_status(draft_id, status, feedback=None, final_body=None):
    _exec(
        "UPDATE drafts SET status=?, feedback=COALESCE(?, feedback), final_body=COALESCE(?, final_body), "
        "reviewed_at=datetime('now') WHERE id=?",
        (status, feedback, final_body, draft_id),
    )


def posted_examples(limit=3):
    """Meera's own final versions -- the strongest voice signal we have after the guide itself."""
    return [
        r["final_body"]
        for r in _rows(
            "SELECT final_body FROM drafts WHERE status='posted' AND final_body IS NOT NULL "
            "ORDER BY reviewed_at DESC LIMIT ?",
            (limit,),
        )
    ]


def recent_rejection_feedback(limit=5):
    return [
        r["feedback"]
        for r in _rows(
            "SELECT feedback FROM drafts WHERE status='rejected' AND feedback IS NOT NULL AND feedback != '' "
            "ORDER BY reviewed_at DESC LIMIT ?",
            (limit,),
        )
    ]


def posts_this_week():
    return _rows(
        "SELECT COUNT(*) AS n FROM drafts WHERE status='posted' AND reviewed_at >= datetime('now', '-7 days')"
    )[0]["n"]


def _hydrate_draft(r):
    r["angle"] = json.loads(r["angle_json"]) if r.get("angle_json") else None
    r["lint"] = json.loads(r["lint_json"]) if r.get("lint_json") else None
    r["note"] = get_note(r["note_id"])
    return r


# ---- runs ----

def save_run(run):
    run = dict(run)
    run.setdefault("id", uuid.uuid4().hex[:12])
    for k in ("stage1", "searches", "post_grade"):
        run[f"{k}_json"] = json.dumps(run.pop(k, None))
    cols = ["id", "note_id", "input_text", "score", "stage1_json", "thesis", "searches_json",
            "outcome", "draft_id", "message", "delivered", "error", "post_grade_json"]
    _exec(
        f"INSERT OR REPLACE INTO runs({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})",
        tuple(run.get(c) for c in cols),
    )
    return run["id"]


def recent_runs(limit=30):
    out = []
    for r in _rows("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)):
        r["stage1"] = json.loads(r["stage1_json"]) if r.get("stage1_json") else None
        r["searches"] = json.loads(r["searches_json"]) if r.get("searches_json") else []
        r["post_grade"] = json.loads(r["post_grade_json"]) if r.get("post_grade_json") else None
        r["draft"] = get_draft(r["draft_id"]) if r.get("draft_id") else None
        out.append(r)
    return out


def get_run(run_id):
    rows = _rows("SELECT * FROM runs WHERE id=?", (run_id,))
    if not rows:
        return None
    r = rows[0]
    r["stage1"] = json.loads(r["stage1_json"]) if r.get("stage1_json") else None
    r["post_grade"] = json.loads(r["post_grade_json"]) if r.get("post_grade_json") else None
    return r
