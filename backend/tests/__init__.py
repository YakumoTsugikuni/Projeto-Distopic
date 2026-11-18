"""Aponta a configuração para uma pasta temporária antes de importar o app."""
import os
import tempfile
from pathlib import Path

ROOT = Path(tempfile.mkdtemp(prefix="distopia_test_")).resolve()
os.environ["DISTOPIA_ALLOWED_ROOTS"] = str(ROOT)
# Banco de conversas descartável: os testes nunca tocam no banco real.
os.environ["DISTOPIA_DATA_DIR"] = tempfile.mkdtemp(prefix="distopia_test_db_")

# Os testes nunca falam com o Ollama real (nem carregam o modelo na GPU).
os.environ["DISTOPIA_PREWARM"] = "0"
os.environ["DISTOPIA_OLLAMA_URL"] = "http://127.0.0.1:9"

# Senha só para os testes, com custo de scrypt mínimo para serem rápidos.
import hashlib  # noqa: E402
import secrets  # noqa: E402

PASSWORD = secrets.token_urlsafe(16)
_salt = secrets.token_bytes(16)
_digest = hashlib.scrypt(PASSWORD.encode(), salt=_salt, n=16, r=8, p=1, dklen=32)
os.environ["DISTOPIA_PASSWORD_HASH"] = f"scrypt:16:8:1:{_salt.hex()}:{_digest.hex()}"

# Embeddings falsos e determinísticos (saco de palavras): os testes de RAG não
# dependem do Ollama. Textos com palavras em comum ficam próximos.
import math  # noqa: E402
import re  # noqa: E402
import zlib  # noqa: E402

from app import auth, storage  # noqa: E402
from app.rag import embedder  # noqa: E402


async def fake_embedding(text: str) -> list[float]:
    vector = [0.0] * 128
    for word in re.findall(r"\w+", text.lower()):
        vector[zlib.crc32(word.encode()) % 128] += 1.0
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


embedder.get_embedding = fake_embedding

# Como numa instalação que já usava a senha compartilhada: ela vira a conta "admin".
storage.init()
auth.bootstrap()
ADMIN = storage.get_credentials("admin")[0]

(ROOT / "docs").mkdir()
(ROOT / "notas.txt").write_text("oi", encoding="utf-8")
(ROOT / ".env").write_text("SEGREDO=1", encoding="utf-8")
