"""Quem está usando as ferramentas neste turno."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolContext:
    """Conta e conversa do turno atual. O padrão não autoriza nada (falha fechada)."""

    user_id: str = ""
    is_admin: bool = False
    conversation_id: str | None = None
    # Documentos que esta conta pode buscar aqui (Biblioteca + anexos da conversa).
    document_names: tuple[str, ...] = ()
