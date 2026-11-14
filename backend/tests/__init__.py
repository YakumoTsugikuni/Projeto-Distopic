"""Aponta a configuração para uma pasta temporária antes de importar o app."""
import os
import tempfile
from pathlib import Path

ROOT = Path(tempfile.mkdtemp(prefix="distopic_test_")).resolve()
os.environ["DISTOPIC_ALLOWED_ROOTS"] = str(ROOT)
# Banco de conversas descartável: os testes nunca tocam no banco real.
os.environ["DISTOPIC_DATA_DIR"] = tempfile.mkdtemp(prefix="distopic_test_db_")

# Os testes nunca falam com o Ollama real (nem carregam o modelo na GPU).
os.environ["DISTOPIC_PREWARM"] = "0"
os.environ["DISTOPIC_OLLAMA_URL"] = "http://127.0.0.1:9"

# Senha só para os testes, com custo de scrypt mínimo para serem rápidos.
import hashlib  # noqa: E402
import secrets  # noqa: E402

PASSWORD = secrets.token_urlsafe(16)
_salt = secrets.token_bytes(16)
_digest = hashlib.scrypt(PASSWORD.encode(), salt=_salt, n=16, r=8, p=1, dklen=32)
os.environ["DISTOPIC_PASSWORD_HASH"] = f"scrypt:16:8:1:{_salt.hex()}:{_digest.hex()}"

(ROOT / "docs").mkdir()
(ROOT / "notas.txt").write_text("oi", encoding="utf-8")
(ROOT / ".env").write_text("SEGREDO=1", encoding="utf-8")
