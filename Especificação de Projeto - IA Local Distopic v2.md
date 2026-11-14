# Especificação Técnica do Projeto: Assistente de IA Local "Distopic" — v2

Esta versão substitui a v1 (PDF). Ela registra o que já foi construído e acrescenta três frentes:
**aplicativo desktop nativo (Tauri)**, **persistência de conversas** e **refinamentos de UI e de personalidade**.

Legenda de estado: ✅ pronto · 🔶 parcial · ⬜ a fazer

---

## 1. Visão geral

A Distopic é uma IA 100% local, hospedada no PC do usuário, sem APIs pagas e com total privacidade.
Ela conversa, age como agente sobre arquivos e aplicativos do computador e pode ser acessada de três formas,
todas servidas pelo **mesmo backend**:

1. **Aplicativo desktop nativo** (janela Tauri) — forma principal de uso no PC. *(novo na v2)*
2. **Navegador na rede local.**
3. **Navegador pela internet**, via Cloudflare Tunnel, com login.

## 2. Hardware e restrição central

| Componente | Especificação |
|---|---|
| CPU | AMD Ryzen 5 5500 |
| GPU | AMD Radeon RX 7600 OC (8 GB de VRAM) |
| RAM | 16 GB DDR4 |
| SO | Windows 11 |

- Modelo base: `qwen2.5-coder:7b` (Q4_K_M, ~4,7 GB) com contexto de 8192 tokens, 100% na GPU.
- **Orçamento de VRAM (regra da v2):** a VRAM pertence ao modelo. Nenhum outro componente da Distopic
  (janela desktop, persistência, interface) pode provocar o transbordo do modelo para a RAM.
  Critério de verificação: com o aplicativo aberto e uma conversa em andamento, `ollama ps` continua mostrando `100% GPU`.

## 3. Stack definida

| Camada | Tecnologia | Estado |
|---|---|---|
| Inferência | Ollama (`localhost:11434`), modelo customizado `distopic` via `Modelfile` | ✅ |
| Backend | Python + FastAPI (`backend/`), streaming NDJSON | ✅ |
| Frontend | HTML/CSS/JS puro (`frontend/`), sem etapa de build | ✅ |
| Persistência | SQLite (módulo `sqlite3` da biblioteca padrão) | ✅ |
| Desktop | Tauri v2 (`src-tauri/`) | 🔶 |

## 4. Requisitos funcionais

### 4.1 Identidade e personalidade 🔶
- ✅ Modelo `distopic` com temperatura 0.3 e prompt de sistema orientado a ferramentas.
- ✅ **Personalidade no estilo J.A.R.V.I.S.**, pré-aquecimento e teto de resposta — ver seção 8.
- Medido com o modelo real (RX 7600): `100% GPU`, 5,1 GB de VRAM, primeiro token em ~0,1 s em conversa
  e ~1–2 s quando há ferramenta.
- **Limitação conhecida do `qwen2.5-coder:7b`:** escreve chamadas de ferramenta como JSON no texto e às
  vezes chama ferramenta em pergunta geral. O backend interpreta o JSON e se recupera do erro, mas o tom
  nem sempre é mantido. Alternativa a avaliar: `qwen2.5:7b` (não-coder) como modelo base.

### 4.2 Agente / Function Calling 🔶
- ✅ `listar_diretorio`, `ler_arquivo` (leitura, sem confirmação).
- ✅ `criar_arquivo` (escrita, com confirmação do usuário na interface).
- ✅ Confinamento a pastas permitidas (`DISTOPIC_ALLOWED_ROOTS`) e bloqueio de arquivos sensíveis.
- ⬜ `abrir_aplicativo`, edição parcial de arquivos, mover/renomear/apagar, execução de comandos.

### 4.3 RAG local ⬜
- Ingestão de `.md`, `.txt`, `.pdf`; busca contextual; ferramenta `buscar_documentacao`.
- Os embeddings devem respeitar a regra de VRAM da seção 2 (modelo de embedding pequeno ou execução em CPU).

### 4.4 Acesso remoto e autenticação ✅
- Login por senha (hash scrypt), sessão em cookie HttpOnly, bloqueio de força bruta, exposição via `cloudflared`.

