"""Define (ou troca) a senha de acesso pelo terminal: python -m app.set_password

Grava apenas o hash (scrypt) no arquivo de configuração — a senha nunca é armazenada.
Na primeira execução a senha também pode ser criada pela própria interface.
"""
from __future__ import annotations

import getpass

from . import auth
from .config import ENV_FILE


def main() -> None:
    password = getpass.getpass(f"Nova senha (mínimo {auth.MIN_PASSWORD_LENGTH} caracteres): ")
    if len(password) < auth.MIN_PASSWORD_LENGTH:
        raise SystemExit(f"Senha muito curta: use pelo menos {auth.MIN_PASSWORD_LENGTH} caracteres.")
    if password != getpass.getpass("Repita a senha: "):
        raise SystemExit("As senhas não conferem.")
    auth.store_password(password)
    print(f"Senha definida. Hash gravado em {ENV_FILE}")


if __name__ == "__main__":
    main()
