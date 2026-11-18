"""Registro de ferramentas (Function Calling).

Regras de segurança aplicadas a TODA chamada vinda do modelo:
  1. Só executa ferramentas registradas aqui (lista fechada).
  2. Cada ferramenta é autorizada contra a conta da sessão (ToolContext): as
     que tocam o disco do PC são exclusivas de administradores. A regra vale
     duas vezes — na lista enviada ao modelo e de novo na execução.
  3. Argumentos são validados por um schema Pydantic estrito.
  4. Cada ferramenta declara um nível de risco; qualquer coisa acima de
     LEITURA só roda com `approved=True`, que o agente obtém do usuário
     (falha fechada: sem aprovação explícita, não executa).
  5. A saída é truncada para não estourar o contexto do modelo.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum

import anyio
from pydantic import BaseModel, ValidationError

from . import buscar_documentos, criar_arquivo, ler_arquivo, listar_diretorio, sandbox
from .context import ToolContext
from .sandbox import ToolError

log = logging.getLogger("distopia.tools")

MAX_OUTPUT_CHARS = 6000
MAX_LISTED_DOCUMENTS = 20

ADMIN_ONLY_MESSAGE = (
    "Acesso negado: as ferramentas de arquivos e pastas do computador são exclusivas de "
    "contas de administrador. Informe isso ao usuário; não tente outro caminho."
)


class Risk(Enum):
    READ = "leitura"  # executa direto
    WRITE = "escrita"  # exige confirmação do usuário
    EXECUTE = "execucao"  # exige confirmação do usuário


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    risk: Risk
    # Uma das duas formas de execução:
    handler: Callable[[BaseModel], str] | None = None  # síncrona, roda em thread
    context_handler: Callable[[BaseModel, ToolContext], Awaitable[str]] | None = None  # usa a conta da sessão
    # Toca arquivos/pastas do PC: só administradores.
    admin_only: bool = False
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
            admin_only=True,
        ),
        Tool(
            name="ler_arquivo",
            description=(
                "Lê um arquivo de texto que EXISTE em uma pasta do computador do usuário. Use somente "
                "quando o usuário citar um arquivo real ou depois de vê-lo em listar_diretorio. "
                "Não use para responder perguntas de conhecimento geral."
            ),
            args_model=ler_arquivo.Args,
            handler=ler_arquivo.run,
            risk=Risk.READ,
            admin_only=True,
        ),
        Tool(
            name="criar_arquivo",
            description=(
                "Cria um arquivo de texto com o conteúdo informado. Use somente quando o usuário "
                "pedir explicitamente para criar ou salvar um arquivo. As pastas do caminho que "
                "ainda não existem são criadas automaticamente (ex.: src/main/java/App.java). "
                "O usuário confirma a ação na interface antes de ela ser executada."
            ),
            args_model=criar_arquivo.Args,
            handler=criar_arquivo.run,
            risk=Risk.WRITE,
            admin_only=True,
            describe=criar_arquivo.describe,
        ),
        Tool(
            name="buscar_documentos",
            description=(
                "Busca trechos nos documentos que o usuário enviou para a Distopia (a Biblioteca dele "
                "e os anexos desta conversa). Use SOMENTE quando a pergunta citar explicitamente um "
                "arquivo, PDF, especificação, contrato, manual, anexo ou 'meus documentos' / 'minha "
                "biblioteca'. NÃO use para conhecimento geral, programação ou conversa, nem para "
                "arquivos que estão em pastas do computador."
            ),
            args_model=buscar_documentos.Args,
            context_handler=buscar_documentos.run,
            risk=Risk.READ,
        ),
    )
}


def authorize(tool: Tool, context: ToolContext) -> None:
    """Lança ToolError se a conta da sessão não pode usar a ferramenta."""
    if tool.admin_only and not context.is_admin:
        raise ToolError(ADMIN_ONLY_MESSAGE)


def available(context: ToolContext) -> list[Tool]:
    """Ferramentas que valem a pena oferecer ao modelo para esta conta, neste turno."""
    offered = []
    for tool in TOOLS.values():
        if tool.admin_only and not context.is_admin:
            continue
        # Sem documentos não há o que buscar: nem oferecer evita chamadas inúteis.
        if tool.name == "buscar_documentos" and not context.document_names:
            continue
        offered.append(tool)
    return offered


def _description(tool: Tool, context: ToolContext) -> str:
    """Descrição com o que só se sabe na hora: pastas liberadas e documentos da conta."""
    if tool.admin_only:
        return f"{tool.description} {sandbox.roots_hint()}"
    if tool.name == "buscar_documentos":
        names = list(context.document_names[:MAX_LISTED_DOCUMENTS])
        more = len(context.document_names) - len(names)
        listing = ", ".join(names) + (f" e mais {more}" if more > 0 else "")
        return f"{tool.description} Documentos disponíveis: {listing}."
    return tool.description


def ollama_schemas(context: ToolContext) -> list[dict]:
    """Definições no formato `tools` aceito por /api/chat, só do que a conta pode usar."""
    schemas = []
    for tool in available(context):
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
                    "description": _description(tool, context),
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": raw.get("required", []),
                    },
                },
            }
        )
    return schemas


def prepare(name: str, arguments: object, context: ToolContext) -> tuple[Tool, BaseModel] | ToolResult:
    """Localiza a ferramenta, confere a permissão e valida os argumentos, sem executar nada."""
    tool = TOOLS.get(name)
    if tool is None:
        return ToolResult(False, f"Ferramenta desconhecida: {name!r}.")
    try:
        authorize(tool, context)
    except ToolError as exc:
        return ToolResult(False, str(exc))
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


async def run(tool: Tool, args: BaseModel, context: ToolContext, *, approved: bool = False) -> ToolResult:
    try:
        # Conferida de novo aqui, no ponto de execução: não depende de quem chamou
        # ter passado por prepare().
        authorize(tool, context)
        if tool.needs_confirmation and not approved:
            return ToolResult(False, "Ação bloqueada: requer confirmação do usuário.")
        if tool.context_handler is not None:
            output = await tool.context_handler(args, context)
        else:
            output = await anyio.to_thread.run_sync(tool.handler, args)
    except ToolError as exc:
        return ToolResult(False, str(exc))
    except Exception:
        log.exception("Falha inesperada na ferramenta %s", tool.name)
        return ToolResult(False, "Erro interno ao executar a ferramenta.")
    if len(output) > MAX_OUTPUT_CHARS:
        output = output[:MAX_OUTPUT_CHARS] + "\n[saída truncada]"
    return ToolResult(True, output)


async def execute(name: str, arguments: object, context: ToolContext) -> ToolResult:
    """Valida e executa sem aprovação — portanto só ferramentas de leitura."""
    prepared = prepare(name, arguments, context)
    if isinstance(prepared, ToolResult):
        return prepared
    return await run(*prepared, context)
