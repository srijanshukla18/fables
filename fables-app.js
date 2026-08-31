"use strict";

const $ = (id) => document.getElementById(id);
const Core = window.FablesCore;
const CHUNK = 150;
const PRE_CAP = 15000;
const SOURCE_ORDER = [
  "claude", "cowork", "codex", "copilot", "vscode", "cursor",
  "gemini", "opencode", "continue", "cline", "roo", "goose", "hermes",
];
const SOURCE_META = {
  claude: { label: "Claude Code" },
  cowork: { label: "Claude Cowork" },
  codex: { label: "Codex" },
  copilot: { label: "Copilot CLI" },
  cursor: { label: "Cursor" },
  gemini: { label: "Gemini CLI" },
  opencode: { label: "OpenCode" },
  continue: { label: "Continue" },
  cline: { label: "Cline" },
  roo: { label: "Roo Code" },
  goose: { label: "Goose" },
  hermes: { label: "Hermes Agent" },
  vscode: { label: "VS Code Chat" },
};

const state = {
  sessions: [],
  providers: [],
  filterSrc: "",
  current: null,
  raw: "",
  parsed: null,
  renderQueue: [],
  renderedUpto: 0,
  turnByIndex: [],
  runChangedByIndex: [],
  observer: null,
  query: "",
  requestController: null,
  requestSerial: 0,
  searchTimer: null,
  embeddedSessions: new Map(),
  exportMode: "session",
  exportEntries: [],
  exportFailures: [],
  exportStats: null,
  exportPreview: [],
};

function esc(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function fmtBytes(value) {
  const size = Number(value) || 0;
  if (size >= 1048576) return (size / 1048576).toFixed(1) + " MB";
  if (size >= 1024) return Math.round(size / 1024) + " KB";
  return size + " B";
}

function fmtTokens(value) {
  const count = Number(value) || 0;
  if (count >= 1e6) return (count / 1e6).toFixed(1) + "M";
  if (count >= 1e3) return (count / 1e3).toFixed(1) + "k";
  return String(count);
}

function dateValue(value) {
  if (typeof value === "number" && value > 0 && value < 100000000000) {
    return value * 1000;
  }
  return value;
}

function fmtWhen(value) {
  const date = new Date(dateValue(value));
  if (Number.isNaN(date.getTime())) return "";
  const now = new Date();
  const time = date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (date.toDateString() === now.toDateString()) return time;
  return date.toLocaleDateString([], {
    day: "numeric",
    month: "short",
    year: "2-digit",
  }) + " " + time;
}

function fmtTime(value) {
  const date = new Date(dateValue(value));
  return Number.isNaN(date.getTime()) ? "" :
    date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function slug(value) {
  return String(value || "session")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 60) || "session";
}

function sourceLabel(source) {
  return (SOURCE_META[source] && SOURCE_META[source].label) || source || "agent";
}

function sourceClass(source) {
  return "source-" + String(source || "unknown").replace(/[^a-z0-9_-]/gi, "");
}

function setLive(message) {
  $("livestatus").textContent = message;
}

function setLoading(message) {
  const node = $("loadingStatus");
  node.textContent = message || "";
  node.hidden = !message;
  setLive(message || "");
}

function mdInline(source) {
  let text = esc(source);
  text = text.replace(/`([^`]+)`/g, "<code>$1</code>");
  text = text.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  text = text.replace(
    /(^|[\s(])\*([^*\s][^*]*?)\*(?=[\s.,;:!?)]|$)/g,
    "$1<em>$2</em>"
  );
  text = text.replace(
    /\[([^\]]+)\]\((https?:[^)\s"'<>]+)\)/g,
    (_, label, url) =>
      '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + label + "</a>"
  );
  return text;
}

function mdText(source) {
  const lines = source.split("\n");
  let html = "";
  let paragraph = [];
  let list = null;
  const flushParagraph = () => {
    if (!paragraph.length) return;
    html += "<p>" + paragraph.map(mdInline).join("<br>") + "</p>";
    paragraph = [];
  };
  const flushList = () => {
    if (!list) return;
    html += "<" + list.tag + ">" +
      list.items.map((item) => "<li>" + mdInline(item) + "</li>").join("") +
      "</" + list.tag + ">";
    list = null;
  };
  for (const line of lines) {
    const heading = line.match(/^(#{1,4})\s+(.*)/);
    const unordered = line.match(/^\s*[-*]\s+(.*)/);
    const ordered = line.match(/^\s*\d+[.)]\s+(.*)/);
    if (heading) {
      flushParagraph();
      flushList();
      const level = heading[1].length;
      html += "<h" + level + ">" + mdInline(heading[2]) + "</h" + level + ">";
    } else if (/^\s*(---+|\*\*\*+)\s*$/.test(line)) {
      flushParagraph();
      flushList();
      html += "<hr>";
    } else if (unordered) {
      flushParagraph();
      if (!list || list.tag !== "ul") {
        flushList();
        list = { tag: "ul", items: [] };
      }
      list.items.push(unordered[1]);
    } else if (ordered) {
      flushParagraph();
      if (!list || list.tag !== "ol") {
        flushList();
        list = { tag: "ol", items: [] };
      }
      list.items.push(ordered[1]);
    } else if (/^\s*>\s?/.test(line)) {
      flushParagraph();
      flushList();
      html += "<blockquote>" + mdInline(line.replace(/^\s*>\s?/, "")) + "</blockquote>";
    } else if (!line.trim()) {
      flushParagraph();
      flushList();
    } else {
      flushList();
      paragraph.push(line);
    }
  }
  flushParagraph();
  flushList();
  return html;
}

function md(source) {
  const text = String(source || "");
  if (text.length > 60000) {
    return "<pre class='code'><code>" + esc(text) + "</code></pre>";
  }
  const parts = text.split("```");
  let html = "";
  for (let index = 0; index < parts.length; index++) {
    if (index % 2 === 0) {
      html += mdText(parts[index]);
      continue;
    }
    let code = parts[index];
    let language = "";
    const newline = code.indexOf("\n");
    if (newline > -1 && /^[\w+-]{0,20}$/.test(code.slice(0, newline).trim())) {
      language = code.slice(0, newline).trim();
      code = code.slice(newline + 1);
    }
    html += "<pre class='code'" +
      (language ? " data-lang='" + esc(language) + "'" : "") +
      "><code>" + esc(code) + "</code></pre>";
  }
  return html;
}

