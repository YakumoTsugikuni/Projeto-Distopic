import asyncio
import json
import unittest

import httpx

from . import ROOT
from app import agent, auth, confirmations, tools
from app.tools.context import ToolContext
from app.tools.sandbox import ToolError, resolve_path, roots_hint

ADMIN_CTX = ToolContext(user_id="admin-de-teste", is_admin=True)
USER_CTX = ToolContext(user_id="conta-comum-de-teste", is_admin=False)


class SandboxTest(unittest.TestCase):
    def test_accepts_paths_inside_root(self):
        self.assertEqual(resolve_path("."), ROOT)
        self.assertEqual(resolve_path("sub/arquivo.txt"), ROOT / "sub" / "arquivo.txt")

    def test_rejects_escapes(self):
        for raw in ("..", "sub/../../x", "C:\\Windows", "\\\\servidor\\share", str(ROOT) + "_irmao"):
            with self.subTest(raw=raw), self.assertRaises(ToolError):
                resolve_path(raw)

    def test_rejects_sensitive(self):
        for raw in (".env", ".env.local", ".ssh/id_rsa", "chave.PEM", "AppData/x"):
            with self.subTest(raw=raw), self.assertRaises(ToolError):
                resolve_path(raw)


class ExecuteTest(unittest.TestCase):
    def run_tool(self, name, args):
        return asyncio.run(tools.execute(name, args, ADMIN_CTX))

    def test_lists_directory_and_hides_sensitive(self):
        result = self.run_tool("listar_diretorio", {"caminho": "."})
        self.assertTrue(result.ok)
        self.assertIn("[PASTA] docs", result.output)
        self.assertIn("[ARQUIVO] notas.txt", result.output)
        self.assertNotIn(".env", result.output)

    def test_rejects_bad_calls(self):
        self.assertFalse(self.run_tool("apagar_tudo", {}).ok)
        self.assertFalse(self.run_tool("listar_diretorio", {"caminho": "C:\\Windows"}).ok)
        self.assertFalse(self.run_tool("listar_diretorio", {"extra": 1}).ok)
        self.assertFalse(self.run_tool("listar_diretorio", "nao-e-objeto").ok)


