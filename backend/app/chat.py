"""Um turno de conversa: monta o contexto a partir do banco, roda o agente e grava o resultado."""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import anyio
import httpx

from . import agent, storage
from .tools.context import ToolContext

log = logging.getLogger("distopia.chat")


async def run_turn(client: httpx.AsyncClient, conversation_id: str, user: dict) -> AsyncIterator[dict]:
    """Repassa os eventos do agente e persiste texto e ferramentas.

    A mensagem do usuário já deve estar gravada e a posse da conversa por
    `user` já verificada. Se o stream for interrompido (cliente desconectou,
    erro), o texto parcial é salvo marcado como tal.

    Os documentos do usuário NÃO são consultados automaticamente: a busca é a
    ferramenta `buscar_documentos`, que o modelo aciona quando a pergunta pede.
    Aqui só se informa ao agente de quem é o turno e quais documentos existem.
    """
    history = await anyio.to_thread.run_sync(storage.context_messages, conversation_id)
    names = await anyio.to_thread.run_sync(storage.document_names, user["id"], conversation_id)
    context = ToolContext(
        user_id=user["id"],
        is_admin=user["is_admin"],
        conversation_id=conversation_id,
        document_names=tuple(names),
    )

    segment = ""  # texto do assistente ainda não gravado
    finished = False
    call: dict = {}

    def flush(interrupted: bool) -> None:
        nonlocal segment
        text, segment = segment, ""
        if text.strip():
            meta = {"interrupted": True} if interrupted else None
            storage.add_message(conversation_id, "assistant", text, meta)

    try:
        async for event in agent.run(client, history, context):
            kind = event["type"]
            if kind == "token":
                segment += event["text"]
            elif kind == "tool_call":
                await anyio.to_thread.run_sync(flush, False)
                call = event
            elif kind == "tool_result":
                meta = {"name": event["name"], "args": call.get("args"), "ok": event["ok"]}
                if "confirmed" in event:
                    meta["confirmed"] = event["confirmed"]
                await anyio.to_thread.run_sync(
                    storage.add_message, conversation_id, "tool", event["output"], meta
                )
            elif kind == "done":
                finished = True
            yield event
    finally:
        # Síncrono de propósito: em cancelamento não é possível aguardar outra thread.
        try:
            flush(interrupted=not finished)
        except Exception:
            log.exception("Falha ao gravar a resposta da conversa %s", conversation_id)
