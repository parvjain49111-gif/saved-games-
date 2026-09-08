"""
=============================================================================
 Car Trends Car Mall - PERSISTENT STORAGE
=============================================================================

 WHY SQLITE
 ----------
 One Instagram account produces thousands of conversations, not millions.
 SQLite handles that comfortably, needs no server to install or babysit, and
 keeps the whole project a copy-and-run affair on a Windows PC.

 Every query below is plain SQL against a small DAO surface, so if the shop
 ever outgrows SQLite the same statements move to Postgres with only the
 connection function rewritten.

 THREAD SAFETY
 -------------
 FastAPI runs our background tasks in a worker-thread pool, so several
 replies can be written at once. SQLite connections cannot be shared across
 threads, so each thread gets its own via threading.local(). WAL journalling
 is switched on so readers (the dashboard) never block the writer (the bot).

 NOTHING IS EVER FAKED
 ---------------------
 Every counter the dashboard shows is a COUNT/SUM over real rows. A fresh
 database reports zeros, and that is the correct answer for a fresh install.
=============================================================================
"""

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import config

_local = threading.local()

# One writer lock. SQLite serialises writes anyway; taking the lock in
# Python turns "database is locked" retries into an orderly queue.
_write_lock = threading.RLock()   # re-entrant: lookups nest inside writes


# ---------------------------------------------------------------------------
# TIME HELPERS
# ---------------------------------------------------------------------------
# Everything is stored as ISO-8601 UTC text. Sorting text in that format is
# the same as sorting by time, which keeps the queries simple.
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def now_iso() -> str:
    return iso(utc_now())


def parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text)


