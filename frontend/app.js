"use strict";

const $ = (id) => document.getElementById(id);
const loginView = $("login-view");
const chatView = $("chat-view");
const sidebar = $("sidebar");
const scrim = $("scrim");
const messagesEl = $("messages");
const conversationsEl = $("conversations");
const searchInput = $("search");
const input = $("input");
const sendButton = $("send");
const stopButton = $("stop");
const jumpButton = $("jump");
const attachButton = $("attach");
const attachMenu = $("attach-menu");
const docFileInput = $("doc-file");
const uploadStatus = $("upload-status");

// O histórico vive no servidor (SQLite). Aqui fica só o estado da tela.
const state = {
  user: null, // conta da sessão: { id, username, is_admin }
  id: null, // conversa aberta (null = nova, ainda não gravada)
  oldest: null, // id da mensagem mais antiga carregada
  hasMore: false, // há mensagens mais antigas no servidor
  loadingOlder: false,
  conversations: [],
  abort: null, // AbortController da resposta em andamento
  follow: true, // a rolagem acompanha o fim da conversa
  smoothing: false,
};
// Distância até o fim de cada conversa, para voltar ao mesmo ponto de leitura.
const scrollMemory = new Map();

const jsonRequest = (url, body, method = "POST", signal) =>
  fetch(url, { method, signal, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

// Todo texto vindo do modelo ou de ferramentas entra via textContent — nunca
// innerHTML — para que nada do que a IA escreva seja interpretado como HTML.
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

/* ---------- Rolagem ---------- */

const nearBottom = () => messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 48;

function toBottom() {
  messagesEl.scrollTop = messagesEl.scrollHeight;
  state.follow = true;
  jumpButton.hidden = true;
}

let smoothTimer = 0;
function smoothToBottom() {
  state.follow = true;
  state.smoothing = true; // ignora eventos de scroll da própria animação
  jumpButton.hidden = true;
  messagesEl.scrollTo({ top: messagesEl.scrollHeight, behavior: "smooth" });
  clearTimeout(smoothTimer);
  smoothTimer = setTimeout(() => {
    state.smoothing = false;
    if (state.follow) toBottom(); // o texto pode ter crescido durante a animação
  }, 450);
}

// Conteúdo novo chegou: acompanha se o usuário está no fim; senão só avisa.
function contentGrew() {
  if (state.follow) toBottom();
  else jumpButton.hidden = false;
}

function append(node) {
  const welcome = messagesEl.querySelector(".welcome");
  if (welcome) welcome.remove();
  messagesEl.appendChild(node);
  contentGrew();
  return node;
}

messagesEl.addEventListener(
  "scroll",
  () => {
    if (!state.smoothing) {
      state.follow = nearBottom();
      if (state.follow) jumpButton.hidden = true;
    }
    if (messagesEl.scrollTop < 200) loadOlder();
  },
  { passive: true }
);
jumpButton.addEventListener("click", smoothToBottom);

/* ---------- Sessão ---------- */

// Na primeira execução (nenhuma conta ainda, acessando do próprio PC) a tela de
// entrada vira "criar a conta do administrador"; em qualquer outro caso é o
// login normal. Não existe cadastro aberto: as demais contas o administrador cria.
let setupMode = false;
let minPasswordLength = 12;

async function showLogin() {
  stopGeneration();
  chatView.hidden = true;
  const status = await fetch("/api/status").then((r) => r.json()).catch(() => ({}));
  setupMode = status.setup === true;
  minPasswordLength = status.min_password_length || minPasswordLength;
  $("login-hint").textContent = setupMode
    ? `Primeiro acesso: crie a conta do administrador (senha com no mínimo ${minPasswordLength} caracteres).`
    : "IA local · acesso restrito";
  $("username").autocomplete = setupMode ? "off" : "username";
  $("password").placeholder = setupMode ? "Nova senha" : "Senha";
  $("password").autocomplete = setupMode ? "new-password" : "current-password";
  $("password2").hidden = !setupMode;
  $("password2").required = setupMode;
  $("login-submit").textContent = setupMode ? "Criar conta e entrar" : "Entrar";
  $("login-error").textContent = "";
  for (const dialog of document.querySelectorAll("dialog[open]")) dialog.close();
  state.user = null;
  loginView.hidden = false;
  $("username").focus();
}

async function showChat(me) {
  state.user = me.user;
  loginView.hidden = true;
  chatView.hidden = false;
  $("model").textContent = me.model;
  $("open-account").textContent = `Conta: ${me.user.username}`;
  $("admin-section").hidden = !me.user.is_admin;
  setUploadStatus("");
  resetChat();
  await loadConversations();
  await route();
}

async function checkSession() {
  const response = await fetch("/api/me");
  if (response.ok) await showChat(await response.json());
  else showLogin();
}

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const errorEl = $("login-error");
  errorEl.textContent = "";
  const password = $("password").value;
  if (setupMode) {
    if (password.length < minPasswordLength) {
      errorEl.textContent = `Use pelo menos ${minPasswordLength} caracteres.`;
      return;
    }
    if (password !== $("password2").value) {
      errorEl.textContent = "As senhas não conferem.";
      return;
    }
  }
  const username = $("username").value.trim();
  const response = await jsonRequest(setupMode ? "/api/setup" : "/api/login", { username, password });
  $("password").value = "";
  $("password2").value = "";
  if (response.ok) return checkSession();
  const data = await response.json().catch(() => ({}));
  errorEl.textContent = typeof data.detail === "string" ? data.detail : "Não foi possível entrar";
});

