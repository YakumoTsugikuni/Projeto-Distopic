from .indexer import collection
from .embedder import get_embedding

async def search_docs(query: str, top_k: int = 3) -> str:
    if collection.count() == 0:
        return ""

    query_emb = await get_embedding(query)
    results = collection.query(
        query_embeddings=[query_emb],
        n_results=top_k
    )

    docs = results.get("documents", [[]])[0]
    metas = results.get("metadatas", [[]])[0]

    if not docs:
        return ""

    context_blocks = []
    for doc, meta in zip(docs, metas):
        source = meta.get("source", "desconhecido")
        context_blocks.append(f"[Fonte: {source}]\n{doc}")

    return "\n\n---\n\n".join(context_blocks)