import httpx

from ..config import settings

OLLAMA_URL = f"{settings.ollama_url}/api/embeddings"
EMBED_MODEL = "nomic-embed-text"
# O modelo de embedding tem contexto curto; texto além disso só seria truncado por ele.
MAX_EMBED_CHARS = 4000


async def get_embedding(text: str) -> list[float]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            OLLAMA_URL,
            json={"model": EMBED_MODEL, "prompt": text[:MAX_EMBED_CHARS]},
        )
        response.raise_for_status()
        return response.json()["embedding"]
