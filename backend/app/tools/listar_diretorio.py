"""Ferramenta listar_diretorio (somente leitura)."""
from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .sandbox import ToolError, is_sensitive, resolve_path

MAX_ENTRIES = 200


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")

    caminho: str = Field(
        default=".",
        max_length=500,
        description="Pasta a listar. Caminho absoluto ou relativo à pasta de trabalho. Use '.' para a pasta de trabalho.",
    )


def _size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{num_bytes} B"


def run(args: Args) -> str:
    path = resolve_path(args.caminho)
    if not path.exists():
        raise ToolError(f"A pasta não existe: {path}")
    if not path.is_dir():
        raise ToolError(f"Não é uma pasta: {path}")

    try:
        with os.scandir(path) as it:
            entries = [e for e in it if not is_sensitive(Path(e.name))]
    except PermissionError as exc:
        raise ToolError(f"Sem permissão para ler: {path}") from exc

    # Pastas primeiro, depois arquivos, em ordem alfabética.
    entries.sort(key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))

    lines = [f"Conteúdo de {path} ({len(entries)} itens):"]
    for entry in entries[:MAX_ENTRIES]:
        if entry.is_dir(follow_symlinks=False):
            lines.append(f"[PASTA] {entry.name}")
        else:
            try:
                size = _size(entry.stat(follow_symlinks=False).st_size)
            except OSError:
                size = "?"
            lines.append(f"[ARQUIVO] {entry.name} ({size})")
    if len(entries) > MAX_ENTRIES:
        lines.append(f"... e mais {len(entries) - MAX_ENTRIES} itens não exibidos.")
    if not entries:
        lines.append("(pasta vazia)")
    return "\n".join(lines)
