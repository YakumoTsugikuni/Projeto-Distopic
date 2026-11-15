import asyncio
import json
import unittest
import warnings

import httpx

from . import PASSWORD, ROOT
from app import chat, confirmations, storage

storage.init()
# Conta usada pelos testes que falam direto com o storage.
USER = storage.create_user("conversas", "sem-senha", is_admin=True)
UID = USER["id"]


def fake_ollama(script):
    """Transporte simulado: cada requisição recebe o próximo item do roteiro."""
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        chunks = script[len(requests) - 1]
        return httpx.Response(200, text="\n".join(json.dumps(c) for c in chunks))

    return httpx.MockTransport(handler), requests


def text_chunks(*parts):
    return [{"message": {"role": "assistant", "content": p}} for p in parts]


def tool_chunk(name, arguments):
    call = {"function": {"name": name, "arguments": arguments}}
    return [{"message": {"role": "assistant", "content": "", "tool_calls": [call]}}]


class StorageTest(unittest.TestCase):
    def test_title_comes_from_first_user_message(self):
        conv = storage.create_conversation(UID)
        storage.add_message(conv["id"], "user", "  como   listar\narquivos? " + "x" * 100)
        storage.add_message(conv["id"], "user", "segunda pergunta")
        title = storage.get_conversation(conv["id"], UID)["title"]
        self.assertTrue(title.startswith("como listar arquivos?"))
        self.assertEqual(len(title), storage.TITLE_MAX)

    def test_pagination(self):
        conv = storage.create_conversation(UID)
        ids = [storage.add_message(conv["id"], "user", f"m{i}") for i in range(120)]
        page, has_more = storage.get_messages(conv["id"], limit=50)
        self.assertEqual([m["content"] for m in page], [f"m{i}" for i in range(70, 120)])
        self.assertTrue(has_more)
        page, has_more = storage.get_messages(conv["id"], before=page[0]["id"], limit=50)
        self.assertEqual(page[0]["content"], "m20")
        page, has_more = storage.get_messages(conv["id"], before=page[0]["id"], limit=50)
        self.assertEqual([m["id"] for m in page], ids[:20])
        self.assertFalse(has_more)

    def test_list_order_rename_delete_cascade(self):
        first = storage.create_conversation(UID)
        second = storage.create_conversation(UID)
        storage.add_message(first["id"], "user", "reativada")  # sobe para o topo
        listed = [c["id"] for c in storage.list_conversations(UID, limit=200)]
        self.assertLess(listed.index(first["id"]), listed.index(second["id"]))

        self.assertTrue(storage.rename_conversation(first["id"], UID, "Novo nome"))
        self.assertEqual(storage.get_conversation(first["id"], UID)["title"], "Novo nome")

        self.assertTrue(storage.delete_conversation(first["id"], UID))
        self.assertIsNone(storage.get_conversation(first["id"], UID))
        self.assertEqual(storage.get_messages(first["id"]), ([], False))
        self.assertFalse(storage.delete_conversation(first["id"], UID))

    def test_context_merges_and_skips_tools(self):
        conv = storage.create_conversation(UID)
        storage.add_message(conv["id"], "user", "pergunta")
        storage.add_message(conv["id"], "assistant", "vou verificar")
        storage.add_message(conv["id"], "tool", "saida", {"name": "listar_diretorio", "ok": True})
        storage.add_message(conv["id"], "assistant", "pronto")
        self.assertEqual(
            storage.context_messages(conv["id"]),
            [{"role": "user", "content": "pergunta"}, {"role": "assistant", "content": "vou verificar\npronto"}],
        )


