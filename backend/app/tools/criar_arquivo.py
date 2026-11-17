"""Ferramenta criar_arquivo (escrita — só executa após confirmação do usuário)."""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .sandbox import ToolError, resolve_path


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")

    caminho: str = Field(
        max_length=500,
        description=(
            "Arquivo a criar, com as pastas se houver (ex.: src/main/java/App.java). "
            "Caminho absoluto ou relativo à pasta de trabalho."
        ),
    )
    conteudo: str = Field(max_length=200_000, description="Conteúdo completo do arquivo.")
    sobrescrever: bool = Field(
        default=False,
        description="true substitui um arquivo existente. Use somente se o usuário pediu para substituir.",
    )


def _missing_folders(path: Path) -> list[Path]:
    """Pastas do caminho que ainda não existem, da mais externa para a mais interna."""
    missing = []
    for parent in path.parents:
        if parent.exists():
            if not parent.is_dir():
                raise ToolError(f"Não é possível criar dentro de {parent}: já existe um arquivo com esse nome.")
            break
        missing.append(parent)
    return list(reversed(missing))


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
    summary = f"{action} {path} ({len(args.conteudo)} caracteres)"
    folders = _missing_folders(path)
    if folders:  # o usuário aprova sabendo que pastas também serão criadas
        summary += f". Pastas novas: {folders[0]}" + (f" … {folders[-1]}" if len(folders) > 1 else "")
    return summary, args.conteudo


def run(args: Args) -> str:
    path = _target(args)
    folders = _missing_folders(path)
    try:
        # Cria toda a árvore de pastas que faltar. O caminho já foi validado por
        # resolve_path(), então nenhuma delas fica fora das pastas permitidas.
        path.parent.mkdir(parents=True, exist_ok=True)
        # Modo "x" falha se o arquivo surgiu depois da confirmação: nunca
        # sobrescreve algo que o usuário não aprovou substituir.
        with open(path, "w" if args.sobrescrever else "x", encoding="utf-8", newline="") as f:
            f.write(args.conteudo)
    except FileExistsError as exc:
        raise ToolError(f"O arquivo já existe: {path}") from exc
    except OSError as exc:
        raise ToolError(f"Não foi possível gravar {path}: {exc.strerror}") from exc
    created = f" Pastas criadas: {len(folders)}." if folders else ""
    return f"Arquivo gravado: {path} ({len(args.conteudo)} caracteres).{created}"