function toolArgSummary(input) {
  if (input === undefined || input === null) return "";
  if (typeof input === "string") return input;
  const candidate = input.command || input.cmd || input.file_path || input.path ||
    input.pattern || input.query || input.url || input.prompt || input.description || "";
  if (candidate) return String(candidate);
  const keys = Object.keys(input);
  return keys.length ? keys.map((key) =>
    key + ": " + String(input[key]).slice(0, 30)
  ).join(", ").slice(0, 100) : "";
}

function runLabel(run) {
  return run && run.model ? String(run.model) : "";
}

function runKey(run) {
  return run && (run.model || run.effort) ?
    String(run.model || "") + "\n" + String(run.effort || "") : "";
}

const EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max"];

function normalizedEffort(value) {
  const effort = String(value || "").toLowerCase().replace(/[_\s-]+/g, "");
  if (effort === "low" || effort === "minimal") return "low";
  if (effort === "medium" || effort === "med") return "medium";
  if (effort === "high") return "high";
  if (effort === "xhigh" || effort === "extrahigh" || effort === "extra") return "xhigh";
  if (effort === "max" || effort === "maximum") return "max";
  return "";
}

function effortScale(effort) {
  const values = Array.isArray(effort) ? effort : [effort];
  const levels = new Set(values.map(normalizedEffort).filter(Boolean));
  if (!levels.size) return null;
  const wrap = document.createElement("span");
  wrap.className = "effortscale";
  wrap.title = "reasoning depth: faster to smarter";
  const faster = document.createElement("span");
  faster.className = "endpoint";
  faster.textContent = "Faster";
  const rail = document.createElement("span");
  rail.className = "rail";
  for (const level of EFFORT_LEVELS) {
    const dot = document.createElement("span");
    dot.className = "dot" + (levels.has(level) ? " on" : "");
    dot.title = level;
    rail.appendChild(dot);
  }
  const smarter = document.createElement("span");
  smarter.className = "endpoint";
  smarter.textContent = "Smarter";
  wrap.append(faster, rail, smarter);
  return wrap;
}

function preBlock(text, className) {
  const full = String(text === undefined || text === null ? "" : text);
  const wrap = document.createElement("div");
  const pre = document.createElement("pre");
  if (className) pre.className = className;
  if (full.length <= PRE_CAP) {
    pre.textContent = full;
    wrap.appendChild(pre);
    return wrap;
  }
  pre.textContent = full.slice(0, PRE_CAP);
  const button = document.createElement("button");
  button.className = "morebtn";
  button.textContent = "show all " + fmtBytes(new Blob([full]).size);
  button.addEventListener("click", () => {
    pre.textContent = full;
    button.remove();
  });
  wrap.append(pre, button);
  return wrap;
}

function highlightMatches(root, query) {
  if (!query) return;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) {
    const node = walker.currentNode;
    const parent = node.parentElement;
    if (!parent || parent.closest(".rawbtn, pre.rawview") ||
      ["SCRIPT", "STYLE", "BUTTON"].includes(parent.tagName)) continue;
    if (node.nodeValue.toLowerCase().includes(query)) nodes.push(node);
  }
  for (const node of nodes) {
    const text = node.nodeValue;
    const lower = text.toLowerCase();
    const fragment = document.createDocumentFragment();
    let cursor = 0;
    let match;
    while ((match = lower.indexOf(query, cursor)) !== -1) {
      if (match > cursor) fragment.appendChild(document.createTextNode(text.slice(cursor, match)));
      const mark = document.createElement("mark");
      mark.textContent = text.slice(match, match + query.length);
      fragment.appendChild(mark);
      cursor = match + query.length;
    }
    if (cursor < text.length) fragment.appendChild(document.createTextNode(text.slice(cursor)));
    node.replaceWith(fragment);
  }
}

function turnMark(turnNo, item, searchContext) {
  const mark = document.createElement("div");
  mark.className = "turnmark";
  const prefix = turnNo ? "turn " + turnNo : "preamble";
  mark.textContent = prefix +
    (item.ts ? " · " + fmtTime(item.ts) : "") +
    (searchContext ? " · match" : "");
  if (item.run && item.run.effort) {
    const scale = effortScale(item.run.effort);
    if (scale) mark.appendChild(scale);
  }
  return mark;
}

function rawButton(item, element) {
  if (!Array.isArray(item.raw) || !item.raw.length) return;
  const button = document.createElement("button");
  button.className = "rawbtn";
  button.textContent = "{raw}";
  button.title = "Show the source records behind this block";
  button.setAttribute("aria-expanded", "false");
  button.addEventListener("click", () => {
    const existing = element.querySelector("pre.rawview");
    if (existing) {
      existing.remove();
      button.setAttribute("aria-expanded", "false");
      return;
    }
    const pre = document.createElement("pre");
    pre.className = "rawview";
    try {
      pre.textContent = item.raw.map((value) => JSON.stringify(value, null, 1))
        .join("\n---\n").slice(0, 100000);
    } catch (error) {
      pre.textContent = "Raw record could not be serialized: " + error.message;
    }
    element.appendChild(pre);
    button.setAttribute("aria-expanded", "true");
  });
  element.appendChild(button);
}

