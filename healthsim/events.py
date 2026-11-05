"""Durable append-only event record and local source snapshots (SQLite).

Event ids are stable, and inserts ignore duplicates, so a retried or resumed task
cannot record the same event twice. Nothing is ever updated or deleted.
"""

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path


def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds") if ts else None


class EventStore:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
                debate_id TEXT NOT NULL, at REAL NOT NULL, role TEXT NOT NULL, round INTEGER NOT NULL,
                phase TEXT NOT NULL, visibility TEXT NOT NULL, type TEXT NOT NULL,
                refs TEXT NOT NULL, causes TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS events_debate ON events(debate_id, seq);
            CREATE TABLE IF NOT EXISTS passages (
                debate_id TEXT NOT NULL, passage_id TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY (debate_id, passage_id));
            CREATE TABLE IF NOT EXISTS debates (
                debate_id TEXT PRIMARY KEY, request TEXT NOT NULL, created_at REAL NOT NULL,
                result TEXT);
        """)
        self._db.commit()

    def close(self):
        with self._lock:
            self._db.close()

    def emit(self, debate_id, event_id, at, role, round_no, phase, visibility, type_, refs=(), causes=(),
             payload=None):
        """Record an event once. Returns True if it was new."""
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO events (event_id, debate_id, at, role, round, phase, visibility, "
                "type, refs, causes, payload) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (f"{debate_id}:{event_id}", debate_id, at, role, round_no, phase, visibility, type_,
                 json.dumps(list(refs)), json.dumps(list(causes)),
                 json.dumps(payload or {}, ensure_ascii=False, default=str)))
            self._db.commit()
            return cur.rowcount == 1

    def events(self, debate_id):
        with self._lock:
            rows = self._db.execute(
                "SELECT event_id, at, role, round, phase, visibility, type, refs, causes, payload "
                "FROM events WHERE debate_id=? ORDER BY seq", (debate_id,)).fetchall()
        prefix = len(debate_id) + 1
        return [{"event_id": r[0][prefix:], "at": r[1], "role": r[2], "round": r[3], "phase": r[4],
                 "visibility": r[5], "type": r[6], "refs": json.loads(r[7]), "causes": json.loads(r[8]),
                 "payload": json.loads(r[9])} for r in rows]

    def save_passage(self, debate_id, passage):
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO passages VALUES (?,?,?)",
                             (debate_id, passage["passage_id"], json.dumps(passage, ensure_ascii=False)))
            self._db.commit()

    def passage(self, debate_id, passage_id):
        with self._lock:
            row = self._db.execute("SELECT payload FROM passages WHERE debate_id=? AND passage_id=?",
                                   (debate_id, passage_id)).fetchone()
        return json.loads(row[0]) if row else None

    def save_debate(self, debate_id, request_json, created_at):
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO debates (debate_id, request, created_at) VALUES (?,?,?)",
                             (debate_id, request_json, created_at))
            self._db.commit()

    def save_result(self, debate_id, result_json):
        with self._lock:
            self._db.execute("UPDATE debates SET result=? WHERE debate_id=?", (result_json, debate_id))
            self._db.commit()

    def debate(self, debate_id):
        with self._lock:
            row = self._db.execute("SELECT request, result FROM debates WHERE debate_id=?",
                                   (debate_id,)).fetchone()
        return {"request": row[0], "result": row[1]} if row else None
