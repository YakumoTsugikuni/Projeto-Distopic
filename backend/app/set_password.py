"""Define a senha de acesso: python -m app.set_password

Grava apenas o hash (scrypt) em backend/.env — a senha nunca é armazenada.
"""
from __future__ import annotations

import getpass

from .auth import hash_password
from .config import ENV_FILE

KEY = "DISTOPIC_PASSWORD_HASH"
MIN_LENGTH = 12


def main() -> None:
    password = getpass.getpass(f"Nova senha (mínimo {MIN_LENGTH} caracteres): ")
    if len(password) < MIN_LENGTH:
        raise SystemExit(f"Senha muito curta: use pelo menos {MIN_LENGTH} caracteres.")
    if password != getpass.getpass("Repita a senha: "):
        raise SystemExit("As senhas não conferem.")

    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.is_file() else []
    lines = [line for line in lines if not line.startswith(f"{KEY}=")]
    lines.append(f"{KEY}={hash_password(password)}")
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Senha definida. Hash gravado em {ENV_FILE}")


if __name__ == "__main__":
    main()
