import httpx

from ..config import settings

OLLAMA_URL = f"{settings.ollama_url}/api/embeddings"
EMBED_MODEL = "nomic-embed-text"


async def get_embedding(text: str) -> list[float]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            OLLAMA_URL,
            json={"model": EMBED_MODEL, "prompt": text},
        )
        response.raise_for_status()
        return response.json()["embedding"]