class AgentLoopTest(unittest.TestCase):
    def test_tool_call_round_trip(self):
        requests = []

        def fake_ollama(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            requests.append(body)
            if len(requests) == 1:
                call = {"function": {"name": "listar_diretorio", "arguments": {"caminho": "."}}}
                chunks = [{"message": {"role": "assistant", "content": "", "tool_calls": [call]}, "done": True}]
            else:
                chunks = [
                    {"message": {"role": "assistant", "content": "Há "}, "done": False},
                    {"message": {"role": "assistant", "content": "notas.txt"}, "done": True},
                ]
            return httpx.Response(200, text="\n".join(json.dumps(c) for c in chunks))

        async def collect():
            async with httpx.AsyncClient(transport=httpx.MockTransport(fake_ollama)) as client:
                history = [{"role": "user", "content": "o que tem na pasta?"}]
                return [event async for event in agent.run(client, history, ADMIN_CTX)]

        events = asyncio.run(collect())
        self.assertEqual(
            [e["type"] for e in events], ["tool_call", "tool_result", "token", "token", "done"]
        )
        self.assertTrue(events[1]["ok"])
        # O resultado da ferramenta voltou para o modelo na segunda requisição.
        tool_message = requests[1]["messages"][-1]
        self.assertEqual(tool_message["role"], "tool")
        self.assertIn("notas.txt", tool_message["content"])
        self.assertEqual(requests[0]["tools"][0]["function"]["name"], "listar_diretorio")


class FileToolsTest(unittest.TestCase):
    def run_tool(self, name, args, approved=False):
        async def go():
            prepared = tools.prepare(name, args, ADMIN_CTX)
            if isinstance(prepared, tools.ToolResult):
                return prepared
            return await tools.run(*prepared, ADMIN_CTX, approved=approved)

        return asyncio.run(go())

    def test_read_file_pages(self):
        (ROOT / "longo.txt").write_text("\n".join(f"linha {i}" for i in range(1, 501)), encoding="utf-8")
        first = self.run_tool("ler_arquivo", {"caminho": "longo.txt"})
        self.assertTrue(first.ok)
        self.assertIn("linhas 1 a 200 de 500", first.output)
        self.assertIn("linha_inicial=201", first.output)
        last = self.run_tool("ler_arquivo", {"caminho": "longo.txt", "linha_inicial": 401})
        self.assertIn("linha 500", last.output)
        self.assertNotIn("Há mais linhas", last.output)

    def test_read_file_rejections(self):
        (ROOT / "bin.dat").write_bytes(b"\x00\x01\x02")
        self.assertFalse(self.run_tool("ler_arquivo", {"caminho": ".env"}).ok)
        self.assertFalse(self.run_tool("ler_arquivo", {"caminho": "bin.dat"}).ok)
        self.assertFalse(self.run_tool("ler_arquivo", {"caminho": "nao_existe.txt"}).ok)
        self.assertFalse(self.run_tool("ler_arquivo", {"caminho": "docs"}).ok)

    def test_write_requires_approval(self):
        args = {"caminho": "novo/a.txt", "conteudo": "linha1\nlinha2"}
        blocked = self.run_tool("criar_arquivo", args)
        self.assertFalse(blocked.ok)
        self.assertFalse((ROOT / "novo" / "a.txt").exists())
        self.assertFalse(asyncio.run(tools.execute("criar_arquivo", args, ADMIN_CTX)).ok)

        self.assertTrue(self.run_tool("criar_arquivo", args, approved=True).ok)
        self.assertEqual((ROOT / "novo" / "a.txt").read_bytes(), b"linha1\nlinha2")

    def test_write_never_overwrites_silently(self):
        (ROOT / "existente.txt").write_text("original", encoding="utf-8")
        args = {"caminho": "existente.txt", "conteudo": "novo"}
        self.assertFalse(self.run_tool("criar_arquivo", args, approved=True).ok)
        self.assertEqual((ROOT / "existente.txt").read_text(encoding="utf-8"), "original")

        tool, parsed = tools.prepare("criar_arquivo", {**args, "sobrescrever": True}, ADMIN_CTX)
        self.assertIn("SOBRESCREVER", tools.describe(tool, parsed)[0])
        self.assertTrue(self.run_tool("criar_arquivo", {**args, "sobrescrever": True}, approved=True).ok)
        self.assertEqual((ROOT / "existente.txt").read_text(encoding="utf-8"), "novo")

    def test_write_rejects_bad_targets(self):
        for caminho in ("../fora.txt", ".env", "docs", "a.txt:oculto", "C:\\Windows\\x.txt"):
            with self.subTest(caminho=caminho):
                result = self.run_tool("criar_arquivo", {"caminho": caminho, "conteudo": "x"}, approved=True)
                self.assertFalse(result.ok)


class ConfirmationFlowTest(unittest.TestCase):
    def run_agent(self, filename, approve):
        """Modelo simulado pede criar_arquivo; o 'usuário' responde ao confirm_request."""
        requests = []

        def fake_ollama(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content))
            if len(requests) == 1:
                call = {"function": {"name": "criar_arquivo", "arguments": {"caminho": filename, "conteudo": "oi"}}}
                chunk = {"message": {"role": "assistant", "content": "", "tool_calls": [call]}}
            else:
                chunk = {"message": {"role": "assistant", "content": "ok"}}
            return httpx.Response(200, text=json.dumps(chunk))

        async def collect():
            events = []
            async with httpx.AsyncClient(transport=httpx.MockTransport(fake_ollama)) as client:
                async for event in agent.run(client, [{"role": "user", "content": "crie"}], ADMIN_CTX):
                    events.append(event)
                    if event["type"] == "confirm_request":
                        # Nada foi gravado enquanto o usuário não decide.
                        self.assertFalse((ROOT / filename).exists())
                        self.assertTrue(confirmations.resolve(event["id"], approve))
            return events

        return asyncio.run(collect()), requests

    def test_approved(self):
        events, _ = self.run_agent("aprovado.txt", approve=True)
        self.assertEqual(
            [e["type"] for e in events], ["tool_call", "confirm_request", "tool_result", "token", "done"]
        )
        self.assertTrue(events[2]["ok"])
        self.assertEqual((ROOT / "aprovado.txt").read_text(encoding="utf-8"), "oi")
        # Um pedido já respondido não pode ser reutilizado.
        self.assertFalse(confirmations.resolve(events[1]["id"], True))

    def test_denied(self):
        events, requests = self.run_agent("recusado.txt", approve=False)
        self.assertFalse(events[2]["ok"])
        self.assertFalse((ROOT / "recusado.txt").exists())
        self.assertIn("não autorizou", requests[1]["messages"][-1]["content"])

    def test_invalid_call_skips_confirmation(self):
        events, _ = self.run_agent("../fora.txt", approve=True)
        self.assertNotIn("confirm_request", [e["type"] for e in events])


