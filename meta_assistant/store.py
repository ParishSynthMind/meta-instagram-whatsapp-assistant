"""SQLite state: seen events (dedupe), conversation history, human takeovers,
and a log of every reply or skip. Each log row is also appended to a JSONL
file so there's a plain-text record that survives outside the database."""
import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY, channel TEXT, received_at REAL
);
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT, user_id TEXT,
    role TEXT, text TEXT, ts REAL
);
CREATE TABLE IF NOT EXISTS takeovers (
    channel TEXT, user_id TEXT, until_ts REAL, reason TEXT,
    PRIMARY KEY (channel, user_id)
);
CREATE TABLE IF NOT EXISTS reply_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, channel TEXT, user_id TEXT,
    event_id TEXT, inbound TEXT, reply TEXT, action TEXT, reason TEXT,
    sent INTEGER, error TEXT
);
CREATE TABLE IF NOT EXISTS leads (
    leadgen_id TEXT PRIMARY KEY, ts REAL, page_id TEXT, form_id TEXT, ad_id TEXT,
    created_time TEXT, fields TEXT, status TEXT, error TEXT
);
CREATE INDEX IF NOT EXISTS idx_history_user ON history (channel, user_id, id);
CREATE INDEX IF NOT EXISTS idx_log_user ON reply_log (user_id, ts);
"""


class Store:
    def __init__(self, db_path: str = ":memory:", log_path: str | None = None, clock=time.time,
                 lead_log_path: str | None = None):
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.executescript(SCHEMA)
        self.lock = threading.Lock()
        self.log_path = Path(log_path) if log_path else None
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.lead_log_path = Path(lead_log_path) if lead_log_path else None
        if self.lead_log_path:
            self.lead_log_path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock

    # Dedupe -----------------------------------------------------------------
    def claim_event(self, event_id: str, channel: str) -> bool:
        """True the first time an event ID is seen; False on every retry after."""
        with self.lock, self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO events VALUES (?, ?, ?)", (event_id, channel, self.clock())
            )
            return cur.rowcount == 1

    # History ----------------------------------------------------------------
    def add_history(self, channel: str, user_id: str, role: str, text: str) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO history (channel, user_id, role, text, ts) VALUES (?, ?, ?, ?, ?)",
                (channel, user_id, role, text, self.clock()),
            )

    def recent_history(self, channel: str, user_id: str, turns: int) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT role, text FROM history WHERE channel=? AND user_id=? ORDER BY id DESC LIMIT ?",
                (channel, user_id, turns * 2),
            ).fetchall()
        return [{"role": r, "text": t} for r, t in reversed(rows)]

    # Human takeover ---------------------------------------------------------
    def set_takeover(self, channel: str, user_id: str, hours: float, reason: str) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO takeovers VALUES (?, ?, ?, ?)",
                (channel, user_id, self.clock() + hours * 3600, reason),
            )

    def clear_takeover(self, channel: str, user_id: str) -> None:
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM takeovers WHERE channel=? AND user_id=?", (channel, user_id))

    def takeover_active(self, channel: str, user_id: str) -> bool:
        with self.lock:
            row = self.conn.execute(
                "SELECT until_ts FROM takeovers WHERE channel=? AND user_id=?", (channel, user_id)
            ).fetchone()
        return bool(row and row[0] > self.clock())

    # Rate limits ------------------------------------------------------------
    def replies_since(self, since_ts: float, user_id: str | None = None) -> int:
        # Counts every reply produced without an error, so dry runs are limited too.
        sql = "SELECT COUNT(*) FROM reply_log WHERE reply != '' AND error = '' AND ts>=?"
        args: list = [since_ts]
        if user_id is not None:
            sql += " AND user_id=?"
            args.append(user_id)
        with self.lock:
            return self.conn.execute(sql, args).fetchone()[0]

    # Reply log --------------------------------------------------------------
    def log(self, *, channel: str, user_id: str, event_id: str, inbound: str, reply: str,
            action: str, reason: str = "", sent: bool = False, error: str = "") -> dict:
        row = {
            "ts": self.clock(), "channel": channel, "user_id": user_id, "event_id": event_id,
            "inbound": inbound, "reply": reply, "action": action, "reason": reason,
            "sent": sent, "error": error,
        }
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO reply_log (ts, channel, user_id, event_id, inbound, reply, action, reason, sent, error)"
                " VALUES (:ts, :channel, :user_id, :event_id, :inbound, :reply, :action, :reason, :sent, :error)",
                {**row, "sent": int(sent)},
            )
            if self.log_path:
                with self.log_path.open("a") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row

    # Lead Ads ---------------------------------------------------------------
    def save_lead(self, *, leadgen_id: str, page_id: str, form_id: str, ad_id: str, created_time: str,
                  fields: dict, status: str, error: str = "") -> dict:
        row = {
            "ts": self.clock(), "leadgen_id": leadgen_id, "page_id": page_id, "form_id": form_id,
            "ad_id": ad_id, "created_time": created_time, "fields": fields, "status": status, "error": error,
        }
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO leads VALUES (:leadgen_id, :ts, :page_id, :form_id, :ad_id,"
                " :created_time, :fields, :status, :error)",
                {**row, "fields": json.dumps(fields, ensure_ascii=False)},
            )
            if self.lead_log_path:
                with self.lead_log_path.open("a") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row

    def all_leads(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT leadgen_id, page_id, form_id, ad_id, created_time, fields, status, error"
                " FROM leads ORDER BY ts").fetchall()
        cols = ["leadgen_id", "page_id", "form_id", "ad_id", "created_time", "fields", "status", "error"]
        return [{**dict(zip(cols, r)), "fields": json.loads(r[5])} for r in rows]

    def all_logs(self) -> list[dict]:
        with self.lock:
            cur = self.conn.execute(
                "SELECT ts, channel, user_id, event_id, inbound, reply, action, reason, sent, error"
                " FROM reply_log ORDER BY id"
            )
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
