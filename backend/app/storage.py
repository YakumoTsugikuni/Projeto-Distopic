"""Persistência local em SQLite: contas, conversas e registro de documentos.

Só CPU e disco: nada aqui chama o modelo. Funções síncronas — chame-as de
rotas `def` (o FastAPI as roda em thread) ou via anyio.to_thread.

Isolamento: toda leitura ou alteração de conversa/documento recebe o `user_id`
e o aplica no WHERE. Linhas sem dono (dados anteriores às contas) não aparecem
para ninguém até serem atribuídas por `claim_orphan_conversations`.
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
SCHEMA_VERSION = 2
TITLE_MAX = 60

_SCHEMA_V1 = """
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

# v2: contas individuais e propriedade dos dados.
_MIGRATION_V2 = """
CREATE TABLE users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
ALTER TABLE conversations ADD COLUMN user_id TEXT REFERENCES users(id) ON DELETE CASCADE;
CREATE INDEX idx_conversations_user ON conversations(user_id, updated_at DESC);
CREATE TABLE documents (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    -- NULL = Biblioteca permanente do usuário; preenchido = só daquela conversa.
    conversation_id TEXT REFERENCES conversations(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    stored_path TEXT,
    chunks INTEGER NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX idx_documents_owner ON documents(user_id, conversation_id);
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
    """Cria o banco ou o migra, passo a passo, até a versão atual."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA journal_mode = WAL")  # leituras não bloqueiam a escrita
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for target, script in ((1, _SCHEMA_V1), (2, _MIGRATION_V2)):
            if version < target:
                conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {target};\nCOMMIT;")
                version = target
    finally:
        conn.close()


# ---------- Contas ----------


def _user(row: sqlite3.Row) -> dict:
    """Dados públicos da conta — o hash da senha nunca sai daqui."""
    return {
        "id": row["id"],
        "username": row["username"],
        "is_admin": bool(row["is_admin"]),
        "created_at": row["created_at"],
    }


def count_users() -> int:
    with _db() as db:
        return db.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def create_user(username: str, password_hash: str, is_admin: bool = False) -> dict:
    user_id = uuid.uuid4().hex
    try:
        with _db() as db:
            db.execute(
                "INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, username, password_hash, int(is_admin), time.time()),
            )
    except sqlite3.IntegrityError as exc:
        raise ValueError("Já existe uma conta com esse nome.") from exc
    return get_user(user_id)


def get_user(user_id: str) -> dict | None:
    with _db() as db:
        row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _user(row) if row else None


def get_credentials(username: str) -> tuple[dict, str] | None:
    """(conta, hash da senha) para o login."""
    with _db() as db:
        row = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    return (_user(row), row["password_hash"]) if row else None


def get_password_hash(user_id: str) -> str | None:
    with _db() as db:
        row = db.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["password_hash"] if row else None


def list_users() -> list[dict]:
    with _db() as db:
        rows = db.execute("SELECT * FROM users ORDER BY created_at").fetchall()
    return [_user(r) for r in rows]


def first_admin() -> dict | None:
    with _db() as db:
        row = db.execute("SELECT * FROM users WHERE is_admin = 1 ORDER BY created_at LIMIT 1").fetchone()
    return _user(row) if row else None


def set_password_hash(user_id: str, password_hash: str) -> bool:
    with _db() as db:
        cur = db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))
    return cur.rowcount > 0


def delete_user(user_id: str) -> bool:
    with _db() as db:  # conversas, mensagens e registros de documentos saem em cascata
        cur = db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return cur.rowcount > 0


def claim_orphan_conversations(user_id: str) -> int:
    """Atribui a uma conta as conversas criadas antes de existirem contas."""
    with _db() as db:
        cur = db.execute("UPDATE conversations SET user_id = ? WHERE user_id IS NULL", (user_id,))
    return cur.rowcount


# ---------- Conversas ----------


def _conversation(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in ("id", "title", "created_at", "updated_at")}