### 4.5 Persistência de conversas ✅ *(novo — seção 6; a interface de histórico ainda é provisória, ver seção 7)*
### 4.6 Aplicativo desktop 🔶 *(novo — seção 5)*
- ✅ Janela Tauri, tela de carregamento, início/encerramento do backend, login por senha com criação
  no primeiro acesso, instância única, ícones. Verificado: `ollama ps` em `100% GPU` com o aplicativo aberto.
- ⬜ Instalador independente (backend empacotado com PyInstaller como sidecar + `tauri build`). Hoje o
  executável depende da pasta do projeto e da `.venv`.
### 4.7 Refinamentos de UI ✅ *(novo — seção 7; falta apenas a logo, seção 5.4)*

---

## 5. Aplicativo desktop nativo (Tauri)

### 5.1 Arquitetura

```
┌────────────────────── Distopic.exe (Tauri v2) ──────────────────────┐
│  Janela WebView2  ──HTTP──►  Backend FastAPI (processo filho)       │
│  carrega http://127.0.0.1:<porta>      │                            │
│                                         ├──► Ollama (localhost:11434)│
│                                         └──► SQLite (conversas)      │
└──────────────────────────────────────────────────────────────────────┘
```

- **O backend continua sendo a única fonte da interface.** A janela Tauri carrega a interface servida pelo
  FastAPI em `127.0.0.1`, em vez de arquivos embutidos com origem `tauri://`. Motivo: a mesma origem preserva
  o cookie de sessão, a política CSP e a verificação de origem já implementados, e o acesso por navegador/túnel
  continua usando exatamente o mesmo código.
- **O frontend é empacotado junto com o aplicativo** como recurso do pacote; o backend o serve a partir daí.
- **Ciclo de vida:** ao abrir, o Tauri inicia o backend como processo filho, mostra uma tela de carregamento
  local (logo sobre fundo cinza) e só navega para a interface quando o backend responder. Ao fechar a janela,
  o backend é encerrado. Uma segunda instância do aplicativo apenas foca a janela existente.
- **Distribuição do backend:** em desenvolvimento, o Tauri usa o Python da `.venv`; no instalador, o backend
  vai como executável único (PyInstaller) declarado como *sidecar*.
- **Ollama não é empacotado.** Se não estiver rodando, a interface mostra um aviso claro com a instrução, sem travar.

### 5.2 Autenticação no desktop
- **Revisado a pedido do usuário:** o aplicativo pede a senha a cada abertura, igual ao navegador.
  O login automático por token (versão anterior desta seção) foi removido.
- **Primeiro acesso:** se não há senha definida, a tela de entrada vira "criar senha". Isso só é aceito
  em conexão direta de `127.0.0.1` sem cabeçalhos de proxy — nunca pela rede local nem pelo túnel.
- O aplicativo instalado guarda a senha (hash scrypt) em `%LOCALAPPDATA%\Distopic\.env`, separado do
  `backend\.env` usado em desenvolvimento.

### 5.3 Uso de GPU pela janela
- Sem efeitos que dependam de composição pesada: nada de `backdrop-filter`, desfoques, sombras grandes
  animadas, vídeo ou canvas/WebGL.
- Animações limitadas a `opacity` e `transform`, curtas (≤ 150 ms), e desativadas com `prefers-reduced-motion`.
- Medir a VRAM com a janela aberta. Se o critério da seção 2 falhar, desativar a aceleração por hardware
  do WebView2 nas opções da janela e medir de novo.

### 5.4 Logo e ícones
- Arquivo de origem: `frontend/Logo/logo distopic.jpg` (512×512, JPEG).
- **Estado real do arquivo:** emblema **preto com detalhes brancos sobre fundo branco**; não há vermelho,
  não há transparência e o emblema ocupa só ~30% da área.