# ---------------------------------------------------------------------------
# CONNECTION
# ---------------------------------------------------------------------------
def get_connection() -> sqlite3.Connection:
    """Return this thread's connection, creating it on first use."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(config.DB_PATH, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


def close_connection() -> None:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


# ---------------------------------------------------------------------------
# SCHEMA
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id     TEXT PRIMARY KEY,
    customer_identifier TEXT NOT NULL,
    start_time          TEXT NOT NULL,
    last_activity       TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'OPEN'
);
CREATE INDEX IF NOT EXISTS ix_conv_customer  ON conversations(customer_identifier);
CREATE INDEX IF NOT EXISTS ix_conv_activity  ON conversations(last_activity);
CREATE INDEX IF NOT EXISTS ix_conv_start     ON conversations(start_time);

CREATE TABLE IF NOT EXISTS messages (
    message_id       TEXT PRIMARY KEY,
    conversation_id  TEXT NOT NULL REFERENCES conversations(conversation_id),
    direction        TEXT NOT NULL,          -- IN | OUT
    message_text     TEXT NOT NULL,
    timestamp        TEXT NOT NULL,
    source           TEXT,                   -- FAQ | RULE | AI | ESCALATION | MANAGER
    response_time_ms INTEGER
);
CREATE INDEX IF NOT EXISTS ix_msg_conv ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS ix_msg_time ON messages(timestamp);

CREATE TABLE IF NOT EXISTS conversation_intelligence (
    conversation_id        TEXT PRIMARY KEY
                           REFERENCES conversations(conversation_id),
    primary_intent         TEXT,
    service                TEXT,
    sub_topic              TEXT,
    buying_intent          TEXT,             -- LOW | MEDIUM | HIGH
    lead_status            TEXT,
    booking_status         TEXT,
    human_escalation       INTEGER NOT NULL DEFAULT 0,
    bot_confidence         REAL,
    knowledge_base_coverage INTEGER NOT NULL DEFAULT 0,
    resolution_status      TEXT,
    updated_at             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ci_service   ON conversation_intelligence(service);
CREATE INDEX IF NOT EXISTS ix_ci_intent    ON conversation_intelligence(primary_intent);
CREATE INDEX IF NOT EXISTS ix_ci_escalate  ON conversation_intelligence(human_escalation);
CREATE INDEX IF NOT EXISTS ix_ci_buying    ON conversation_intelligence(buying_intent);

CREATE TABLE IF NOT EXISTS leads (
    lead_id             TEXT PRIMARY KEY,
    conversation_id     TEXT NOT NULL REFERENCES conversations(conversation_id),
    customer_identifier TEXT NOT NULL,
    customer_name       TEXT,
    phone               TEXT,
    car_brand           TEXT,
    car_model           TEXT,
    variant_year        TEXT,
    service             TEXT,
    requirement         TEXT,
    preferred_date      TEXT,
    preferred_time      TEXT,
    pickup_drop         INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE(conversation_id)
);
CREATE INDEX IF NOT EXISTS ix_lead_status   ON leads(status);
CREATE INDEX IF NOT EXISTS ix_lead_service  ON leads(service);
CREATE INDEX IF NOT EXISTS ix_lead_created  ON leads(created_at);

CREATE TABLE IF NOT EXISTS knowledge_gaps (
    gap_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    topic            TEXT NOT NULL UNIQUE,   -- normalised, e.g. "PPF warranty"
    sample_question  TEXT NOT NULL,
    category         TEXT,
    service          TEXT,
    occurrences      INTEGER NOT NULL DEFAULT 1,
    first_seen       TEXT NOT NULL,
    last_seen        TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'INFORMATION_REQUIRED'
);
CREATE INDEX IF NOT EXISTS ix_gap_occ  ON knowledge_gaps(occurrences DESC);
CREATE INDEX IF NOT EXISTS ix_gap_seen ON knowledge_gaps(last_seen);

CREATE TABLE IF NOT EXISTS faq_entries (
    id                  INTEGER PRIMARY KEY,
    question            TEXT NOT NULL,
    answer              TEXT,
    category            TEXT,
    source              TEXT,
    confirmed           INTEGER NOT NULL,
    escalation_required INTEGER NOT NULL,
    service             TEXT,
    intent              TEXT
);
CREATE INDEX IF NOT EXISTS ix_faq_service ON faq_entries(service);

-- Per-conversation dialogue state: what the customer asked for, and which
-- slot (car model / year) the bot is currently waiting on. This is what lets
-- "kia seltos" be understood as the answer to "which car?" rather than as a
-- brand-new enquiry. One row per conversation, never shared across them.
CREATE TABLE IF NOT EXISTS conversation_state (
    conversation_id TEXT PRIMARY KEY REFERENCES conversations(conversation_id),
    state_json      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- Durable duplicate protection. Meta re-delivers an event if it does not
-- get a 200 quickly enough; the in-memory deque in bot.py is the fast path
-- and this table survives a restart.
CREATE TABLE IF NOT EXISTS processed_events (
    mid      TEXT PRIMARY KEY,
    seen_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_event_seen ON processed_events(seen_at);

-- Which commenter opened each Instagram comment thread we answered. A reply
-- inside a thread is answered only when it comes from that same commenter;
-- a friend chatting in the thread is not our customer.
CREATE TABLE IF NOT EXISTS comment_threads (
    comment_id   TEXT PRIMARY KEY,
    commenter_id TEXT NOT NULL,
    media_id     TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
"""


def init_db() -> None:
    """Create the schema if it does not exist, and sync the FAQ table."""
    conn = get_connection()
    with _write_lock:
        conn.executescript(SCHEMA)
        conn.commit()
    sync_faqs()


def sync_faqs() -> int:
    """Mirror knowledge.APPROVED_FAQS into the database.

    The Python list stays the editable source of truth; this copy exists so
    the dashboard can join and filter FAQs in SQL. Safe to run repeatedly.
    """
    import knowledge

    conn = get_connection()
    rows = [
        (f["id"], f["question"], f["answer"], f["category"], f["source"],
         1 if f["confirmed"] else 0, 1 if f["escalation_required"] else 0,
         f["service"], f["intent"])
        for f in knowledge.APPROVED_FAQS
    ]
    with _write_lock:
        conn.executemany(
            "INSERT INTO faq_entries "
            "(id, question, answer, category, source, confirmed, "
            " escalation_required, service, intent) "
            "VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "  question=excluded.question, answer=excluded.answer, "
            "  category=excluded.category, source=excluded.source, "
            "  confirmed=excluded.confirmed, "
            "  escalation_required=excluded.escalation_required, "
            "  service=excluded.service, intent=excluded.intent",
            rows,
        )
        conn.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# DUPLICATE EVENT PROTECTION
