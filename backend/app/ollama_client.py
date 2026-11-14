"""Cliente de streaming para a API /api/chat do Ollama."""
from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from .config import settings


class OllamaError(Exception):
    pass


async def warm_up(client: httpx.AsyncClient) -> None:
    """Carrega o modelo na VRAM sem gerar texto (conversa vazia).

    Usa o mesmo num_ctx das conversas: com um valor diferente o Ollama
    recarregaria o modelo na primeira mensagem, anulando o aquecimento.
    """
    payload = {
        "model": settings.model,
        "messages": [],
        "stream": False,
        "keep_alive": settings.keep_alive,
        "options": {"num_ctx": settings.num_ctx},
    }
    response = await client.post(f"{settings.ollama_url}/api/chat", json=payload)
    if response.status_code != 200:
        raise OllamaError(f"Ollama respondeu {response.status_code}: {response.text[:200]}")


async def chat_stream(
    client: httpx.AsyncClient, messages: list[dict], tools: list[dict]
) -> AsyncIterator[dict]:
    """Envia a conversa e produz cada fragmento NDJSON da resposta."""
    payload = {
        "model": settings.model,
        "messages": messages,
        "tools": tools,
        "stream": True,
        "keep_alive": settings.keep_alive,
        "options": {
            "num_ctx": settings.num_ctx,
            "temperature": settings.temperature,
            "num_predict": settings.num_predict,
        },
    }
    async with client.stream("POST", f"{settings.ollama_url}/api/chat", json=payload) as response:
        if response.status_code != 200:
            body = (await response.aread()).decode("utf-8", "replace")[:300]
            raise OllamaError(f"Ollama respondeu {response.status_code}: {body}")
        async for line in response.aiter_lines():
            if not line:
                continue
            chunk = json.loads(line)
            if "error" in chunk:
                raise OllamaError(str(chunk["error"]))
            yield chunk
