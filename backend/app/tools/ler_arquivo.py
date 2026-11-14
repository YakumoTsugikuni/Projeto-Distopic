"""Ferramenta ler_arquivo (somente leitura).

Devolve o texto em páginas pequenas: com 8192 tokens de contexto, despejar um
arquivo inteiro expulsaria a conversa. O modelo pede a continuação por linha.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .sandbox import ToolError, resolve_path

MAX_BYTES = 2_000_000
MAX_LINES = 200
CHAR_BUDGET = 5000


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")

    caminho: str = Field(
        max_length=500,
        description="Arquivo de texto a ler. Caminho absoluto ou relativo à pasta de trabalho.",
    )
    linha_inicial: int = Field(
        default=1, ge=1, description="Linha a partir da qual ler (1 = início do arquivo)."
    )


def run(args: Args) -> str:
    path = resolve_path(args.caminho)
    if not path.exists():
        # A segunda frase recupera o caso em que o modelo inventou um arquivo
        # para responder a uma pergunta de conhecimento geral.
        raise ToolError(
            f"O arquivo não existe: {path}. Se o usuário não citou esse arquivo, ignore este erro "
            "e responda à pergunta dele diretamente com o que você sabe, sem usar ferramentas."
        )
    if not path.is_file():
        raise ToolError(f"Não é um arquivo: {path}")
    if path.suffix.lower() == ".pdf":
        raise ToolError("ler_arquivo não lê PDF; apenas arquivos de texto.")
    try:
        if path.stat().st_size > MAX_BYTES:
            raise ToolError(f"Arquivo grande demais para leitura direta (limite: {MAX_BYTES // 1_000_000} MB).")
        data = path.read_bytes()
    except PermissionError as exc:
        raise ToolError(f"Sem permissão para ler: {path}") from exc

    if b"\x00" in data[:8192]:
        raise ToolError("O arquivo parece ser binário, não texto.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252", "replace")

    lines = text.splitlines()
    if not lines:
        return f"{path}: (arquivo vazio)"
    start = args.linha_inicial
    if start > len(lines):
        raise ToolError(f"O arquivo tem apenas {len(lines)} linhas.")

    page: list[str] = []
    used = 0
    for line in lines[start - 1 : start - 1 + MAX_LINES]:
        line = line[:CHAR_BUDGET]
        if page and used + len(line) + 1 > CHAR_BUDGET:
            break
        page.append(line)
        used += len(line) + 1

    end = start - 1 + len(page)
    out = [f"{path} — linhas {start} a {end} de {len(lines)}:", *page]
    if end < len(lines):
        out.append(f"[Há mais linhas. Chame ler_arquivo com linha_inicial={end + 1} para continuar.]")
    return "\n".join(out)
