"""Configuração central. Valores vêm de variáveis de ambiente ou de backend/.env."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Distopia"

# Empacotado (PyInstaller, aplicativo instalado): não existe pasta de projeto.
# O frontend vem embutido no executável e a configuração fica na pasta de dados.
FROZEN = getattr(sys, "frozen", False)
if FROZEN:
    FRONTEND_DIR = Path(sys._MEIPASS) / "frontend"
    ENV_FILE = DEFAULT_DATA_DIR / ".env"
    DEFAULT_WORKSPACE = Path.home() / "Distopia"
else:
    BACKEND_DIR = Path(__file__).resolve().parent.parent
    FRONTEND_DIR = BACKEND_DIR.parent / "frontend"
    ENV_FILE = BACKEND_DIR / ".env"
    DEFAULT_WORKSPACE = BACKEND_DIR.parent / "workspace"


def _read_env_file(path: Path) -> dict[str, str]:
    """Parser mínimo de .env (KEY=VALUE, sem interpolação)."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    password_hash: str
    session_ttl_seconds: int
    allowed_roots: tuple[Path, ...]
    data_dir: Path
    ollama_url: str
    model: str
    num_ctx: int
    temperature: float
    keep_alive: str
    num_predict: int
    prewarm: bool


def load_settings() -> Settings:
    file_values = _read_env_file(ENV_FILE)

    def get(key: str, default: str) -> str:
        return os.environ.get(key, file_values.get(key, default))

    # Várias pastas separadas por ';'. A primeira é a pasta de trabalho (base dos
    # caminhos relativos); as regras de acesso ficam em tools/sandbox.py.
    raw_roots = get("DISTOPIA_ALLOWED_ROOTS", str(DEFAULT_WORKSPACE))
    roots = tuple(Path(r.strip()).resolve() for r in raw_roots.split(";") if r.strip())

    return Settings(
        # 127.0.0.1 por padrão: o cloudflared conecta via localhost, então não é
        # preciso escutar na rede. Use 0.0.0.0 apenas para acesso pela LAN.
        host=get("DISTOPIA_HOST", "127.0.0.1"),
        port=int(get("DISTOPIA_PORT", "8000")),
        password_hash=get("DISTOPIA_PASSWORD_HASH", ""),
        session_ttl_seconds=int(get("DISTOPIA_SESSION_TTL", str(12 * 3600))),
        allowed_roots=roots,
        # Fora da pasta do projeto: ela pode estar no OneDrive, e sincronizar um
        # banco SQLite em uso causa conflitos e corrupção.
        data_dir=Path(get("DISTOPIA_DATA_DIR", str(DEFAULT_DATA_DIR))).resolve(),
        ollama_url=get("DISTOPIA_OLLAMA_URL", "http://localhost:11434").rstrip("/"),
        model=get("DISTOPIA_MODEL", "distopia"),
        # 8192 tokens: um 7B Q4_K_M (~4,7 GB) + cache KV desse tamanho cabe nos
        # 8 GB da RX 7600 sem transbordar para a RAM.
        num_ctx=int(get("DISTOPIA_NUM_CTX", "8192")),
        temperature=float(get("DISTOPIA_TEMPERATURE", "0.3")),
        # Mantém o modelo carregado na VRAM entre mensagens (evita recarga de ~10 s).
        keep_alive=get("DISTOPIA_KEEP_ALIVE", "30m"),
        # Teto de tokens por resposta: mantém o estilo curto e evita respostas
        # intermináveis. Folga suficiente para o conteúdo de um criar_arquivo.
        num_predict=int(get("DISTOPIA_NUM_PREDICT", "1024")),
        # Carrega o modelo na VRAM ao iniciar, para a 1ª resposta não esperar a carga.
        prewarm=get("DISTOPIA_PREWARM", "1").lower() not in ("0", "false", "no"),
    )


settings = load_settings()
