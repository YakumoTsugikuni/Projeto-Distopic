"""Indexação de documentos no ChromaDB, sempre com dono.

Metadados de cada trecho:
  owner_id         conta que enviou o arquivo (obrigatório)
  doc_id           documento ao qual o trecho pertence
  source           nome do arquivo, usado para citar a fonte
  scope            "library" (Biblioteca permanente) ou "conversation" (temporário)
  conversation_id  conversa do temporário; "" para a Biblioteca
  chunk_index      posição do trecho no documento
"""
import os

import anyio
import chromadb
from pypdf import PdfReader

from . import embedder
from ..config import settings

# Caminho de persistência do ChromaDB no SSD
CHROMA_PATH = settings.data_dir / "vector_db"
chroma_client = chromadb.PersistentClient(path=str(CHROMA_PATH))
collection = chroma_client.get_or_create_collection(name="distopic_docs")

SCOPE_LIBRARY = "library"
SCOPE_CONVERSATION = "conversation"


class UnreadableFile(Exception):
    """O arquivo não pôde ser lido (corrompido, protegido etc.)."""


def extract_text(file_path: str) -> str:
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".pdf":
        try:
            reader = PdfReader(file_path)
            pages = [page.extract_text() or "" for page in reader.pages]
        except Exception as exc:  # pypdf lança tipos variados para PDFs inválidos ou protegidos
            raise UnreadableFile("Não foi possível ler este PDF (arquivo corrompido ou protegido por senha).") from exc
        return "\n".join(pages)
    else:
        # Suporta .md, .txt, arquivos de código, etc.
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()


def chunk_text(text: str, chunk_size: int = 600, overlap: int = 100) -> list[str]:
    text = " ".join(text.split())  # Limpa quebras de linha excessivas
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start += chunk_size - overlap
    return chunks


async def index_file(
    file_path: str,
    *,
    owner_id: str,
    doc_id: str,
    source: str,
    conversation_id: str | None = None,
) -> int:
    """Indexa o arquivo para `owner_id`. Retorna o número de trechos (0 = sem texto)."""
    if not owner_id or not doc_id:
        raise ValueError("Todo documento indexado precisa de dono e identificador.")

    raw_text = await anyio.to_thread.run_sync(extract_text, file_path)
    chunks = chunk_text(raw_text)
    if not chunks:
        return 0

    embeddings = [await embedder.get_embedding(chunk) for chunk in chunks]
    metadatas = [
        {
            "owner_id": owner_id,
            "doc_id": doc_id,
            "source": source,
            "scope": SCOPE_CONVERSATION if conversation_id else SCOPE_LIBRARY,
            "conversation_id": conversation_id or "",
            "chunk_index": i,
        }
        for i in range(len(chunks))
    ]
    # O id inclui o doc_id: documentos de contas diferentes nunca colidem, e
    # reenviar um arquivo gera um conjunto novo (o antigo é apagado à parte).
    ids = [f"{doc_id}:{i}" for i in range(len(chunks))]

    def write() -> None:
        collection.upsert(ids=ids, embeddings=embeddings, metadatas=metadatas, documents=chunks)

    await anyio.to_thread.run_sync(write)
    return len(chunks)


# ---------- Remoção (funções síncronas) ----------


def delete_document(owner_id: str, doc_id: str) -> None:
    collection.delete(where={"$and": [{"owner_id": owner_id}, {"doc_id": doc_id}]})


def delete_conversation(owner_id: str, conversation_id: str) -> None:
    """Remove os vetores dos arquivos temporários de uma conversa."""
    collection.delete(where={"$and": [{"owner_id": owner_id}, {"conversation_id": conversation_id}]})


def delete_owner(owner_id: str) -> None:
    collection.delete(where={"owner_id": owner_id})


# ---------- Migração de vetores anteriores às contas ----------


def ownerless_sources() -> dict[str, list[tuple[str, dict]]]:
    """Trechos indexados antes das contas (sem owner_id), agrupados por arquivo de origem.

    Esses trechos não aparecem em nenhuma busca enquanto não forem atribuídos.
    """
    data = collection.get(include=["metadatas"])
    groups: dict[str, list[tuple[str, dict]]] = {}
    for chunk_id, meta in zip(data["ids"], data["metadatas"]):
        meta = meta or {}
        if not meta.get("owner_id"):
            groups.setdefault(str(meta.get("source", "desconhecido")), []).append((chunk_id, meta))
    return groups


def assign_owner(chunks: list[tuple[str, dict]], *, owner_id: str, doc_id: str, source: str) -> None:
    """Passa trechos sem dono para a Biblioteca de `owner_id`."""
    collection.update(
        ids=[chunk_id for chunk_id, _ in chunks],
        metadatas=[
            {
                **meta,
                "owner_id": owner_id,
                "doc_id": doc_id,
                "source": source,
                "scope": SCOPE_LIBRARY,
                "conversation_id": "",
            }
            for _, meta in chunks
        ],
    )
