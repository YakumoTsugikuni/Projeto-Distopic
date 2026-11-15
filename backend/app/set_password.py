"""Define a senha de uma conta pelo terminal: python -m app.set_password [usuario]

Serve para recuperar o acesso (senha esquecida). Sem argumento, age sobre a
conta administradora; se ainda não existir nenhuma conta, cria a do administrador.
Só o hash (scrypt) é gravado — a senha nunca é armazenada.
"""
from __future__ import annotations

import getpass
import sys

from . import auth, storage


def _ask_password() -> str:
    password = getpass.getpass(f"Nova senha (mínimo {auth.MIN_PASSWORD_LENGTH} caracteres): ")
    if len(password) < auth.MIN_PASSWORD_LENGTH:
        raise SystemExit(f"Senha muito curta: use pelo menos {auth.MIN_PASSWORD_LENGTH} caracteres.")
    if password != getpass.getpass("Repita a senha: "):
        raise SystemExit("As senhas não conferem.")
    return password


def main(args: list[str] | None = None) -> None:
    args = sys.argv[1:] if args is None else args
    storage.init()
    auth.bootstrap()

    if auth.setup_needed():
        username = args[0] if args else auth.LEGACY_USERNAME
        try:
            user = auth.create_user(username, _ask_password(), is_admin=True)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        auth.adopt_existing_data(user)
        print(f"Conta administradora {user['username']!r} criada.")
        return

    if args:
        found = storage.get_credentials(args[0])
        if found is None:
            raise SystemExit(f"Não existe a conta {args[0]!r}.")
        user = found[0]
    else:
        user = storage.first_admin()
        if user is None:
            raise SystemExit("Informe o usuário: python -m app.set_password <usuario>")
    auth.set_password(user["id"], _ask_password())
    print(f"Senha da conta {user['username']!r} alterada.")


if __name__ == "__main__":
    main()