- Tratamento exigido antes de gerar os ícones:
  1. Recortar o emblema e centralizá-lo ocupando ~80% de um quadrado de 1024×1024.
  2. Remover o fundo branco (PNG com transparência).
  3. Vermelho: **decisão pendente do usuário** — (a) ele fornece a versão oficial com detalhes em vermelho, ou
     (b) o Claude colore o hexágono central de vermelho (`--red`, seção 7.1) e submete para aprovação.
     Não inventar uma variação definitiva sem aprovação.
- **Feito:** o usuário aprovou a "opção A" (anel hexagonal central e ponto em vermelho `#d4252f`).
  Gerados `frontend/assets/logo.png` (256 px, interface), `frontend/assets/logo-1024.png` (origem dos
  ícones do Tauri) e `frontend/favicon.ico`. Falta só gerar os ícones do Tauri (etapa 5).
- Saídas: `frontend/assets/logo.png` (interface e tela de login), `frontend/favicon.ico` e o conjunto de
  ícones do Tauri gerado com `tauri icon` a partir do PNG de 1024×1024 (ícone do `.exe`, barra de tarefas, instalador).
- O ícone precisa ser legível em 16×16 sobre barra de tarefas clara e escura; se o preto sumir no escuro,
  usar uma base cinza-claro arredondada atrás do emblema.

### 5.5 Pré-requisitos (não instalados hoje)
- Rust (`rustup`) e Microsoft C++ Build Tools. O WebView2 já vem no Windows 11.
- A instalação é feita pelo usuário; o Claude deve verificar e orientar, não instalar por conta própria.

### 5.6 Critérios de aceite
- `tauri dev` abre a janela com a interface funcionando, sem pedir senha.
- `tauri build` gera um instalador; o aplicativo instalado funciona sem a pasta do projeto.
- Fechar a janela não deixa processo do backend aberto.
- Ícone correto no `.exe`, na barra de tarefas e na janela.
- `ollama ps` mostra `100% GPU` com o aplicativo em uso.

---

## 6. Persistência de conversas

### 6.1 Princípios
- **Local e privado:** tudo em um único arquivo SQLite; nada sai do PC.
- **Só CPU e disco:** salvar, listar e abrir conversas nunca chama o modelo. Em particular, o **título da
  conversa é derivado da primeira mensagem do usuário** (primeiros ~60 caracteres), não gerado pela IA.
- **Fora do OneDrive:** o banco fica em `%LOCALAPPDATA%\Distopic\distopic.db` (configurável por
  `DISTOPIC_DATA_DIR`). O projeto está numa pasta sincronizada pelo OneDrive, e sincronizar um SQLite em uso
  causa conflitos e corrupção.
- **O servidor é a fonte do histórico.** O cliente deixa de enviar a conversa inteira a cada mensagem: envia
  `conversation_id` + a nova mensagem, e o backend monta o contexto a partir do banco. Isso também impede um
  cliente de forjar turnos anteriores.

### 6.2 Modelo de dados

```sql
conversations(id TEXT PRIMARY KEY, title TEXT, created_at REAL, updated_at REAL)
messages(id INTEGER PRIMARY KEY, conversation_id TEXT REFERENCES conversations ON DELETE CASCADE,
         role TEXT,          -- user | assistant | tool
         content TEXT,
         meta TEXT,          -- JSON: chamada de ferramenta, argumentos, ok/erro, decisão de confirmação
         created_at REAL)
-- índice em messages(conversation_id, id); modo WAL; versão do schema em PRAGMA user_version
```

### 6.3 API (todas exigem sessão)

| Rota | Função |
|---|---|
| `GET /api/conversations` | Lista (id, título, data), mais recentes primeiro, paginada |
| `POST /api/conversations` | Cria conversa vazia |
| `GET /api/conversations/{id}?before=<msg_id>&limit=50` | Mensagens em páginas, das mais recentes para trás |
| `PATCH /api/conversations/{id}` | Renomeia |
| `DELETE /api/conversations/{id}` | Apaga (com confirmação na interface) |
| `POST /api/chat` | Passa a receber `{conversation_id, message}` |

