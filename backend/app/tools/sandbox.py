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


def _locate(raw: str, roots: tuple[Path, ...]) -> Path:
    """Transforma o caminho pedido pelo modelo em um candidato (ainda NÃO validado).

    - Caminho completo (C:\\..., \\\\servidor\\...): usado como veio.
    - "/src/App.java" ou "\\src\\App.java": no Windows isso apontaria para a raiz do
      disco; o modelo quer dizer "dentro da pasta de trabalho", então a barra inicial sai.
    - "Projetos/app/x.py", sendo "Projetos" o nome de uma pasta permitida: vai para ela.
      É assim que um caminho relativo alcança as outras pastas de DISTOPIC_ALLOWED_ROOTS
      (e "workspace/x" não vira workspace/workspace/x).
    - Qualquer outro relativo parte da primeira pasta permitida.

    A segurança não depende daqui: resolve_path() valida o resultado em seguida.
    """
    path = Path(raw.replace("\\", "/"))
    if path.is_absolute():
        return path
    if path.drive:  # "C:arquivo" (relativo ao disco): ambíguo, recusado
        raise ToolError("Caminho inválido.")
    parts = [part for part in path.parts if part not in ("/", "\\")]
    if parts and parts[0] not in (".", ".."):
        for root in roots:
            if root.name and root.name.casefold() == parts[0].casefold():
                return root.joinpath(*parts[1:])
    return roots[0].joinpath(*parts)


def roots_hint(roots: tuple[Path, ...] | None = None) -> str:
    """Frase para a descrição das ferramentas: onde o modelo pode ler e criar arquivos."""
    roots = settings.allowed_roots if roots is None else roots
    if not roots:
        return "Nenhuma pasta está liberada."
    hint = f"Caminhos relativos partem da pasta de trabalho: {roots[0]}."
    if len(roots) > 1:
        others = "; ".join(str(root) for root in roots[1:])
        hint += f" Outras pastas permitidas (use o caminho completo ou comece pelo nome da pasta): {others}."
    return hint


def resolve_path(raw: str, roots: tuple[Path, ...] | None = None) -> Path:
    roots = settings.allowed_roots if roots is None else roots
    if not roots:
        raise ToolError("Nenhuma pasta permitida está configurada.")
    if "\x00" in raw:
        raise ToolError("Caminho inválido.")

    candidate = _locate(raw.strip() or ".", roots)
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
