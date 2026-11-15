"""Inicia o servidor: python -m app"""
import os
import threading

import uvicorn

from .config import settings


def _exit_with_parent(pid: int) -> None:
    """Encerra este processo quando o aplicativo desktop que o iniciou fechar.

    Cobre também o caso de o aplicativo travar ou ser finalizado à força, em
    que ele não teria chance de encerrar o backend.
    """
    import ctypes

    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    synchronize, infinite = 0x00100000, 0xFFFFFFFF
    handle = kernel32.OpenProcess(synchronize, False, pid)
    if handle:
        kernel32.WaitForSingleObject(handle, infinite)
    os._exit(0)  # o pai já não existe (ou nunca existiu)


def main() -> None:
    import sys

    if sys.argv[1:2] == ["set-password"]:  # usado pelo executável instalado
        from .set_password import main as set_password

        return set_password(sys.argv[2:])

    parent = os.environ.get("DISTOPIC_PARENT_PID", "")
    if parent.isdigit() and os.name == "nt":
        threading.Thread(target=_exit_with_parent, args=(int(parent),), daemon=True).start()

    from .main import app  # objeto em vez de "app.main:app": funciona também empacotado

    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
