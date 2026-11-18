"""Contas individuais e isolamento de conversas e documentos (RAG) entre elas."""
import asyncio
import json
import unittest
import warnings
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from . import ADMIN, PASSWORD
from app import auth, documents, storage
from app.main import app
from app.rag import indexer, retriever

warnings.simplefilter("ignore")

SENHA = "senha-de-teste-longa"


def search(query, **filters):
    return asyncio.run(retriever.search_docs(query, **filters))


def sources(query, **filters):
    return {hit["source"] for hit in search(query, **filters)}


def upload(client, name, text, **fields):
    return client.post("/api/docs/upload", files={"file": (name, text.encode("utf-8"))}, data=fields)


class AccountsCase(unittest.TestCase):
    """Três sessões: o administrador e duas contas comuns, Ana e Bia."""

    @classmethod
    def setUpClass(cls):
        auth.throttle.reset()
        cls.clients = []

        def session(username, password):
            ctx = TestClient(app)
            client = ctx.__enter__()
            cls.clients.append(ctx)
            assert client.post("/api/login", json={"username": username, "password": password}).status_code == 200
            return client

        cls.admin = session("admin", PASSWORD)
        suffix = cls.__name__.lower()
        cls.users = {}
        for name in ("ana", "bia"):
            created = cls.admin.post("/api/users", json={"username": f"{name}.{suffix}", "password": SENHA})
            assert created.status_code == 200, created.text
            cls.users[name] = created.json()
        cls.ana = session(f"ana.{suffix}", SENHA)
        cls.bia = session(f"bia.{suffix}", SENHA)
        cls.ana_id = cls.users["ana"]["id"]
        cls.bia_id = cls.users["bia"]["id"]

    @classmethod
    def tearDownClass(cls):
        for ctx in cls.clients:
            ctx.__exit__(None, None, None)


