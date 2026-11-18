"""Documentos enviados pelos usuários: arquivo em disco + registro no SQLite + vetores no ChromaDB.

Dois destinos, sempre privados da conta que enviou:
  - Biblioteca: permanente, em Biblioteca/<user_id>/, consultada em todas as conversas do usuário.
  - Conversa:   temporário, em Temporarios/<user_id>/<conversation_id>/, consultado só naquela
                conversa e apagado junto com ela.
"""
from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from uuid import uuid4

import anyio
import httpx

from . import storage
from .config import settings
from .rag import indexer

log = logging.getLogger("distopia.documents")

LIBRARY_DIR = settings.data_dir / "Biblioteca"
TEMP_DIR = settings.data_dir / "Temporarios"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
ALLOWED_EXTENSIONS = {".pdf", ".md", ".txt"}

_LEGACY_PREFIX = re.compile(r"^[0-9a-f]{32}_")


class UploadError(Exception):
    """Erro esperado no envio; `status` é o código HTTP e `message` vai para o usuário."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def safe_name(filename: str) -> str:
    """Só o nome do arquivo, sem pastas nem caracteres que o Windows rejeita."""
    name = Path(filename.replace("\\", "/")).name
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name[-120:]


def _folder(user_id: str, conversation_id: str | None) -> Path:
    # Os dois identificadores são gerados pelo servidor (uuid4().hex) e a posse
    # da conversa é verificada antes; nunca vêm crus do cliente para o caminho.
    return TEMP_DIR / user_id / conversation_id if conversation_id else LIBRARY_DIR / user_id


async def save_upload(user_id: str, filename: str, content: bytes, conversation_id: str | None = None) -> dict:
    """Guarda e indexa um arquivo para `user_id`. `conversation_id` o torna temporário."""
    name = safe_name(filename or "")
    if not name or Path(name).suffix.lower() not in ALLOWED_EXTENSIONS:
        raise UploadError(400, "Envie um arquivo PDF, MD ou TXT.")
    if not content:
        raise UploadError(400, "O arquivo está vazio.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise UploadError(413, "O arquivo excede o limite de 20 MB.")

    doc_id = uuid4().hex
    path = _folder(user_id, conversation_id) / f"{doc_id}_{name}"

    def write() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    await anyio.to_thread.run_sync(write)

    def discard() -> None:  # nada do envio que falhou pode sobrar em disco ou no índice
        path.unlink(missing_ok=True)
        indexer.delete_document(user_id, doc_id)

    try:
        chunks = await indexer.index_file(
            str(path), owner_id=user_id, doc_id=doc_id, source=name, conversation_id=conversation_id
        )
    except indexer.UnreadableFile as exc:
        await anyio.to_thread.run_sync(discard)
        raise UploadError(422, str(exc)) from exc
    except httpx.ConnectError as exc:
        await anyio.to_thread.run_sync(discard)
        raise UploadError(503, "Não foi possível conectar ao Ollama para indexar o arquivo.") from exc
    except httpx.HTTPError as exc:
        await anyio.to_thread.run_sync(discard)
        raise UploadError(
            503,
            "O Ollama não gerou os embeddings. Verifique se o modelo está instalado: "
            "ollama pull nomic-embed-text",
        ) from exc
    except BaseException:
        await anyio.to_thread.run_sync(discard)
        raise

    if chunks == 0:
        await anyio.to_thread.run_sync(discard)
        raise UploadError(
            422,
            "Não foi possível extrair texto deste arquivo. Se for um PDF digitalizado "
            "(imagem), ele precisa passar por OCR antes.",
        )

    def record() -> dict:
        # Reenviar um arquivo com o mesmo nome substitui a versão anterior:
        # sem isso, os trechos antigos continuariam respondendo às buscas.
        for old in storage.find_documents(user_id, name, conversation_id):
            remove(old)
        return storage.add_document(doc_id, user_id, name, str(path), chunks, conversation_id)

    return await anyio.to_thread.run_sync(record)


# ---------- Remoção (funções síncronas) ----------


def remove(document: dict) -> None:
    """Apaga vetores, arquivo e registro de um documento (linha vinda de storage)."""
    indexer.delete_document(document["user_id"], document["id"])
    if document["stored_path"]:
        Path(document["stored_path"]).unlink(missing_ok=True)
    storage.delete_document(document["id"], document["user_id"])


def remove_conversation_files(user_id: str, conversation_id: str) -> None:
    """Apaga os temporários de uma conversa. Os registros saem em cascata com a conversa."""
    indexer.delete_conversation(user_id, conversation_id)
    shutil.rmtree(TEMP_DIR / user_id / conversation_id, ignore_errors=True)


def remove_user_files(user_id: str) -> None:
    indexer.delete_owner(user_id)
    shutil.rmtree(LIBRARY_DIR / user_id, ignore_errors=True)
    shutil.rmtree(TEMP_DIR / user_id, ignore_errors=True)


# ---------- Migração dos documentos anteriores às contas ----------


def adopt_legacy(user_id: str) -> int:
    """Entrega à Biblioteca de `user_id` os documentos indexados antes das contas.

    Antes disso eles ficam isolados: sem owner_id, nenhuma busca os alcança.
    Chamado uma única vez, quando o primeiro administrador passa a existir.
    Retorna quantos documentos foram adotados.
    """
    adopted = 0
    for source, chunks in indexer.ownerless_sources().items():
        doc_id = uuid4().hex
        name = safe_name(_LEGACY_PREFIX.sub("", source)) or "documento"
        stored: Path | None = None
        legacy_file = LIBRARY_DIR / source  # antes ficavam soltos na raiz da Biblioteca
        try:
            if safe_name(source) == source and legacy_file.is_file():
                stored = LIBRARY_DIR / user_id / f"{doc_id}_{name}"
                stored.parent.mkdir(parents=True, exist_ok=True)
                legacy_file.replace(stored)
            indexer.assign_owner(chunks, owner_id=user_id, doc_id=doc_id, source=name)
            storage.add_document(doc_id, user_id, name, str(stored) if stored else None, len(chunks))
            adopted += 1
        except Exception:  # um documento problemático não impede os demais; ele segue isolado
            log.exception("Falha ao migrar o documento antigo %r", source)
    return adopted
