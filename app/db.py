"""SQLite 영속 저장소.

계정, 통화 기록, 예약을 저장한다. 스레드 간 공유를 위해
check_same_thread=False + lock 으로 직렬화한다.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "callprotocol.db")

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
    return _conn


def init_db():
    with _lock:
        conn = _connect()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                display_name TEXT,
                accessibility TEXT,          -- JSON 설정
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                phone TEXT NOT NULL,
                purpose TEXT,                -- hospital / restaurant / store
                plan TEXT,                   -- JSON 통화 계획
                transcript TEXT,             -- JSON 자막 로그
                summary TEXT,
                outcome TEXT,                -- success / failed / needs_action
                extracted TEXT,              -- JSON 추출 정보
                mode TEXT,                   -- auto / assisted / manual
                status TEXT,                 -- planning / in_call / ended
                created_at REAL NOT NULL,
                ended_at REAL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS reservations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                phone TEXT NOT NULL,
                purpose TEXT,
                plan TEXT,                   -- JSON
                scheduled_at REAL NOT NULL,  -- 발신 예약 시각(epoch)
                status TEXT NOT NULL,        -- scheduled / dispatched / done / canceled / failed
                call_id INTEGER,             -- 발신 후 연결된 call
                created_at REAL NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );
            """
        )
        conn.commit()


# ---------- Users ----------
def create_user(email, password_hash, salt, display_name) -> int:
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "INSERT INTO users(email, password_hash, salt, display_name, accessibility, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (email, password_hash, salt, display_name, json.dumps({}), time.time()),
        )
        conn.commit()
        return cur.lastrowid


def get_user_by_email(email):
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        return dict(row) if row else None


def get_user(user_id):
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None


def update_accessibility(user_id, settings: dict):
    with _lock:
        conn = _connect()
        conn.execute(
            "UPDATE users SET accessibility=? WHERE id=?",
            (json.dumps(settings), user_id),
        )
        conn.commit()


def set_credentials(user_id, password_hash, salt):
    with _lock:
        conn = _connect()
        conn.execute(
            "UPDATE users SET password_hash=?, salt=? WHERE id=?",
            (password_hash, salt, user_id),
        )
        conn.commit()


# ---------- Sessions ----------
def create_session(token, user_id):
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT INTO sessions(token, user_id, created_at) VALUES(?,?,?)",
            (token, user_id, time.time()),
        )
        conn.commit()


def get_session_user(token):
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT user_id FROM sessions WHERE token=?", (token,)).fetchone()
        return row["user_id"] if row else None


def delete_session(token):
    with _lock:
        conn = _connect()
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()


# ---------- Calls ----------
def create_call(user_id, phone, purpose, plan, mode) -> int:
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "INSERT INTO calls(user_id, phone, purpose, plan, transcript, mode, status, created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (user_id, phone, purpose, json.dumps(plan), json.dumps([]), mode, "planning", time.time()),
        )
        conn.commit()
        return cur.lastrowid


def update_call(call_id, **fields):
    if not fields:
        return
    cols = []
    vals = []
    for k, v in fields.items():
        cols.append(f"{k}=?")
        if k in ("plan", "transcript", "extracted") and not isinstance(v, str):
            v = json.dumps(v, ensure_ascii=False)
        vals.append(v)
    vals.append(call_id)
    with _lock:
        conn = _connect()
        conn.execute(f"UPDATE calls SET {','.join(cols)} WHERE id=?", vals)
        conn.commit()


def get_call(call_id):
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
        return dict(row) if row else None


def list_calls(user_id):
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM calls WHERE user_id=? ORDER BY created_at DESC", (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def delete_call(call_id, user_id):
    with _lock:
        conn = _connect()
        conn.execute("DELETE FROM calls WHERE id=? AND user_id=?", (call_id, user_id))
        conn.commit()


# ---------- Reservations ----------
def create_reservation(user_id, phone, purpose, plan, scheduled_at) -> int:
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "INSERT INTO reservations(user_id, phone, purpose, plan, scheduled_at, status, created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (user_id, phone, purpose, json.dumps(plan), scheduled_at, "scheduled", time.time()),
        )
        conn.commit()
        return cur.lastrowid


def list_reservations(user_id):
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM reservations WHERE user_id=? ORDER BY scheduled_at ASC", (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def due_reservations(now: float):
    """발신 예약 시각이 지난 scheduled 상태 예약 (중복 방지: 즉시 dispatched 전환)."""
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM reservations WHERE status='scheduled' AND scheduled_at<=?",
            (now,),
        ).fetchall()
        result = [dict(r) for r in rows]
        for r in result:
            conn.execute(
                "UPDATE reservations SET status='dispatched' WHERE id=? AND status='scheduled'",
                (r["id"],),
            )
        conn.commit()
        return result


def update_reservation(res_id, user_id=None, **fields):
    if not fields:
        return
    cols = []
    vals = []
    for k, v in fields.items():
        cols.append(f"{k}=?")
        if k == "plan" and not isinstance(v, str):
            v = json.dumps(v, ensure_ascii=False)
        vals.append(v)
    where = "id=?"
    vals.append(res_id)
    if user_id is not None:
        where += " AND user_id=?"
        vals.append(user_id)
    with _lock:
        conn = _connect()
        conn.execute(f"UPDATE reservations SET {','.join(cols)} WHERE {where}", vals)
        conn.commit()


def get_reservation(res_id):
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT * FROM reservations WHERE id=?", (res_id,)).fetchone()
        return dict(row) if row else None