def _message(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["meta"] = json.loads(data["meta"]) if data["meta"] else None
    return data


def _clean_title(text: str) -> str:
    return " ".join(text.split())[:TITLE_MAX]


def create_conversation(user_id: str) -> dict:
    now = time.time()
    conversation = {"id": uuid.uuid4().hex, "title": "", "created_at": now, "updated_at": now}
    with _db() as db:
        db.execute(
            "INSERT INTO conversations (id, title, created_at, updated_at, user_id) "
            "VALUES (:id, :title, :created_at, :updated_at, :user_id)",
            {**conversation, "user_id": user_id},
        )
    return conversation


def list_conversations(user_id: str, limit: int = 50, offset: int = 0) -> list[dict]:
    with _db() as db:
        rows = db.execute(
            "SELECT * FROM conversations WHERE user_id = ? ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (user_id, limit, offset),
        ).fetchall()
    return [_conversation(r) for r in rows]


def get_conversation(conversation_id: str, user_id: str) -> dict | None:
    """A conversa, somente se pertencer a `user_id`."""
    with _db() as db:
        row = db.execute(
            "SELECT * FROM conversations WHERE id = ? AND user_id = ?", (conversation_id, user_id)
        ).fetchone()
    return _conversation(row) if row else None


def rename_conversation(conversation_id: str, user_id: str, title: str) -> bool:
    with _db() as db:
        cur = db.execute(
            "UPDATE conversations SET title = ? WHERE id = ? AND user_id = ?",
            (_clean_title(title), conversation_id, user_id),
        )
    return cur.rowcount > 0


def delete_conversation(conversation_id: str, user_id: str) -> bool:
    with _db() as db:  # mensagens e registros de documentos temporários saem em cascata
        cur = db.execute(
            "DELETE FROM conversations WHERE id = ? AND user_id = ?", (conversation_id, user_id)
        )
    return cur.rowcount > 0


# As funções de mensagens abaixo não recebem user_id: chame-as só depois de
# confirmar a posse da conversa com get_conversation(conversation_id, user_id).


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


# ---------- Documentos (registro; o conteúdo fica em disco e no índice vetorial) ----------


def add_document(
    doc_id: str,
    user_id: str,
    filename: str,
    stored_path: str | None,
    chunks: int,
    conversation_id: str | None = None,
) -> dict:
    with _db() as db:
        db.execute(
            "INSERT INTO documents (id, user_id, conversation_id, filename, stored_path, chunks, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (doc_id, user_id, conversation_id, filename, stored_path, chunks, time.time()),
        )
    return get_document(doc_id, user_id)


def get_document(doc_id: str, user_id: str) -> dict | None:
    """O documento, somente se pertencer a `user_id`."""
    with _db() as db:
        row = db.execute("SELECT * FROM documents WHERE id = ? AND user_id = ?", (doc_id, user_id)).fetchone()
    return dict(row) if row else None


def list_documents(user_id: str, conversation_id: str | None = None) -> list[dict]:
    """Biblioteca do usuário (conversation_id=None) ou os temporários de uma conversa dele."""
    with _db() as db:
        rows = db.execute(
            "SELECT * FROM documents WHERE user_id = ? AND conversation_id IS ? ORDER BY created_at DESC",
            (user_id, conversation_id),
        ).fetchall()
    return [dict(r) for r in rows]


def find_documents(user_id: str, filename: str, conversation_id: str | None) -> list[dict]:
    """Documentos com o mesmo nome no mesmo escopo (para substituir em vez de duplicar)."""
    with _db() as db:
        rows = db.execute(
            "SELECT * FROM documents WHERE user_id = ? AND conversation_id IS ? AND filename = ? COLLATE NOCASE",
            (user_id, conversation_id, filename),
        ).fetchall()
    return [dict(r) for r in rows]


def has_documents(user_id: str, conversation_id: str | None) -> bool:
    """Há algo a buscar para este usuário nesta conversa (Biblioteca ou temporários)?"""
    with _db() as db:
        row = db.execute(
            "SELECT 1 FROM documents WHERE user_id = ? AND (conversation_id IS NULL OR conversation_id = ?) LIMIT 1",
            (user_id, conversation_id),
        ).fetchone()
    return row is not None


def delete_document(doc_id: str, user_id: str) -> bool:
    with _db() as db:
        cur = db.execute("DELETE FROM documents WHERE id = ? AND user_id = ?", (doc_id, user_id))
    return cur.rowcount > 0