function renderItem(item, index, turnNo, runChanged, forceTurnMark) {
  const fragment = document.createDocumentFragment();
  if ((item.kind === "user" && !item.side) || forceTurnMark) {
    fragment.appendChild(turnMark(turnNo, item, forceTurnMark));
  }
  const label = runLabel(item.run);
  if (runChanged && label) {
    const mark = document.createElement("div");
    mark.className = "runmark";
    mark.append("setting ");
    const bold = document.createElement("b");
    bold.textContent = label;
    mark.appendChild(bold);
    fragment.appendChild(mark);
  }

  const element = document.createElement("div");
  element.className = "item " + item.kind;
  element.dataset.idx = index;

  if (item.kind === "user" || item.kind === "assistant") {
    const speaker = document.createElement("div");
    speaker.className = "speaker";
    speaker.append(item.kind === "user" ? "you" : "the machine");
    if (item.side) {
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = "sidechain";
      speaker.appendChild(badge);
    }
    if (item.kind === "assistant" && label) {
      const badge = document.createElement("span");
      badge.className = "badge runbadge";
      badge.textContent = label;
      speaker.appendChild(badge);
    }
    const prose = document.createElement("div");
    prose.className = "prose";
    prose.innerHTML = md(item.text);
    element.append(speaker, prose);
  } else if (item.kind === "thinking") {
    const details = document.createElement("details");
    details.className = "think";
    const summary = document.createElement("summary");
    summary.textContent = "✳ thinking · " + String(item.text || "").length + " chars";
    const prose = document.createElement("div");
    prose.className = "prose";
    prose.innerHTML = md(item.text);
    details.append(summary, prose);
    element.appendChild(details);
  } else if (item.kind === "tool") {
    const details = document.createElement("details");
    details.className = "tool";
    const summary = document.createElement("summary");
    const status = item.output === null || item.output === undefined ?
      ["none", "·"] : item.isError ? ["err", "✗"] : ["ok", "✓"];
    const name = document.createElement("span");
    name.className = "tname";
    name.textContent = "⌘ " + (item.name || "tool");
    const argument = document.createElement("span");
    argument.className = "targ";
    argument.textContent = toolArgSummary(item.input).slice(0, 160);
    const result = document.createElement("span");
    result.className = "tstat " + status[0];
    result.textContent = status[1];
    summary.append(name, argument, result);
    const body = document.createElement("div");
    body.className = "tbody";
    const inputLabel = document.createElement("div");
    inputLabel.className = "tlabel";
    inputLabel.textContent = "input";
    const inputText = typeof item.input === "string" ?
      item.input : JSON.stringify(item.input === undefined ? {} : item.input, null, 2);
    const outputLabel = document.createElement("div");
    outputLabel.className = "tlabel";
    outputLabel.textContent = item.output === null || item.output === undefined ?
      "no result recorded" : item.isError ? "result · error" : "result";
    body.append(inputLabel, preBlock(inputText || "(none)"), outputLabel);
    if (item.output !== null && item.output !== undefined) {
      body.appendChild(preBlock(item.output || "(empty)", item.isError ? "errout" : ""));
    }
    details.append(summary, body);
    element.appendChild(details);
  } else if (item.kind === "command") {
    const command = document.createElement("span");
    command.className = "cmd";
    command.textContent = "» " + (item.text || "");
    element.appendChild(command);
  } else {
    element.className += " info";
    if (String(item.text || "").length > 120) {
      const details = document.createElement("details");
      details.className = "info";
      const summary = document.createElement("summary");
      const infoLabel = document.createElement("span");
      infoLabel.className = "ilabel";
      infoLabel.textContent = "◦ " + (item.label || "note");
      summary.append(infoLabel, " · " + String(item.text || "").length + " chars");
      const pre = document.createElement("pre");
      pre.textContent = item.text || "";
      details.append(summary, pre);
      element.appendChild(details);
    } else {
      const infoLabel = document.createElement("span");
      infoLabel.className = "ilabel";
      infoLabel.textContent = "◦ " + (item.label || "note");
      element.append(infoLabel, " " + (item.text || ""));
    }
  }

  rawButton(item, element);
  highlightMatches(element, state.query);
  fragment.appendChild(element);
  return fragment;
}

function prepareContexts(items) {
  state.turnByIndex = [];
  state.runChangedByIndex = [];
  let turn = 0;
  let previousRun = "";
  for (let index = 0; index < items.length; index++) {
    const item = items[index];
    if (item.kind === "user" && !item.side) turn++;
    state.turnByIndex[index] = turn;
    const key = runKey(item.run);
    state.runChangedByIndex[index] = !!key && key !== previousRun;
    if (key) previousRun = key;
  }
}

function finishTranscript(container) {
  const sentinel = $("sentinel");
  if (sentinel) sentinel.remove();
  if (state.observer) state.observer.disconnect();
  const end = document.createElement("div");
  end.className = "endmark";
  end.textContent = state.renderQueue.length ? "· fin ·" : "no matching passages";
  container.appendChild(end);
}

function renderChunk() {
  if (!state.parsed) return;
  const container = $("transcript");
  const end = Math.min(state.renderedUpto + CHUNK, state.renderQueue.length);
  const fragment = document.createDocumentFragment();
  for (let position = state.renderedUpto; position < end; position++) {
    const index = state.renderQueue[position];
    const previousIndex = position > 0 ? state.renderQueue[position - 1] : -1;
    const forceTurn = !!state.query &&
      (position === 0 || state.turnByIndex[index] !== state.turnByIndex[previousIndex]);
    fragment.appendChild(renderItem(
      state.parsed.items[index],
      index,
      state.turnByIndex[index],
      state.runChangedByIndex[index],
      forceTurn
    ));
  }
  state.renderedUpto = end;
  const sentinel = $("sentinel");
  if (sentinel) container.insertBefore(fragment, sentinel);
  if (end >= state.renderQueue.length) finishTranscript(container);
}

function resetTranscript(query, scrollToTop) {
  if (!state.parsed) return;
  state.query = query.length >= 2 ? query.toLowerCase() : "";
  state.renderQueue = state.parsed.items
    .map((item, index) => ({ item, index }))
    .filter(({ item }) => !state.query ||
      String(item.search || Core.itemSearchText(item)).includes(state.query))
    .map(({ index }) => index);
  state.renderedUpto = 0;
  const container = $("transcript");
  container.innerHTML = '<div class="sentinel" id="sentinel"></div>';
  if (state.observer) state.observer.disconnect();
  state.observer = new IntersectionObserver((entries) => {
    if (entries.some((entry) => entry.isIntersecting)) renderChunk();
  }, { root: $("scrollpane"), rootMargin: "1200px" });
  state.observer.observe($("sentinel"));
  renderChunk();
  if (scrollToTop) $("scrollpane").scrollTop = 0;
  $("searchStatus").textContent = state.query ?
    state.renderQueue.length + " of " + state.parsed.items.length + " passages" :
    state.parsed.items.length + " passages";
  setLive($("searchStatus").textContent);
}

function diagnosticDetails(meta) {
  const details = $("diagnostics");
  const list = $("diagnosticList");
  const diag = meta.diagnostics || {};
  const unsupported = Object.entries(diag.unsupported || {});
  const issueCount = (diag.malformedLines || 0) + (diag.orphanCalls || 0) +
    (diag.orphanResults || 0) +
    unsupported.reduce((sum, entry) => sum + entry[1], 0) +
    (diag.warnings || []).length;
  const rows = [];
  if (diag.malformedLines) rows.push(diag.malformedLines + " malformed source records");
  if (diag.orphanCalls) rows.push(diag.orphanCalls + " tool calls without results");
  if (diag.orphanResults) rows.push(diag.orphanResults + " tool results without calls");
  for (const [type, count] of unsupported) {
    rows.push(count + " unsupported " + type + " records");
  }
  for (const warning of diag.warnings || []) rows.push(warning);
  if (diag.ignoredRecords) rows.push(diag.ignoredRecords + " routine bookkeeping records hidden");
  details.hidden = !rows.length;
  details.classList.toggle("has-errors", issueCount > 0);
  details.querySelector("summary").textContent = issueCount ?
    issueCount + " parse notes" :
    (diag.ignoredRecords || 0) + " bookkeeping records hidden";
  list.innerHTML = "";
  for (const row of rows) {
    const item = document.createElement("li");
    item.textContent = row;
    list.appendChild(item);
  }
}