$("logout").addEventListener("click", async () => {
  stopGeneration();
  await fetch("/api/logout", { method: "POST" });
  history.replaceState(null, "", "#/");
  conversationsEl.replaceChildren();
  scrollMemory.clear();
  showLogin();
});

/* ---------- Lista de conversas ---------- */

function groupLabel(timestamp) {
  const startOfToday = new Date().setHours(0, 0, 0, 0);
  const day = 24 * 3600 * 1000;
  const time = timestamp * 1000;
  if (time >= startOfToday) return "Hoje";
  if (time >= startOfToday - day) return "Ontem";
  if (time >= startOfToday - 7 * day) return "Últimos 7 dias";
  return "Mais antigas";
}

function conversationRow(conversation) {
  const row = el("div", conversation.id === state.id ? "conv active" : "conv");
  const title = conversation.title || "Nova conversa";

  const open = el("button", "open", title);
  open.type = "button";
  open.title = title;
  open.addEventListener("click", () => {
    closeDrawer();
    location.hash = `#/c/${conversation.id}`;
  });

  const rename = el("button", "icon", "✎");
  rename.type = "button";
  rename.setAttribute("aria-label", `Renomear ${title}`);
  rename.addEventListener("click", () => startRename(row, conversation));

  // Apagar em dois cliques, sem caixa de diálogo: o primeiro arma, o segundo confirma.
  const remove = el("button", "icon delete", "✕");
  remove.type = "button";
  remove.setAttribute("aria-label", `Apagar ${title}`);
  remove.addEventListener("click", async () => {
    if (!remove.classList.contains("armed")) {
      remove.classList.add("armed");
      remove.textContent = "Apagar?";
      setTimeout(() => {
        remove.classList.remove("armed");
        remove.textContent = "✕";
      }, 3000);
      return;
    }
    await fetch(`/api/conversations/${encodeURIComponent(conversation.id)}`, { method: "DELETE" });
    scrollMemory.delete(conversation.id);
    if (conversation.id === state.id) location.hash = "#/";
    await loadConversations();
  });

  row.append(open, rename, remove);
  return row;
}

function startRename(row, conversation) {
  const field = el("input");
  field.value = conversation.title;
  field.maxLength = 200;
  field.setAttribute("aria-label", "Novo título");
  row.replaceChildren(field);
  field.focus();
  field.select();

  let finished = false;
  const finish = async (save) => {
    if (finished) return;
    finished = true;
    const title = field.value.trim();
    if (save && title && title !== conversation.title) {
      await jsonRequest(`/api/conversations/${encodeURIComponent(conversation.id)}`, { title }, "PATCH");
    }
    await loadConversations();
  };
  field.addEventListener("keydown", (event) => {
    if (event.key === "Enter") finish(true);
    if (event.key === "Escape") {
      event.stopPropagation(); // Esc aqui cancela a edição, não a resposta em andamento
      finish(false);
    }
  });
  field.addEventListener("blur", () => finish(true));
}

