"""Servidor da Distopic: API autenticada + frontend estático."""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import anyio
import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, confirmations, ollama_client, storage
from .chat import run_turn
from .config import FRONTEND_DIR, settings
from .ollama_client import OllamaError

from pathlib import Path
from uuid import uuid4
from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from .rag.indexer import index_file

log = logging.getLogger("distopic")

DOCS_DIR = settings.data_dir / "Biblioteca"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
ALLOWED_DOC_EXTENSIONS = {".pdf", ".md", ".txt"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Sem senha definida o servidor sobe, mas nada é acessível: a única ação
    # possível é criar a senha, e só a partir deste próprio PC (ver /api/setup).
    for root in settings.allowed_roots:
        root.mkdir(parents=True, exist_ok=True)
    storage.init()
    # Sem timeout de leitura: a primeira resposta espera o modelo carregar na VRAM.
    timeout = httpx.Timeout(connect=5.0, read=None, write=30.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        app.state.http = client
        # Em segundo plano: o servidor já atende enquanto o modelo carrega.
        warm = asyncio.create_task(_prewarm(client)) if settings.prewarm else None
        yield
        if warm:
            warm.cancel()


async def _prewarm(client: httpx.AsyncClient) -> None:
    try:
        await ollama_client.warm_up(client)
        log.info("Modelo %s carregado na VRAM.", settings.model)
    except Exception as exc:  # Ollama fechado ou modelo ausente: o chat avisa depois
        log.warning("Pré-aquecimento do modelo falhou: %s", exc)


# Documentação automática desligada: o servidor fica exposto à internet.
app = FastAPI(title="Distopic", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    # Anti-CSRF (complementa o cookie SameSite=Strict): requisições que alteram
    # estado só são aceitas se vierem da própria origem.
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return JSONResponse({"detail": "Origem não permitida"}, status_code=403)
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = "default-src 'self'; frame-ancestors 'none'"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


class LoginBody(BaseModel):
    password: str = Field(max_length=256)


class ChatBody(BaseModel):
    # O cliente envia só a mensagem nova; o histórico vem do banco, então ele
    # não consegue forjar turnos anteriores, instruções ou resultados de ferramentas.
    conversation_id: str | None = Field(default=None, max_length=64)  # None cria uma conversa
    message: str = Field(min_length=1, max_length=32_000)


class RenameBody(BaseModel):
    title: str = Field(min_length=1, max_length=200)


@app.post("/api/login")
async def login(body: LoginBody, request: Request, response: Response):
    if auth.throttle.blocked():
        raise HTTPException(429, "Muitas tentativas. Aguarde alguns minutos.")
    if not auth.check_password(body.password):
        auth.throttle.record_failure()
        raise HTTPException(401, "Senha incorreta")
    auth.throttle.reset()
    auth.create_session(request, response)
    return {"ok": True}


class SetupBody(BaseModel):
    password: str = Field(min_length=auth.MIN_PASSWORD_LENGTH, max_length=256)


def _can_setup(request: Request) -> bool:
    # Criar a primeira senha só é possível neste próprio PC: nunca pela rede
    # local nem pelo túnel, senão o primeiro a chegar definiria a senha.
    return not auth.password_configured() and auth.is_direct_local(request)


@app.get("/api/status")
async def status(request: Request):
    """Público: diz à tela de entrada se ela deve pedir a criação da senha."""
    return {"setup": _can_setup(request), "min_password_length": auth.MIN_PASSWORD_LENGTH}


@app.post("/api/setup")
async def setup(body: SetupBody, request: Request, response: Response):
    """Primeira execução: cria a senha de acesso e já inicia a sessão."""
    if not _can_setup(request):
        raise HTTPException(403, "A senha já foi definida ou este acesso não pode defini-la.")
    await anyio.to_thread.run_sync(auth.store_password, body.password)
    auth.create_session(request, response)
    return {"ok": True}


@app.post("/api/logout")
async def logout(request: Request, response: Response):
    auth.destroy_session(request, response)
    return {"ok": True}


@app.get("/api/me", dependencies=[Depends(auth.require_session)])
async def me():
    return {"model": settings.model}


# Rotas de conversas são `def`: o FastAPI as executa em thread, sem travar o streaming.
@app.get("/api/conversations", dependencies=[Depends(auth.require_session)])
def list_conversations(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    return {"conversations": storage.list_conversations(limit, offset)}


@app.post("/api/conversations", dependencies=[Depends(auth.require_session)])
def create_conversation():
    return storage.create_conversation()


@app.get("/api/conversations/{conversation_id}", dependencies=[Depends(auth.require_session)])
def get_conversation(
    conversation_id: str, before: int | None = Query(None, ge=1), limit: int = Query(50, ge=1, le=200)
):
    conversation = storage.get_conversation(conversation_id)
    if conversation is None:
        raise HTTPException(404, "Conversa não encontrada")
    messages, has_more = storage.get_messages(conversation_id, before, limit)
    return {"conversation": conversation, "messages": messages, "has_more": has_more}


@app.patch("/api/conversations/{conversation_id}", dependencies=[Depends(auth.require_session)])
def rename_conversation(conversation_id: str, body: RenameBody):
    if not storage.rename_conversation(conversation_id, body.title):
        raise HTTPException(404, "Conversa não encontrada")
    return {"ok": True}


@app.delete("/api/conversations/{conversation_id}", dependencies=[Depends(auth.require_session)])
def delete_conversation(conversation_id: str):
    if not storage.delete_conversation(conversation_id):
        raise HTTPException(404, "Conversa não encontrada")
    return {"ok": True}


def _begin_turn(conversation_id: str | None, message: str) -> dict | None:
    """Garante a conversa e grava a mensagem do usuário antes de chamar o modelo."""
    if conversation_id is None:
        conversation_id = storage.create_conversation()["id"]
    elif storage.get_conversation(conversation_id) is None:
        return None
    storage.add_message(conversation_id, "user", message)
    return storage.get_conversation(conversation_id)


@app.post("/api/chat", dependencies=[Depends(auth.require_session)])
async def chat(body: ChatBody, request: Request):
    conversation = await anyio.to_thread.run_sync(_begin_turn, body.conversation_id, body.message)
    if conversation is None:
        raise HTTPException(404, "Conversa não encontrada")
    client: httpx.AsyncClient = request.app.state.http

    async def events():
        yield json.dumps({"type": "conversation", **conversation}, ensure_ascii=False) + "\n"
        try:
            async for event in run_turn(client, conversation["id"]):
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except httpx.ConnectError:
            yield _error(f"Não foi possível conectar ao Ollama em {settings.ollama_url}.")
        except OllamaError as exc:
            yield _error(str(exc))
        except Exception:
            log.exception("Falha no chat")
            yield _error("Erro interno no servidor.")

    return StreamingResponse(
        events(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


class ConfirmBody(BaseModel):
    id: str = Field(max_length=64)
    approved: bool


@app.post("/api/confirm", dependencies=[Depends(auth.require_session)])
async def confirm(body: ConfirmBody):
    if not confirmations.resolve(body.id, body.approved):
        raise HTTPException(404, "Pedido de confirmação expirado ou inexistente")
    return {"ok": True}


def _error(message: str) -> str:
    return json.dumps({"type": "error", "message": message}, ensure_ascii=False) + "\n"

@app.post("/api/docs/upload", dependencies=[Depends(auth.require_session)])
async def upload_document(file: UploadFile = File(...)):
    original_name = Path(file.filename or "").name
    extension = Path(original_name).suffix.lower()

    if not original_name or extension not in ALLOWED_DOC_EXTENSIONS:
        raise HTTPException(400, "Envie um arquivo PDF, MD ou TXT.")

    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if not content:
        raise HTTPException(400, "O arquivo está vazio.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "O arquivo excede o limite de 20 MB.")

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid4().hex}_{original_name}"
    file_path = DOCS_DIR / stored_name
    file_path.write_bytes(content)

    chunks_indexed = await index_file(str(file_path))
    return {
        "status": "ok",
        "filename": original_name,
        "chunks_indexed": chunks_indexed,
    }
# Por último: tudo que não é /api/* serve o frontend (a tela de login é pública).
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
