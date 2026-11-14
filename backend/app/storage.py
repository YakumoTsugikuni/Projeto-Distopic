"""Persistência local das conversas em SQLite.

Só CPU e disco: nada aqui chama o modelo. Funções síncronas — chame-as de
rotas `def` (o FastAPI as roda em thread) ou via anyio.to_thread.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from .config import settings

DB_PATH = settings.data_dir / "distopic.db"
SCHEMA_VERSION = 1
TITLE_MAX = 60

_SCHEMA = """
CREATE TABLE conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'tool')),
    content TEXT NOT NULL,
    meta TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX idx_messages_conversation ON messages(conversation_id, id);
CREATE INDEX idx_conversations_updated ON conversations(updated_at DESC);
"""


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with conn:  # uma transação: commit ao sair, rollback em erro
            yield conn
    finally:
        conn.close()


def init() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA journal_mode = WAL")  # leituras não bloqueiam a escrita
        if conn.execute("PRAGMA user_version").fetchone()[0] == 0:
            conn.executescript(_SCHEMA)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()
    finally:
        conn.close()


def _conversation(row: sqlite3.Row) -> dict:
    return dict(row)


def _message(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["meta"] = json.loads(data["meta"]) if data["meta"] else None
    return data


def _clean_title(text: str) -> str:
    return " ".join(text.split())[:TITLE_MAX]


def create_conversation() -> dict:
    now = time.time()
    conversation = {"id": uuid.uuid4().hex, "title": "", "created_at": now, "updated_at": now}
    with _db() as db:
        db.execute(
            "INSERT INTO conversations (id, title, created_at, updated_at) "
            "VALUES (:id, :title, :created_at, :updated_at)",
            conversation,
        )
    return conversation


def list_conversations(limit: int = 50, offset: int = 0) -> list[dict]:
    with _db() as db:
        rows = db.execute(
            "SELECT * FROM conversations ORDER BY updated_at DESC LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()
    return [_conversation(r) for r in rows]


def get_conversation(conversation_id: str) -> dict | None:
    with _db() as db:
        row = db.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
    return _conversation(row) if row else None


def rename_conversation(conversation_id: str, title: str) -> bool:
    with _db() as db:
        cur = db.execute(
            "UPDATE conversations SET title = ? WHERE id = ?", (_clean_title(title), conversation_id)
        )
    return cur.rowcount > 0


def delete_conversation(conversation_id: str) -> bool:
    with _db() as db:  # as mensagens saem junto (ON DELETE CASCADE)
        cur = db.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
    return cur.rowcount > 0


def add_message(conversation_id: str, role: str, content: str, meta: dict | None = None) -> int:
    now = time.time()
    with _db() as db:
        cur = db.execute(
            "INSERT INTO messages (conversation_id, role, content, meta, created_at) VALUES (?, ?, ?, ?, ?)",
            (conversation_id, role, content, json.dumps(meta, ensure_ascii=False) if meta else None, now),
        )
        # O título vem da primeira mensagem do usuário — nunca do modelo.
        db.execute(
            "UPDATE conversations SET updated_at = ?, "
            "title = CASE WHEN title = '' AND ? = 'user' THEN ? ELSE title END WHERE id = ?",
            (now, role, _clean_title(content), conversation_id),
        )
    return cur.lastrowid


def get_messages(conversation_id: str, before: int | None = None, limit: int = 50) -> tuple[list[dict], bool]:
    """Página de mensagens em ordem cronológica, terminando antes de `before`.

    Retorna (mensagens, há_mais_antigas).
    """
    with _db() as db:
        rows = db.execute(
            "SELECT id, role, content, meta, created_at FROM messages "
            "WHERE conversation_id = ? AND (? IS NULL OR id < ?) ORDER BY id DESC LIMIT ?",
            (conversation_id, before, before, limit + 1),
        ).fetchall()
    has_more = len(rows) > limit
    return [_message(r) for r in reversed(rows[:limit])], has_more


def context_messages(conversation_id: str, limit: int = 200) -> list[dict]:
    """Turnos recentes de usuário/assistente, no formato enviado ao modelo."""
    with _db() as db:
        rows = db.execute(
            "SELECT role, content FROM messages WHERE conversation_id = ? "
            "AND role IN ('user', 'assistant') ORDER BY id DESC LIMIT ?",
            (conversation_id, limit),
        ).fetchall()
    messages: list[dict] = []
    for row in reversed(rows):
        # Trechos seguidos do mesmo papel (texto antes e depois de uma ferramenta) viram um turno só.
        if messages and messages[-1]["role"] == row["role"]:
            messages[-1]["content"] += "\n" + row["content"]
        else:
            messages.append({"role": row["role"], "content": row["content"]})
    return messages
