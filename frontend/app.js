"use strict";

const $ = (id) => document.getElementById(id);
const loginView = $("login-view");
const chatView = $("chat-view");
const chatPane = chatView.querySelector(".chat");
const sidebar = $("sidebar");
const scrim = $("scrim");
const messagesEl = $("messages");
const conversationsEl = $("conversations");
const searchInput = $("search");
const chatForm = $("chat-form");
const composer = chatForm.querySelector(".composer");
const input = $("input");
const inputCount = $("input-count");
const sendButton = $("send");
const stopButton = $("stop");
const jumpButton = $("jump");
const attachButton = $("attach");
const attachMenu = $("attach-menu");
const docFileInput = $("doc-file");
const uploadStatus = $("upload-status");
const menuButton = $("menu");

// O histórico vive no servidor (SQLite). Aqui fica só o estado da tela.
const state = {
  user: null, // conta da sessão: { id, username, is_admin }
  model: "", // modelo local em uso (informado pelo servidor)
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

// Ícone do sprite em index.html.
const SVG_NS = "http://www.w3.org/2000/svg";
function icon(name) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", "ico");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(SVG_NS, "use");
  use.setAttribute("href", `#i-${name}`);
  svg.appendChild(use);
  return svg;
}

function iconButton(name, label, className = "btn-icon small") {
  const button = el("button", className);
  button.type = "button";
  button.setAttribute("aria-label", label);
  button.title = label;
  button.appendChild(icon(name));
  return button;
}

/* ---------- Estado da IA (núcleo da barra superior) ---------- */

const PHASES = {
  idle: "Pronto",
  thinking: "Processando",
  generating: "Gerando",
  tool: "Executando",
  confirm: "Aguardando você",
};

function setPhase(phase) {
  chatPane.dataset.phase = phase;
  $("status").textContent = PHASES[phase];
}

/* ---------- Copiar ---------- */

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // Fora de contexto seguro (acesso pela rede local via http) a API não existe.
    const area = el("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    let ok = false;
    try {
      ok = document.execCommand("copy");
    } catch {
      ok = false;
    }
    area.remove();
    return ok;
  }
}

function copyButton(getText, label = "Copiar") {
  const button = el("button", "btn-icon");
  button.type = "button";
  const idle = () => button.replaceChildren(icon("copy"), el("span", "", label));
  idle();
  let timer = 0;
  button.addEventListener("click", async () => {
    const ok = await copyText(getText());
    button.replaceChildren(icon(ok ? "check" : "x"), el("span", "", ok ? "Copiado" : "Não foi possível copiar"));
    button.classList.toggle("copied", ok);
    clearTimeout(timer);
    timer = setTimeout(() => {
      idle();
      button.classList.remove("copied");
    }, 1600);
  });
  return button;
}

/* ---------- Markdown ---------- */
// Montado nó a nó com createElement/textContent: nenhum HTML vindo do modelo é interpretado.

const INLINE =
  /(`+)([^`]|[\s\S]*?[^`])\1(?!`)|\*\*(?=\S)([\s\S]*?\S)\*\*|__(?=\S)([\s\S]*?\S)__|~~(?=\S)([\s\S]*?\S)~~|\*(?=[^\s*])([\s\S]*?[^\s*])\*|(?<![\w])_(?=[^\s_])([\s\S]*?[^\s_])_(?![\w])|\[([^\]\n]+)\]\(([^)\s]+)\)/g;

function inline(parent, text) {
  let last = 0;
  for (const match of text.matchAll(INLINE)) {
    if (match.index > last) parent.append(text.slice(last, match.index));
    const [, ticks, code, bold1, bold2, strike, em1, em2, linkText, linkUrl] = match;
    if (ticks) {
      parent.append(el("code", "", code));
    } else if (bold1 || bold2) {
      const node = el("strong");
      inline(node, bold1 || bold2);
      parent.append(node);
    } else if (strike) {
      const node = el("del");
      inline(node, strike);
      parent.append(node);
    } else if (em1 || em2) {
      const node = el("em");
      inline(node, em1 || em2);
      parent.append(node);
    } else if (linkText) {
      // Links ficam como texto: a interface nunca navega para fora sozinha.
      parent.append(linkText);
      if (linkUrl !== linkText) parent.append(el("span", "count", ` ${linkUrl}`));
    }
    last = match.index + match[0].length;
  }
  if (last < text.length) parent.append(text.slice(last));
}