class AccountManagementTest(AccountsCase):
    def test_no_open_signup_and_admin_only_management(self):
        with TestClient(app) as anonymous:
            self.assertEqual(anonymous.post("/api/users", json={"username": "intruso", "password": SENHA}).status_code, 401)
            # Já existem contas: o fluxo de primeira configuração está fechado, até do próprio PC.
            self.assertEqual(anonymous.post("/api/setup", json={"username": "intruso", "password": SENHA}).status_code, 403)
        with TestClient(app, client=("127.0.0.1", 50000)) as local:
            self.assertFalse(local.get("/api/status").json()["setup"])
            self.assertEqual(local.post("/api/setup", json={"username": "intruso", "password": SENHA}).status_code, 403)

        self.assertEqual(self.ana.post("/api/users", json={"username": "outra", "password": SENHA}).status_code, 403)
        self.assertEqual(self.ana.get("/api/users").status_code, 403)
        self.assertEqual(self.ana.delete(f"/api/users/{self.bia_id}").status_code, 403)
        self.assertEqual(self.ana.post(f"/api/users/{self.bia_id}/password", json={"password": SENHA}).status_code, 403)

        me = self.ana.get("/api/me").json()["user"]
        self.assertEqual((me["id"], me["is_admin"]), (self.ana_id, False))
        listed = {u["username"]: u for u in self.admin.get("/api/users").json()["users"]}
        self.assertTrue(listed["admin"]["is_admin"])
        self.assertNotIn("password_hash", listed["admin"])

    def test_account_validation(self):
        post = lambda body: self.admin.post("/api/users", json=body).status_code  # noqa: E731
        self.assertEqual(post({"username": "ab", "password": SENHA}), 422)
        self.assertEqual(post({"username": "nome com espaço", "password": SENHA}), 422)
        self.assertEqual(post({"username": "valido", "password": "curta"}), 422)
        self.assertEqual(post({"username": "ADMIN", "password": SENHA}), 422)  # repetido (sem diferenciar caixa)
        self.assertEqual(self.admin.delete(f"/api/users/{ADMIN['id']}").status_code, 400)  # a própria conta

    def test_passwords_are_hashed_and_changeable(self):
        stored = storage.get_password_hash(self.ana_id)
        self.assertTrue(stored.startswith("scrypt:"))
        self.assertNotIn(SENHA, stored)

        created = self.admin.post("/api/users", json={"username": "troca.senha", "password": SENHA}).json()
        with TestClient(app) as c:
            login = lambda pw: c.post("/api/login", json={"username": "troca.senha", "password": pw}).status_code  # noqa: E731
            self.assertEqual(login(SENHA), 200)
            wrong = c.post("/api/me/password", json={"current_password": "errada", "new_password": "nova-senha-longa-1"})
            self.assertEqual(wrong.status_code, 403)
            ok = c.post("/api/me/password", json={"current_password": SENHA, "new_password": "nova-senha-longa-1"})
            self.assertEqual(ok.status_code, 200)
            self.assertEqual(c.get("/api/me").status_code, 200)  # a sessão atual continua
            # O administrador redefine a senha: as sessões daquela conta caem.
            reset = self.admin.post(f"/api/users/{created['id']}/password", json={"password": "redefinida-pelo-admin"})
            self.assertEqual(reset.status_code, 200)
            self.assertEqual(c.get("/api/me").status_code, 401)
            self.assertEqual(login("nova-senha-longa-1"), 401)
            self.assertEqual(login("redefinida-pelo-admin"), 200)
        auth.throttle.reset()

    def test_first_account_setup_is_local_only_and_becomes_admin(self):
        """Instalação nova (sem contas e sem senha antiga), em um banco à parte."""
        import tempfile

        saved = (storage.DB_PATH, auth._legacy_hash)
        storage.DB_PATH = Path(tempfile.mkdtemp(prefix="distopia_test_setup_")) / "novo.db"
        auth._legacy_hash = ""
        try:
            with TestClient(app, client=("192.168.0.50", 50000)) as lan:
                self.assertFalse(lan.get("/api/status").json()["setup"])
                self.assertEqual(lan.post("/api/setup", json={"username": "dono", "password": SENHA}).status_code, 403)
            with TestClient(app, client=("127.0.0.1", 50000)) as c:
                tunnel = {"CF-Connecting-IP": "203.0.113.9"}  # Cloudflare Tunnel chega por localhost
                self.assertFalse(c.get("/api/status", headers=tunnel).json()["setup"])
                self.assertEqual(c.post("/api/setup", json={"username": "dono", "password": SENHA}, headers=tunnel).status_code, 403)

                self.assertTrue(c.get("/api/status").json()["setup"])
                self.assertEqual(c.post("/api/setup", json={"username": "dono", "password": "curta"}).status_code, 422)
                self.assertEqual(c.post("/api/setup", json={"username": "dono", "password": SENHA}).status_code, 200)
                me = c.get("/api/me").json()["user"]
                self.assertEqual((me["username"], me["is_admin"]), ("dono", True))
                self.assertFalse(c.get("/api/status").json()["setup"])
                self.assertEqual(c.post("/api/setup", json={"username": "segundo", "password": SENHA}).status_code, 403)
        finally:
            storage.DB_PATH, auth._legacy_hash = saved
            auth.throttle.reset()

    def test_removing_account_erases_its_data(self):
        created = self.admin.post("/api/users", json={"username": "de.saida", "password": SENHA}).json()
        with TestClient(app) as c:
            c.post("/api/login", json={"username": "de.saida", "password": SENHA})
            self.assertEqual(upload(c, "particular.txt", "relatorio particular da conta de saida").status_code, 200)
            folder = documents.LIBRARY_DIR / created["id"]
            self.assertTrue(any(folder.iterdir()))
            self.assertEqual(self.admin.delete(f"/api/users/{created['id']}").status_code, 200)
            self.assertEqual(c.get("/api/me").status_code, 401)
        self.assertFalse(folder.exists())
        self.assertEqual(search("relatorio particular", owner_id=created["id"]), [])


