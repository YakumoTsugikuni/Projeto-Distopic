"""Laço do agente: modelo -> chamadas de ferramenta -> modelo, até a resposta final."""
from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator

import httpx

from . import confirmations, tools
from .config import settings
from .ollama_client import chat_stream
from .tools.context import ToolContext
from .tools.sandbox import ToolError

MAX_STEPS = 5  # limite de idas e voltas modelo<->ferramentas por mensagem


def trim_history(messages: list[dict]) -> list[dict]:
    """Mantém as mensagens mais recentes que cabem em ~metade do contexto.

    Estimativa conservadora de 4 caracteres por token; a outra metade fica
    para o prompt de sistema, as ferramentas e a resposta.
    """
    budget = settings.num_ctx * 2
    kept: list[dict] = []
    for message in reversed(messages):
        budget -= len(message["content"])
        if budget < 0 and kept:
            break
        kept.append(message)
    return list(reversed(kept))


# Modelos pequenos (qwen2.5-coder em especial) às vezes escrevem a chamada de
# ferramenta como JSON no texto, em vez de usar o campo estruturado `tool_calls`.
_TOOL_TEXT_PREFIXES = ("{", "<tool_call", "```json", "```\n{")


def _looks_like_text(content: str) -> bool | None:
    """True = texto comum; False = parece chamada de ferramenta; None = cedo para dizer."""
    head = content.lstrip()
    if not head:
        return None
    if head.startswith(_TOOL_TEXT_PREFIXES):
        return False
    if any(prefix.startswith(head) for prefix in _TOOL_TEXT_PREFIXES):
        return None
    return True


def _strip_wrappers(content: str) -> str:
    return re.sub(r"</?tool_call>|```(?:json)?", "", content).strip()


def _unwrap_invented_tool(content: str) -> str | None:
    """O modelo às vezes inventa uma ferramenta (ex.: "responder") só para entregar texto.

    Nesse caso devolve o texto dos argumentos; nada é executado.
    """
    try:
        obj = json.loads(_strip_wrappers(content))
    except ValueError:
        return None
    arguments = obj.get("arguments") if isinstance(obj, dict) else None
    if not isinstance(arguments, dict) or not arguments or "name" not in obj:
        return None
    if not all(isinstance(value, str) for value in arguments.values()):
        return None
    return "\n".join(arguments.values())


def _parse_text_tool_calls(content: str) -> list[dict]:
    """Extrai chamadas escritas como JSON. Só aceita se TODO o texto for chamadas a
    ferramentas registradas.

    Reconhecer não é autorizar: a chamada segue para tools.prepare(), que recusa
    com um erro claro o que a conta não pode usar (em vez de o JSON cru ir para a tela).
    """
    text = _strip_wrappers(content)
    decoder = json.JSONDecoder()
    calls: list[dict] = []
    while text:
        try:
            obj, end = decoder.raw_decode(text)
        except ValueError:
            return []
        arguments = obj.get("arguments", obj.get("parameters")) if isinstance(obj, dict) else None
        if not isinstance(arguments, dict) or obj.get("name") not in tools.TOOLS:
            return []
        calls.append({"function": {"name": obj["name"], "arguments": arguments}})
        text = text[end:].strip()
    return calls


async def run(
    client: httpx.AsyncClient, history: list[dict], context: ToolContext | None = None
) -> AsyncIterator[dict]:
    """Produz eventos para o frontend:
    token, tool_call, confirm_request, tool_result, truncated, done, error.

    `context` diz de quem é o turno. Sem ele nenhuma ferramenta é oferecida nem executada.
    """
    context = context or ToolContext()
    messages = trim_history(history)
    # O modelo só fica sabendo das ferramentas que esta conta pode usar; a
    # permissão é conferida de novo na execução (tools.prepare / tools.run).
    schemas = tools.ollama_schemas(context)

    for _ in range(MAX_STEPS):
        content = ""
        calls: list[dict] = []
        truncated = False
        streaming: bool | None = None  # None = ainda não decidido
        async for chunk in chat_stream(client, messages, schemas):
            message = chunk.get("message") or {}
            if text := message.get("content"):
                content += text
                if streaming is None:
                    # Segura o início até saber se é texto ou uma chamada de ferramenta escrita como JSON.
                    streaming = _looks_like_text(content)
                    if streaming:
                        yield {"type": "token", "text": content}
                elif streaming:
                    yield {"type": "token", "text": text}
            calls.extend(message.get("tool_calls") or [])
            truncated = chunk.get("done_reason") == "length"

        if not calls and not streaming and content:
            calls = _parse_text_tool_calls(content)
            if calls:
                content = ""
            else:  # resposta embrulhada em ferramenta inventada, ou texto que só parecia JSON
                yield {"type": "token", "text": _unwrap_invented_tool(content) or content}

        if not calls:
            if truncated:  # bateu no teto de tokens (num_predict)
                yield {"type": "truncated"}
            yield {"type": "done"}
            return

        messages.append({"role": "assistant", "content": content, "tool_calls": calls})
        for call in calls:
            function = call.get("function") or {}
            name = str(function.get("name", ""))
            arguments = function.get("arguments") or {}
            yield {"type": "tool_call", "name": name, "args": arguments}

            confirmed: bool | None = None  # decisão do usuário, se foi consultado
            prepared = tools.prepare(name, arguments, context)
            if isinstance(prepared, tools.ToolResult):
                result = prepared
            else:
                tool, args = prepared
                result = None
                approved = False
                if tool.needs_confirmation:
                    try:
                        summary, detail = tools.describe(tool, args)
                    except ToolError as exc:
                        # Chamada inviável: nem incomoda o usuário.
                        result = tools.ToolResult(False, str(exc))
                    else:
                        confirmation_id = confirmations.create()
                        yield {
                            "type": "confirm_request",
                            "id": confirmation_id,
                            "name": name,
                            "summary": summary,
                            "detail": detail,
                        }
                        approved = confirmed = await confirmations.wait(confirmation_id)
                        if not approved:
                            result = tools.ToolResult(
                                False,
                                "O usuário não autorizou esta ação. Não tente de novo sem que ele peça.",
                            )
                if result is None:
                    result = await tools.run(tool, args, context, approved=approved)

            event = {"type": "tool_result", "name": name, "ok": result.ok, "output": result.output}
            if confirmed is not None:
                event["confirmed"] = confirmed
            yield event
            messages.append({"role": "tool", "tool_name": name, "content": result.output})

    yield {"type": "error", "message": "Limite de passos do agente atingido."}