const FENCE = /^\s*(`{3,}|~{3,})\s*([\w+#.-]*)/;
const LIST_ITEM = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
const TABLE_SEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;
const HEADING = /^(#{1,6})\s+(.*?)\s*#*\s*$/;
const RULE = /^\s*([-*_])(\s*\1){2,}\s*$/;

const indentOf = (line) => line.match(/^\s*/)[0].replace(/\t/g, "    ").length;
const isTableStart = (line, next) => line.includes("|") && next !== undefined && TABLE_SEP.test(next);

function isBlockStart(line, next) {
  return (
    FENCE.test(line) || HEADING.test(line) || RULE.test(line) || /^\s*>/.test(line) ||
    LIST_ITEM.test(line) || isTableStart(line, next)
  );
}

function codeBlock(code, lang) {
  const block = el("div", "code");
  const head = el("div", "code-head");
  head.append(el("span", "", lang || "texto"), copyButton(() => code));
  const pre = el("pre");
  pre.append(el("code", "", code));
  block.append(head, pre);
  return block;
}

function tableBlock(lines, i) {
  const cells = (line) => line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
  const aligns = cells(lines[i + 1]).map((cell) =>
    cell.startsWith(":") && cell.endsWith(":") ? "center" : cell.endsWith(":") ? "right" : ""
  );
  const table = el("table");
  const headRow = el("tr");
  cells(lines[i]).forEach((text, index) => {
    const th = el("th");
    if (aligns[index]) th.style.textAlign = aligns[index];
    inline(th, text);
    headRow.append(th);
  });
  const thead = el("thead");
  thead.append(headRow);
  const tbody = el("tbody");
  i += 2;
  while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
    const row = el("tr");
    cells(lines[i]).forEach((text, index) => {
      const td = el("td");
      if (aligns[index]) td.style.textAlign = aligns[index];
      inline(td, text);
      row.append(td);
    });
    tbody.append(row);
    i++;
  }
  table.append(thead, tbody);
  const wrap = el("div", "table-wrap");
  wrap.append(table);
  return [wrap, i];
}

function listBlock(lines, i) {
  const base = indentOf(lines[i]);
  const ordered = /\d/.test(lines[i].match(LIST_ITEM)[2]);
  const list = el(ordered ? "ol" : "ul");
  if (ordered) {
    const start = parseInt(lines[i].match(LIST_ITEM)[2], 10);
    if (start !== 1) list.start = start;
  }
  let item = null;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      const next = lines[i + 1];
      if (next !== undefined && LIST_ITEM.test(next) && indentOf(next) >= base) {
        i++;
        continue;
      }
      break;
    }
    const indent = indentOf(line);
    const match = line.match(LIST_ITEM);
    if (match && indent < base) break;
    if (match && indent <= base + 1) {
      if (/\d/.test(match[2]) !== ordered) break;
      item = el("li");
      inline(item, match[3]);
      list.append(item);
      i++;
    } else if (match) {
      const [sub, next] = listBlock(lines, i);
      (item || list).append(sub);
      i = next;
    } else if (item && indent > base) {
      item.append(el("br"));
      inline(item, line.trim());
      i++;
    } else {
      break;
    }
  }
  return [list, i];
}

function parseBlocks(lines, target) {
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const fence = line.match(FENCE);
    if (fence) {
      const marker = fence[1];
      const body = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith(marker)) body.push(lines[i++]);
      i++; // fecha o bloco (durante o streaming ele pode ainda não ter fechado)
      target.append(codeBlock(body.join("\n"), fence[2]));
      continue;
    }
    if (!line.trim()) {
      i++;
      continue;
    }
    const heading = line.match(HEADING);
    if (heading) {
      const node = el(`h${Math.min(heading[1].length, 4)}`);
      inline(node, heading[2]);
      target.append(node);
      i++;
      continue;
    }
    if (RULE.test(line)) {
      target.append(el("hr"));
      i++;
      continue;
    }
    if (isTableStart(line, lines[i + 1])) {
      const [node, next] = tableBlock(lines, i);
      target.append(node);
      i = next;
      continue;
    }
    if (/^\s*>/.test(line)) {
      const quoted = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) quoted.push(lines[i++].replace(/^\s*>\s?/, ""));
      const quote = el("blockquote");
      parseBlocks(quoted, quote);
      target.append(quote);
      continue;
    }
    if (LIST_ITEM.test(line)) {
      const [node, next] = listBlock(lines, i);
      target.append(node);
      i = next;
      continue;
    }
    const paragraph = el("p");
    let first = true;
    while (i < lines.length && lines[i].trim() && (first || !isBlockStart(lines[i], lines[i + 1]))) {
      if (!first) paragraph.append(el("br"));
      inline(paragraph, lines[i].trim());
      first = false;
      i++;
    }
    target.append(paragraph);
  }
}

function renderMarkdown(target, text) {
  const fragment = document.createDocumentFragment();
  parseBlocks(text.replace(/\r\n?/g, "\n").split("\n"), fragment);
  target.replaceChildren(fragment);
}

/* ---------- Mensagens ---------- */

function assistantMessage(text) {
  const node = el("div", "msg assistant");
  node.append(el("div", "md"));
  if (text) {
    renderMarkdown(node.firstChild, text);
    finishAssistant(node, text);
  }
  return node;
}

function finishAssistant(node, text) {
  node.classList.remove("pending", "streaming");
  if (!text.trim() || node.querySelector(".msg-actions")) return;
  const actions = el("div", "msg-actions");
  actions.append(copyButton(() => text, "Copiar resposta"));
  node.append(actions);
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
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  messagesEl.scrollTo({ top: messagesEl.scrollHeight, behavior: reduce ? "auto" : "smooth" });
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
  $("password-label").textContent = setupMode ? "Nova senha" : "Senha";
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
  state.model = me.model || "";
  loginView.hidden = true;
  chatView.hidden = false;
  $("model").textContent = state.model;
  $("model").title = state.model ? `Modelo local: ${state.model}` : "";
  $("user-name").textContent = me.user.username;
  $("user-initial").textContent = me.user.username.slice(0, 1).toUpperCase();
  $("user-role").textContent = me.user.is_admin ? "administrador" : "conta comum";
  $("admin-section").hidden = !me.user.is_admin;
  setPhase("idle");
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
  const submit = $("login-submit");
  submit.disabled = true;
  try {
    const response = await jsonRequest(setupMode ? "/api/setup" : "/api/login", { username, password });
    $("password").value = "";
    $("password2").value = "";
    if (response.ok) return checkSession();
    const data = await response.json().catch(() => ({}));
    errorEl.textContent = typeof data.detail === "string" ? data.detail : "Não foi possível entrar";
  } catch {
    errorEl.textContent = "Servidor indisponível. Verifique se a Distopia está em execução.";
  } finally {
    submit.disabled = false;
  }
});

$("logout").addEventListener("click", async () => {
  stopGeneration();
  await fetch("/api/logout", { method: "POST" });
  history.replaceState(null, "", "#/");
  conversationsEl.replaceChildren();
  scrollMemory.clear();
  showLogin();
});

/* ---------- Remoção em dois cliques ---------- */
// Sem caixa de diálogo: o primeiro clique arma, o segundo confirma.
function armOnFirstClick(button, armedText, onConfirm) {
  let timer = 0;
  button.addEventListener("click", async () => {
    if (!button.classList.contains("armed")) {
      button.classList.add("armed");
      button.replaceChildren(armedText);
      timer = setTimeout(() => {
        button.classList.remove("armed");
        button.replaceChildren(icon("x"));
      }, 3000);
      return;
    }
    clearTimeout(timer);
    await onConfirm();
  });
  return button;
}

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
  if (conversation.id === state.id) open.setAttribute("aria-current", "page");
  open.addEventListener("click", () => {
    closeDrawer();
    location.hash = `#/c/${conversation.id}`;
  });

  const rename = iconButton("edit", `Renomear ${title}`);
  rename.addEventListener("click", () => startRename(row, conversation));

  const remove = armOnFirstClick(iconButton("x", `Apagar ${title}`, "btn-icon small delete"), "Apagar?", async () => {
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

/* ---------- Barra lateral: gaveta (telas estreitas) e recolhimento (telas largas) ---------- */

const wideScreen = matchMedia("(min-width: 761px)");
const SIDEBAR_KEY = "distopia.sidebar";

function sidebarCollapsed() {
  return chatView.classList.contains("sidebar-collapsed");
}

function syncSidebar() {
  const hidden = wideScreen.matches ? sidebarCollapsed() : !sidebar.classList.contains("open");
  sidebar.inert = wideScreen.matches && sidebarCollapsed();
  menuButton.setAttribute("aria-expanded", String(!hidden));
  menuButton.setAttribute("aria-label", hidden ? "Mostrar conversas" : "Ocultar conversas");
  menuButton.title = menuButton.getAttribute("aria-label");
}

function setCollapsed(collapsed) {
  chatView.classList.toggle("sidebar-collapsed", collapsed);
  try {
    localStorage.setItem(SIDEBAR_KEY, collapsed ? "collapsed" : "open");
  } catch {
    // armazenamento indisponível: a preferência vale só nesta sessão
  }
  syncSidebar();
}

function openDrawer() {
  if (wideScreen.matches) {
    if (sidebarCollapsed()) setCollapsed(false);
    return;
  }
  sidebar.classList.add("open");
  scrim.hidden = false;
  syncSidebar();
}

function closeDrawer() {
  sidebar.classList.remove("open");
  scrim.hidden = true;
  syncSidebar();
}

menuButton.addEventListener("click", () => {
  if (wideScreen.matches) setCollapsed(!sidebarCollapsed());
  else if (sidebar.classList.contains("open")) closeDrawer();
  else openDrawer();
});
scrim.addEventListener("click", closeDrawer);
wideScreen.addEventListener("change", () => {
  closeDrawer();
  syncSidebar();
});
try {
  if (localStorage.getItem(SIDEBAR_KEY) === "collapsed") chatView.classList.add("sidebar-collapsed");
} catch {
  // sem armazenamento: barra lateral aberta
}
syncSidebar();

/* ---------- Abrir conversa / histórico ---------- */

const TOOL_LABELS = {
  buscar_documentos: "Busca nos documentos",
  criar_arquivo: "Criar arquivo",
  ler_arquivo: "Ler arquivo",
  listar_diretorio: "Listar pasta",
};
const TOOL_STATES = { running: "executando", done: "concluída", failed: "falhou" };

function toolCard(name, args) {
  const card = el("details", "tool");
  const signature = `${name}(${JSON.stringify(args)})`;
  const summary = el("summary");
  summary.title = signature;
  const chevron = icon("chevron");
  chevron.classList.add("tool-chevron");
  summary.append(
    el("span", "tool-led"),
    el("span", "tool-label", TOOL_LABELS[name] || name || "Ferramenta"),
    el("span", "tool-args", signature),
    el("span", "tool-state"),
    chevron
  );
  card.appendChild(summary);
  return card;
}

function setToolState(card, toolState) {
  card.classList.remove("running", "done", "failed");
  card.classList.add(toolState);
  card.querySelector(".tool-state").textContent = TOOL_STATES[toolState];
}

function fillToolCard(card, ok, output) {
  setToolState(card, ok ? "done" : "failed");
  card.appendChild(el("pre", "", output));
}

function renderStored(message) {
  if (message.role === "tool") {
    const meta = message.meta || {};
    const card = toolCard(meta.name, meta.args);
    fillToolCard(card, meta.ok, message.content);
    return [card];
  }
  const nodes = [message.role === "assistant" ? assistantMessage(message.content) : el("div", `msg ${message.role}`, message.content)];
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

// Transição curta ao trocar de conversa: o conteúdo novo assenta no lugar.
let switchTimer = 0;
function playSwitch() {
  messagesEl.classList.remove("switching");
  void messagesEl.offsetWidth;
  messagesEl.classList.add("switching");
  clearTimeout(switchTimer);
  switchTimer = setTimeout(() => messagesEl.classList.remove("switching"), 400);
}

function welcomeScreen() {
  const welcome = el("div", "welcome");
  const logo = el("img");
  logo.src = "assets/logo.png";
  logo.alt = "";
  logo.width = logo.height = 64;

  const readout = el("dl", "welcome-readout");
  const line = (label, ...values) => {
    const dd = el("dd");
    dd.append(...values);
    readout.append(el("dt", "", label), dd);
  };
  if (state.model) line("Modelo", state.model);
  if (state.user) line("Conta", state.user.username);
  const shortcut = (keys, action) => {
    const node = el("span", "shortcut");
    node.append(el("kbd", "", keys), action);
    return node;
  };
  line("Atalhos", shortcut("Ctrl K", "buscar"), shortcut("Ctrl N", "nova conversa"), shortcut("Esc", "parar resposta"));

  welcome.append(logo, el("h2", "welcome-title", "Distopia online. O que deseja?"), readout);
  return welcome;
}

function resetChat() {
  stopGeneration();
  rememberScroll();
  state.id = null;
  state.oldest = null;
  state.hasMore = false;
  $("title").textContent = "Nova conversa";
  messagesEl.replaceChildren(welcomeScreen());
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
  playSwitch();

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

/* ---------- Envio de documentos ---------- */
// "Alimentar a IA" = indexar o arquivo no RAG privado da conta; o modelo não é treinado.

const MAX_UPLOAD_BYTES = 20 * 1024 * 1024;
const ALLOWED_DOC_EXTENSIONS = [".pdf", ".md", ".txt"];
let uploadMode = "library";

// kind: uploading | indexing | done | failed — cada estado tem marcador e trilho próprios.
function setUploadStatus(text, kind = "done", progress = 0) {
  uploadStatus.textContent = text;
  uploadStatus.className = `upload-status ${kind}`;
  uploadStatus.style.setProperty("--progress", String(progress));
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
attachMenu.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    event.stopPropagation();
    toggleAttachMenu(false);
    attachButton.focus();
  }
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
      const fraction = event.loaded / event.total;
      setUploadStatus(`Enviando ${file.name}… ${Math.round(fraction * 100)}%`, "uploading", fraction);
    });
    request.upload.addEventListener("load", () => {
      setUploadStatus(`Indexando ${file.name}… isso pode levar alguns segundos.`, "indexing");
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
    return setUploadStatus("Envie um arquivo PDF, MD ou TXT.", "failed");
  }
  if (file.size === 0) return setUploadStatus("O arquivo está vazio.", "failed");
  if (file.size > MAX_UPLOAD_BYTES) return setUploadStatus("O arquivo excede o limite de 20 MB.", "failed");

  attachButton.disabled = true;
  setUploadStatus(`Enviando ${file.name}…`, "uploading", 0);
  const { status, body } = await sendDocument(file, mode);
  attachButton.disabled = false;

  if (status === 401) {
    setUploadStatus("");
    return showLogin(); // sessão expirada
  }
  if (status === 0) return setUploadStatus("Falha de conexão ao enviar o arquivo.", "failed");
  if (status < 200 || status >= 300) {
    const detail = typeof body.detail === "string" ? body.detail : `Falha no servidor ao processar o arquivo (erro ${status}).`;
    return setUploadStatus(detail, "failed");
  }

  const pieces = `${body.chunks_indexed} ${body.chunks_indexed === 1 ? "trecho indexado" : "trechos indexados"}`;
  if (mode === "library") {
    setUploadStatus(`${body.filename}: ${pieces} na sua Biblioteca.`, "done");
    return;
  }
  setUploadStatus(`${body.filename}: ${pieces}, disponível só nesta conversa.`, "done");
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
// Clique fora do painel (no fundo escurecido) também fecha.
for (const dialog of document.querySelectorAll("dialog")) {
  dialog.addEventListener("click", (event) => {
    if (event.target !== dialog) return;
    const box = dialog.getBoundingClientRect();
    const inside = event.clientX >= box.left && event.clientX <= box.right && event.clientY >= box.top && event.clientY <= box.bottom;
    if (!inside) dialog.close();
  });
}

function confirmButton(label, onConfirm, armedText = "Apagar?") {
  const button = iconButton("x", label, "btn-icon small delete");
  return armOnFirstClick(button, armedText, async () => {
    button.disabled = true;
    button.closest(".row")?.classList.add("removing");
    await onConfirm();
  });
}

const formatDate = (seconds) =>
  new Date(seconds * 1000).toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit", year: "numeric" });

function documentRows(container, documents, emptyText) {
  if (!documents.length) return container.replaceChildren(el("div", "empty-list", emptyText));
  container.replaceChildren(
    ...documents.map((doc) => {
      const row = el("div", "row");
      const extension = (doc.filename.includes(".") ? doc.filename.split(".").pop() : "").toLowerCase();
      const tag = el("span", `file-tag ${extension}`, extension.toUpperCase().slice(0, 4) || "DOC");
      const name = el("span", "row-name", doc.filename);
      name.title = doc.filename;
      const remove = confirmButton(`Apagar ${doc.filename}`, async () => {
        await fetch(`/api/docs/${encodeURIComponent(doc.id)}`, { method: "DELETE" });
        await loadDocuments();
      });
      row.append(tag, name, el("span", "row-meta", `${doc.chunks} ${doc.chunks === 1 ? "trecho" : "trechos"}`));
      if (typeof doc.created_at === "number") row.append(el("span", "row-meta", formatDate(doc.created_at)));
      row.append(remove);
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
  $("docs-library-count").textContent = data.library.length ? String(data.library.length) : "";
  $("docs-conversation-count").textContent = data.conversation.length ? String(data.conversation.length) : "";
  documentRows($("docs-library"), data.library, "Nenhum documento na Biblioteca. Use o clipe ao lado da mensagem para adicionar.");
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

function showResult(node, text, ok) {
  node.textContent = text;
  node.className = `result ${ok ? "ok" : "fail"}`;
}

function userRow(user) {
  const row = el("div", "row");
  const name = el("span", "row-name", user.username);
  row.append(name, el("span", user.is_admin ? "role-tag admin" : "role-tag", user.is_admin ? "administrador" : "conta comum"));

  // Redefinir a senha de outra conta (quem esqueceu a própria).
  const reset = el("button", "btn-icon", "Redefinir senha");
  reset.type = "button";
  reset.addEventListener("click", () => {
    const field = el("input");
    field.type = "password";
    field.placeholder = `Nova senha de ${user.username}`;
    field.autocomplete = "new-password";
    field.setAttribute("aria-label", `Nova senha de ${user.username}`);
    const save = el("button", "btn btn-primary", "Salvar");
    save.type = "button";
    const result = el("span", "result");
    save.addEventListener("click", async () => {
      const response = await jsonRequest(`/api/users/${encodeURIComponent(user.id)}/password`, { password: field.value });
      showResult(result, response.ok ? "Senha redefinida." : await responseMessage(response, "Não foi possível redefinir."), response.ok);
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
      confirmButton(
        `Remover a conta ${user.username}`,
        async () => {
          await fetch(`/api/users/${encodeURIComponent(user.id)}`, { method: "DELETE" });
          await loadUsers();
        },
        "Remover?"
      )
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
  showResult(result, response.ok ? "Senha alterada." : await responseMessage(response, "Não foi possível trocar a senha."), response.ok);
  if (response.ok) event.target.reset();
});

$("new-user-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const result = $("new-user-result");
  const username = $("new-user-name").value.trim();
  const response = await jsonRequest("/api/users", { username, password: $("new-user-password").value });
  if (response.status === 401) return showLogin();
  if (!response.ok) {
    showResult(result, await responseMessage(response, "Não foi possível criar a conta."), false);
    return;
  }
  showResult(result, `Conta ${username} criada. Passe o usuário e a senha inicial à pessoa.`, true);
  event.target.reset();
  await loadUsers();
});

/* ---------- Chat ---------- */

function setGenerating(controller) {
  state.abort = controller;
  const generating = controller !== null;
  sendButton.hidden = generating;
  stopButton.hidden = !generating;
  setPhase(generating ? "thinking" : "idle");
}

function stopGeneration() {
  if (state.abort) state.abort.abort();
}

stopButton.addEventListener("click", stopGeneration);

// Pedido de confirmação de uma ação de escrita: nada é executado no PC até
// o usuário clicar em Autorizar. Nenhum botão vem focado: a decisão é explícita.
function addConfirmCard(event) {
  const card = el("div", "confirm");
  card.setAttribute("role", "group");
  card.setAttribute("aria-label", "Autorização necessária");

  const head = el("div", "confirm-head");
  head.append(icon("shield"), el("strong", "", "Autorização necessária"), el("code", "", event.name));

  const body = el("div", "confirm-body");
  const intro = el("p", "confirm-summary");
  intro.append(
    `A Distopia quer executar ${TOOL_LABELS[event.name] ? `“${TOOL_LABELS[event.name]}”` : "uma ação"}. Nada acontece até você decidir.`
  );
  body.append(intro);
  if (event.summary) body.append(el("p", "confirm-summary", event.summary));
  if (event.detail) body.append(el("div", "confirm-label", "O que será feito"), el("pre", "", event.detail));

  const actions = el("div", "confirm-actions");
  const status = el("span", "result");

  const decide = async (approved) => {
    for (const button of actions.querySelectorAll("button")) button.disabled = true;
    const response = await jsonRequest("/api/confirm", { id: event.id, approved }).catch(() => null);
    const ok = response && response.ok;
    status.textContent = ok ? (approved ? "Autorizado" : "Recusado") : "Pedido expirado";
    status.className = `result ${ok && approved ? "ok" : ""}`;
    card.classList.add("decided", ok ? (approved ? "approved" : "denied") : "expired");
    if (state.abort) setPhase(ok && approved ? "tool" : "thinking");
  };

  const deny = el("button", "btn btn-ghost", "Recusar");
  deny.type = "button";
  deny.addEventListener("click", () => decide(false));
  const allow = el("button", "btn btn-authorize", "Autorizar");
  allow.type = "button";
  allow.addEventListener("click", () => decide(true));

  actions.append(deny, allow, status);
  body.append(actions);
  card.append(head, body);
  append(card);
}

async function sendMessage(text, controller) {
  state.follow = true; // enviar sempre leva ao fim da conversa
  append(el("div", "msg user fresh", text));

  // Indicador imediato de "processando", antes do primeiro token.
  let bubble = assistantMessage("");
  bubble.classList.add("pending");
  append(bubble);
  let raw = ""; // texto bruto do balão atual; é renderizado como markdown a cada quadro
  let card = null;

  // Os tokens são acumulados e aplicados no máximo uma vez por quadro, em vez
  // de uma alteração de DOM (e um recálculo de layout) por token.
  let pending = "";
  let frame = 0;
  const flush = () => {
    cancelAnimationFrame(frame);
    frame = 0;
    if (!pending) return;
    if (!bubble) bubble = append(assistantMessage(""));
    if (!raw) {
      bubble.classList.remove("pending");
      bubble.classList.add("streaming");
      setPhase("generating");
    }
    raw += pending;
    pending = "";
    renderMarkdown(bubble.querySelector(".md"), raw);
    contentGrew();
  };
  // Fecha o balão atual: remove se ficou vazio, senão finaliza (sem cursor, com ações).
  const dropEmptyBubble = () => {
    if (bubble && !raw) bubble.remove();
    else if (bubble) finishAssistant(bubble, raw);
    bubble = null;
    raw = "";
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
      setToolState(card, "running");
      setPhase("tool");
    } else if (event.type === "confirm_request") {
      addConfirmCard(event);
      setPhase("confirm");
    } else if (event.type === "tool_result") {
      if (card) fillToolCard(card, event.ok, event.output);
      bubble = assistantMessage("");
      bubble.classList.add("pending");
      append(bubble);
      setPhase("thinking");
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
    // Ferramenta sem resultado (resposta interrompida no meio da execução).
    if (card && card.classList.contains("running")) setToolState(card, "failed");
  }
}

// Mensagens longas: contador discreto e máscara no topo quando o campo rola.
function updateComposer() {
  composer.classList.toggle("long", input.scrollHeight > input.clientHeight + 2);
  const length = input.value.length;
  if (length < 280) {
    inputCount.textContent = "";
    return;
  }
  const lines = input.value.split("\n").length;
  inputCount.textContent = `${lines} ${lines === 1 ? "linha" : "linhas"}, ${length.toLocaleString("pt-BR")} caracteres`;
}

chatForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = input.value.trim();
  if (!text || state.abort) return;
  input.value = "";
  input.style.height = "auto";
  updateComposer();
  chatForm.classList.remove("sent");
  void chatForm.offsetWidth;
  chatForm.classList.add("sent");
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
    chatForm.requestSubmit();
  }
});

input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = `${input.scrollHeight}px`;
  updateComposer();
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