class ConversationIsolationTest(AccountsCase):
    def test_conversations_are_private(self):
        cid = self.ana.post("/api/conversations").json()["id"]
        storage.add_message(cid, "user", "assunto particular da Ana")

        self.assertIn(cid, [c["id"] for c in self.ana.get("/api/conversations").json()["conversations"]])
        self.assertNotIn(cid, [c["id"] for c in self.bia.get("/api/conversations").json()["conversations"]])
        self.assertNotIn(cid, [c["id"] for c in self.admin.get("/api/conversations").json()["conversations"]])

        # Para outra conta a conversa simplesmente não existe — inclusive para o administrador.
        for other in (self.bia, self.admin):
            self.assertEqual(other.get(f"/api/conversations/{cid}").status_code, 404)
            self.assertEqual(other.patch(f"/api/conversations/{cid}", json={"title": "invadida"}).status_code, 404)
            self.assertEqual(other.delete(f"/api/conversations/{cid}").status_code, 404)
            self.assertEqual(other.post("/api/chat", json={"conversation_id": cid, "message": "oi"}).status_code, 404)
            self.assertEqual(other.get(f"/api/docs?conversation_id={cid}").status_code, 404)

        detail = self.ana.get(f"/api/conversations/{cid}").json()
        self.assertEqual(detail["conversation"]["title"], "assunto particular da Ana")
        self.assertEqual(len(detail["messages"]), 1)  # nenhuma mensagem de terceiros entrou

    def test_pc_file_tools_are_admin_only(self):
        """As ferramentas leem as pastas do dono do PC: contas comuns não as recebem."""
        seen = []

        def fake_ollama(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            call = {"function": {"name": "listar_diretorio", "arguments": {"caminho": "."}}}
            if seen[-1]["messages"][-1]["role"] == "user":  # o modelo tenta usar a ferramenta
                chunk = {"message": {"role": "assistant", "content": "", "tool_calls": [call]}}
            else:
                chunk = {"message": {"role": "assistant", "content": "ok"}}
            return httpx.Response(200, text=json.dumps(chunk))

        app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(fake_ollama))
        events = lambda r: [json.loads(line)["type"] for line in r.text.splitlines()]  # noqa: E731

        blocked = self.ana.post("/api/chat", json={"message": "liste a pasta"})
        self.assertEqual(seen[0]["tools"], [])  # nem é oferecida ao modelo
        denied = next(e for e in map(json.loads, blocked.text.splitlines()) if e["type"] == "tool_result")
        self.assertFalse(denied["ok"])  # e, se o modelo insiste, a execução recusa
        self.assertIn("administrador", denied["output"])
        seen.clear()
        admin = events(self.admin.post("/api/chat", json={"message": "liste a pasta"}))
        self.assertIn("listar_diretorio", [t["function"]["name"] for t in seen[0]["tools"]])
        self.assertIn("tool_result", admin)
        self.assertEqual(self.ana.post("/api/confirm", json={"id": "x", "approved": True}).status_code, 403)