function renderConversations() {
  const query = searchInput.value.trim().toLowerCase();
  const visible = state.conversations.filter((c) => !query || c.title.toLowerCase().includes(query));
  const nodes = [];
  let lastLabel = null;
  for (const conversation of visible) {
    const label = groupLabel(conversation.updated_at);
    if (label !== lastLabel) nodes.push(el("div", "group", label));
    lastLabel = label;
    nodes.push(conversationRow(conversation));
  }
  if (!nodes.length) nodes.push(el("div", "empty-list", query ? "Nada encontrado." : "Nenhuma conversa ainda."));
  conversationsEl.replaceChildren(...nodes);
}

async function loadConversations() {
  const response = await fetch("/api/conversations?limit=200");
  if (!response.ok) return;
  state.conversations = (await response.json()).conversations;
  renderConversations();
  const current = state.conversations.find((c) => c.id === state.id);
  if (current) $("title").textContent = current.title || "Nova conversa";
}

searchInput.addEventListener("input", renderConversations);

/* ---------- Gaveta (telas estreitas) ---------- */

function openDrawer() {
  sidebar.classList.add("open");
  scrim.hidden = false;
}

function closeDrawer() {
  sidebar.classList.remove("open");
  scrim.hidden = true;
}

$("menu").addEventListener("click", openDrawer);
scrim.addEventListener("click", closeDrawer);

/* ---------- Abrir conversa / histórico ---------- */

function toolCard(name, args) {
  const card = el("details", "tool");
  card.appendChild(el("summary", "", `⚙ ${name}(${JSON.stringify(args)})`));
  return card;
}

function fillToolCard(card, ok, output) {
  if (!ok) card.classList.add("failed");
  card.appendChild(el("pre", "", output));
}

function renderStored(message) {
  if (message.role === "tool") {
    const meta = message.meta || {};
    const card = toolCard(meta.name, meta.args);
    fillToolCard(card, meta.ok, message.content);
    return [card];
  }
  const nodes = [el("div", `msg ${message.role}`, message.content)];
  if (message.meta && message.meta.interrupted) nodes.push(el("div", "note", "Resposta interrompida."));
  return nodes;
}

async function fetchPage(id, before) {
  const query = before ? `?before=${before}` : "";
  const response = await fetch(`/api/conversations/${encodeURIComponent(id)}${query}`);
  if (response.status === 401) showLogin();
  return response.ok ? response.json() : null;
}

function rememberScroll() {
  if (state.id) scrollMemory.set(state.id, messagesEl.scrollHeight - messagesEl.scrollTop);
}

function resetChat() {
  stopGeneration();
  rememberScroll();
  state.id = null;
  state.oldest = null;
  state.hasMore = false;
  $("title").textContent = "Nova conversa";
  const welcome = el("div", "welcome");
  const logo = el("img");
  logo.src = "assets/logo.png";
  logo.alt = "";
  logo.width = logo.height = 96;
  welcome.append(logo, "Distopic online. O que deseja?");
  messagesEl.replaceChildren(welcome);
  toBottom();
  renderConversations();
  input.focus();
}

async function showConversation(id) {
  stopGeneration();
  rememberScroll();
  const page = await fetchPage(id);
  if (!page) {
    // Conversa apagada ou link inválido.
    history.replaceState(null, "", "#/");
    return resetChat();
  }
  state.id = id;
  state.oldest = page.messages.length ? page.messages[0].id : null;
  state.hasMore = page.has_more;
  $("title").textContent = page.conversation.title || "Nova conversa";

  const fragment = document.createDocumentFragment();
  for (const message of page.messages) fragment.append(...renderStored(message));
  messagesEl.replaceChildren(fragment);

  toBottom();
  const fromEnd = scrollMemory.get(id);
  if (fromEnd) {
    messagesEl.scrollTop = messagesEl.scrollHeight - fromEnd;
    state.follow = nearBottom();
  }
  renderConversations();
  input.focus();
  if (messagesEl.scrollTop < 200) loadOlder();
}

