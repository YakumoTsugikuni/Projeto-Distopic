"""Confirmações pendentes de ações de escrita/execução.

O agente pausa o stream do chat à espera de um Future; o frontend o resolve
com POST /api/confirm. Sem resposta dentro do prazo, a ação é negada.
"""
from __future__ import annotations

import asyncio
import secrets

TIMEOUT_SECONDS = 300

_pending: dict[str, asyncio.Future[bool]] = {}


def create() -> str:
    confirmation_id = secrets.token_urlsafe(16)
    _pending[confirmation_id] = asyncio.get_running_loop().create_future()
    return confirmation_id


def resolve(confirmation_id: str, approved: bool) -> bool:
    """Registra a decisão do usuário. False se o pedido não existe mais."""
    future = _pending.get(confirmation_id)
    if future is None or future.done():
        return False
    future.set_result(approved)
    return True


async def wait(confirmation_id: str) -> bool:
    try:
        return await asyncio.wait_for(_pending[confirmation_id], TIMEOUT_SECONDS)
    except TimeoutError:
        return False
    finally:
        # Também roda se o cliente desconectar e o stream for cancelado.
        _pending.pop(confirmation_id, None)