class AdminOnlyToolsTest(unittest.TestCase):
    """Ferramentas que tocam o disco do PC são exclusivas de administradores."""

    DISK_TOOLS = {"listar_diretorio", "ler_arquivo", "criar_arquivo"}

    def names(self, context):
        return {schema["function"]["name"] for schema in tools.ollama_schemas(context)}

    def test_only_admins_are_offered_disk_tools(self):
        self.assertEqual(self.names(ADMIN_CTX), self.DISK_TOOLS)
        self.assertEqual(self.names(USER_CTX), set())
        self.assertEqual(self.names(ToolContext()), set())  # sem conta: nada

    def test_execution_is_blocked_for_non_admins(self):
        calls = {
            "listar_diretorio": {"caminho": "."},
            "ler_arquivo": {"caminho": "notas.txt"},
            "criar_arquivo": {"caminho": "invasao/x.txt", "conteudo": "x"},
        }
        for context in (USER_CTX, ToolContext()):
            for name, args in calls.items():
                with self.subTest(name=name, admin=context.is_admin):
                    denied = tools.prepare(name, args, context)
                    self.assertIsInstance(denied, tools.ToolResult)
                    self.assertFalse(denied.ok)
                    self.assertIn("administrador", denied.output)
                    # Mesmo pulando prepare() e alegando aprovação, a execução recusa.
                    tool, parsed = tools.prepare(name, args, ADMIN_CTX)
                    forced = asyncio.run(tools.run(tool, parsed, context, approved=True))
                    self.assertFalse(forced.ok)
                    self.assertIn("administrador", forced.output)
        self.assertFalse((ROOT / "invasao").exists())

    def test_agent_refuses_disk_tools_for_common_account(self):
        """O modelo insiste em chamar a ferramenta (estruturada e como JSON no texto)."""
        for as_text in (False, True):
            requests = []

            def fake_ollama(request: httpx.Request) -> httpx.Response:
                requests.append(json.loads(request.content))
                call = {"name": "criar_arquivo", "arguments": {"caminho": "invasao/y.txt", "conteudo": "y"}}
                if len(requests) > 1:
                    message = {"role": "assistant", "content": "Sem permissão."}
                elif as_text:
                    message = {"role": "assistant", "content": json.dumps(call)}
                else:
                    message = {"role": "assistant", "content": "", "tool_calls": [{"function": call}]}
                return httpx.Response(200, text=json.dumps({"message": message}))

            async def collect():
                async with httpx.AsyncClient(transport=httpx.MockTransport(fake_ollama)) as client:
                    history = [{"role": "user", "content": "crie um arquivo"}]
                    return [event async for event in agent.run(client, history, USER_CTX)]

            events = asyncio.run(collect())
            self.assertEqual(requests[0]["tools"], [])
            self.assertNotIn("confirm_request", [e["type"] for e in events])
            self.assertFalse((ROOT / "invasao").exists())
            # Nos dois formatos o modelo recebe o erro claro (e o JSON cru não vai para a tela).
            result = next(e for e in events if e["type"] == "tool_result")
            self.assertFalse(result["ok"])
            self.assertIn("administrador", result["output"])
            self.assertNotIn("criar_arquivo", "".join(e.get("text", "") for e in events))