# ---------------------------------------------------------------------------
def event_already_seen(mid: str) -> bool:
    """Record a Meta message id; True if we had already recorded it.

    The INSERT is the test: the primary key makes a second insert fail, and
    that failure is the duplicate signal. Doing it in one statement avoids a
    check-then-write race between worker threads.
    """
    if not mid:
        return False
    conn = get_connection()
    with _write_lock:
        try:
            conn.execute(
                "INSERT INTO processed_events (mid, seen_at) VALUES (?, ?)",
                (mid, now_iso()),
            )
            conn.commit()
            return False
        except sqlite3.IntegrityError:
            conn.rollback()          # never leave the write lock held
            return True


def event_recorded(mid: str) -> bool:
    """Read-only: has this id been recorded? (event_already_seen CLAIMS it.)"""
    if not mid:
        return False
    row = get_connection().execute(
        "SELECT 1 FROM processed_events WHERE mid = ?", (mid,)).fetchone()
    return row is not None


def forget_event(mid: str) -> None:
    """Release a claimed id so a redelivery can be processed again (used when
    a reply could not be delivered)."""
    if not mid:
        return
    conn = get_connection()
    with _write_lock:
        conn.execute("DELETE FROM processed_events WHERE mid = ?", (mid,))
        conn.commit()


def remember_thread(comment_id: str, commenter_id: str, media_id: str) -> None:
    """Record who opened a comment thread we are answering."""
    if not comment_id:
        return
    conn = get_connection()
    with _write_lock:
        conn.execute(
            "INSERT OR IGNORE INTO comment_threads (comment_id, commenter_id, media_id, created_at) "
            "VALUES (?, ?, ?, ?)", (comment_id, commenter_id, media_id, now_iso()))
        conn.commit()


def thread_owner(comment_id: str) -> Optional[str]:
    """The commenter who opened this thread, if we answered it."""
    if not comment_id:
        return None
    row = get_connection().execute(
        "SELECT commenter_id FROM comment_threads WHERE comment_id = ?", (comment_id,)).fetchone()
    return row["commenter_id"] if row else None


def prune_processed_events(days: int = 7) -> int:
    """Drop old dedup rows so the table cannot grow without bound."""
    cutoff = iso(utc_now() - timedelta(days=days))
    conn = get_connection()
    with _write_lock:
        cur = conn.execute(
            "DELETE FROM processed_events WHERE seen_at < ?", (cutoff,))
        conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------------------
# CONVERSATIONS
# ---------------------------------------------------------------------------
def get_or_create_conversation(customer_identifier: str) -> str:
    """Return the id of the customer's open conversation, creating if needed.

    The lookup and the insert happen under one lock, so two concurrent first
    messages from a customer can never create two open conversations.
    """
    with _write_lock:
        return _get_or_create_conversation_locked(customer_identifier)


def _get_or_create_conversation_locked(customer_identifier: str) -> str:
    """Return the id of the customer's open conversation, creating if needed.

    A conversation is considered the same one while the customer keeps
    replying within CONTEXT_TTL_MINUTES. After that gap the next message
    starts a fresh conversation, which is what makes "conversations" a
    meaningful unit to count.
    """
    conn = get_connection()
    cutoff = iso(utc_now() - timedelta(minutes=config.CONTEXT_TTL_MINUTES))

    row = conn.execute(
        "SELECT conversation_id FROM conversations "
        "WHERE customer_identifier = ? AND status = 'OPEN' "
        "  AND last_activity >= ? "
        "ORDER BY last_activity DESC LIMIT 1",
        (customer_identifier, cutoff),
    ).fetchone()
    if row:
        return row["conversation_id"]

    conversation_id = uuid.uuid4().hex
    stamp = now_iso()
    with _write_lock:
        # Close any stale conversation so it stops being picked up.
        conn.execute(
            "UPDATE conversations SET status = 'CLOSED' "
            "WHERE customer_identifier = ? AND status = 'OPEN'",
            (customer_identifier,),
        )
        conn.execute(
            "INSERT INTO conversations "
            "(conversation_id, customer_identifier, start_time, "
            " last_activity, status) VALUES (?,?,?,?,'OPEN')",
            (conversation_id, customer_identifier, stamp, stamp),
        )
        conn.commit()
    return conversation_id