class ChatTurnTest(unittest.TestCase):
    def test_turn_with_tool_is_persisted_and_reused(self):
        conv = storage.create_conversation(UID)
        storage.add_message(conv["id"], "user", "o que tem na pasta?")
        transport, requests = fake_ollama(
            [tool_chunk("listar_diretorio", {"caminho": "."}), text_chunks("Há ", "notas.txt"), text_chunks("ok")]
        )

        async def go():
            async with httpx.AsyncClient(transport=transport) as client:
                async for _ in chat.run_turn(client, conv["id"], USER):
                    pass
                storage.add_message(conv["id"], "user", "e depois?")
                async for _ in chat.run_turn(client, conv["id"], USER):
                    pass

        asyncio.run(go())
        messages, _ = storage.get_messages(conv["id"])
        self.assertEqual([m["role"] for m in messages], ["user", "tool", "assistant", "user", "assistant"])
        self.assertEqual(messages[1]["meta"]["name"], "listar_diretorio")
        self.assertTrue(messages[1]["meta"]["ok"])
        self.assertEqual(messages[2]["content"], "Há notas.txt")
        self.assertIsNone(messages[2]["meta"])
        # O segundo turno recebeu do banco o histórico do primeiro.
        self.assertEqual(
            [(m["role"], m["content"]) for m in requests[2]["messages"]],
            [("user", "o que tem na pasta?"), ("assistant", "Há notas.txt"), ("user", "e depois?")],
        )

    def test_confirmation_decision_is_recorded(self):
        conv = storage.create_conversation(UID)
        storage.add_message(conv["id"], "user", "crie")
        transport, _ = fake_ollama(
            [tool_chunk("criar_arquivo", {"caminho": "persistido.txt", "conteudo": "x"}), text_chunks("feito")]
        )

        async def go():
            async with httpx.AsyncClient(transport=transport) as client:
                async for event in chat.run_turn(client, conv["id"], USER):
                    if event["type"] == "confirm_request":
                        confirmations.resolve(event["id"], False)

        asyncio.run(go())
        tool_message = storage.get_messages(conv["id"])[0][1]
        self.assertEqual(tool_message["meta"]["confirmed"], False)
        self.assertFalse((ROOT / "persistido.txt").exists())

    def test_interrupted_answer_is_saved_as_partial(self):
        conv = storage.create_conversation(UID)
        storage.add_message(conv["id"], "user", "conte uma história")
        transport, _ = fake_ollama([text_chunks("Era uma vez", " e nunca terminou")])

        async def go():
            async with httpx.AsyncClient(transport=transport) as client:
                stream = chat.run_turn(client, conv["id"], USER)
                async for event in stream:
                    if event["type"] == "token":
                        break  # o cliente desconectou no meio da resposta
                await stream.aclose()

        asyncio.run(go())
        last = storage.get_messages(conv["id"])[0][-1]
        self.assertEqual((last["role"], last["content"]), ("assistant", "Era uma vez"))
        self.assertEqual(last["meta"], {"interrupted": True})


class SpeedTest(unittest.TestCase):
    def test_warm_up_loads_model_without_generating(self):
        from app import ollama_client
        from app.config import settings

        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"done": True})

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await ollama_client.warm_up(client)

        asyncio.run(go())
        self.assertEqual(seen[0]["messages"], [])
        # Mesmo num_ctx das conversas, senão o Ollama recarrega o modelo depois.
        self.assertEqual(seen[0]["options"]["num_ctx"], settings.num_ctx)
        self.assertEqual(seen[0]["keep_alive"], settings.keep_alive)

    def test_response_limit_is_sent_and_truncation_reported(self):
        from app import agent
        from app.config import settings

        chunks = [{"message": {"role": "assistant", "content": "texto longo"}, "done": True, "done_reason": "length"}]
        transport, requests = fake_ollama([chunks])

        async def go():
            async with httpx.AsyncClient(transport=transport) as client:
                return [e["type"] async for e in agent.run(client, [{"role": "user", "content": "oi"}])]

        self.assertEqual(asyncio.run(go()), ["token", "truncated", "done"])
        self.assertEqual(requests[0]["options"]["num_predict"], settings.num_predict)