function renderHeader() {
  const { meta, items } = state.parsed;
  $("mainhead").hidden = false;
  $("stitle").textContent = meta.title ||
    (state.current && state.current.title) || "untitled session";
  document.title = $("stitle").textContent + " · Fables";
  const strip = $("metastrip");
  strip.innerHTML = "";

  const source = document.createElement("span");
  source.className = "src " + sourceClass(meta.source);
  source.textContent = sourceLabel(meta.source);
  strip.appendChild(source);

  const models = [...(meta.models || [])];
  if (models.length) {
    const value = document.createElement("span");
    value.className = "kv";
    value.append((models.length === 1 ? "model " : "models "));
    const bold = document.createElement("b");
    bold.textContent = models.join(", ");
    value.appendChild(bold);
    strip.appendChild(value);
  }

  const efforts = [...(meta.efforts || [])].filter(Boolean);
  if (efforts.length) {
    const value = document.createElement("span");
    value.className = "kv";
    value.append("reasoning ");
    const scale = effortScale(efforts);
    if (scale) value.appendChild(scale);
    strip.appendChild(value);
  }

  if (meta.cwd) {
    const value = document.createElement("span");
    value.className = "kv";
    value.append("cwd ");
    const bold = document.createElement("b");
    bold.textContent = String(meta.cwd).replace(/^\/Users\/[^/]+/, "~");
    value.appendChild(bold);
    strip.appendChild(value);
  }
  if (meta.branch && meta.branch !== "HEAD") {
    const value = document.createElement("span");
    value.className = "kv";
    value.append("branch ");
    const bold = document.createElement("b");
    bold.textContent = meta.branch;
    value.appendChild(bold);
    strip.appendChild(value);
  }
  if (meta.start) {
    const value = document.createElement("span");
    value.className = "kv";
    value.textContent = fmtWhen(meta.start) +
      (meta.end && meta.end !== meta.start ? " → " + fmtWhen(meta.end) : "");
    strip.appendChild(value);
  }
  const tokens = meta.tokens || {};
  if ((tokens.input || 0) + (tokens.output || 0) + (tokens.cacheRead || 0) > 0) {
    const value = document.createElement("span");
    value.className = "kv";
    value.append("tokens ");
    const bold = document.createElement("b");
    bold.textContent = fmtTokens(tokens.input) + " in · " + fmtTokens(tokens.output) + " out";
    value.appendChild(bold);
    if (tokens.reasoning) value.append(" · " + fmtTokens(tokens.reasoning) + " thinking");
    if (tokens.cacheRead) value.append(" · " + fmtTokens(tokens.cacheRead) + " cached");
    strip.appendChild(value);
  }
  const counts = document.createElement("span");
  counts.className = "kv";
  const turns = items.filter((item) => item.kind === "user" && !item.side).length;
  const tools = items.filter((item) => item.kind === "tool").length;
  counts.innerHTML = "<b>" + turns + "</b> turns · <b>" + tools + "</b> tool calls";
  strip.appendChild(counts);
  diagnosticDetails(meta);
}

function normalizeParsed(parsed) {
  parsed.meta.models = parsed.meta.models instanceof Set ?
    parsed.meta.models : new Set(parsed.meta.models || []);
  parsed.meta.efforts = parsed.meta.efforts instanceof Set ?
    parsed.meta.efforts : new Set(parsed.meta.efforts || []);
  for (const item of parsed.items || []) {
    if (!item.search) item.search = Core.itemSearchText(item);
  }
  return parsed;
}

function renderSession() {
  prepareContexts(state.parsed.items);
  renderHeader();
  $("insearch").value = "";
  resetTranscript("", true);
  $("shareBtn").disabled = false;
}

let parseWorker = null;
let parseRequestId = 0;
const parseRequests = new Map();

function parseOnPage(request, warning) {
  try {
    const parsed = Core.parseSession(request.raw, request.format, request.source);
    if (warning) parsed.meta.diagnostics.warnings.push(warning);
    request.resolve(parsed);
  } catch (error) {
    request.reject(error);
  }
}

function resetParseWorker(warning) {
  if (parseWorker) parseWorker.terminate();
  parseWorker = null;
  const pending = [...parseRequests.values()];
  parseRequests.clear();
  for (const request of pending) parseOnPage(request, warning);
}

function sharedParseWorker() {
  if (parseWorker) return parseWorker;
  parseWorker = new Worker("/fables-worker.js");
  parseWorker.addEventListener("message", (event) => {
    const data = event.data || {};
    const request = parseRequests.get(data.id);
    if (!request) return;
    parseRequests.delete(data.id);
    if (data.parsed) request.resolve(data.parsed);
    else parseOnPage(request,
      "Background parsing failed; the session was parsed on the page.");
  });
  parseWorker.addEventListener("error", () => {
    resetParseWorker("Background parser stopped; the session was parsed on the page.");
  });
  return parseWorker;
}

function parseOffThread(raw, format, source) {
  if (document.body.classList.contains("standalone") || !window.Worker) {
    return Promise.resolve(Core.parseSession(raw, format, source));
  }
  return new Promise((resolve, reject) => {
    let worker;
    try {
      worker = sharedParseWorker();
    } catch (error) {
      resolve(Core.parseSession(raw, format, source));
      return;
    }
    const id = ++parseRequestId;
    const request = { raw, format, source, resolve, reject };
    parseRequests.set(id, request);
    try {
      worker.postMessage({ id, raw, format, source });
    } catch (error) {
      parseRequests.delete(id);
      parseOnPage(request,
        "Background parsing could not start; the session was parsed on the page.");
    }
  });
}

async function responseText(response, progress) {
  const total = Number(response.headers.get("Content-Length")) || 0;
  if (!response.body || !response.body.getReader) return response.text();
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const parts = [];
  let received = 0;
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    received += value.byteLength;
    parts.push(decoder.decode(value, { stream: true }));
    if (progress) progress(received, total);
  }
  parts.push(decoder.decode());
  return parts.join("");
}

function setRoute(id, mode) {
  const hash = id ? "#s=" + encodeURIComponent(id) : location.pathname + location.search;
  if (mode === "push") history.pushState(null, "", hash);
  else if (mode === "replace") history.replaceState(null, "", hash);
}