class NestedPathsTest(unittest.TestCase):
    """criar_arquivo cria a árvore de pastas e resolve bem os caminhos que o modelo manda."""

    def create(self, caminho, conteudo="x", **extra):
        async def go():
            tool, args = tools.prepare("criar_arquivo", {"caminho": caminho, "conteudo": conteudo, **extra}, ADMIN_CTX)
            return tools.describe(tool, args)[0], await tools.run(tool, args, ADMIN_CTX, approved=True)

        return asyncio.run(go())

    def test_creates_missing_folder_tree(self):
        summary, result = self.create("projeto-java/src/main/java/App.java", "class App {}")
        self.assertTrue(result.ok, result.output)
        target = ROOT / "projeto-java" / "src" / "main" / "java" / "App.java"
        self.assertEqual(target.read_text(encoding="utf-8"), "class App {}")
        self.assertIn("Pastas novas", summary)  # o usuário aprova sabendo das pastas
        self.assertIn("Pastas criadas: 4", result.output)
        # Segundo arquivo na mesma árvore: nada a criar além do arquivo.
        summary, result = self.create("projeto-java/src/main/java/Util.java")
        self.assertTrue(result.ok)
        self.assertNotIn("Pastas novas", summary)

    def test_leading_slash_and_backslashes_stay_in_workspace(self):
        for caminho, expected in (
            ("/raiz-barra/a/b.txt", ROOT / "raiz-barra" / "a" / "b.txt"),
            ("\\raiz-contra\\c\\d.txt", ROOT / "raiz-contra" / "c" / "d.txt"),
            ("./ponto/e.txt", ROOT / "ponto" / "e.txt"),
            (f"{ROOT.name}/pelo-nome/f.txt", ROOT / "pelo-nome" / "f.txt"),  # não vira raiz/raiz/…
            (str(ROOT / "absoluto" / "g.txt"), ROOT / "absoluto" / "g.txt"),
        ):
            with self.subTest(caminho=caminho):
                _, result = self.create(caminho)
                self.assertTrue(result.ok, result.output)
                self.assertTrue(expected.is_file())

    def test_folder_name_taken_by_a_file(self):
        (ROOT / "ocupado").write_text("sou um arquivo", encoding="utf-8")
        prepared = tools.prepare("criar_arquivo", {"caminho": "ocupado/dentro/x.txt", "conteudo": "x"}, ADMIN_CTX)
        result = asyncio.run(tools.run(*prepared, ADMIN_CTX, approved=True))
        self.assertFalse(result.ok)
        self.assertIn("já existe um arquivo", result.output)

    def test_multiple_allowed_roots(self):
        import tempfile
        from pathlib import Path

        work = Path(tempfile.mkdtemp(prefix="distopic_raiz_a_")).resolve()
        projects = Path(tempfile.mkdtemp(prefix="distopic_raiz_b_")).resolve()
        roots = (work, projects)

        self.assertEqual(resolve_path("src/App.java", roots), work / "src" / "App.java")
        self.assertEqual(resolve_path(f"{projects.name}/site/index.html", roots), projects / "site" / "index.html")
        self.assertEqual(resolve_path(str(projects / "api" / "main.py"), roots), projects / "api" / "main.py")
        self.assertEqual(resolve_path(f"{work.name}/notas.md", roots), work / "notas.md")
        # As validações continuam valendo em todas as raízes.
        for raw in (f"{projects.name}/../../fora.txt", f"{projects.name}/.env", f"{projects.name}/.ssh/id",
                    "C:\\Windows\\x", "C:solto.txt", str(projects) + "_vizinha/x.txt"):
            with self.subTest(raw=raw), self.assertRaises(ToolError):
                resolve_path(raw, roots)
        hint = roots_hint(roots)
        self.assertIn(str(work), hint)
        self.assertIn(str(projects), hint)


class AuthTest(unittest.TestCase):
    def test_password_hash(self):
        stored = auth.hash_password("uma senha de teste")
        self.assertTrue(auth.verify_password("uma senha de teste", stored))
        self.assertFalse(auth.verify_password("outra senha", stored))
        self.assertFalse(auth.verify_password("x", "lixo"))

    def test_throttle(self):
        throttle = auth.LoginThrottle(max_failures=2)
        throttle.record_failure()
        self.assertFalse(throttle.blocked())
        throttle.record_failure()
        self.assertTrue(throttle.blocked())


if __name__ == "__main__":
    unittest.main()
