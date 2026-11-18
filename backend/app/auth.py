"""Contas individuais (usuário + senha) e sessão em cookie HttpOnly.

Pensado para exposição via Cloudflare Tunnel: toda rota /api/* (exceto login e
a criação da primeira conta) exige sessão válida; o cookie é SameSite=Strict e
Secure quando atrás de HTTPS.

Não há cadastro público. A primeira conta nasce pelo fluxo local de
configuração e é administradora; as demais só um administrador cria.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import time

from fastapi import Depends, HTTPException, Request, Response

from . import documents, storage
from .config import settings

log = logging.getLogger("distopia.auth")

COOKIE_NAME = "distopia_session"
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**15, 8, 1
_SCRYPT_MAXMEM = 2**26

MIN_PASSWORD_LENGTH = 12
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
USERNAME_RULE = "O nome de usuário deve ter de 3 a 32 caracteres: letras, números, ponto, hífen ou sublinhado."

# Antes das contas havia uma única senha compartilhada (DISTOPIA_PASSWORD_HASH).
# Quem a usava continua entrando: na primeira execução ela vira a senha da
# conta administradora com este nome.
LEGACY_USERNAME = "admin"
_legacy_hash = settings.password_hash

# token -> (id da conta, instante de expiração). Em memória: reiniciar o servidor desloga todos.
_sessions: dict[str, tuple[str, float]] = {}


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=_SCRYPT_MAXMEM, dklen=32
    )


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = _scrypt(password, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
    return f"scrypt:{_SCRYPT_N}:{_SCRYPT_R}:{_SCRYPT_P}:{salt.hex()}:{digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, digest_hex = stored.split(":")
        if scheme != "scrypt":
            return False
        digest = _scrypt(password, bytes.fromhex(salt_hex), int(n), int(r), int(p))
        return hmac.compare_digest(digest, bytes.fromhex(digest_hex))
    except (ValueError, TypeError):
        return False


class LoginThrottle:
    """Bloqueio global após falhas repetidas.

    Global (e não por IP) de propósito: atrás do túnel todo tráfego chega de
    127.0.0.1, e cabeçalhos como CF-Connecting-IP podem ser forjados na LAN.
    """

    def __init__(self, max_failures: int = 5, window_seconds: int = 300) -> None:
        self.max_failures = max_failures
        self.window = window_seconds
        self._failures: list[float] = []

    def blocked(self) -> bool:
        cutoff = time.monotonic() - self.window
        self._failures = [t for t in self._failures if t > cutoff]
        return len(self._failures) >= self.max_failures

    def record_failure(self) -> None:
        self._failures.append(time.monotonic())

    def reset(self) -> None:
        self._failures.clear()


throttle = LoginThrottle()


# ---------- Contas ----------

# Verificado quando o usuário não existe, para que o tempo de resposta do login
# não revele quais nomes de conta são válidos.
_DUMMY_HASH = hash_password(secrets.token_hex(16))


def validate_new_account(username: str, password: str) -> None:
    if not USERNAME_PATTERN.fullmatch(username):
        raise ValueError(USERNAME_RULE)
    validate_password(password)


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"A senha deve ter pelo menos {MIN_PASSWORD_LENGTH} caracteres.")


def create_user(username: str, password: str, is_admin: bool = False) -> dict:
    """Cria uma conta. Só o hash da senha é gravado. Lança ValueError se inválida ou repetida."""
    validate_new_account(username, password)
    return storage.create_user(username, hash_password(password), is_admin)


def set_password(user_id: str, password: str) -> None:
    validate_password(password)
    storage.set_password_hash(user_id, hash_password(password))


def authenticate(username: str, password: str) -> dict | None:
    found = storage.get_credentials(username)
    valid = verify_password(password, found[1] if found else _DUMMY_HASH)
    return found[0] if found and valid else None


def check_own_password(user_id: str, password: str) -> bool:
    stored = storage.get_password_hash(user_id)
    return bool(stored) and verify_password(password, stored)


def setup_needed() -> bool:
    """Ainda não existe nenhuma conta: falta criar a do administrador."""
    return storage.count_users() == 0


def adopt_existing_data(user: dict) -> None:
    """Entrega ao primeiro administrador o que existia antes das contas.

    Conversas e documentos antigos não têm dono e, por isso, não aparecem para
    ninguém. Eles passam a pertencer a esta conta — nunca a todas.
    """
    conversations = storage.claim_orphan_conversations(user["id"])
    documents_adopted = documents.adopt_legacy(user["id"])
    if conversations or documents_adopted:
        log.info(
            "Dados anteriores às contas atribuídos a %s: %d conversas, %d documentos.",
            user["username"], conversations, documents_adopted,
        )


def bootstrap() -> None:
    """Migra a senha compartilhada antiga para uma conta administradora, uma única vez."""
    if _legacy_hash and setup_needed():
        user = storage.create_user(LEGACY_USERNAME, _legacy_hash, is_admin=True)
        log.info("Senha compartilhada migrada para a conta administradora %r.", LEGACY_USERNAME)
        adopt_existing_data(user)


# ---------- Origem da requisição ----------

_PROXY_HEADERS = ("x-forwarded-for", "forwarded", "cf-connecting-ip", "cf-ray")


def is_direct_local(request: Request) -> bool:
    """Conexão feita neste próprio PC, sem intermediários.

    O Cloudflare Tunnel também chega por 127.0.0.1, mas sempre com cabeçalhos
    de proxy — por isso eles desqualificam a requisição.
    """
    return (
        request.client is not None
        and request.client.host in ("127.0.0.1", "::1")
        and not any(header in request.headers for header in _PROXY_HEADERS)
    )


# ---------- Sessões ----------


def create_session(request: Request, response: Response, user_id: str) -> None:
    ttl = settings.session_ttl_seconds
    token = secrets.token_urlsafe(32)
    _sessions[token] = (user_id, time.time() + ttl)
    https = (
        request.url.scheme == "https"
        or request.headers.get("x-forwarded-proto", "").lower() == "https"
    )
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=ttl,
        httponly=True,
        samesite="strict",
        secure=https,
        path="/",
    )


def destroy_session(request: Request, response: Response) -> None:
    _sessions.pop(request.cookies.get(COOKIE_NAME, ""), None)
    response.delete_cookie(COOKIE_NAME, path="/")


def drop_sessions(user_id: str, keep: str = "") -> None:
    """Encerra as sessões de uma conta (senha trocada ou conta removida)."""
    for token in [t for t, (owner, _) in _sessions.items() if owner == user_id and t != keep]:
        del _sessions[token]


def current_user(request: Request) -> dict:
    """Dependência FastAPI: a conta da sessão, ou 401.

    A conta é relida do banco a cada requisição: removê-la ou rebaixá-la vale
    imediatamente, sem esperar a sessão expirar.
    """
    token = request.cookies.get(COOKIE_NAME, "")
    session = _sessions.get(token)
    if session is not None and session[1] >= time.time():
        user = storage.get_user(session[0])
        if user is not None:
            return user
    _sessions.pop(token, None)
    raise HTTPException(status_code=401, detail="Não autenticado")


def require_admin(user: dict = Depends(current_user)) -> dict:
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Apenas o administrador pode fazer isso.")
    return user