async function openSession(session, options) {
  const opts = { history: "push", ...(options || {}) };
  if (!session) return;
  if (state.requestController) state.requestController.abort();
  const controller = new AbortController();
  state.requestController = controller;
  const serial = ++state.requestSerial;
  $("shareBtn").disabled = true;
  setLoading("Opening " + session.title + "…");
  try {
    const embeddedArchive = state.embeddedSessions.get(session.id);
    let raw;
    let parsed;
    if (embeddedArchive) {
      raw = JSON.stringify(embeddedArchive);
      parsed = normalizeParsed(Core.parseArchive(embeddedArchive));
    } else {
      const response = await fetch("/api/session/" + session.id, {
        signal: controller.signal,
      });
      if (!response.ok) throw new Error("HTTP " + response.status);
      raw = await responseText(response, (received, total) => {
        setLoading("Reading " + fmtBytes(received) +
          (total ? " of " + fmtBytes(total) : "") + "…");
      });
      if (serial !== state.requestSerial) return;
      setLoading("Parsing " + sourceLabel(session.source) + " session…");
      parsed = normalizeParsed(await parseOffThread(
        raw,
        session.format || session.source,
        session.source
      ));
    }
    if (serial !== state.requestSerial) return;
    state.raw = raw;
    state.current = session;
    state.parsed = parsed;
    if (!parsed.meta.title) parsed.meta.title = session.title;
    if (!parsed.meta.cwd) parsed.meta.cwd = session.project || "";
    if (opts.history) setRoute(session.id, opts.history);
    renderSession();
    renderShelf();
    closeShelf();
    setLoading("");
  } catch (error) {
    if (error.name === "AbortError") return;
    setLoading("");
    setLive("Could not load session: " + error.message);
    window.alert("Couldn't load that session: " + error.message);
  }
}

function visibleSessions() {
  const query = $("finder").value.trim().toLowerCase();
  return state.sessions.filter((session) => {
    if (state.filterSrc && session.source !== state.filterSrc) return false;
    return !query || (
      String(session.title || "") + " " +
      String(session.project || "") + " " +
      sourceLabel(session.source)
    ).toLowerCase().includes(query);
  });
}

function renderShelf() {
  const shelf = $("shelf");
  shelf.innerHTML = "";
  const sessions = visibleSessions();
  for (const session of sessions) {
    const button = document.createElement("button");
    button.className = "card" +
      (state.current && state.current.id === session.id ? " on" : "");
    if (state.current && state.current.id === session.id) {
      button.setAttribute("aria-current", "page");
    }
    const title = document.createElement("div");
    title.className = "t";
    title.textContent = session.title;
    const meta = document.createElement("div");
    meta.className = "m";
    const dot = document.createElement("span");
    dot.className = "dot " + sourceClass(session.source);
    meta.appendChild(dot);
    if (session.sub) {
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = "sub";
      meta.appendChild(badge);
    }
    if (session.experimental) {
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = "experimental";
      meta.appendChild(badge);
    }
    const project = document.createElement("span");
    project.className = "proj";
    project.textContent = session.project || sourceLabel(session.source);
    const when = document.createElement("span");
    when.className = "when";
    when.textContent = fmtWhen(session.mtime) + " · " + fmtBytes(session.size);
    meta.append(project, when);
    button.append(title, meta);
    button.addEventListener("click", () => openSession(session));
    shelf.appendChild(button);
  }
  if (!sessions.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "nothing on this shelf.";
    shelf.appendChild(empty);
  }
}

function selectSource(source) {
  state.filterSrc = source;
  for (const chip of $("sourceChips").querySelectorAll(".chip")) {
    const active = chip.dataset.src === source;
    chip.classList.toggle("on", active);
    chip.setAttribute("aria-pressed", String(active));
  }
  renderShelf();
}

function renderSourceChips() {
  const counts = new Map();
  for (const session of state.sessions) {
    counts.set(session.source, (counts.get(session.source) || 0) + 1);
  }
  const sources = [...counts.keys()].sort((left, right) => {
    const leftIndex = SOURCE_ORDER.indexOf(left);
    const rightIndex = SOURCE_ORDER.indexOf(right);
    return (leftIndex === -1 ? 99 : leftIndex) - (rightIndex === -1 ? 99 : rightIndex);
  });
  const wrap = $("sourceChips");
  wrap.innerHTML = "";
  for (const source of ["", ...sources]) {
    const button = document.createElement("button");
    button.className = "chip" + (source === state.filterSrc ? " on" : "");
    button.dataset.src = source;
    button.setAttribute("aria-pressed", String(source === state.filterSrc));
    button.textContent = source ?
      sourceLabel(source).replace(/ (Code|CLI)$/, "").toLowerCase() + " " + counts.get(source) :
      "all " + state.sessions.length;
    button.addEventListener("click", () => selectSource(source));
    wrap.appendChild(button);
  }
}

function renderProviderStatus() {
  const warnings = state.providers.filter((provider) => provider.status !== "ok");
  const experimental = state.providers.filter(
    (provider) => provider.stability === "experimental" && provider.count > 0
  );
  const node = $("providerStatus");
  node.classList.toggle("warn", warnings.length > 0);
  if (warnings.length) {
    node.textContent = warnings.map((provider) =>
      sourceLabel(provider.source) + ": " + (provider.message || provider.status)
    ).join(" · ");
  } else {
    const active = state.providers.filter((provider) => provider.count > 0).length;
    node.textContent = active + " sources discovered locally" +
      (experimental.length ? " · " + experimental.length + " experimental" : "");
    node.title = experimental.map((provider) =>
      sourceLabel(provider.source) + ": " + (provider.message || "experimental format")
    ).join("\n");
  }
}

function closeShelf() {
  document.body.classList.remove("shelf-open");
  $("shelfbackdrop").hidden = true;
  $("shelftoggle").setAttribute("aria-expanded", "false");
}

function toggleShelf() {
  const open = !document.body.classList.contains("shelf-open");
  document.body.classList.toggle("shelf-open", open);
  $("shelfbackdrop").hidden = !open;
  $("shelftoggle").setAttribute("aria-expanded", String(open));
}

function showWelcome() {
  if (state.requestController) state.requestController.abort();
  state.requestSerial++;
  state.current = null;
  state.parsed = null;
  $("mainhead").hidden = true;
  $("shareBtn").disabled = true;
  $("transcript").innerHTML =
    '<div class="welcome"><div class="glyph">❦</div>' +
    "<h3>Every session is a story.</h3>" +
    "<p>Pick a chronicle from the shelf.<br>" +
    "Local coding-agent transcripts, together in one reading room.</p></div>";
  document.title = "Fables · a reader for agent chronicles";
  renderShelf();
}

