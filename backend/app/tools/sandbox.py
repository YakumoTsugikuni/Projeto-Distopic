"""Confinamento de caminhos: toda ferramenta de arquivo passa por resolve_path().

O modelo nunca recebe acesso direto ao disco. Um caminho só é aceito se, depois
de resolvidos `..` e links simbólicos, ficar dentro de uma raiz permitida
(DISTOPIC_ALLOWED_ROOTS) e não tocar em nada sensível.
"""
from __future__ import annotations

from pathlib import Path

from ..config import settings


class ToolError(Exception):
    """Erro esperado, cuja mensagem pode ser devolvida ao modelo."""


# Bloqueados mesmo dentro de uma raiz permitida.
_DENIED_DIRS = {".ssh", ".gnupg", ".aws", ".azure", ".kube", "appdata", ".git"}
_DENIED_SUFFIXES = {".pem", ".key", ".pfx", ".p12", ".kdbx"}


def is_sensitive(relative: Path) -> bool:
    """Recebe o caminho relativo à raiz permitida (a raiz em si é escolha do usuário)."""
    if any(part.lower() in _DENIED_DIRS for part in relative.parts):
        return True
    name = relative.name.lower()
    return name.startswith(".env") or relative.suffix.lower() in _DENIED_SUFFIXES


def resolve_path(raw: str, roots: tuple[Path, ...] | None = None) -> Path:
    roots = settings.allowed_roots if roots is None else roots
    if not roots:
        raise ToolError("Nenhuma pasta permitida está configurada.")
    if "\x00" in raw:
        raise ToolError("Caminho inválido.")

    candidate = Path(raw.strip() or ".")
    if not candidate.is_absolute():
        candidate = roots[0] / candidate
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise ToolError("Caminho inválido.") from exc

    matches = [root for root in roots if resolved.is_relative_to(root)]
    if not matches:
        permitted = "; ".join(str(r) for r in roots)
        raise ToolError(f"Acesso negado: fora das pastas permitidas ({permitted}).")
    # ':' em um nome endereçaria um fluxo alternativo NTFS (arquivo.txt:oculto).
    if any(":" in part for part in resolved.relative_to(matches[0]).parts):
        raise ToolError("Caminho inválido.")
    # Com raízes aninhadas, basta uma delas considerar o caminho sensível.
    if any(is_sensitive(resolved.relative_to(root)) for root in matches):
        raise ToolError("Acesso negado: caminho protegido.")
    return resolved