class DocumentIsolationTest(AccountsCase):
    def test_library_is_private_to_its_owner(self):
        sent = upload(self.ana, "contrato-ana.txt", "o codigo secreto do contrato da Ana e ZEBRA-41", mode="library")
        self.assertEqual(sent.status_code, 200, sent.text)
        body = sent.json()
        self.assertGreaterEqual(body["chunks_indexed"], 1)
        self.assertEqual((body["filename"], body["mode"], body["conversation_id"]), ("contrato-ana.txt", "library", None))

        # Guardado na pasta privada da Ana, dentro da Biblioteca.
        stored = list((documents.LIBRARY_DIR / self.ana_id).glob("*_contrato-ana.txt"))
        self.assertEqual([p.name for p in stored], [f"{body['document_id']}_contrato-ana.txt"])

        query = "qual o codigo secreto do contrato"
        self.assertIn("contrato-ana.txt", sources(query, owner_id=self.ana_id))
        self.assertEqual(search(query, owner_id=self.bia_id), [])
        self.assertEqual(search(query, owner_id=ADMIN["id"]), [])

        self.assertIn("contrato-ana.txt", [d["filename"] for d in self.ana.get("/api/docs").json()["library"]])
        self.assertNotIn("contrato-ana.txt", [d["filename"] for d in self.bia.get("/api/docs").json()["library"]])
        # Outra conta não consegue apagar nem enxergar o documento.
        self.assertEqual(self.bia.delete(f"/api/docs/{body['document_id']}").status_code, 404)
        self.assertEqual(self.admin.delete(f"/api/docs/{body['document_id']}").status_code, 404)
        self.assertTrue(stored[0].is_file())

    def test_search_without_owner_is_refused(self):
        with self.assertRaises(ValueError):
            search("qualquer coisa", owner_id="")
        with self.assertRaises(ValueError):
            asyncio.run(indexer.index_file("x.txt", owner_id="", doc_id="d", source="x.txt"))

    def test_rag_runs_only_on_demand_and_only_over_own_documents(self):
        from app.rag import embedder

        upload(self.ana, "manual-foguete.txt", "o foguete Aurora decola da base Polaris ao amanhecer")
        seen = []
        use_tool = False

        def fake_ollama(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            last = seen[-1]["messages"][-1]
            if use_tool and last["role"] == "user":  # o modelo decide consultar os documentos
                call = {"function": {"name": "buscar_documentos", "arguments": {"consulta": last["content"]}}}
                message = {"role": "assistant", "content": "", "tool_calls": [call]}
            else:
                message = {"role": "assistant", "content": "ok"}
            return httpx.Response(200, text=json.dumps({"message": message}))

        app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(fake_ollama))
        searches = []
        real_embedding = embedder.get_embedding

        async def counting_embedding(text):
            searches.append(text)
            return await real_embedding(text)

        embedder.get_embedding = counting_embedding
        try:
            # Conversa comum: nenhuma busca, nada injetado na mensagem.
            self.ana.post("/api/chat", json={"message": "bom dia"})
            self.assertEqual(searches, [])
            self.assertEqual(seen[-1]["messages"][-1]["content"], "bom dia")
            offered = {t["function"]["name"]: t["function"] for t in seen[-1]["tools"]}
            # A ferramenta é oferecida (sem as de disco) e lista os documentos da conta.
            self.assertEqual(set(offered), {"buscar_documentos"})
            self.assertIn("manual-foguete.txt", offered["buscar_documentos"]["description"])

            use_tool = True
            question = "segundo o manual, de onde decola o foguete Aurora"
            answer = self.ana.post("/api/chat", json={"message": question})
            events = [json.loads(line) for line in answer.text.splitlines()]
            self.assertEqual(len(searches), 1)  # buscou uma vez, porque o modelo pediu
            result = next(e for e in events if e["type"] == "tool_result")
            self.assertTrue(result["ok"])
            self.assertIn("[Fonte: manual-foguete.txt]", result["output"])
            self.assertIn("base Polaris", result["output"])
            # O resultado volta ao modelo como mensagem de ferramenta; a pergunta fica intacta.
            self.assertEqual(seen[-1]["messages"][-1]["role"], "tool")
            self.assertIn("base Polaris", seen[-1]["messages"][-1]["content"])
            cid = events[0]["id"]
            stored = self.ana.get(f"/api/conversations/{cid}").json()["messages"]
            self.assertEqual(stored[0]["content"], question)
            self.assertEqual(stored[1]["meta"]["name"], "buscar_documentos")

            # A outra conta chama a mesma ferramenta e não alcança nada da Ana.
            answer = self.bia.post("/api/chat", json={"message": question})
            result = next(e for e in map(json.loads, answer.text.splitlines()) if e["type"] == "tool_result")
            self.assertNotIn("Polaris", result["output"])
            self.assertNotIn("manual-foguete.txt", result["output"])
            self.assertNotIn("Polaris", json.dumps(seen[-1], ensure_ascii=False))
        finally:
            embedder.get_embedding = real_embedding

    def test_search_tool_is_scoped_by_session_not_by_arguments(self):
        from app import tools
        from app.tools.context import ToolContext

        upload(self.ana, "cofre.txt", "a combinacao do cofre da Ana e 48-15-16")
        run = lambda ctx, args: asyncio.run(tools.execute("buscar_documentos", args, ctx))  # noqa: E731
        ana = ToolContext(user_id=self.ana_id, document_names=("cofre.txt",))
        bia = ToolContext(user_id=self.bia_id)

        self.assertIn("48-15-16", run(ana, {"consulta": "combinacao do cofre"}).output)
        self.assertNotIn("48-15-16", run(bia, {"consulta": "combinacao do cofre"}).output)
        # O dono não é um argumento: o modelo não tem como apontar para outra conta.
        forged = run(bia, {"consulta": "combinacao do cofre", "owner_id": self.ana_id})
        self.assertFalse(forged.ok)
        self.assertFalse(run(ToolContext(), {"consulta": "combinacao do cofre"}).ok)  # sem conta
        # Sem documentos, a ferramenta nem é oferecida ao modelo.
        self.assertEqual(tools.ollama_schemas(bia), [])
        self.assertEqual([s["function"]["name"] for s in tools.ollama_schemas(ana)], ["buscar_documentos"])

    def test_temporary_file_is_limited_to_its_conversation(self):
        first = self.ana.post("/api/conversations").json()["id"]
        second = self.ana.post("/api/conversations").json()["id"]
        sent = upload(self.ana, "rascunho.txt", "o orcamento provisorio do evento e de 9731 reais",
                      mode="conversation", conversation_id=first)
        self.assertEqual(sent.status_code, 200, sent.text)
        self.assertEqual(sent.json()["conversation_id"], first)

        query = "qual o orcamento provisorio do evento"
        self.assertIn("rascunho.txt", sources(query, owner_id=self.ana_id, conversation_id=first))
        self.assertNotIn("rascunho.txt", sources(query, owner_id=self.ana_id, conversation_id=second))
        self.assertNotIn("rascunho.txt", sources(query, owner_id=self.ana_id))  # não entrou na Biblioteca
        self.assertEqual(search(query, owner_id=self.bia_id, conversation_id=first), [])

        listing = self.ana.get(f"/api/docs?conversation_id={first}").json()
        self.assertEqual([d["filename"] for d in listing["conversation"]], ["rascunho.txt"])
        self.assertNotIn("rascunho.txt", [d["filename"] for d in listing["library"]])
        self.assertEqual(self.ana.get(f"/api/docs?conversation_id={second}").json()["conversation"], [])

        # Guardado na área temporária, separada por usuário e conversa.
        folder = documents.TEMP_DIR / self.ana_id / first
        self.assertEqual(len(list(folder.iterdir())), 1)
        self.assertFalse(any("rascunho" in p.name for p in (documents.LIBRARY_DIR / self.ana_id).glob("*")))

        # Ninguém anexa arquivo à conversa de outra pessoa.
        self.assertEqual(upload(self.bia, "x.txt", "texto", mode="conversation", conversation_id=first).status_code, 404)

        # Apagar a conversa leva junto o arquivo, os vetores e o registro.
        self.assertEqual(self.ana.delete(f"/api/conversations/{first}").status_code, 200)
        self.assertFalse(folder.exists())
        self.assertNotIn("rascunho.txt", sources(query, owner_id=self.ana_id, conversation_id=first))
        leftovers = indexer.collection.get(where={"conversation_id": first})
        self.assertEqual(leftovers["ids"], [])
        self.assertIsNone(storage.get_document(sent.json()["document_id"], self.ana_id))

    def test_temporary_upload_without_conversation_creates_one(self):
        sent = upload(self.bia, "nota.txt", "lembrete avulso sobre a reuniao de quinta", mode="conversation")
        self.assertEqual(sent.status_code, 200, sent.text)
        cid = sent.json()["conversation_id"]
        self.assertEqual(self.bia.get(f"/api/conversations/{cid}").status_code, 200)
        self.assertEqual(self.ana.get(f"/api/conversations/{cid}").status_code, 404)
        # Envio inválido não deixa uma conversa vazia para trás.
        before = len(self.bia.get("/api/conversations").json()["conversations"])
        self.assertEqual(upload(self.bia, "vazio.txt", "", mode="conversation").status_code, 400)
        self.assertEqual(len(self.bia.get("/api/conversations").json()["conversations"]), before)

    def test_upload_validation(self):
        self.assertEqual(upload(self.ana, "planilha.exe", "x").status_code, 400)
        self.assertEqual(upload(self.ana, "vazio.txt", "").status_code, 400)
        self.assertEqual(upload(self.ana, "ok.txt", "texto", mode="publico").status_code, 400)
        blank = upload(self.ana, "so-espacos.txt", "   \n  ")  # mesmo caminho de um PDF sem texto
        self.assertEqual(blank.status_code, 422)
        self.assertIn("extrair texto", blank.json()["detail"])
        broken = self.ana.post("/api/docs/upload", files={"file": ("quebrado.pdf", b"isto nao e um pdf")})
        self.assertEqual(broken.status_code, 422)
        with TestClient(app) as anonymous:
            self.assertEqual(upload(anonymous, "a.txt", "texto").status_code, 401)
        # Nada dos envios recusados ficou em disco nem no índice.
        names = [p.name for p in (documents.LIBRARY_DIR / self.ana_id).glob("*")]
        self.assertFalse([n for n in names if "so-espacos" in n or "quebrado" in n or "planilha" in n])
        # Um nome com caminho não escapa da pasta do usuário.
        tricky = upload(self.ana, "..\\..\\fora.txt", "conteudo do arquivo com caminho no nome")
        self.assertEqual(tricky.json()["filename"], "fora.txt")

    def test_reupload_and_delete_leave_no_stale_chunks(self):
        first = upload(self.bia, "politica.txt", "a politica antiga permite reembolso em 90 dias").json()
        second = upload(self.bia, "politica.txt", "a politica nova permite reembolso em 7 dias").json()
        self.assertNotEqual(first["document_id"], second["document_id"])

        texts = [hit["text"] for hit in search("politica de reembolso", owner_id=self.bia_id, top_k=10)]
        self.assertTrue(any("7 dias" in t for t in texts))
        self.assertFalse(any("90 dias" in t for t in texts))
        self.assertEqual(indexer.collection.get(where={"doc_id": first["document_id"]})["ids"], [])
        library = [d for d in self.bia.get("/api/docs").json()["library"] if d["filename"] == "politica.txt"]
        self.assertEqual(len(library), 1)

        self.assertEqual(self.bia.delete(f"/api/docs/{second['document_id']}").status_code, 200)
        self.assertEqual(indexer.collection.get(where={"doc_id": second["document_id"]})["ids"], [])
        self.assertFalse(list((documents.LIBRARY_DIR / self.bia_id).glob("*politica.txt")))
        self.assertEqual(self.bia.delete(f"/api/docs/{second['document_id']}").status_code, 404)


