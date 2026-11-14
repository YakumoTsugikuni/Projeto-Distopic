"""Registro de ferramentas (Function Calling).

Regras de segurança aplicadas a TODA chamada vinda do modelo:
  1. Só executa ferramentas registradas aqui (lista fechada).
  2. Argumentos são validados por um schema Pydantic estrito.
  3. Cada ferramenta declara um nível de risco; qualquer coisa acima de
     LEITURA só roda com `approved=True`, que o agente obtém do usuário
     (falha fechada: sem aprovação explícita, não executa).
  4. A saída é truncada para não estourar o contexto do modelo.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

import anyio
from pydantic import BaseModel, ValidationError

from . import criar_arquivo, ler_arquivo, listar_diretorio
from .sandbox import ToolError

log = logging.getLogger("distopic.tools")

MAX_OUTPUT_CHARS = 6000


class Risk(Enum):
    READ = "leitura"  # executa direto
    WRITE = "escrita"  # exige confirmação do usuário
    EXECUTE = "execucao"  # exige confirmação do usuário


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: Callable[[BaseModel], str]
    risk: Risk
    # (resumo, detalhe) exibidos ao usuário no pedido de confirmação.
    describe: Callable[[BaseModel], tuple[str, str]] | None = None

    @property
    def needs_confirmation(self) -> bool:
        return self.risk is not Risk.READ


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    output: str


TOOLS: dict[str, Tool] = {
    tool.name: tool
    for tool in (
        Tool(
            name="listar_diretorio",
            description=(
                "Lista os arquivos e subpastas de uma pasta do computador do usuário. "
                "Use somente quando o usuário perguntar sobre pastas ou arquivos dele."
            ),
            args_model=listar_diretorio.Args,
            handler=listar_diretorio.run,
            risk=Risk.READ,
        ),
        Tool(
            name="ler_arquivo",
            description=(
                "Lê um arquivo de texto que EXISTE no computador do usuário. Use somente quando o "
                "usuário citar um arquivo real ou depois de vê-lo em listar_diretorio. "
                "Não use para responder perguntas de conhecimento geral."
            ),
            args_model=ler_arquivo.Args,
            handler=ler_arquivo.run,
            risk=Risk.READ,
        ),
        Tool(
            name="criar_arquivo",
            description=(
                "Cria um arquivo de texto com o conteúdo informado. Use somente quando o usuário "
                "pedir explicitamente para criar ou salvar um arquivo. "
                "O usuário confirma a ação na interface antes de ela ser executada."
            ),
            args_model=criar_arquivo.Args,
            handler=criar_arquivo.run,
            risk=Risk.WRITE,
            describe=criar_arquivo.describe,
        ),
    )
}


def ollama_schemas() -> list[dict]:
    """Definições no formato `tools` aceito por /api/chat."""
    schemas = []
    for tool in TOOLS.values():
        raw = tool.args_model.model_json_schema()
        properties = {
            name: {k: v for k, v in prop.items() if k != "title"}
            for name, prop in raw.get("properties", {}).items()
        }
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": raw.get("required", []),
                    },
                },
            }
        )
    return schemas


def prepare(name: str, arguments: object) -> tuple[Tool, BaseModel] | ToolResult:
    """Localiza a ferramenta e valida os argumentos, sem executar nada."""
    tool = TOOLS.get(name)
    if tool is None:
        return ToolResult(False, f"Ferramenta desconhecida: {name!r}.")
    try:
        return tool, tool.args_model.model_validate(arguments)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(map(str, e['loc'])) or 'argumentos'}: {e['msg']}" for e in exc.errors()
        )
        return ToolResult(False, f"Argumentos inválidos: {problems}")


def describe(tool: Tool, args: BaseModel) -> tuple[str, str]:
    """Descrição da ação para o usuário aprovar. Pode lançar ToolError."""
    if tool.describe is not None:
        return tool.describe(args)
    return tool.name, json.dumps(args.model_dump(), ensure_ascii=False, indent=2)


async def run(tool: Tool, args: BaseModel, *, approved: bool = False) -> ToolResult:
    if tool.needs_confirmation and not approved:
        return ToolResult(False, "Ação bloqueada: requer confirmação do usuário.")
    try:
        output = await anyio.to_thread.run_sync(tool.handler, args)
    except ToolError as exc:
        return ToolResult(False, str(exc))
    except Exception:
        log.exception("Falha inesperada na ferramenta %s", tool.name)
        return ToolResult(False, "Erro interno ao executar a ferramenta.")
    if len(output) > MAX_OUTPUT_CHARS:
        output = output[:MAX_OUTPUT_CHARS] + "\n[saída truncada]"
    return ToolResult(True, output)


async def execute(name: str, arguments: object) -> ToolResult:
    """Valida e executa sem aprovação — portanto só ferramentas de leitura."""
    prepared = prepare(name, arguments)
    if isinstance(prepared, ToolResult):
        return prepared
    return await run(*prepared)