// Ao chegar perto do topo, busca a página anterior mantendo o ponto de leitura.
async function loadOlder() {
  if (!state.hasMore || state.loadingOlder || !state.id) return;
  state.loadingOlder = true;
  const id = state.id;
  try {
    const page = await fetchPage(id, state.oldest);
    if (!page || id !== state.id) return;
    const anchor = messagesEl.firstElementChild;
    const before = anchor ? anchor.getBoundingClientRect().top : 0;
    const fragment = document.createDocumentFragment();
    for (const message of page.messages) fragment.append(...renderStored(message));
    messagesEl.prepend(fragment);
    if (anchor) messagesEl.scrollTop += anchor.getBoundingClientRect().top - before;
    if (page.messages.length) state.oldest = page.messages[0].id;
    state.hasMore = page.has_more;
  } finally {
    state.loadingOlder = false;
  }
}

// A conversa aberta fica na URL (#/c/<id>): voltar, avançar e recarregar funcionam.
async function route() {
  if (chatView.hidden) return;
  const match = location.hash.match(/^#\/c\/([\w-]+)$/);
  const id = match ? match[1] : null;
  if (id === state.id) return;
  if (id) await showConversation(id);
  else resetChat();
}

window.addEventListener("hashchange", route);

function newConversation() {
  closeDrawer();
  if (location.hash === "#/" || location.hash === "") resetChat();
  else location.hash = "#/";
}

$("new-chat").addEventListener("click", newConversation);

/* ---------- Chat ---------- */

/* ---------- Envio de documentos ---------- */
// "Alimentar a IA" = indexar o arquivo no RAG privado da conta; o modelo não é treinado.

const MAX_UPLOAD_BYTES = 20 * 1024 * 1024;
const ALLOWED_DOC_EXTENSIONS = [".pdf", ".md", ".txt"];
let uploadMode = "library";

function setUploadStatus(text, isError = false) {
  uploadStatus.textContent = text;
  uploadStatus.classList.toggle("failed", isError);
  uploadStatus.hidden = !text;
}

function toggleAttachMenu(open) {
  attachMenu.hidden = !open;
  attachButton.setAttribute("aria-expanded", String(open));
}

attachButton.addEventListener("click", () => toggleAttachMenu(attachMenu.hidden));
document.addEventListener("click", (event) => {
  if (!event.target.closest(".attach")) toggleAttachMenu(false);
});
for (const option of attachMenu.querySelectorAll("button")) {
  option.addEventListener("click", () => {
    uploadMode = option.dataset.mode;
    toggleAttachMenu(false);
    docFileInput.click();
  });
}

// XMLHttpRequest em vez de fetch: é o que informa o progresso do envio.
function sendDocument(file, mode) {
  return new Promise((resolve) => {
    const form = new FormData();
    form.append("file", file);
    form.append("mode", mode);
    if (mode === "conversation" && state.id) form.append("conversation_id", state.id);

    const request = new XMLHttpRequest();
    request.open("POST", "/api/docs/upload");
    // Sem Content-Type manual: o navegador define multipart/form-data com o boundary.
    request.upload.addEventListener("progress", (event) => {
      if (!event.lengthComputable) return;
      const percent = Math.round((event.loaded / event.total) * 100);
      setUploadStatus(`Enviando ${file.name}… ${percent}%`);
    });
    request.upload.addEventListener("load", () => {
      setUploadStatus(`Indexando ${file.name}… isso pode levar alguns segundos.`);
    });
    request.addEventListener("load", () => {
      let body = {};
      try {
        body = JSON.parse(request.responseText);
      } catch {
        // resposta sem JSON (erro do servidor): tratada pelo status
      }
      resolve({ status: request.status, body });
    });
    request.addEventListener("error", () => resolve({ status: 0, body: {} }));
    request.send(form);
  });
}

docFileInput.addEventListener("change", async () => {
  const file = docFileInput.files?.[0];
  docFileInput.value = "";
  if (!file) return;
  const mode = uploadMode;

  const extension = file.name.slice(file.name.lastIndexOf(".")).toLowerCase();
  if (!ALLOWED_DOC_EXTENSIONS.includes(extension)) {
    return setUploadStatus("Envie um arquivo PDF, MD ou TXT.", true);
  }
  if (file.size === 0) return setUploadStatus("O arquivo está vazio.", true);
  if (file.size > MAX_UPLOAD_BYTES) return setUploadStatus("O arquivo excede o limite de 20 MB.", true);

  attachButton.disabled = true;
  setUploadStatus(`Enviando ${file.name}…`);
  const { status, body } = await sendDocument(file, mode);
  attachButton.disabled = false;

  if (status === 401) {
    setUploadStatus("");
    return showLogin(); // sessão expirada
  }
  if (status === 0) return setUploadStatus("Falha de conexão ao enviar o arquivo.", true);
  if (status < 200 || status >= 300) {
    const detail = typeof body.detail === "string" ? body.detail : `Falha no servidor ao processar o arquivo (erro ${status}).`;
    return setUploadStatus(detail, true);
  }

  const pieces = `${body.chunks_indexed} ${body.chunks_indexed === 1 ? "trecho indexado" : "trechos indexados"}`;
  if (mode === "library") {
    setUploadStatus(`${body.filename}: ${pieces} na sua Biblioteca.`);
    return;
  }
  setUploadStatus(`${body.filename}: ${pieces}, disponível só nesta conversa.`);
  if (!state.id) {
    // O servidor criou a conversa para receber o arquivo.
    state.id = body.conversation_id;
    history.replaceState(null, "", `#/c/${state.id}`);
    loadConversations();
  }
  append(el("div", "note", `Documento anexado a esta conversa: ${body.filename} (${pieces}).`));
});

/* ---------- Painéis: documentos e conta ---------- */

for (const button of document.querySelectorAll("dialog [data-close]")) {
  button.addEventListener("click", () => button.closest("dialog").close());
}

// Remoção em dois cliques, sem caixa de diálogo: o primeiro arma, o segundo confirma.
function confirmButton(label, onConfirm) {
  const button = el("button", "icon delete", "✕");
  button.type = "button";
  button.setAttribute("aria-label", label);
  button.addEventListener("click", async () => {
    if (!button.classList.contains("armed")) {
      button.classList.add("armed");
      button.textContent = "Apagar?";
      setTimeout(() => {
        button.classList.remove("armed");
        button.textContent = "✕";
      }, 3000);
      return;
    }
    button.disabled = true;
    await onConfirm();
  });
  return button;
}

function documentRows(container, documents, emptyText) {
  if (!documents.length) return container.replaceChildren(el("div", "empty-list", emptyText));
  container.replaceChildren(
    ...documents.map((doc) => {
      const row = el("div", "row");
      const name = el("span", "row-name", doc.filename);
      name.title = doc.filename;
      const remove = confirmButton(`Apagar ${doc.filename}`, async () => {
        await fetch(`/api/docs/${encodeURIComponent(doc.id)}`, { method: "DELETE" });
        await loadDocuments();
      });
      row.append(name, el("span", "muted", `${doc.chunks} ${doc.chunks === 1 ? "trecho" : "trechos"}`), remove);
      return row;
    })
  );
}

async function loadDocuments() {
  const query = state.id ? `?conversation_id=${encodeURIComponent(state.id)}` : "";
  const response = await fetch(`/api/docs${query}`);
  if (response.status === 401) return showLogin();
  if (!response.ok) return;
  const data = await response.json();
  documentRows($("docs-library"), data.library, "Nenhum documento na Biblioteca. Use o botão ＋ ao lado da mensagem.");
  documentRows(
    $("docs-conversation"),
    data.conversation,
    state.id ? "Nenhum arquivo anexado a esta conversa." : "Abra uma conversa para ver os arquivos anexados a ela."
  );
}

$("open-docs").addEventListener("click", async () => {
  closeDrawer();
  await loadDocuments();
  $("docs-dialog").showModal();
});

async function responseMessage(response, fallback) {
  const data = await response.json().catch(() => ({}));
  return typeof data.detail === "string" ? data.detail : fallback;
}

function userRow(user) {
  const row = el("div", "row");
  const name = el("span", "row-name", user.username);
  row.append(name, el("span", "muted", user.is_admin ? "administrador" : "conta comum"));

  // Redefinir a senha de outra conta (quem esqueceu a própria).
  const reset = el("button", "icon", "Redefinir senha");
  reset.type = "button";
  reset.addEventListener("click", () => {
    const field = el("input");
    field.type = "password";
    field.placeholder = `Nova senha de ${user.username}`;
    field.autocomplete = "new-password";
    const save = el("button", "", "Salvar");
    save.type = "button";
    const result = el("span", "muted");
    save.addEventListener("click", async () => {
      const response = await jsonRequest(`/api/users/${encodeURIComponent(user.id)}/password`, { password: field.value });
      result.textContent = response.ok ? "Senha redefinida." : await responseMessage(response, "Não foi possível redefinir.");
      if (response.ok) field.value = "";
    });
    const form = el("div", "row row-form");
    form.append(field, save, result);
    row.after(form);
    reset.disabled = true;
    field.focus();
  });
  row.append(reset);

  if (user.id !== state.user.id) {
    row.append(
      confirmButton(`Remover a conta ${user.username}`, async () => {
        await fetch(`/api/users/${encodeURIComponent(user.id)}`, { method: "DELETE" });
        await loadUsers();
      })
    );
  }
  return row;
}

async function loadUsers() {
  if (!state.user || !state.user.is_admin) return;
  const response = await fetch("/api/users");
  if (!response.ok) return;
  const { users } = await response.json();
  $("users").replaceChildren(...users.map(userRow));
}

$("open-account").addEventListener("click", async () => {
  closeDrawer();
  $("own-password-result").textContent = "";
  $("new-user-result").textContent = "";
  await loadUsers();
  $("account-dialog").showModal();
});

$("own-password-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const result = $("own-password-result");
  const response = await jsonRequest("/api/me/password", {
    current_password: $("current-password").value,
    new_password: $("new-password").value,
  });
  if (response.status === 401) return showLogin();
  result.textContent = response.ok ? "Senha alterada." : await responseMessage(response, "Não foi possível trocar a senha.");
  if (response.ok) event.target.reset();
});