function shareOptions() {
  return {
    includeThinking: $("shareThinking").checked,
    includeTools: $("shareTools").checked,
    includeInfo: $("shareInfo").checked,
    includeRaw: $("shareRaw").checked,
    redactSecrets: $("redactSecrets").checked,
    redactPaths: $("redactPaths").checked,
    redactEmails: $("redactEmails").checked,
  };
}

function shareArchive() {
  return Core.makeShareArchive(state.parsed, shareOptions());
}

function sharePreview(archive) {
  const previewItems = [];
  if (archive.fablesLibraryVersion) {
    for (const entry of archive.sessions || []) {
      previewItems.push({ kind: "session", text: entry.session.title });
      previewItems.push(...(entry.archive.items || []).slice(0, 2));
      if (previewItems.length >= 10) break;
    }
  } else {
    previewItems.push(...(archive.items || []).slice(0, 8));
  }
  const lines = previewItems.slice(0, 10).map((item) => {
    if (item.kind === "session") return "\n[session] " + item.text;
    if (item.kind === "tool") {
      return "[tool] " + item.name + " " + toolArgSummary(item.input).slice(0, 100);
    }
    return "[" + item.kind + "] " + String(item.text || "").replace(/\s+/g, " ").slice(0, 180);
  });
  if (!archive.fablesLibraryVersion && archive.items.length > 8) {
    lines.push("… " + (archive.items.length - 8) + " more passages");
  }
  $("sharePreview").textContent = lines.join("\n") || "(nothing selected)";
}

function updateShareReview() {
  if (state.exportMode === "session" && !state.parsed) return;
  if (state.exportMode === "all") {
    const stats = state.exportStats || { sessions: 0, passages: 0, sourceBytes: 0 };
    $("shareEstimate").textContent = stats.sessions + " sessions · " +
      fmtBytes(stats.sourceBytes) + " source data · processed once during download";
    $("sharePreview").textContent = state.exportPreview.join("\n") || "(nothing selected)";
    return;
  }
  const archive = shareArchive();
  const bytes = new Blob([JSON.stringify(archive)]).size;
  if (archive.fablesLibraryVersion) {
    const passages = archive.sessions.reduce(
      (total, entry) => total + entry.archive.items.length, 0
    );
    $("shareEstimate").textContent = archive.sessions.length + " sessions · " +
      passages + " passages · about " + fmtBytes(bytes) + " standalone";
  } else {
    $("shareEstimate").textContent =
      archive.items.length + " passages · about " + fmtBytes(bytes) + " standalone";
  }
  sharePreview(archive);
}

function showShareFindings(findings) {
  const parts = [];
  if (findings.secrets) parts.push(findings.secrets + " likely secrets");
  if (findings.paths) parts.push(findings.paths + " local paths");
  if (findings.emails) parts.push(findings.emails + " email addresses");
  const node = $("shareFindings");
  node.textContent = parts.length ?
    "Pattern scan found " + parts.join(", ") + ". Review the preview; detection is best-effort." :
    "Pattern scan found no obvious secrets. Detection is best-effort.";
  node.classList.toggle("warn", parts.length > 0);
}

function openShareReview() {
  if (!state.parsed) return;
  state.exportMode = "session";
  state.exportEntries = [];
  state.exportFailures = [];
  $("shareTitle").textContent = "Review standalone export";
  $("shareIntro").textContent =
    "Exports contain only the selected normalized passages. Hidden raw source " +
    "records are excluded unless explicitly enabled.";
  $("exportBtn").textContent = "download standalone html";
  showShareFindings(Core.inspectSensitive(state.parsed));
  updateShareReview();
  $("shareDialog").showModal();
}

async function loadSessionForExport(session) {
  if (state.current && state.current.id === session.id && state.parsed) {
    return { session, parsed: state.parsed };
  }
  const response = await fetch("/api/session/" + session.id);
  if (!response.ok) throw new Error("HTTP " + response.status);
  const raw = await response.text();
  const parsed = normalizeParsed(await parseOffThread(
    raw, session.format || session.source, session.source
  ));
  if (!parsed.meta.title) parsed.meta.title = session.title;
  if (!parsed.meta.cwd) parsed.meta.cwd = session.project || "";
  return { session, parsed };
}

function openExportAllReview() {
  if (!state.sessions.length || document.body.classList.contains("standalone")) return;
  state.exportMode = "all";
  state.exportEntries = state.sessions.map((session) => ({ session }));
  state.exportFailures = [];
  state.exportStats = {
    sessions: state.sessions.length,
    passages: 0,
    sourceBytes: state.sessions.reduce(
      (total, session) => total + (Number(session.size) || 0), 0
    ),
  };
  state.exportPreview = state.sessions.slice(0, 10).map(
    (session) => "[session] " + session.title
  );
  $("shareTitle").textContent = "Review all-sessions export";
  $("shareIntro").textContent =
    "This creates one ZIP containing a manifest and one JSONL file per discovered session. " +
    "After you confirm, each session is loaded, normalized, redacted, and written exactly once.";
  $("exportBtn").textContent = "download all as zip";
  const findings = $("shareFindings");
  findings.textContent =
    "Sensitive-pattern scanning and the selected redactions run session by session during export.";
  findings.classList.remove("warn");
  updateShareReview();
  $("shareDialog").showModal();
}

async function fetchAsset(path) {
  const response = await fetch(path);
  if (!response.ok) throw new Error(path + " returned HTTP " + response.status);
  return response.text();
}

function safeScriptSource(source) {
  return String(source).replace(/<\/script/gi, "<\\/script");
}

function standaloneTemplate(page, css, core, app) {
  const csp = '<meta http-equiv="Content-Security-Policy" content="' +
    "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline' blob:; " +
    "worker-src blob:; img-src data:; object-src 'none'; base-uri 'none'" +
    '">';
  const output = page
    .replace('<link rel="stylesheet" href="/fables.css">', "<style>" + css + "</style>")
    .replace('<script src="/fables-core.js"></script>',
      "<script>" + safeScriptSource(core) + "</scr" + "ipt>")
    .replace('<script src="/fables-app.js"></script>',
      "<script>" + safeScriptSource(app) + "</scr" + "ipt>")
    .replace("</head>", csp + "</head>");
  const bodyEnd = output.lastIndexOf("</body>");
  if (bodyEnd < 0) throw new Error("page template has no closing body tag");
  return [output.slice(0, bodyEnd), output.slice(bodyEnd)];
}

