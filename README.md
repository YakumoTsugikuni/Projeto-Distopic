# Distopic

Assistente de IA 100% local (Ollama + FastAPI + frontend web próprio).

## Estrutura

```
backend/
  Modelfile              modelo customizado `distopic` (qwen2.5-coder:7b)
  requirements.txt
  .env.example           configuração (copie para .env)
  app/
    __main__.py          python -m app  -> inicia o servidor
    main.py              rotas da API + frontend estático + cabeçalhos de segurança
    config.py            configuração via .env / variáveis de ambiente
    auth.py              contas (scrypt), sessão em cookie, administrador, bloqueio de força bruta
    set_password.py      python -m app.set_password [usuario]
    ollama_client.py     streaming de /api/chat do Ollama
    agent.py             laço modelo <-> ferramentas
    tools/
      __init__.py        registro de ferramentas, níveis de risco, validação
      sandbox.py         confinamento de caminhos às pastas permitidas
      listar_diretorio.py  (leitura)
      ler_arquivo.py       (leitura, paginada)
      criar_arquivo.py     (escrita — pede confirmação; cria as pastas do caminho)
      buscar_documentos.py (leitura — RAG sob demanda nos documentos da conta)
      context.py           conta e conversa do turno (ToolContext)
    confirmations.py     pedidos de confirmação pendentes (POST /api/confirm)
    storage.py           contas, conversas e registro de documentos em SQLite (%LOCALAPPDATA%\Distopic\distopic.db)
    chat.py              um turno: contexto do banco + RAG -> agente -> gravação
    documents.py         upload: arquivo em disco + registro + vetores; remoção; migração dos antigos
    rag/                 embeddings (Ollama), indexação e busca no ChromaDB, sempre filtradas por dono
  tests/
frontend/
  index.html  style.css  app.js     login + chat com streaming
src-tauri/               aplicativo desktop (Tauri v2)
  src/main.rs            inicia o backend, espera responder e abre a interface
  shell/                 tela de carregamento local
  icons/                 ícones gerados a partir de frontend/assets/logo-1024.png
  tauri.conf.json
workspace/               pasta de trabalho padrão da Distopic (criada ao iniciar)
```

## Primeira execução (PowerShell, dentro de `backend/`)

```powershell
ollama pull qwen2.5-coder:7b
ollama create distopic -f Modelfile
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m app
```

Também é preciso o modelo de embeddings do RAG: `ollama pull nomic-embed-text`.

## Contas

- Cada pessoa tem uma conta (usuário + senha). Conversas, documentos e buscas são isolados por conta —
  nem o administrador vê os dados dos outros.
- **Não há cadastro aberto.** A primeira conta é criada na tela de entrada, só a partir deste PC, e é a
  administradora. As demais o administrador cria em **Conta** (barra lateral), onde também redefine senhas
  e remove contas (o que apaga conversas, arquivos e vetores daquela conta).
- **Quem já usava a senha única:** na primeira execução ela vira a senha da conta `admin`, que recebe as
  conversas e os documentos que já existiam.
- Senha esquecida: `.\.venv\Scripts\python.exe -m app.set_password [usuario]` (sem argumento: o administrador).
- As ferramentas de arquivos do PC (`listar_diretorio`, `ler_arquivo`, `criar_arquivo`) são exclusivas de
  administradores: não são oferecidas ao modelo em contas comuns e, se ele as chamar mesmo assim, a
  execução recusa com um erro claro. `criar_arquivo` cria as pastas do caminho que faltarem.
- Pastas liberadas: `DISTOPIC_ALLOWED_ROOTS` aceita várias, separadas por `;` (ver `backend/.env.example`).

## Documentos (RAG privado)

O botão **＋** ao lado da mensagem envia PDF, MD ou TXT (até 20 MB), com duas opções:

- **Adicionar à minha Biblioteca** — guardado em `Biblioteca\<id da conta>\` e consultado em todas as
  conversas daquela conta.
- **Usar só nesta conversa** — guardado em `Temporarios\<id da conta>\<id da conversa>\`, consultado só ali
  e apagado junto com a conversa.

A busca é **sob demanda**: é a ferramenta `buscar_documentos`, que o modelo aciona quando a pergunta cita
um documento ("segundo a especificação…", "no PDF que enviei…", "na minha biblioteca…"). Conversa comum
não consulta o índice. A ferramenta só é oferecida a contas que têm documentos, lista os nomes deles na
própria descrição, busca na Biblioteca da conta + anexos da conversa e devolve os trechos com o nome do
arquivo, para a resposta citar a fonte. O dono vem da sessão, nunca dos argumentos. "Alimentar a IA" é isso: indexar e
recuperar — o modelo não é treinado nem modificado. Todo vetor no ChromaDB leva `owner_id` (e
`conversation_id` nos temporários), e toda busca filtra por eles; vetores antigos sem dono não aparecem
para ninguém até a migração entregá-los ao administrador.

Abra http://localhost:8000. Depois de uma resposta, `ollama ps` deve mostrar `100% GPU`.

Testes: `.\.venv\Scripts\python.exe -m unittest discover -s tests -t .`

## Aplicativo desktop (Tauri)

Requer Rust e Microsoft C++ Build Tools. Dentro de `src-tauri/`:

```powershell
cargo run
```

- A janela inicia o backend sozinha (não rode `python -m app` ao mesmo tempo: a porta 8000 fica ocupada)
  e o encerra ao fechar.
- Pede a senha a cada abertura, como no navegador. No primeiro acesso (sem senha definida) a tela de
  entrada pede para criar a senha; isso só é possível a partir do próprio PC, nunca pela rede ou túnel.
- Log do backend: `%LOCALAPPDATA%\Distopic\backend.log`.
- Os arquivos de compilação ficam em `%LOCALAPPDATA%\Distopic\build` (fora do OneDrive); o caminho está
  em `src-tauri/.cargo/config.toml`.
- `cargo run` usa o backend e a `.venv` desta pasta do projeto.

### Instalador

Na raiz do projeto:

```powershell
powershell -ExecutionPolicy Bypass -File build-desktop.ps1
```

Gera o instalador em `%LOCALAPPDATA%\Distopic\build\release\bundle\nsis\`. O aplicativo instalado é
independente da pasta do projeto e guarda a configuração (incluindo a senha, criada no primeiro
acesso) em `%LOCALAPPDATA%\Distopic\.env` — ele **não** lê o `backend\.env`. A pasta de trabalho
padrão dele é `%USERPROFILE%\Distopic`. Para trocar a senha depois:
`distopic-backend.exe set-password` na pasta de instalação.

## Acesso remoto

- **Cloudflare Tunnel:** `cloudflared tunnel --url http://localhost:8000` (o servidor pode continuar em `127.0.0.1`).
- **Rede local:** defina `DISTOPIC_HOST=0.0.0.0` no `.env`. Pela LAN o tráfego é HTTP sem criptografia.

## Adicionando uma ferramenta

1. Crie `app/tools/<nome>.py` com um modelo `Args` (Pydantic, `extra="forbid"`) e uma função `run(args) -> str`.
2. Passe todo caminho por `sandbox.resolve_path()`.
3. Registre em `TOOLS` (`app/tools/__init__.py`) com o `Risk` correto. Ferramentas `WRITE`/`EXECUTE`
   só rodam depois que o usuário clica em Autorizar na interface; forneça um `describe(args)`
   que diga exatamente o que será feito.

Depois de alterar o `Modelfile`, rode `ollama create distopic -f Modelfile` de novo.
