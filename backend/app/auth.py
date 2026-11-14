"""Autenticação por senha única + sessão em cookie HttpOnly.

Pensado para exposição via Cloudflare Tunnel: toda rota /api/* (exceto login)
exige sessão válida; o cookie é SameSite=Strict e Secure quando atrás de HTTPS.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from fastapi import HTTPException, Request, Response

from .config import ENV_FILE, settings

COOKIE_NAME = "distopic_session"
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**15, 8, 1
_SCRYPT_MAXMEM = 2**26

# token -> instante de expiração. Em memória: reiniciar o servidor desloga todos.
_sessions: dict[str, float] = {}


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


MIN_PASSWORD_LENGTH = 12
_PASSWORD_KEY = "DISTOPIC_PASSWORD_HASH"
_password_hash = settings.password_hash


def password_configured() -> bool:
    return bool(_password_hash)


def check_password(password: str) -> bool:
    return verify_password(password, _password_hash)


def store_password(password: str) -> None:
    """Grava o hash (nunca a senha) no arquivo de configuração e passa a usá-lo."""
    global _password_hash
    hashed = hash_password(password)
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.is_file() else []
    lines = [line for line in lines if not line.startswith(f"{_PASSWORD_KEY}=")]
    lines.append(f"{_PASSWORD_KEY}={hashed}")
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _password_hash = hashed


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


def create_session(request: Request, response: Response) -> None:
    ttl = settings.session_ttl_seconds
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + ttl
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


def require_session(request: Request) -> None:
    """Dependência FastAPI: 401 se não houver sessão válida."""
    token = request.cookies.get(COOKIE_NAME, "")
    expires = _sessions.get(token)
    if expires is None or expires < time.time():
        _sessions.pop(token, None)
        raise HTTPException(status_code=401, detail="Não autenticado")
