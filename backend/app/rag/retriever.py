"""Busca nos documentos de UMA conta. Não existe busca sem dono."""
import anyio

from . import embedder
from .indexer import SCOPE_LIBRARY, collection


async def search_docs(
    query: str,
    *,
    owner_id: str,
    conversation_id: str | None = None,
    top_k: int = 3,
) -> list[dict]:
    """Trechos mais próximos da pergunta: [{"source": nome do arquivo, "text": trecho}].

    Procura na Biblioteca de `owner_id` e, se `conversation_id` for dado, também
    nos arquivos temporários daquela conversa. O filtro de dono é sempre
    aplicado: vetores de outra conta, ou antigos e sem dono, nunca retornam.
    """
    if not owner_id:
        raise ValueError("A busca exige o dono dos documentos.")

    scope: dict = {"scope": SCOPE_LIBRARY}
    if conversation_id:
        scope = {"$or": [scope, {"conversation_id": conversation_id}]}
    where = {"$and": [{"owner_id": owner_id}, scope]}

    query_emb = await embedder.get_embedding(query)

    def run() -> dict:
        return collection.query(query_embeddings=[query_emb], n_results=top_k, where=where)

    results = await anyio.to_thread.run_sync(run)
    docs = (results.get("documents") or [[]])[0]
    metas = (results.get("metadatas") or [[]])[0]
    return [
        {"source": (meta or {}).get("source", "desconhecido"), "text": doc}
        for doc, meta in zip(docs, metas)
    ]


def format_context(hits: list[dict]) -> str:
    """Trechos com o nome do documento, para que a resposta possa citar a fonte."""
    return "\n\n---\n\n".join(f"[Fonte: {hit['source']}]\n{hit['text']}" for hit in hits)