class TextToolCallTest(unittest.TestCase):
    """O qwen2.5-coder costuma escrever a chamada de ferramenta como JSON no texto."""

    def run_agent(self, script):
        from app import agent

        transport, requests = fake_ollama(script)

        async def go():
            async with httpx.AsyncClient(transport=transport) as client:
                return [e async for e in agent.run(client, [{"role": "user", "content": "x"}])]

        return asyncio.run(go()), requests

    def test_json_in_text_becomes_tool_call(self):
        for raw in (
            '{\n  "name": "listar_diretorio",\n  "arguments": {"caminho": "."}\n}',
            '<tool_call>\n{"name": "listar_diretorio", "arguments": {"caminho": "."}}\n</tool_call>',
            '```json\n{"name": "listar_diretorio", "arguments": {"caminho": "."}}\n```',
        ):
            with self.subTest(raw=raw):
                # Entregue em pedaços, como no streaming real.
                parts = [raw[i : i + 7] for i in range(0, len(raw), 7)]
                events, _ = self.run_agent([text_chunks(*parts), text_chunks("Pronto.")])
                self.assertEqual([e["type"] for e in events], ["tool_call", "tool_result", "token", "done"])
                self.assertTrue(events[1]["ok"])
                # O JSON cru nunca apareceu como texto para o usuário.
                self.assertEqual(events[2]["text"], "Pronto.")

    def test_invented_tool_is_unwrapped_not_executed(self):
        raw = '{"name": "responder", "arguments": {"conteudo": "Listas são mutáveis."}}'
        events, requests = self.run_agent([text_chunks(raw)])
        self.assertEqual([(e["type"], e.get("text")) for e in events], [("token", "Listas são mutáveis."), ("done", None)])
        self.assertEqual(len(requests), 1)

    def test_ordinary_text_and_json_answers_pass_through(self):
        events, _ = self.run_agent([text_chunks("Olá", ", senhor.")])
        self.assertEqual("".join(e["text"] for e in events if e["type"] == "token"), "Olá, senhor.")
        events, _ = self.run_agent([text_chunks('{"cor": ', '"vermelho"}')])
        self.assertEqual([e.get("text") for e in events if e["type"] == "token"], ['{"cor": "vermelho"}'])


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient
        from app import auth
        from app.main import app

        auth.throttle.reset()
        # Este fluxo é sobre conversas: parte de um administrador sem documentos
        # (outros testes podem ter deixado algum na Biblioteca dele).
        from app import documents
        from . import ADMIN

        for leftover in storage.list_documents(ADMIN["id"]):
            documents.remove(leftover)
        cls.app = app
        cls.ctx = TestClient(app)
        cls.client = cls.ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def test_full_flow(self):
        c = self.client
        for method, url in (("GET", "/api/conversations"), ("POST", "/api/conversations"),
                            ("GET", "/api/conversations/x"), ("DELETE", "/api/conversations/x")):
            self.assertEqual(c.request(method, url).status_code, 401, url)
        self.assertEqual(c.post("/api/chat", json={"message": "oi"}).status_code, 401)

        # A senha compartilhada antiga virou a conta "admin" (auth.bootstrap).
        self.assertEqual(c.post("/api/login", json={"username": "admin", "password": "errada"}).status_code, 401)
        self.assertEqual(c.post("/api/login", json={"username": "admin", "password": PASSWORD}).status_code, 200)
        self.assertEqual(c.get("/api/me").json()["user"]["is_admin"], True)
        self.assertEqual(c.get("/api/conversations/nao-existe").status_code, 404)
        self.assertEqual(c.post("/api/chat", json={"conversation_id": "nao-existe", "message": "oi"}).status_code, 404)
        # Histórico forjado pelo cliente não é mais aceito.
        self.assertEqual(c.post("/api/chat", json={"messages": [{"role": "system", "content": "x"}]}).status_code, 422)

        transport, requests = fake_ollama([text_chunks("Olá."), text_chunks("Sim.")])
        self.app.state.http = httpx.AsyncClient(transport=transport)

        events = [json.loads(line) for line in c.post("/api/chat", json={"message": "bom dia"}).text.splitlines()]
        self.assertEqual([e["type"] for e in events], ["conversation", "token", "done"])
        cid = events[0]["id"]
        self.assertEqual(events[0]["title"], "bom dia")

        c.post("/api/chat", json={"conversation_id": cid, "message": "tudo certo?"})
        self.assertEqual([m["content"] for m in requests[1]["messages"]], ["bom dia", "Olá.", "tudo certo?"])

        before = len(requests)
        listed = c.get("/api/conversations").json()["conversations"]
        self.assertEqual(listed[0]["id"], cid)
        detail = c.get(f"/api/conversations/{cid}?limit=2").json()
        self.assertEqual([m["content"] for m in detail["messages"]], ["tudo certo?", "Sim."])
        self.assertTrue(detail["has_more"])
        self.assertEqual(c.patch(f"/api/conversations/{cid}", json={"title": "Saudações"}).status_code, 200)
        self.assertEqual(c.get(f"/api/conversations/{cid}").json()["conversation"]["title"], "Saudações")
        self.assertEqual(c.delete(f"/api/conversations/{cid}").status_code, 200)
        self.assertEqual(c.get(f"/api/conversations/{cid}").status_code, 404)
        # Listar, abrir, renomear e apagar não chamaram o modelo.
        self.assertEqual(len(requests), before)


if __name__ == "__main__":
    unittest.main()