def touch_conversation(conversation_id: str) -> None:
    conn = get_connection()
    with _write_lock:
        conn.execute(
            "UPDATE conversations SET last_activity = ? "
            "WHERE conversation_id = ?", (now_iso(), conversation_id))
        conn.commit()


def add_message(conversation_id: str, direction: str, text: str,
                source: Optional[str] = None,
                response_time_ms: Optional[int] = None,
                message_id: Optional[str] = None) -> str:
    conn = get_connection()
    mid = message_id or uuid.uuid4().hex
    with _write_lock:
        conn.execute(
            "INSERT OR REPLACE INTO messages "
            "(message_id, conversation_id, direction, message_text, "
            " timestamp, source, response_time_ms) VALUES (?,?,?,?,?,?,?)",
            (mid, conversation_id, direction, text, now_iso(), source,
             response_time_ms),
        )
        conn.execute(
            "UPDATE conversations SET last_activity = ? "
            "WHERE conversation_id = ?", (now_iso(), conversation_id))
        conn.commit()
    return mid


def get_messages(conversation_id: str, limit: int = 100) -> List[sqlite3.Row]:
    conn = get_connection()
    return conn.execute(
        "SELECT * FROM messages WHERE conversation_id = ? "
        "ORDER BY timestamp ASC, rowid ASC LIMIT ?",
        (conversation_id, limit),
    ).fetchall()