const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let index = 0; index < 256; index++) {
    let value = index;
    for (let bit = 0; bit < 8; bit++) {
      value = (value & 1) ? 0xedb88320 ^ (value >>> 1) : value >>> 1;
    }
    table[index] = value >>> 0;
  }
  return table;
})();

function crc32(bytes) {
  let value = 0xffffffff;
  for (const byte of bytes) value = CRC_TABLE[(value ^ byte) & 0xff] ^ (value >>> 8);
  return (value ^ 0xffffffff) >>> 0;
}

function zipHeader(size) {
  const bytes = new Uint8Array(size);
  return { bytes, view: new DataView(bytes.buffer) };
}

function zipDate(value) {
  const date = new Date(dateValue(value));
  const safe = Number.isNaN(date.getTime()) ? new Date() : date;
  const year = Math.max(1980, safe.getFullYear());
  return {
    time: (safe.getHours() << 11) | (safe.getMinutes() << 5) | (safe.getSeconds() >> 1),
    date: ((year - 1980) << 9) | ((safe.getMonth() + 1) << 5) | safe.getDate(),
  };
}

async function deflateRaw(bytes) {
  if (!window.CompressionStream) return { method: 0, bytes };
  try {
    const stream = new Blob([bytes]).stream().pipeThrough(new CompressionStream("deflate-raw"));
    return { method: 8, bytes: new Uint8Array(await new Response(stream).arrayBuffer()) };
  } catch (error) {
    return { method: 0, bytes };
  }
}

class ZipWriter {
  constructor(writable) {
    this.writable = writable;
    this.parts = writable ? null : [];
    this.entries = [];
    this.offset = 0;
  }

  async write(bytes) {
    if (this.offset + bytes.byteLength > 0xffffffff) {
      throw new Error("export exceeds the 4 GB ZIP limit");
    }
    if (this.writable) await this.writable.write(bytes);
    else this.parts.push(bytes);
    this.offset += bytes.byteLength;
  }

  async add(name, bytes, modified) {
    const filename = new TextEncoder().encode(name);
    const compressed = await deflateRaw(bytes);
    const stamp = zipDate(modified);
    const checksum = crc32(bytes);
    const offset = this.offset;
    const header = zipHeader(30);
    header.view.setUint32(0, 0x04034b50, true);
    header.view.setUint16(4, 20, true);
    header.view.setUint16(6, 0x0800, true);
    header.view.setUint16(8, compressed.method, true);
    header.view.setUint16(10, stamp.time, true);
    header.view.setUint16(12, stamp.date, true);
    header.view.setUint32(14, checksum, true);
    header.view.setUint32(18, compressed.bytes.byteLength, true);
    header.view.setUint32(22, bytes.byteLength, true);
    header.view.setUint16(26, filename.byteLength, true);
    await this.write(header.bytes);
    await this.write(filename);
    await this.write(compressed.bytes);
    this.entries.push({
      filename, method: compressed.method, stamp, checksum, offset,
      compressedSize: compressed.bytes.byteLength, size: bytes.byteLength,
    });
  }

  async close() {
    const centralOffset = this.offset;
    for (const entry of this.entries) {
      const header = zipHeader(46);
      header.view.setUint32(0, 0x02014b50, true);
      header.view.setUint16(4, 0x0314, true);
      header.view.setUint16(6, 20, true);
      header.view.setUint16(8, 0x0800, true);
      header.view.setUint16(10, entry.method, true);
      header.view.setUint16(12, entry.stamp.time, true);
      header.view.setUint16(14, entry.stamp.date, true);
      header.view.setUint32(16, entry.checksum, true);
      header.view.setUint32(20, entry.compressedSize, true);
      header.view.setUint32(24, entry.size, true);
      header.view.setUint16(28, entry.filename.byteLength, true);
      header.view.setUint32(42, entry.offset, true);
      await this.write(header.bytes);
      await this.write(entry.filename);
    }
    const centralSize = this.offset - centralOffset;
    const end = zipHeader(22);
    end.view.setUint32(0, 0x06054b50, true);
    end.view.setUint16(8, this.entries.length, true);
    end.view.setUint16(10, this.entries.length, true);
    end.view.setUint32(12, centralSize, true);
    end.view.setUint32(16, centralOffset, true);
    await this.write(end.bytes);
    if (this.writable) await this.writable.close();
    return this.parts ? new Blob(this.parts, { type: "application/zip" }) : null;
  }
}

function sessionJsonl(session, parsed, options) {
  const archive = Core.makeShareArchive(parsed, options);
  const metadata = {
    type: "session",
    schema: "fables.session.jsonl",
    version: 1,
    session: {
      id: session.id,
      source: session.source,
      title: archive.meta.title || session.title,
      project: archive.meta.cwd || "",
      mtime: session.mtime,
    },
    meta: archive.meta,
  };
  const lines = [JSON.stringify(metadata)];
  for (let index = 0; index < archive.items.length; index++) {
    lines.push(JSON.stringify({ type: "item", index, ...archive.items[index] }));
  }
  return new TextEncoder().encode(lines.join("\n") + "\n");
}

async function exportAllZip(button, pickerPromise) {
  const writable = pickerPromise ? await (await pickerPromise).createWritable() : null;
  const zip = new ZipWriter(writable);
  const manifest = [];
  const failures = [];
  const findings = { secrets: 0, paths: 0, emails: 0 };
  let passages = 0;
  try {
    for (let index = 0; index < state.exportEntries.length; index++) {
      const session = state.exportEntries[index].session;
      button.textContent = "writing " + (index + 1) + "/" + state.exportEntries.length;
      setLive("Writing export " + (index + 1) + " of " + state.exportEntries.length + "…");
      try {
        const loaded = await loadSessionForExport(session);
        const found = Core.inspectSensitive(loaded.parsed);
        findings.secrets += found.secrets;
        findings.paths += found.paths;
        findings.emails += found.emails;
        passages += loaded.parsed.items.length;
        const filename = "sessions/" + session.id + "-" + slug(session.title) + ".jsonl";
        const bytes = sessionJsonl(session, loaded.parsed, shareOptions());
        await zip.add(filename, bytes, session.mtime);
        manifest.push({ ...session, file: filename });
      } catch (error) {
        failures.push({ id: session.id, title: session.title, error: error.message });
      }
    }
    if (!manifest.length) throw new Error("no sessions could be written");
    const manifestBytes = new TextEncoder().encode(JSON.stringify({
      fablesExportVersion: 1,
      format: "fables.session.jsonl",
      exportedAt: new Date().toISOString(),
      sessions: manifest,
      failures,
      findings,
      passages,
    }, null, 2) + "\n");
    await zip.add("manifest.json", manifestBytes, Date.now());
    const blob = await zip.close();
    if (blob) {
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = "fables-all-sessions.zip";
      link.click();
      setTimeout(() => URL.revokeObjectURL(link.href), 5000);
    }
    state.exportFailures.push(...failures);
  } catch (error) {
    if (writable) await writable.abort().catch(() => {});
    throw error;
  }
}