### 6.4 Regras de gravação
- A mensagem do usuário é gravada **antes** de chamar o modelo.
- A resposta é gravada ao fim do stream; se a conexão cair no meio, grava-se o texto parcial marcado como interrompido.
- Chamadas de ferramenta, resultados e decisões de confirmação são gravados em `meta` e reexibidos ao reabrir a conversa.
- Ao montar o contexto, o backend usa apenas as mensagens recentes que cabem no orçamento de tokens (regra já existente).
- As gravações não podem atrasar o streaming: escrita fora do laço de eventos, uma transação por mensagem.

### 6.5 Critérios de aceite
- Fechar e reabrir o aplicativo mantém todas as conversas.
- Abrir uma conversa de 1.000 mensagens é imediato (carrega só a última página).
- Nenhuma requisição ao Ollama ocorre ao listar, abrir, renomear ou apagar conversas.
- Testes automatizados cobrindo criação, paginação, exclusão em cascata e resposta interrompida.

---

## 7. Refinamentos de UI

### 7.1 Tema — predominantemente cinza
A interface é cinza neutro. Preto, branco e vermelho vêm da logo e aparecem só como detalhe.

| Token | Valor | Uso |
|---|---|---|
| `--bg` | `#1b1c1e` | Fundo da janela |
| `--surface` | `#242528` | Barra lateral, cabeçalho |
| `--panel` | `#2d2e32` | Balões da assistente, campos |
| `--panel-user` | `#3a3b40` | Balões do usuário |
| `--border` | `#3f4045` | Divisórias |
| `--text` | `#e6e6e8` | Texto principal |
| `--muted` | `#9a9ba1` | Texto secundário |
| `--red` | `#d4252f` | **Só** destaque: botão principal, foco, conversa ativa, erros, indicador de "gerando" |
| `--black` / `--white` | `#0b0b0c` / `#ffffff` | Logo e detalhes pontuais |

- O verde atual (`#35e0a1`) é removido.
- Contraste mínimo de 4,5:1 para texto. Vermelho nunca como cor de texto corrido.
- Tela de login e tela de carregamento do desktop usam o mesmo tema, com a logo centralizada.

### 7.2 Layout
- **Barra lateral esquerda:** logo + nome, botão "Nova conversa", lista de conversas agrupada por data
  (Hoje, Ontem, Últimos 7 dias, Mais antigas), busca por título, renomear e apagar por item.
- **Área de chat:** cabeçalho com título da conversa e estado do modelo; mensagens; campo de entrada fixo embaixo.
- Em telas estreitas (celular via túnel) a barra lateral vira gaveta.

### 7.3 Scroll suave na janela de chat
- **Acompanhar só quando o usuário está no fim.** Se ele rolou para cima para ler, o streaming não puxa a
  tela de volta; aparece um botão "↓ Novas mensagens".
- Tokens recebidos são acumulados e aplicados no máximo uma vez por quadro (`requestAnimationFrame`), em vez
  de uma alteração de DOM por token.
- Rolagens disparadas por ação do usuário (botão, abrir conversa) usam `scroll-behavior: smooth`; o
  acompanhamento durante o streaming é instantâneo, para não "correr atrás" do texto.
- Conversas longas não pesam porque só a última página (50 mensagens) é carregada; as anteriores entram
  sob demanda. (`content-visibility: auto` foi descartado: altura estimada de itens fora da tela causa
  saltos ao inserir mensagens antigas no topo.)
- Barra de rolagem fina, no tom do tema.

### 7.4 Navegação fluida pelo histórico
- Trocar de conversa não recarrega a página: a lista permanece e só a área de chat é substituída.
- Carregamento incremental: ao rolar até o topo, busca a página anterior de mensagens **preservando a posição de leitura**.
- A conversa aberta fica na URL (`#/c/<id>`), então voltar/avançar e recarregar funcionam.
- A posição de rolagem de cada conversa é lembrada durante a sessão.
- Atalhos: `Ctrl+N` nova conversa, `Ctrl+K` buscar, `Esc` interrompe a geração.
- Botão **Parar** durante a geração (cancela o stream e libera o modelo).

### 7.5 Critérios de aceite
- Streaming de uma resposta longa sem travamentos visíveis nem saltos de rolagem.
- Rolar para cima durante o streaming mantém a posição.
- Alternar entre conversas leva menos de 100 ms depois da primeira carga.

