"""Ferramenta criar_arquivo (escrita — só executa após confirmação do usuário)."""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .sandbox import ToolError, resolve_path


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")

    caminho: str = Field(
        max_length=500,
        description="Arquivo a criar. Caminho absoluto ou relativo à pasta de trabalho.",
    )
    conteudo: str = Field(max_length=200_000, description="Conteúdo completo do arquivo.")
    sobrescrever: bool = Field(
        default=False,
        description="true substitui um arquivo existente. Use somente se o usuário pediu para substituir.",
    )


def _target(args: Args) -> Path:
    path = resolve_path(args.caminho)
    if path.is_dir():
        raise ToolError(f"Já existe uma pasta com esse nome: {path}")
    if path.exists() and not args.sobrescrever:
        raise ToolError(
            f"O arquivo já existe: {path}. Para substituí-lo use sobrescrever=true, "
            "somente se o usuário pediu."
        )
    return path


def describe(args: Args) -> tuple[str, str]:
    """Texto mostrado ao usuário no pedido de confirmação: (resumo, detalhe)."""
    path = _target(args)
    action = "SOBRESCREVER o arquivo existente" if path.exists() else "Criar o arquivo"
    return f"{action} {path} ({len(args.conteudo)} caracteres)", args.conteudo


def run(args: Args) -> str:
    path = _target(args)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Modo "x" falha se o arquivo surgiu depois da confirmação: nunca
        # sobrescreve algo que o usuário não aprovou substituir.
        with open(path, "w" if args.sobrescrever else "x", encoding="utf-8", newline="") as f:
            f.write(args.conteudo)
    except FileExistsError as exc:
        raise ToolError(f"O arquivo já existe: {path}") from exc
    except OSError as exc:
        raise ToolError(f"Não foi possível gravar {path}: {exc.strerror}") from exc
    return f"Arquivo gravado: {path} ({len(args.conteudo)} caracteres)"