async function exportStandalone() {
  const button = $("exportBtn");
  const pickerPromise = state.exportMode === "all" && window.showSaveFilePicker ?
    window.showSaveFilePicker({
      suggestedName: "fables-all-sessions.zip",
      types: [{ description: "ZIP archive", accept: { "application/zip": [".zip"] } }],
    }) : null;
  button.disabled = true;
  button.textContent = "binding…";
  try {
    if (state.exportMode === "all") {
      await exportAllZip(button, pickerPromise);
      $("shareDialog").close();
      setLive("Session archive exported.");
      return;
    }
    const assets = await Promise.all([
      fetchAsset("/"),
      fetchAsset("/fables.css"),
      fetchAsset("/fables-core.js"),
      fetchAsset("/fables-app.js"),
    ]);
    const archive = shareArchive();
    const archiveJson = JSON.stringify(archive).replace(/</g, "\\u003c");
    const [prefix, suffix] = standaloneTemplate(...assets);
    const embedded = '\n<script type="application/json" id="embedded-data">' +
      archiveJson + "</scr" + "ipt>\n";
    const output = prefix + embedded + suffix;
    const blob = new Blob([output], { type: "text/html" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = "fable-" + slug($("stitle").textContent) + ".html";
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 5000);
    $("shareDialog").close();
    setLive("Standalone HTML exported.");
  } catch (error) {
    window.alert("Export failed: " + error.message);
  } finally {
    button.disabled = false;
    button.textContent = state.exportMode === "all" ?
      "download all as zip" : "download standalone html";
  }
}

function handleSearchInput() {
  clearTimeout(state.searchTimer);
  state.searchTimer = setTimeout(() => {
    resetTranscript($("insearch").value.trim(), true);
  }, 80);
}

function activeInput() {
  const active = document.activeElement;
  return active && (
    active.tagName === "INPUT" ||
    active.tagName === "TEXTAREA" ||
    active.isContentEditable
  );
}

function navigateShelf(offset) {
  const sessions = visibleSessions();
  if (!sessions.length) return;
  const index = state.current ?
    sessions.findIndex((session) => session.id === state.current.id) : -1;
  const next = sessions[(index + offset + sessions.length) % sessions.length];
  if (next && (!state.current || next.id !== state.current.id)) openSession(next);
}

function handleKeys(event) {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    $("finder").focus();
    $("finder").select();
    return;
  }
  if (event.key === "Escape") {
    if ($("shareDialog").open) $("shareDialog").close();
    closeShelf();
    return;
  }
  if (activeInput()) return;
  if (event.key === "/") {
    event.preventDefault();
    const input = state.current ? $("insearch") : $("finder");
    input.focus();
    input.select();
  } else if (event.key.toLowerCase() === "j") {
    event.preventDefault();
    navigateShelf(1);
  } else if (event.key.toLowerCase() === "k") {
    event.preventDefault();
    navigateShelf(-1);
  }
}

function routeSession() {
  const match = location.hash.match(/^#s=([a-f0-9]{12})$/);
  if (!match) return null;
  return state.sessions.find((session) => session.id === match[1]) || null;
}

async function boot() {
  if (!Core) {
    document.body.textContent = "Fables core failed to load.";
    return;
  }
  const embedded = $("embedded-data");
  if (embedded) {
    document.body.classList.add("standalone");
    const library = Core.parseLibraryArchive(embedded.textContent);
    if (library) {
      document.body.classList.add("library-standalone");
      state.sessions = library.sessions.map((entry) => entry.session);
      state.embeddedSessions = new Map(
        library.sessions.map((entry) => [entry.session.id, entry.archive])
      );
      const counts = new Map();
      for (const session of state.sessions) {
        counts.set(session.source, (counts.get(session.source) || 0) + 1);
      }
      state.providers = [...counts].map(([source, count]) => ({
        source, count, status: "ok",
      }));
      renderSourceChips();
      renderProviderStatus();
      renderShelf();
      const requested = routeSession();
      if (requested) await openSession(requested, { history: null });
      else showWelcome();
      return;
    }
    state.raw = embedded.textContent;
    state.current = {
      id: "embedded",
      title: "shared session",
      project: "",
      source: "archive",
      format: "archive",
    };
    state.parsed = normalizeParsed(Core.parseArchive(state.raw));
    renderSession();
    return;
  }

  setLoading("Discovering local sessions…");
  try {
    const response = await fetch("/api/sessions");
    if (!response.ok) throw new Error("HTTP " + response.status);
    const data = await response.json();
    state.sessions = Array.isArray(data.sessions) ? data.sessions : [];
    state.providers = Array.isArray(data.providers) ? data.providers : [];
  } catch (error) {
    $("shelf").innerHTML =
      "<div class='empty'>couldn't reach serve.py — is it running?</div>";
    setLoading("");
    setLive("Could not discover sessions: " + error.message);
    return;
  }
  renderSourceChips();
  renderProviderStatus();
  renderShelf();
  $("exportAllBtn").disabled = !state.sessions.length;
  setLoading("");
  const requested = routeSession();
  if (requested) await openSession(requested, { history: null });
}

$("finder").addEventListener("input", renderShelf);
$("insearch").addEventListener("input", handleSearchInput);
$("shareBtn").addEventListener("click", openShareReview);
$("exportAllBtn").addEventListener("click", openExportAllReview);
$("shelftoggle").addEventListener("click", toggleShelf);
$("shelfbackdrop").addEventListener("click", closeShelf);
$("exportBtn").addEventListener("click", exportStandalone);
$("cancelShareBtn").addEventListener("click", () => $("shareDialog").close());
$("shareDialog").addEventListener("click", (event) => {
  if (event.target === $("shareDialog")) $("shareDialog").close();
});
for (const checkbox of $("shareDialog").querySelectorAll('input[type="checkbox"]')) {
  checkbox.addEventListener("change", updateShareReview);
}
window.addEventListener("keydown", handleKeys);
window.addEventListener("hashchange", () => {
  const session = routeSession();
  if (session && (!state.current || session.id !== state.current.id)) {
    openSession(session, { history: null });
  } else if (!session) {
    showWelcome();
  }
});

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot, { once: true });
} else {
  boot();
}