---

## 8. Comunicação no estilo J.A.R.V.I.S. — rápida e direta

### 8.1 Personalidade (vai no `SYSTEM` do `Modelfile`)
- Tom de mordomo técnico: seguro, seco, eficiente, com ironia leve e ocasional. Trata o usuário por "senhor".
- **Resposta primeiro, sem preâmbulo.** Proibido abrir com "Claro!", "Ótima pergunta" ou repetir a pergunta.
- Curta por padrão (1 a 4 frases); detalha só quando pedido ou quando o assunto exige.
- **Age em vez de perguntar:** se uma ferramenta resolve, chama a ferramenta. Relata o resultado em uma linha.
- Assertiva: aponta erros e riscos sem rodeios e sugere o próximo passo.
- "Agressiva" refere-se a ritmo e objetividade — nunca a grosseria com o usuário.
- Não se apresenta como J.A.R.V.I.S. nem cita o personagem: o nome é Distopic.

### 8.2 Velocidade percebida
- **Pré-aquecimento:** ao iniciar o backend, carregar o modelo na VRAM em segundo plano, para a primeira
  resposta não esperar o carregamento.
- `keep_alive` longo (já em 30 min) para o modelo não ser descarregado entre mensagens.
- Streaming desde o primeiro token e indicador imediato de "processando" ao enviar.
- Limite de tamanho de resposta (`num_predict`) coerente com o estilo curto; o usuário pode pedir "continue".
- Meta: primeiro token em menos de 1,5 s com o modelo já carregado.

---

## 9. Segurança (inalterado e obrigatório)
- Lista fechada de ferramentas, argumentos validados, caminhos confinados, confirmação para escrita/execução.
- Nenhuma rota nova sem sessão. As rotas de conversas seguem a mesma verificação de origem.
- O token de sessão do desktop (5.2) não enfraquece o login remoto.
- Apagar conversa pede confirmação na interface.

---

## 10. Instruções para o Claude — ordem de implementação

Trabalhe uma etapa por vez. Ao fim de cada uma: rode os testes, relate o que foi verificado e o que não foi,
e pare para aprovação do usuário. Mantenha `README.md` atualizado.

1. **Persistência (seção 6).** Criar `backend/app/storage.py` (SQLite) e as rotas; migrar `/api/chat` para
   `{conversation_id, message}`; adaptar o agente para ler o histórico do banco; escrever os testes.
2. **UI (seção 7).** Aplicar o tema cinza, a barra lateral de conversas, o scroll e a navegação. Manter
   HTML/CSS/JS puro, sem framework e sem CDN (a CSP só permite recursos próprios). Todo texto do modelo
   continua entrando por `textContent`.
3. **Logo (seção 5.4).** Tratar o arquivo de origem, perguntar ao usuário sobre o vermelho, gerar
   `logo.png`, favicon e ícones. Não sobrescrever o arquivo original.
4. **Personalidade e velocidade (seção 8).** Reescrever o `SYSTEM` do `Modelfile`, adicionar o
   pré-aquecimento e o botão Parar. Lembrar o usuário de rodar `ollama create distopic -f Modelfile`.
5. **Tauri (seção 5).** Verificar Rust e Build Tools (orientar a instalação se faltarem); criar `src-tauri/`
   com Tauri v2; tela de carregamento; iniciar/encerrar o backend; token de sessão local; instância única;
   ícones; depois o empacotamento do backend como sidecar e o instalador. Consultar a documentação atual do
   Tauri v2 para os nomes exatos das opções de configuração em vez de supor.
6. **Verificação de GPU.** Medir `ollama ps` com o aplicativo em uso e registrar o resultado no README.

Etapas da v1 ainda pendentes e independentes destas: `abrir_aplicativo`, demais ferramentas de arquivo e o RAG.

## 11. Decisões pendentes do usuário
1. ~~Vermelho na logo~~ — resolvido: opção A aprovada.
2. **Instalar Rust e C++ Build Tools** para a etapa do Tauri (5.5).
3. **Tratamento "senhor"** na personalidade: manter ou trocar por outra forma (8.1).
