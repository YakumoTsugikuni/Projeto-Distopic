"""Servidor da Distopia: API autenticada + frontend estático."""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import anyio
import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, confirmations, documents, ollama_client, storage
from .chat import run_turn
from .config import FRONTEND_DIR, settings
from .ollama_client import OllamaError

log = logging.getLogger("distopia")

# Nomes mantidos por compatibilidade; a regra vive em documents.py.
DOCS_DIR = documents.LIBRARY_DIR
MAX_UPLOAD_BYTES = documents.MAX_UPLOAD_BYTES
ALLOWED_DOC_EXTENSIONS = documents.ALLOWED_EXTENSIONS


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Sem nenhuma conta o servidor sobe, mas nada é acessível: a única ação
    # possível é criar a conta do administrador, e só a partir deste próprio PC
    # (ver /api/setup).
    for root in settings.allowed_roots:
        root.mkdir(parents=True, exist_ok=True)
    storage.init()
    auth.bootstrap()  # senha compartilhada antiga -> conta administradora
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
app = FastAPI(title="Distopia", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


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


# ---------- Entrada e contas ----------


class LoginBody(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


class AccountBody(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


class PasswordBody(BaseModel):
    password: str = Field(max_length=256)


class OwnPasswordBody(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(max_length=256)


@app.post("/api/login")
async def login(body: LoginBody, request: Request, response: Response):
    if auth.throttle.blocked():
        raise HTTPException(429, "Muitas tentativas. Aguarde alguns minutos.")
    user = await anyio.to_thread.run_sync(auth.authenticate, body.username.strip(), body.password)
    if user is None:
        auth.throttle.record_failure()
        raise HTTPException(401, "Usuário ou senha incorretos")
    auth.throttle.reset()
    auth.create_session(request, response, user["id"])
    return {"ok": True}


def _can_setup(request: Request) -> bool:
    # Criar a primeira conta só é possível neste próprio PC: nunca pela rede
    # local nem pelo túnel, senão o primeiro a chegar viraria administrador.
    return auth.is_direct_local(request) and auth.setup_needed()


@app.get("/api/status")
def status(request: Request):
    """Público: diz à tela de entrada se ela deve pedir a criação da conta do administrador."""
    return {"setup": _can_setup(request), "min_password_length": auth.MIN_PASSWORD_LENGTH}


@app.post("/api/setup")
def setup(body: AccountBody, request: Request, response: Response):
    """Primeira execução: cria a conta do administrador e já inicia a sessão."""
    if not _can_setup(request):
        raise HTTPException(403, "Já existe uma conta ou este acesso não pode criar a primeira.")
    try:
        user = auth.create_user(body.username.strip(), body.password, is_admin=True)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    auth.adopt_existing_data(user)  # conversas e documentos anteriores às contas
    auth.create_session(request, response, user["id"])
    return {"ok": True}


@app.post("/api/logout")
async def logout(request: Request, response: Response):
    auth.destroy_session(request, response)
    return {"ok": True}


@app.get("/api/me")
def me(user: dict = Depends(auth.current_user)):
    return {
        "model": settings.model,
        "user": {"id": user["id"], "username": user["username"], "is_admin": user["is_admin"]},
    }


@app.post("/api/me/password")
def change_own_password(body: OwnPasswordBody, request: Request, user: dict = Depends(auth.current_user)):
    if not auth.check_own_password(user["id"], body.current_password):
        raise HTTPException(403, "A senha atual não confere.")
    try:
        auth.set_password(user["id"], body.new_password)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    # As outras sessões desta conta caem; a atual continua.
    auth.drop_sessions(user["id"], keep=request.cookies.get(auth.COOKIE_NAME, ""))
    return {"ok": True}


# Não existe cadastro aberto: só um administrador cria, lista e remove contas.


@app.get("/api/users")
def list_users(admin: dict = Depends(auth.require_admin)):
    return {"users": storage.list_users()}


@app.post("/api/users")
def create_user(body: AccountBody, admin: dict = Depends(auth.require_admin)):
    try:
        return auth.create_user(body.username.strip(), body.password)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/users/{user_id}/password")
def reset_user_password(user_id: str, body: PasswordBody, admin: dict = Depends(auth.require_admin)):
    if storage.get_user(user_id) is None:
        raise HTTPException(404, "Conta não encontrada")
    try:
        auth.set_password(user_id, body.password)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    auth.drop_sessions(user_id)
    return {"ok": True}


@app.delete("/api/users/{user_id}")
def delete_user(user_id: str, admin: dict = Depends(auth.require_admin)):
    if user_id == admin["id"]:
        raise HTTPException(400, "Você não pode remover a própria conta.")
    if storage.get_user(user_id) is None:
        raise HTTPException(404, "Conta não encontrada")
    auth.drop_sessions(user_id)
    documents.remove_user_files(user_id)  # arquivos e vetores; o banco sai em cascata
    storage.delete_user(user_id)
    return {"ok": True}


# ---------- Conversas ----------
# Toda rota recebe a conta da sessão e a repassa ao storage, que a aplica no
# WHERE: a conversa de outra pessoa responde 404, como se não existisse.
# Rotas `def`: o FastAPI as executa em thread, sem travar o streaming.


class ChatBody(BaseModel):
    # O cliente envia só a mensagem nova; o histórico vem do banco, então ele
    # não consegue forjar turnos anteriores, instruções ou resultados de ferramentas.
    conversation_id: str | None = Field(default=None, max_length=64)  # None cria uma conversa
    message: str = Field(min_length=1, max_length=32_000)


class RenameBody(BaseModel):
    title: str = Field(min_length=1, max_length=200)


@app.get("/api/conversations")
def list_conversations(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: dict = Depends(auth.current_user),
):
    return {"conversations": storage.list_conversations(user["id"], limit, offset)}


@app.post("/api/conversations")
def create_conversation(user: dict = Depends(auth.current_user)):
    return storage.create_conversation(user["id"])


@app.get("/api/conversations/{conversation_id}")
def get_conversation(
    conversation_id: str,
    before: int | None = Query(None, ge=1),
    limit: int = Query(50, ge=1, le=200),
    user: dict = Depends(auth.current_user),
):
    conversation = storage.get_conversation(conversation_id, user["id"])
    if conversation is None:
        raise HTTPException(404, "Conversa não encontrada")
    messages, has_more = storage.get_messages(conversation_id, before, limit)
    return {"conversation": conversation, "messages": messages, "has_more": has_more}


@app.patch("/api/conversations/{conversation_id}")
def rename_conversation(conversation_id: str, body: RenameBody, user: dict = Depends(auth.current_user)):
    if not storage.rename_conversation(conversation_id, user["id"], body.title):
        raise HTTPException(404, "Conversa não encontrada")
    return {"ok": True}


@app.delete("/api/conversations/{conversation_id}")
def delete_conversation(conversation_id: str, user: dict = Depends(auth.current_user)):
    if storage.get_conversation(conversation_id, user["id"]) is None:
        raise HTTPException(404, "Conversa não encontrada")
    # Os arquivos "só desta conversa" e seus vetores morrem com ela.
    documents.remove_conversation_files(user["id"], conversation_id)
    storage.delete_conversation(conversation_id, user["id"])
    return {"ok": True}


def _begin_turn(conversation_id: str | None, message: str, user_id: str) -> dict | None:
    """Garante a conversa (do próprio usuário) e grava a mensagem antes de chamar o modelo."""
    if conversation_id is None:
        conversation_id = storage.create_conversation(user_id)["id"]
    elif storage.get_conversation(conversation_id, user_id) is None:
        return None
    storage.add_message(conversation_id, "user", message)
    return storage.get_conversation(conversation_id, user_id)


@app.post("/api/chat")
async def chat(body: ChatBody, request: Request, user: dict = Depends(auth.current_user)):
    conversation = await anyio.to_thread.run_sync(_begin_turn, body.conversation_id, body.message, user["id"])
    if conversation is None:
        raise HTTPException(404, "Conversa não encontrada")
    client: httpx.AsyncClient = request.app.state.http

    async def events():
        yield json.dumps({"type": "conversation", **conversation}, ensure_ascii=False) + "\n"
        try:
            async for event in run_turn(client, conversation["id"], user):
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


@app.post("/api/confirm", dependencies=[Depends(auth.require_admin)])
async def confirm(body: ConfirmBody):
    if not confirmations.resolve(body.id, body.approved):
        raise HTTPException(404, "Pedido de confirmação expirado ou inexistente")
    return {"ok": True}


def _error(message: str) -> str:
    return json.dumps({"type": "error", "message": message}, ensure_ascii=False) + "\n"


# ---------- Documentos (RAG privado de cada conta) ----------

MODE_LIBRARY = "library"
MODE_CONVERSATION = "conversation"


def _public_document(document: dict) -> dict:
    # O caminho em disco e o dono não saem na API.
    return {key: document[key] for key in ("id", "filename", "chunks", "created_at", "conversation_id")}


@app.get("/api/docs")
def list_documents(
    conversation_id: str | None = Query(None, max_length=64),
    user: dict = Depends(auth.current_user),
):
    """Biblioteca do usuário e, se informada, os arquivos temporários de uma conversa dele."""
    temporary: list[dict] = []
    if conversation_id is not None:
        if storage.get_conversation(conversation_id, user["id"]) is None:
            raise HTTPException(404, "Conversa não encontrada")
        temporary = storage.list_documents(user["id"], conversation_id)
    return {
        "library": [_public_document(d) for d in storage.list_documents(user["id"])],
        "conversation": [_public_document(d) for d in temporary],
    }


@app.post("/api/docs/upload")
async def upload_document(
    file: UploadFile = File(...),
    mode: str = Form(MODE_LIBRARY),
    conversation_id: str | None = Form(None, max_length=64),
    user: dict = Depends(auth.current_user),
):
    """Indexa um documento para a conta da sessão.

    mode=library       -> Biblioteca permanente e privada do usuário.
    mode=conversation  -> vale só para a conversa indicada (criada agora, se não houver).
    """
    if mode not in (MODE_LIBRARY, MODE_CONVERSATION):
        raise HTTPException(400, "Destino inválido para o documento.")

    target: str | None = None
    if mode == MODE_CONVERSATION:
        if conversation_id:
            owned = await anyio.to_thread.run_sync(storage.get_conversation, conversation_id, user["id"])
            if owned is None:
                raise HTTPException(404, "Conversa não encontrada")
            target = conversation_id

    content = await file.read(MAX_UPLOAD_BYTES + 1)
    created_conversation = False
    if mode == MODE_CONVERSATION and target is None:
        target = (await anyio.to_thread.run_sync(storage.create_conversation, user["id"]))["id"]
        created_conversation = True
    try:
        document = await documents.save_upload(user["id"], file.filename or "", content, target)
    except documents.UploadError as exc:
        if created_conversation:  # não deixa uma conversa vazia para trás
            await anyio.to_thread.run_sync(storage.delete_conversation, target, user["id"])
        raise HTTPException(exc.status, exc.message) from exc
    return {
        "status": "ok",
        "filename": document["filename"],
        "chunks_indexed": document["chunks"],
        "document_id": document["id"],
        "mode": mode,
        "conversation_id": target,
    }


@app.delete("/api/docs/{document_id}")
def delete_document(document_id: str, user: dict = Depends(auth.current_user)):
    document = storage.get_document(document_id, user["id"])
    if document is None:
        raise HTTPException(404, "Documento não encontrado")
    documents.remove(document)
    return {"ok": True}


# Por último: tudo que não é /api/* serve o frontend (a tela de login é pública).
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