$("new-user-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const result = $("new-user-result");
  const username = $("new-user-name").value.trim();
  const response = await jsonRequest("/api/users", { username, password: $("new-user-password").value });
  if (response.status === 401) return showLogin();
  if (!response.ok) {
    result.textContent = await responseMessage(response, "Não foi possível criar a conta.");
    return;
  }
  result.textContent = `Conta ${username} criada. Passe o usuário e a senha inicial à pessoa.`;
  event.target.reset();
  await loadUsers();
});

function setGenerating(controller) {
  state.abort = controller;
  const generating = controller !== null;
  sendButton.hidden = generating;
  stopButton.hidden = !generating;
  $("status").hidden = !generating;
}

function stopGeneration() {
  if (state.abort) state.abort.abort();
}

stopButton.addEventListener("click", stopGeneration);

// Pedido de confirmação de uma ação de escrita: nada é executado no PC até
// o usuário clicar em Autorizar.
function addConfirmCard(event) {
  const card = el("div", "confirm");
  const actions = el("div", "confirm-actions");
  const status = el("span", "muted");

  const decide = async (approved) => {
    for (const button of actions.querySelectorAll("button")) button.disabled = true;
    const response = await jsonRequest("/api/confirm", { id: event.id, approved }).catch(() => null);
    if (response && response.ok) status.textContent = approved ? "Autorizado" : "Recusado";
    else status.textContent = "Pedido expirado";
    card.classList.add("decided");
  };

  const deny = el("button", "ghost", "Recusar");
  deny.type = "button";
  deny.addEventListener("click", () => decide(false));
  const allow = el("button", "", "Autorizar");
  allow.type = "button";
  allow.addEventListener("click", () => decide(true));

  actions.append(deny, allow, status);
  card.append(
    el("strong", "", `A Distopic quer executar: ${event.name}`),
    el("p", "", event.summary),
    el("pre", "", event.detail),
    actions
  );
  append(card);
}