class LegacyMigrationTest(unittest.TestCase):
    """Dados de antes das contas: isolados até serem entregues ao primeiro administrador."""

    def test_ownerless_vectors_are_never_visible_until_adopted(self):
        other = storage.create_user("legado.outra", "sem-senha")
        prefix = "0123456789abcdef0123456789abcdef_"
        legacy_file = documents.LIBRARY_DIR / f"{prefix}antigo.txt"
        legacy_file.parent.mkdir(parents=True, exist_ok=True)
        legacy_file.write_text("o cofre antigo abre com a senha GIRASSOL", encoding="utf-8")
        from . import fake_embedding

        text = "o cofre antigo abre com a senha GIRASSOL"
        indexer.collection.upsert(  # exatamente como o código anterior gravava: sem dono
            ids=["legado-0"],
            embeddings=[asyncio.run(fake_embedding(text))],
            metadatas=[{"source": legacy_file.name, "chunk_index": 0}],
            documents=[text],
        )
        query = "como abre o cofre antigo"
        for owner in (ADMIN["id"], other["id"]):
            self.assertNotIn(legacy_file.name, sources(query, owner_id=owner, top_k=10))

        self.assertEqual(documents.adopt_legacy(ADMIN["id"]), 1)

        self.assertIn("antigo.txt", sources(query, owner_id=ADMIN["id"], top_k=10))
        self.assertEqual(search(query, owner_id=other["id"], top_k=10), [])
        self.assertFalse(legacy_file.exists())  # saiu da raiz compartilhada…
        moved = list((documents.LIBRARY_DIR / ADMIN["id"]).glob("*_antigo.txt"))
        self.assertEqual(len(moved), 1)  # …para a pasta privada do administrador
        self.assertIn("antigo.txt", [d["filename"] for d in storage.list_documents(ADMIN["id"])])
        self.assertEqual(documents.adopt_legacy(ADMIN["id"]), 0)  # não há mais nada sem dono

    def test_schema_v1_database_is_migrated(self):
        import sqlite3
        import tempfile

        saved = storage.DB_PATH
        storage.DB_PATH = Path(tempfile.mkdtemp(prefix="distopia_test_v1_")) / "v1.db"
        try:
            conn = sqlite3.connect(storage.DB_PATH)
            conn.executescript(storage._SCHEMA_V1 + "PRAGMA user_version = 1;")
            conn.execute("INSERT INTO conversations (id, title, created_at, updated_at) VALUES ('antiga', 'Conversa antiga', 1, 1)")
            conn.execute("INSERT INTO messages (conversation_id, role, content, created_at) VALUES ('antiga', 'user', 'oi', 1)")
            conn.commit()
            conn.close()

            storage.init()
            storage.init()  # repetir não quebra nada
            first = storage.create_user("primeiro", "hash", is_admin=True)
            second = storage.create_user("segundo", "hash")
            # Sem dono, a conversa antiga não aparece para ninguém…
            self.assertEqual(storage.list_conversations(first["id"]), [])
            self.assertIsNone(storage.get_conversation("antiga", second["id"]))
            # …até ser entregue ao primeiro administrador — e só a ele.
            self.assertEqual(storage.claim_orphan_conversations(first["id"]), 1)
            self.assertEqual(storage.get_conversation("antiga", first["id"])["title"], "Conversa antiga")
            self.assertIsNone(storage.get_conversation("antiga", second["id"]))
            self.assertEqual(len(storage.get_messages("antiga")[0]), 1)
        finally:
            storage.DB_PATH = saved


if __name__ == "__main__":
    unittest.main()