def recent_customer_messages(conversation_id: str, limit: int = 6) -> List[str]:
    """The customer's own recent lines - used to rebuild conversation context."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT message_text FROM messages "
        "WHERE conversation_id = ? AND direction = 'IN' "
        "ORDER BY rowid DESC LIMIT ?",
        (conversation_id, limit),
    ).fetchall()
    return [r["message_text"] for r in reversed(rows)]


# ---------------------------------------------------------------------------
# CONVERSATION STATE  (pending intent / slot filling)
# ---------------------------------------------------------------------------
def get_state(conversation_id: str) -> Dict[str, Any]:
    """The saved dialogue state for one conversation, or {} if none."""
    conn = get_connection()
    row = conn.execute(
        "SELECT state_json FROM conversation_state WHERE conversation_id = ?",
        (conversation_id,)).fetchone()
    if not row:
        return {}
    try:
        return json.loads(row["state_json"]) or {}
    except (TypeError, ValueError):
        return {}


def set_state(conversation_id: str, state: Dict[str, Any]) -> None:
    conn = get_connection()
    with _write_lock:
        conn.execute(
            "INSERT INTO conversation_state (conversation_id, state_json, "
            "updated_at) VALUES (?,?,?) "
            "ON CONFLICT(conversation_id) DO UPDATE SET "
            "  state_json = excluded.state_json, updated_at = excluded.updated_at",
            (conversation_id, json.dumps(state, ensure_ascii=False), now_iso()))
        conn.commit()


# ---------------------------------------------------------------------------
# CONVERSATION INTELLIGENCE
# ---------------------------------------------------------------------------
def upsert_intelligence(conversation_id: str, **fields: Any) -> None:
    """Insert or update the analytics row for a conversation.

    Only the fields supplied are written, so a later message can refine the
    classification (for example raising buying intent) without wiping what
    an earlier message established.
    """
    allowed = {
        "primary_intent", "service", "sub_topic", "buying_intent",
        "lead_status", "booking_status", "human_escalation",
        "bot_confidence", "knowledge_base_coverage", "resolution_status",
    }
    data = {k: v for k, v in fields.items() if k in allowed and v is not None}
    conn = get_connection()
    with _write_lock:
        conn.execute(
            "INSERT OR IGNORE INTO conversation_intelligence "
            "(conversation_id, updated_at) VALUES (?, ?)",
            (conversation_id, now_iso()))
        if data:
            sets = ", ".join(f"{k} = ?" for k in data)
            conn.execute(
                f"UPDATE conversation_intelligence SET {sets}, updated_at = ? "
                "WHERE conversation_id = ?",
                (*data.values(), now_iso(), conversation_id))
        conn.commit()


def get_intelligence(conversation_id: str) -> Optional[sqlite3.Row]:
    conn = get_connection()
    return conn.execute(
        "SELECT * FROM conversation_intelligence WHERE conversation_id = ?",
        (conversation_id,)).fetchone()


# ---------------------------------------------------------------------------
# LEADS
# ---------------------------------------------------------------------------
def upsert_lead(conversation_id: str, customer_identifier: str,
                **fields: Any) -> str:
    """Create or enrich the lead attached to a conversation.

    One lead per conversation (enforced by a UNIQUE constraint). Fields are
    only overwritten when the new value is non-empty, so a detail captured
    early - say the car model - is never erased by a later vaguer message.
    """
    allowed = {
        "customer_name", "phone", "car_brand", "car_model", "variant_year",
        "service", "requirement", "preferred_date", "preferred_time",
        "pickup_drop", "status",
    }
    data = {k: v for k, v in fields.items() if k in allowed and v not in (None, "")}
    conn = get_connection()
    stamp = now_iso()

    with _write_lock:
        row = conn.execute(
            "SELECT lead_id FROM leads WHERE conversation_id = ?",
            (conversation_id,)).fetchone()
        if row:
            lead_id = row["lead_id"]
            if data:
                sets = ", ".join(f"{k} = ?" for k in data)
                conn.execute(
                    f"UPDATE leads SET {sets}, updated_at = ? "
                    "WHERE lead_id = ?", (*data.values(), stamp, lead_id))
        else:
            lead_id = uuid.uuid4().hex
            # Pull status out of `data` so it is supplied exactly once, then
            # append whatever optional fields remain.
            status = data.pop("status", "NEW")
            cols = ["lead_id", "conversation_id", "customer_identifier",
                    "status", "created_at", "updated_at"] + list(data)
            vals = [lead_id, conversation_id, customer_identifier,
                    status, stamp, stamp] + list(data.values())
            placeholders = ",".join("?" * len(cols))
            conn.execute(
                f"INSERT INTO leads ({','.join(cols)}) VALUES ({placeholders})",
                vals)
        conn.commit()
    return lead_id


def get_lead(conversation_id: str) -> Optional[sqlite3.Row]:
    conn = get_connection()
    return conn.execute(
        "SELECT * FROM leads WHERE conversation_id = ?",
        (conversation_id,)).fetchone()


# ---------------------------------------------------------------------------
# KNOWLEDGE GAPS
# ---------------------------------------------------------------------------
def record_knowledge_gap(topic: str, sample_question: str,
                         category: Optional[str] = None,
                         service: Optional[str] = None) -> None:
    """Record that we could not confidently answer something.

    Similar questions are normalised to the same `topic` by brain.py, so
    "PPF warranty?", "PPF ki warranty kya hai?" and "PPF kitne saal ki
    warranty?" all increment ONE row rather than creating three.
    """
    conn = get_connection()
    stamp = now_iso()
    with _write_lock:
        conn.execute(
            "INSERT INTO knowledge_gaps "
            "(topic, sample_question, category, service, occurrences, "
            " first_seen, last_seen) VALUES (?,?,?,?,1,?,?) "
            "ON CONFLICT(topic) DO UPDATE SET "
            "  occurrences = occurrences + 1, last_seen = excluded.last_seen",
            (topic, sample_question, category, service, stamp, stamp))
        conn.commit()


def list_knowledge_gaps(limit: int = 100, offset: int = 0) -> List[sqlite3.Row]:
    conn = get_connection()
    return conn.execute(
        "SELECT * FROM knowledge_gaps ORDER BY occurrences DESC, last_seen DESC "
        "LIMIT ? OFFSET ?", (limit, offset)).fetchall()


# ---------------------------------------------------------------------------
# ANALYTICS - every number below is computed from real rows
# ---------------------------------------------------------------------------
def _range_clause(days: Optional[int], start: Optional[str],
                  end: Optional[str], column: str) -> Tuple[str, list]:
    """Build a WHERE fragment for a date filter. Empty when unfiltered."""
    if start and end:
        return f" AND {column} BETWEEN ? AND ?", [start, end]
    if days is not None:
        return f" AND {column} >= ?", [iso(utc_now() - timedelta(days=days))]
    return "", []


def overview_metrics(days: Optional[int] = None, start: Optional[str] = None,
                     end: Optional[str] = None) -> Dict[str, Any]:
    """Headline dashboard numbers, aggregated in SQL (never in Python)."""
    conn = get_connection()
    where, params = _range_clause(days, start, end, "c.start_time")

    def scalar(sql: str, extra: Optional[list] = None) -> int:
        row = conn.execute(sql, (extra if extra is not None else params)).fetchone()
        return row[0] or 0

    total_conversations = scalar(
        f"SELECT COUNT(*) FROM conversations c WHERE 1=1{where}")
    unique_customers = scalar(
        f"SELECT COUNT(DISTINCT c.customer_identifier) FROM conversations c "
        f"WHERE 1=1{where}")

    lead_where, lead_params = _range_clause(days, start, end, "l.created_at")
    total_leads = scalar(
        f"SELECT COUNT(*) FROM leads l WHERE 1=1{lead_where}", lead_params)
    booking_requests = scalar(
        f"SELECT COUNT(*) FROM leads l "
        f"WHERE l.status = 'BOOKING_REQUESTED'{lead_where}", lead_params)

    escalations = scalar(
        f"SELECT COUNT(*) FROM conversation_intelligence ci "
        f"JOIN conversations c USING(conversation_id) "
        f"WHERE ci.human_escalation = 1{where}")
    unknown_questions = scalar(
        f"SELECT COUNT(*) FROM conversation_intelligence ci "
        f"JOIN conversations c USING(conversation_id) "
        f"WHERE ci.knowledge_base_coverage = 0{where}")
    low_confidence = scalar(
        f"SELECT COUNT(*) FROM conversation_intelligence ci "
        f"JOIN conversations c USING(conversation_id) "
        f"WHERE ci.bot_confidence IS NOT NULL "
        f"  AND ci.bot_confidence < {config.LOW_CONFIDENCE}{where}")

    conversion = (round(total_leads / total_conversations * 100, 1)
                  if total_conversations else 0.0)

    return {
        "total_conversations": total_conversations,
        "unique_customers": unique_customers,
        "total_leads": total_leads,
        "booking_requests": booking_requests,
        "human_escalations": escalations,
        "unknown_questions": unknown_questions,
        "low_confidence": low_confidence,
        "conversion_rate": conversion,
    }


def service_breakdown(days: Optional[int] = None, start: Optional[str] = None,
                      end: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = get_connection()
    where, params = _range_clause(days, start, end, "c.start_time")
    rows = conn.execute(
        f"SELECT ci.service AS service, COUNT(*) AS n "
        f"FROM conversation_intelligence ci "
        f"JOIN conversations c USING(conversation_id) "
        f"WHERE ci.service IS NOT NULL{where} "
        f"GROUP BY ci.service ORDER BY n DESC", params).fetchall()
    return [{"service": r["service"], "conversations": r["n"]} for r in rows]


def price_question_breakdown(days: Optional[int] = None,
                             start: Optional[str] = None,
                             end: Optional[str] = None) -> List[Dict[str, Any]]:
    """Which services customers ask PRICES about, as opposed to general info."""
    conn = get_connection()
    where, params = _range_clause(days, start, end, "c.start_time")
    rows = conn.execute(
        f"SELECT ci.service AS service, COUNT(*) AS n "
        f"FROM conversation_intelligence ci "
        f"JOIN conversations c USING(conversation_id) "
        f"WHERE ci.primary_intent = 'PRICE_INQUIRY'{where} "
        f"GROUP BY ci.service ORDER BY n DESC", params).fetchall()
    return [{"service": r["service"], "price_questions": r["n"]} for r in rows]


def intent_breakdown(days: Optional[int] = None, start: Optional[str] = None,
                     end: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = get_connection()
    where, params = _range_clause(days, start, end, "c.start_time")
    rows = conn.execute(
        f"SELECT ci.primary_intent AS intent, COUNT(*) AS n "
        f"FROM conversation_intelligence ci "
        f"JOIN conversations c USING(conversation_id) "
        f"WHERE ci.primary_intent IS NOT NULL{where} "
        f"GROUP BY ci.primary_intent ORDER BY n DESC", params).fetchall()
    return [{"intent": r["intent"], "conversations": r["n"]} for r in rows]


def ai_performance(days: Optional[int] = None, start: Optional[str] = None,
                   end: Optional[str] = None) -> Dict[str, Any]:
    conn = get_connection()
    where, params = _range_clause(days, start, end, "c.start_time")
    row = conn.execute(
        f"SELECT COUNT(*) AS total, "
        f"       AVG(ci.bot_confidence) AS avg_conf, "
        f"       SUM(ci.human_escalation) AS escalated, "
        f"       SUM(CASE WHEN ci.knowledge_base_coverage = 1 THEN 1 ELSE 0 END) AS covered, "
        f"       SUM(CASE WHEN ci.resolution_status = 'UNRESOLVED' THEN 1 ELSE 0 END) AS unresolved "
        f"FROM conversation_intelligence ci "
        f"JOIN conversations c USING(conversation_id) WHERE 1=1{where}",
        params).fetchone()

    total = row["total"] or 0
    if not total:
        return {"total": 0, "average_confidence": 0.0, "escalation_rate": 0.0,
                "unknown_rate": 0.0, "knowledge_coverage": 0.0,
                "unresolved": 0}
    return {
        "total": total,
        "average_confidence": round(row["avg_conf"] or 0.0, 3),
        "escalation_rate": round((row["escalated"] or 0) / total * 100, 1),
        "unknown_rate": round((total - (row["covered"] or 0)) / total * 100, 1),
        "knowledge_coverage": round((row["covered"] or 0) / total * 100, 1),
        "unresolved": row["unresolved"] or 0,
    }


def search_conversations(service: Optional[str] = None,
                         intent: Optional[str] = None,
                         lead_status: Optional[str] = None,
                         escalated: Optional[bool] = None,
                         customer: Optional[str] = None,
                         keyword: Optional[str] = None,
                         days: Optional[int] = None,
                         start: Optional[str] = None,
                         end: Optional[str] = None,
                         limit: int = 50,
                         offset: int = 0) -> Tuple[List[Dict[str, Any]], int]:
    """Paginated conversation search. Returns (rows, total_matching)."""
    conn = get_connection()
    clauses, params = [], []

    if service:
        clauses.append("ci.service = ?"); params.append(service)
    if intent:
        clauses.append("ci.primary_intent = ?"); params.append(intent)
    if lead_status:
        clauses.append("ci.lead_status = ?"); params.append(lead_status)
    if escalated is not None:
        clauses.append("ci.human_escalation = ?"); params.append(1 if escalated else 0)
    if customer:
        clauses.append("c.customer_identifier LIKE ?"); params.append(f"%{customer}%")
    if keyword:
        clauses.append(
            "EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id = "
            "c.conversation_id AND m.message_text LIKE ?)")
        params.append(f"%{keyword}%")

    where_extra, range_params = _range_clause(days, start, end, "c.start_time")
    if where_extra:
        clauses.append(where_extra.replace(" AND ", "", 1))
        params.extend(range_params)

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    base = ("FROM conversations c "
            "LEFT JOIN conversation_intelligence ci USING(conversation_id)"
            + where)

    total = conn.execute(f"SELECT COUNT(*) {base}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT c.conversation_id, c.customer_identifier, c.start_time, "
        f"       c.last_activity, c.status, ci.primary_intent, ci.service, "
        f"       ci.buying_intent, ci.lead_status, ci.booking_status, "
        f"       ci.human_escalation, ci.bot_confidence, ci.resolution_status, "
        f"       (SELECT COUNT(*) FROM messages m "
        f"        WHERE m.conversation_id = c.conversation_id) AS message_count "
        f"{base} ORDER BY c.last_activity DESC LIMIT ? OFFSET ?",
        (*params, limit, offset)).fetchall()
    return [dict(r) for r in rows], total


def list_leads(status: Optional[str] = None, limit: int = 50,
               offset: int = 0) -> Tuple[List[Dict[str, Any]], int]:
    conn = get_connection()
    where, params = ("", [])
    if status:
        where, params = " WHERE status = ?", [status]
    total = conn.execute(
        f"SELECT COUNT(*) FROM leads{where}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM leads{where} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
        (*params, limit, offset)).fetchall()
    return [dict(r) for r in rows], total


def database_is_empty() -> bool:
    conn = get_connection()
    return conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0