async function sendMessage(text, controller) {
  state.follow = true; // enviar sempre leva ao fim da conversa
  append(el("div", "msg user", text));

  // Indicador imediato de "processando", antes do primeiro token.
  let bubble = append(el("div", "msg assistant pending"));
  let textNode = null;
  let card = null;

  // Os tokens são acumulados e aplicados no máximo uma vez por quadro, em vez
  // de uma alteração de DOM (e um recálculo de layout) por token.
  let pending = "";
  let frame = 0;
  const flush = () => {
    cancelAnimationFrame(frame);
    frame = 0;
    if (!pending) return;
    if (!bubble) bubble = append(el("div", "msg assistant"));
    if (!textNode) {
      bubble.classList.remove("pending");
      textNode = bubble.appendChild(document.createTextNode(""));
    }
    textNode.appendData(pending);
    pending = "";
    contentGrew();
  };
  const dropEmptyBubble = () => {
    if (bubble && !textNode) bubble.remove();
    bubble = null;
    textNode = null;
  };

  const handle = (event) => {
    if (event.type === "conversation") {
      state.id = event.id;
      $("title").textContent = event.title || "Nova conversa";
      history.replaceState(null, "", `#/c/${event.id}`); // não dispara hashchange
      loadConversations();
    } else if (event.type === "token") {
      pending += event.text;
      if (!frame) frame = requestAnimationFrame(flush);
    } else if (event.type === "tool_call") {
      flush();
      dropEmptyBubble(); // o texto após a ferramenta vai em um balão novo
      card = append(toolCard(event.name, event.args));
    } else if (event.type === "confirm_request") {
      addConfirmCard(event);
    } else if (event.type === "tool_result") {
      if (card) fillToolCard(card, event.ok, event.output);
      bubble = append(el("div", "msg assistant pending"));
    } else if (event.type === "truncated") {
      flush();
      append(el("div", "note", "Resposta cortada pelo limite de tamanho. Envie \"continue\" para seguir."));
    } else if (event.type === "error") {
      flush();
      dropEmptyBubble();
      append(el("div", "msg error", event.message));
    }
  };

  try {
    const response = await jsonRequest(
      "/api/chat", { conversation_id: state.id, message: text }, "POST", controller.signal
    );
    if (response.status === 401) return showLogin();
    if (!response.ok) {
      append(el("div", "msg error", `Erro ${response.status}`));
      return;
    }
    // Resposta em NDJSON: um evento JSON por linha, entregue em streaming.
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop();
      for (const line of lines) if (line.trim()) handle(JSON.parse(line));
    }
  } catch (err) {
    flush();
    dropEmptyBubble();
    if (controller.signal.aborted) append(el("div", "note", "Resposta interrompida."));
    else append(el("div", "msg error", "Conexão com o servidor perdida."));
  } finally {
    flush();
    dropEmptyBubble();
  }
}

$("chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = input.value.trim();
  if (!text || state.abort) return;
  input.value = "";
  input.style.height = "auto";
  const controller = new AbortController();
  setGenerating(controller);
  try {
    await sendMessage(text, controller);
  } finally {
    setGenerating(null);
    loadConversations(); // atualiza a ordem (mais recente primeiro)
    input.focus();
  }
});

input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("chat-form").requestSubmit();
  }
});

input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = `${input.scrollHeight}px`;
});

/* ---------- Atalhos ---------- */

document.addEventListener("keydown", (event) => {
  if (chatView.hidden) return;
  const ctrl = event.ctrlKey || event.metaKey;
  if (event.key === "Escape") {
    if (state.abort) stopGeneration();
    else closeDrawer();
  } else if (ctrl && event.key.toLowerCase() === "k") {
    event.preventDefault();
    openDrawer();
    searchInput.focus();
    searchInput.select();
  } else if (ctrl && event.key.toLowerCase() === "n") {
    // Funciona no aplicativo desktop; navegadores reservam Ctrl+N para nova janela.
    event.preventDefault();
    newConversation();
  }
});

checkSession();
