/* AI Collaborator — web client. No framework, no build step. */
"use strict";

const S = {
  config: null,
  sessions: [],
  view: null,          // {session, features, messages, tests, params}
  route: "new",        // new | session | lab
  busy: false,
  picked: new Set(),   // thread ids picked on the map
  bubbles: new Map(),  // id -> {data, el}
  lastBubbleAt: 0,
  lastCtx: "",
  lastActivity: Date.now(),
  idleFired: false,
  bubbleBusy: false,
  stream: null,        // screen-share MediaStream
  video: null,
  notesTimer: null,
  arena: null,
};

// ---------------------------------------------------------------- helpers
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "html") el.innerHTML = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key === "value") el.value = value;
    else if (key === "checked") el.checked = !!value;
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return el;
}

/* replaceChildren that, like h(), accepts nested arrays and skips null / false. */
function fill(el, ...children) {
  el.replaceChildren(...children.flat().filter((c) => c !== null && c !== undefined && c !== false));
}

function esc(text) {
  return String(text).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function inline(text) {
  return text
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
}

/* A deliberately small Markdown renderer: code blocks, headings, lists, paragraphs. */
function md(source) {
  const lines = esc(source || "").split("\n");
  const out = [];
  let list = null, para = [], code = null;
  const flushPara = () => { if (para.length) { out.push("<p>" + inline(para.join("<br>")) + "</p>"); para = []; } };
  const flushList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  for (const line of lines) {
    if (line.trim().startsWith("```")) {
      if (code === null) { flushPara(); flushList(); code = []; }
      else { out.push("<pre><code>" + code.join("\n") + "</code></pre>"); code = null; }
      continue;
    }
    if (code !== null) { code.push(line); continue; }
    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    const bullet = line.match(/^\s*[-*•]\s+(.*)$/);
    const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (heading) {
      flushPara(); flushList();
      out.push(`<h4>${inline(heading[2])}</h4>`);
    } else if (bullet || numbered) {
      flushPara();
      const kind = bullet ? "ul" : "ol";
      if (list !== kind) { flushList(); out.push(`<${kind}>`); list = kind; }
      out.push("<li>" + inline((bullet || numbered)[1]) + "</li>");
    } else if (!line.trim()) {
      flushPara(); flushList();
    } else {
      flushList();
      para.push(line);
    }
  }
  if (code !== null) out.push("<pre><code>" + code.join("\n") + "</code></pre>");
  flushPara(); flushList();
  return out.join("");
}

async function api(method, path, body) {
  const response = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = {};
  try { data = await response.json(); } catch (e) { /* empty body */ }
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}

function toast(message, isError) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.className = isError ? "error" : "";
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.add("hidden"), isError ? 7000 : 2500);
}

function fail(error) {
  console.error(error);
  toast(t("error") + (error.message || error), true);
}

function $(selector) { return document.querySelector(selector); }

function state() { return S.view.session.state; }

function download(name, text, type) {
  const url = URL.createObjectURL(new Blob([text], { type: type || "text/plain" }));
  const a = h("a", { href: url, download: name });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function likertInput(name, keys) {
  return h("div", { class: "likert-set" }, keys.map((key) =>
    h("div", { class: "likert" },
      h("div", { class: "likert-q" }, t("q_" + key)),
      h("div", { class: "likert-scale" },
        h("span", { class: "muted tiny" }, t("likert_low")),
        [1, 2, 3, 4, 5].map((n) => h("label", {},
          h("input", { type: "radio", name: `${name}-${key}`, value: String(n) }), h("span", {}, n))),
        h("span", { class: "muted tiny" }, t("likert_high"))))));
}

function readLikert(root, name, keys) {
  const out = {};
  for (const key of keys) {
    const picked = root.querySelector(`input[name="${name}-${key}"]:checked`);
    if (picked) out[key] = Number(picked.value);
  }
  return out;
}

// ---------------------------------------------------------------- sidebar
function renderSidebar() {
  document.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
  $("#btn-lang").textContent = LANG === "zh" ? "EN" : "中文";
  document.documentElement.lang = LANG;
  if (S.config) {
    $("#provider").textContent = "⚙ " + (S.config.provider === "mock" ? t("mock_note") : S.config.model);
    $("#provider").classList.toggle("warn", S.config.provider === "mock");
  }
  const list = $("#session-list");
  list.replaceChildren();
  if (!S.sessions.length) list.append(h("p", { class: "muted pad" }, t("no_sessions")));
  for (const session of S.sessions) {
    const active = S.route === "session" && S.view && S.view.session.id === session.id;
    list.append(h("a", {
      class: "session-item" + (active ? " active" : ""), href: "#/s/" + session.id },
      h("span", { class: "session-title" }, session.title || "…"),
      session.agent !== "collaborator" ? h("span", { class: "chip tiny" }, session.agent) : null));
  }
}

async function refreshSessions() {
  S.sessions = (await api("GET", "/api/sessions")).sessions;
  renderSidebar();
}

// ------------------------------------------------------------------- new
function renderNew() {
  const main = $("#main");
  const problem = h("textarea", { id: "f-problem", rows: 5, placeholder: t("f_problem_ph") });
  const goal = h("textarea", { rows: 2, placeholder: t("f_goal_ph") });
  const ideas = h("textarea", { rows: 2, placeholder: t("f_ideas_ph") });
  const agent = h("select", {}, Object.keys(S.config.agents).map((name) =>
    h("option", { value: name }, t("agent_" + name))));
  const preAnswer = h("textarea", { rows: 3 });
  const pre = h("div", {}, likertInput("pre", S.config.likert.pre),
    h("label", { class: "field" }, h("span", {}, t("q_open")), preAnswer));
  const button = h("button", { class: "primary big" }, t("start"));
  const form = h("div", { class: "new-form" },
    h("h1", {}, t("new_title")),
    h("p", { class: "lead" }, t("new_sub")),
    h("label", { class: "field" }, h("span", {}, t("f_problem")), problem),
    h("div", { class: "examples" }, h("span", { class: "muted" }, t("examples")),
      (EXAMPLES[LANG] || EXAMPLES.zh).map((text) =>
        h("button", { class: "chip", onclick: () => { problem.value = text; problem.focus(); } }, text))),
    h("div", { class: "two" },
      h("label", { class: "field" }, h("span", {}, t("f_goal")), goal),
      h("label", { class: "field" }, h("span", {}, t("f_ideas")), ideas)),
    h("details", { class: "study" },
      h("summary", {}, t("study")),
      h("label", { class: "field" }, h("span", {}, t("f_agent")), agent),
      h("h4", {}, t("pretest")),
      h("p", { class: "muted" }, t("pretest_hint")),
      pre),
    button);
  button.addEventListener("click", async () => {
    if (!problem.value.trim()) { toast(t("need_problem"), true); problem.focus(); return; }
    const pretest = { likert: readLikert(pre, "pre", S.config.likert.pre), answer: preAnswer.value.trim() };
    const hasPre = pretest.answer || Object.keys(pretest.likert).length;
    button.disabled = true;
    button.textContent = t("reading");
    try {
      const view = await api("POST", "/api/sessions", {
        problem: problem.value, goal: goal.value, ideas: ideas.value,
        agent: agent.value, pretest: hasPre ? pretest : null });
      await refreshSessions();
      S.view = view;
      location.hash = "#/s/" + view.session.id;
    } catch (error) {
      fail(error);
      button.disabled = false;
      button.textContent = t("start");
    }
  });
  main.replaceChildren(form);
  problem.focus();
}

// ----------------------------------------------------------------- frame
function renderFrame() {
  const main = $("#main");
  const frame = state().frame || {};
  const st = frame.structuredness || "ill";
  const percent = Math.round((frame.score || 0) * 100);
  const answers = h("textarea", { rows: 3, placeholder: t("clarify_ph"), value: state().clarifications || "" });
  const custom = h("input", { type: "text", placeholder: t("custom_thread_ph"), value: state().custom_thread || "" });
  const go = h("button", { class: "primary big" }, t("make_plan"));
  const reframe = h("button", { class: "ghost" }, t("reframe"));

  const threadCard = (thread) => {
    const card = h("div", { class: `thread ${thread.type}` + (S.picked.has(thread.id) ? " picked" : ""),
      tabindex: "0", role: "checkbox", "aria-checked": S.picked.has(thread.id) ? "true" : "false" },
      h("div", { class: "thread-head" },
        h("span", { class: "tag " + thread.type }, t(thread.type)),
        h("strong", {}, thread.title),
        h("span", { class: "tick" }, "✓")),
      h("p", {}, thread.summary),
      thread.first_step ? h("p", { class: "small" }, h("b", {}, t("first_step") + "： "), thread.first_step) : null,
      thread.learn ? h("p", { class: "small muted" }, h("b", {}, t("learn") + "： "), thread.learn) : null);
    const toggle = () => {
      if (S.picked.has(thread.id)) S.picked.delete(thread.id); else S.picked.add(thread.id);
      card.classList.toggle("picked", S.picked.has(thread.id));
      card.setAttribute("aria-checked", S.picked.has(thread.id) ? "true" : "false");
    };
    card.addEventListener("click", toggle);
    card.addEventListener("keydown", (e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); toggle(); } });
    return card;
  };
  const threads = frame.threads || [];
  const mains = threads.filter((x) => x.type === "main");
  const sides = threads.filter((x) => x.type !== "main");

  main.replaceChildren(h("div", { class: "frame" },
    h("h1", {}, t("frame_title")),
    h("section", { class: "panel diagnosis" },
      h("div", { class: "diag-head" },
        h("span", { class: "tag st-" + st }, t("st_" + st)),
        h("div", { class: "meter", title: t("structure_meter") },
          h("div", { class: "meter-fill st-" + st, style: `width:${Math.max(6, percent)}%` })),
        h("span", { class: "muted tiny" }, t("structure_meter") + " " + percent + "%")),
      h("p", { class: "muted" }, t("st_" + st + "_d")),
      h("p", { class: "restatement" }, frame.restatement || ""),
      h("div", { class: "two" },
        h("div", {}, h("h4", {}, t("knowns")), h("ul", {}, (frame.knowns || []).map((x) => h("li", {}, x)))),
        h("div", {}, h("h4", {}, t("unknowns")), h("ul", {}, (frame.unknowns || []).map((x) => h("li", {}, x)))))),
    h("section", { class: "panel" },
      h("h3", {}, t("clarify")),
      h("ul", {}, (frame.clarifying_questions || []).map((x) => h("li", {}, x))),
      answers,
      h("div", { class: "row end" }, reframe)),
    (frame.landscape || []).length ? h("section", { class: "panel" },
      h("h3", {}, t("landscape")), h("p", { class: "muted" }, t("landscape_d")),
      h("div", { class: "landscape" }, frame.landscape.map((item) =>
        h("div", { class: "land" }, h("strong", {}, item.topic), h("span", {}, item.why))))) : null,
    h("section", { class: "panel" },
      h("h3", {}, t("threads")), h("p", { class: "muted" }, t("threads_d")),
      mains.length ? h("div", { class: "threads" }, mains.map(threadCard)) : null,
      sides.length ? h("div", { class: "threads" }, sides.map(threadCard)) : null,
      frame.recommendation ? h("p", { class: "suggestion" },
        h("b", {}, t("suggestion") + "： "), frame.recommendation) : null,
      h("label", { class: "field" }, h("span", {}, t("custom_thread")), custom)),
    h("div", { class: "row end" }, go)));

  reframe.addEventListener("click", async () => {
    reframe.disabled = true; reframe.textContent = t("reading");
    try {
      S.view = await api("POST", `/api/sessions/${S.view.session.id}/frame`, { clarifications: answers.value });
      S.picked = new Set();
      renderSession();
    } catch (error) { fail(error); reframe.disabled = false; reframe.textContent = t("reframe"); }
  });
  go.addEventListener("click", async () => {
    if (!S.picked.size && !custom.value.trim()) { toast(t("pick_one"), true); return; }
    go.disabled = true; go.textContent = t("planning");
    try {
      // answers typed but not re-framed still count: they travel with the plan request
      S.view = await api("POST", `/api/sessions/${S.view.session.id}/plan`,
        { thread_ids: [...S.picked], custom_thread: custom.value, clarifications: answers.value });
      await refreshSessions();
      renderSession();
    } catch (error) { fail(error); go.disabled = false; go.textContent = t("make_plan"); }
  });
}

// ------------------------------------------------------------------ work
function renderWork() {
  const main = $("#main");
  const { session, features } = S.view;
  const scaffold = features.scaffold;
  const head = h("header", { class: "work-head" },
    h("h2", {}, session.title),
    session.agent !== "collaborator" ? h("span", { class: "chip" }, t("agent_" + session.agent)) : null,
    h("span", { class: "spacer" }),
    scaffold ? h("button", { class: "ghost small", onclick: remap }, t("remap")) : null,
    h("button", { class: "ghost small", onclick: exportSession }, t("export")),
    h("button", { class: "ghost small", onclick: openPostTest }, t("wrapup")),
    h("button", { class: "ghost small danger", onclick: deleteSession }, t("del")));

  const input = h("textarea", { id: "chat-input", rows: 2, placeholder: t("chat_ph") });
  const send = h("button", { class: "primary", onclick: () => sendChat() }, t("send"));
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendChat(); }
  });
  const chat = h("section", { class: "col chat" },
    h("div", { id: "messages" }),
    h("div", { class: "composer" }, input, send));

  const columns = h("div", { class: "work " + (scaffold ? "three" : "one") });
  if (scaffold) {
    columns.append(h("section", { class: "col left" },
      h("div", { id: "card", class: "panel" }),
      h("div", { id: "tasks", class: "panel" })));
  }
  columns.append(chat);
  if (scaffold) {
    const notes = h("textarea", { id: "notes", placeholder: t("notes_ph"), value: state().notes || "" });
    notes.addEventListener("input", () => {
      state().notes = notes.value;
      clearTimeout(S.notesTimer);
      S.notesTimer = setTimeout(saveNotes, 1200);
    });
    columns.append(h("section", { class: "col right" },
      h("div", { class: "panel notes" }, h("h3", {}, t("notes")), notes),
      features.bubbles ? h("div", { id: "shelf", class: "panel" }) : null,
      features.bubbles ? h("div", { id: "agent", class: "panel" }) : null));
  }
  main.replaceChildren(h("div", { class: "work-wrap" }, head, columns));
  renderMessages();
  if (scaffold) { renderCard(false); renderTasks(); }
  if (features.bubbles) { renderShelf(); renderAgent(); }
  input.focus();
}

async function saveNotes() {
  if (!S.view) return;
  try { await api("PATCH", `/api/sessions/${S.view.session.id}`, { notes: state().notes || "" }); }
  catch (error) { fail(error); }
}

function renderMessages(pending) {
  const box = $("#messages");
  if (!box) return;
  box.textContent = "";
  for (const message of S.view.messages) {
    const type = (message.meta || {}).type;
    if (type === "expand") {
      box.append(h("div", { class: "msg user expand" },
        h("span", { class: "tag" }, "💡 " + t("expanded")), h("span", {}, message.content)));
    } else {
      box.append(h("div", { class: "msg " + message.role, html: md(message.content) }));
    }
  }
  if (pending) box.append(h("div", { class: "msg assistant pending" }, t("thinking")));
  box.scrollTop = box.scrollHeight;
}

async function sendChat() {
  const input = $("#chat-input");
  const content = input.value.trim();
  if (!content || S.busy) return;
  S.busy = true;
  input.value = "";
  S.view.messages.push({ role: "user", content, meta: {} });
  renderMessages(true);
  try {
    const out = await api("POST", `/api/sessions/${S.view.session.id}/chat`, { content });
    S.view.messages.pop();
    S.view.messages.push(...out.messages);
  } catch (error) {
    S.view.messages.pop();
    input.value = content;
    fail(error);
  }
  S.busy = false;
  renderMessages();
  touch();
}

function renderCard(editing) {
  const box = $("#card");
  if (!box) return;
  const card = state().card || { goal: "", givens: [], constraints: [], success_criteria: [] };
  const sections = [["givens", "givens"], ["constraints", "constraints"], ["success_criteria", "criteria"]];
  if (!editing) {
    fill(box,
      h("div", { class: "row" }, h("h3", {}, t("card")), h("span", { class: "spacer" }),
        h("button", { class: "ghost small", onclick: () => renderCard(true) }, t("edit"))),
      h("p", { class: "goal" }, h("b", {}, t("goal") + "： "), card.goal || "—"),
      sections.map(([key, label]) => (card[key] || []).length ? h("div", {},
        h("h4", {}, t(label)), h("ul", {}, card[key].map((x) => h("li", {}, x)))) : null));
    return;
  }
  const goal = h("textarea", { rows: 2, value: card.goal || "" });
  const areas = {};
  fill(box,
    h("h3", {}, t("card")),
    h("label", { class: "field" }, h("span", {}, t("goal")), goal),
    sections.map(([key, label]) => {
      areas[key] = h("textarea", { rows: 3, value: (card[key] || []).join("\n") });
      return h("label", { class: "field" },
        h("span", {}, t(label) + " · ", h("i", { class: "muted" }, t("one_per_line"))), areas[key]);
    }),
    h("div", { class: "row end" },
      h("button", { class: "ghost small", onclick: () => renderCard(false) }, t("cancel")),
      h("button", { class: "primary small", onclick: async () => {
        const next = { goal: goal.value.trim() };
        for (const key of Object.keys(areas)) {
          next[key] = areas[key].value.split("\n").map((x) => x.trim()).filter(Boolean);
        }
        try {
          const view = await api("PATCH", `/api/sessions/${S.view.session.id}`, { card: next });
          state().card = view.session.state.card;
          renderCard(false);
          touch();
        } catch (error) { fail(error); }
      } }, t("save"))));
}

function renderTasks() {
  const box = $("#tasks");
  if (!box) return;
  const tasks = state().tasks || [];
  const save = async () => {
    try { await api("PATCH", `/api/sessions/${S.view.session.id}`, { tasks: state().tasks }); }
    catch (error) { fail(error); }
    touch();
  };
  const add = h("input", { type: "text", placeholder: t("add_task") });
  add.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || e.isComposing || !add.value.trim()) return;
    state().tasks = [...tasks, { id: "u" + Date.now().toString(36), title: add.value.trim(), detail: "", done: false }];
    renderTasks();
    save();
  });
  const done = tasks.filter((x) => x.done).length;
  fill(box,
    h("div", { class: "row" }, h("h3", {}, t("tasks")), h("span", { class: "spacer" }),
      h("span", { class: "muted tiny" }, `${done}/${tasks.length}`)),
    h("ul", { class: "tasks" }, tasks.map((task) => h("li", { class: task.done ? "done" : "" },
      h("label", {},
        h("input", { type: "checkbox", checked: task.done, onchange: (e) => {
          task.done = e.target.checked; renderTasks(); save(); } }),
        h("span", {}, h("span", { class: "task-title" }, task.title),
          task.detail ? h("small", {}, task.detail) : null)),
      h("button", { class: "x", title: t("del"), onclick: () => {
        state().tasks = tasks.filter((x) => x !== task); renderTasks(); save(); } }, "×")))),
    add);
}

function renderShelf() {
  const box = $("#shelf");
  if (!box) return;
  const shelf = state().shelf || [];
  fill(box,
    h("h3", {}, t("shelf")),
    shelf.length ? h("ul", { class: "shelf" }, shelf.map((item) => h("li", {},
      h("span", { class: "dot kind-" + item.kind }), h("span", {}, item.text),
      h("button", { class: "x", onclick: async () => {
        state().shelf = shelf.filter((x) => x !== item);
        renderShelf();
        try { await api("PATCH", `/api/sessions/${S.view.session.id}`, { shelf: state().shelf }); }
        catch (error) { fail(error); }
      } }, "×")))) : h("p", { class: "muted small" }, t("shelf_empty")));
}

async function patchAdapt(change) {
  try {
    const view = await api("PATCH", `/api/sessions/${S.view.session.id}`, { adapt: change });
    state().adapt = view.session.state.adapt;
    S.view.params = view.params;
    renderAgent();
  } catch (error) { fail(error); }
}

function renderAgent() {
  const box = $("#agent");
  if (!box) return;
  const a = state().adapt;
  const slider = h("input", { type: "range", min: "0", max: "100", value: String(Math.round(a.agency * 100)) });
  slider.addEventListener("change", () => patchAdapt({ agency: Number(slider.value) / 100 }));
  const share = h("button", { class: "ghost small", onclick: toggleShare },
    S.stream ? t("stop_share") : t("share_screen"));
  fill(box,
    h("div", { class: "row" }, h("h3", {}, t("agent_panel")), h("span", { class: "spacer" }),
      h("button", { class: "ghost small", onclick: () => patchAdapt({ reset: true }) }, t("reset"))),
    h("div", { class: "agency" },
      h("span", { class: "tiny muted" }, t("agency_low")), slider,
      h("span", { class: "tiny muted" }, t("agency_high"))),
    h("div", { class: "tiny muted center" }, `${t("agency")} ${Math.round(a.agency * 100)}%`),
    h("label", { class: "check" }, h("input", { type: "checkbox", checked: a.locked,
      onchange: (e) => patchAdapt({ locked: e.target.checked }) }), t("lock")),
    h("label", { class: "check" }, h("input", { type: "checkbox", checked: a.paused,
      onchange: (e) => patchAdapt({ paused: e.target.checked }) }), t("pause")),
    h("h4", {}, t("prefs")),
    h("div", { class: "prefs" }, S.config.kinds.map((kind) => h("div", { class: "pref" },
      h("span", { class: "pref-name" }, t("k_" + kind)),
      h("div", { class: "bar" }, h("div", { class: "bar-fill kind-" + kind,
        style: `width:${Math.round((a.prefs[kind] || 0) * 100)}%` }))))),
    h("p", { class: "tiny muted" }, t("prefs_d")),
    h("div", { class: "row" },
      h("button", { class: "ghost small", onclick: () => requestBubbles("manual") }, t("bubble_now")),
      share),
    h("p", { class: "tiny muted" }, t("share_hint")),
    S.stream ? h("p", { class: "tiny " + (canNotify() ? "muted" : "warn-text") },
      t(canNotify() ? "share_notify_on" : "share_notify_off")) : null);
}

async function remap() {
  if (!confirm(t("remap_confirm"))) return;
  try {
    const frame = state().frame || {};
    if ((frame.threads || []).length) {
      S.view.session.phase = "frame";   // show the existing map again (local only)
    } else {
      S.view = await api("POST", `/api/sessions/${S.view.session.id}/frame`, { force_map: true });
    }
    S.picked = new Set(state().chosen || []);
    renderSession();
  } catch (error) { fail(error); }
}

async function exportSession() {
  try {
    const out = await api("GET", `/api/sessions/${S.view.session.id}/export`);
    download((S.view.session.title || "session") + ".md", out.markdown, "text/markdown");
    toast(t("copied"));
  } catch (error) { fail(error); }
}

async function deleteSession() {
  if (!confirm(t("del_confirm"))) return;
  try {
    await api("DELETE", `/api/sessions/${S.view.session.id}`);
    S.view = null;
    clearBubbles();
    await refreshSessions();
    location.hash = "#/new";
  } catch (error) { fail(error); }
}

// --------------------------------------------------------------- bubbles
function touch() {
  S.lastActivity = Date.now();
  S.idleFired = false;
}

function contextSignature() {
  const s = state();
  return [s.notes || "", S.view.messages.length, (s.tasks || []).filter((x) => x.done).length,
    (s.tasks || []).length, (s.card || {}).goal || ""].join("|");
}

function bubblesAllowed() {
  return S.route === "session" && S.view && S.view.features.bubbles && S.view.session.phase === "work";
}

/* ---- When the tab is hidden --------------------------------------------------
   A bubble only means something if the learner could have seen it, and "it faded"
   is feedback that tunes the agent. So:
   - a bubble's life is counted in JS and only runs while the tab is visible;
   - with the tab hidden nothing is generated — unless the learner is sharing their
     screen (they are working in another window; that is the point of sharing) AND
     allowed system notifications, which is then how a bubble reaches them;
   - coming back does not count as "idle", because we could not see their activity. */

function canNotify() {
  return "Notification" in window && Notification.permission === "granted";
}

async function tick() {
  if (!bubblesAllowed() || S.bubbleBusy || S.ticking) return;
  const p = S.view.params;
  if (!p.auto) return;
  const hidden = document.hidden;
  if (hidden && !(S.stream && canNotify())) return;
  const now = Date.now();
  if ((now - S.lastBubbleAt) / 1000 < p.interval) return;
  if (S.bubbles.size >= p.max_active) return;
  S.ticking = true;
  try {
    let trigger = null;
    if (contextSignature() !== S.lastCtx) trigger = "auto";
    else if (S.stream && screenDiffers(await screenSignature(), S.lastScreenSig)) trigger = "screen";
    else if (!hidden && (now - S.lastActivity) / 1000 > p.idle_trigger && !S.idleFired) {
      trigger = "idle"; S.idleFired = true;
    }
    if (trigger) requestBubbles(trigger);
  } finally { S.ticking = false; }
}

/* One frame of the shared screen. ImageCapture reads the track directly and keeps working
   when this tab is in the background; the <video> element is the fallback. */
async function screenFrame() {
  const track = S.stream && S.stream.getVideoTracks()[0];
  if (!track || track.readyState !== "live") return null;
  if ("ImageCapture" in window) {
    try {
      const bitmap = await new ImageCapture(track).grabFrame();
      return { source: bitmap, width: bitmap.width, height: bitmap.height };
    } catch (error) { /* fall through to the video element */ }
  }
  if (S.video && S.video.videoWidth) {
    if (S.video.paused) { try { await S.video.play(); } catch (error) { /* keep the last frame */ } }
    return { source: S.video, width: S.video.videoWidth, height: S.video.videoHeight };
  }
  return null;
}

function drawFrame(frame, width, height) {
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  canvas.getContext("2d").drawImage(frame.source, 0, 0, width, height);
  return canvas;
}

async function grabFrame() {
  const frame = await screenFrame();
  if (!frame || !frame.width) return null;
  const scale = Math.min(1, 1280 / frame.width);
  const canvas = drawFrame(frame, Math.round(frame.width * scale), Math.round(frame.height * scale));
  return canvas.toDataURL("image/jpeg", 0.6).split(",")[1] || null;
}

/* A 16×9 grayscale thumbnail: enough to tell "the screen changed" without a model call. */
async function screenSignature() {
  const frame = await screenFrame();
  if (!frame || !frame.width) return null;
  const pixels = drawFrame(frame, 16, 9).getContext("2d").getImageData(0, 0, 16, 9).data;
  const gray = [];
  for (let i = 0; i < pixels.length; i += 4) gray.push((pixels[i] + pixels[i + 1] + pixels[i + 2]) / 3);
  return gray;
}

function screenDiffers(a, b) {
  if (!a) return false;          // nothing to look at
  if (!b || a.length !== b.length) return true;
  let total = 0;
  for (let i = 0; i < a.length; i += 1) total += Math.abs(a[i] - b[i]);
  return total / a.length > 6;   // mean change of ~2% of the brightness range
}

function stopShare() {
  if (S.stream) S.stream.getTracks().forEach((track) => track.stop());
  S.stream = null; S.video = null; S.lastScreenSig = null;
  renderAgent();
}

async function toggleShare() {
  if (S.stream) return stopShare();
  try {
    const stream = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: false });
    const video = document.createElement("video");
    video.srcObject = stream;
    video.muted = true;
    await video.play();
    stream.getVideoTracks()[0].addEventListener("ended", stopShare);
    S.stream = stream; S.video = video;
    renderAgent();
    // Sharing means working in another window, where on-page bubbles cannot be seen.
    if ("Notification" in window && Notification.permission === "default") {
      await Notification.requestPermission();
      renderAgent();
    }
  } catch (error) { fail(error); }
}

async function requestBubbles(trigger) {
  if (!bubblesAllowed() || S.bubbleBusy) return;
  S.bubbleBusy = true;
  S.lastBubbleAt = Date.now();
  S.lastCtx = contextSignature();
  const sid = S.view.session.id;
  try {
    if (S.stream) S.lastScreenSig = await screenSignature();
    const out = await api("POST", `/api/sessions/${sid}/bubbles`, {
      notes: state().notes || "", selection: String(window.getSelection() || "").slice(0, 1500),
      screenshot: await grabFrame(), trigger, active: S.bubbles.size });
    if (S.view && S.view.session.id === sid) {
      out.bubbles.forEach((bubble, index) => setTimeout(() => showBubble(bubble), index * 700));
    }
  } catch (error) {
    if (trigger === "manual") fail(error); else console.warn(error);
  }
  S.bubbleBusy = false;
}

/* The life of a bubble: `remaining` ms that only run down while the learner can see it. */
function startLife(entry) {
  if (entry.done || entry.running || entry.hovered || document.hidden) return;
  entry.running = true;
  entry.startedAt = Date.now();
  entry.timer = setTimeout(() => resolveBubble(entry.data.id, "fade"), entry.remaining);
  // restart the visual fade from where the clock really is
  const total = entry.ttl / 1000;
  const style = entry.el.style;
  style.animationName = "none";
  void entry.el.offsetWidth;
  style.animationDuration = total + "s";
  style.animationDelay = -(total - entry.remaining / 1000) + "s";
  style.animationName = "bubble-life";
}

function stopLife(entry) {
  if (!entry.running) return;
  entry.running = false;
  clearTimeout(entry.timer);
  entry.remaining = Math.max(0, entry.remaining - (Date.now() - entry.startedAt));
}

function dropNotice(entry) {
  clearTimeout(entry.noticeTimer);
  entry.noticeTimer = null;
  if (entry.notice) { entry.notice.onclick = null; entry.notice.close(); entry.notice = null; }
}

/* Tab hidden: the bubble goes out as a system notification with the same lifetime. */
function notifyBubble(entry) {
  if (!canNotify()) return;
  try {
    entry.notice = new Notification(t("k_" + entry.data.kind) + " · AI Collaborator",
      { body: entry.data.text, tag: "bubble-" + entry.data.id });
  } catch (error) { return; }
  entry.notice.onclick = () => { window.focus(); dropNotice(entry); };
  entry.noticeTimer = setTimeout(() => {
    // still away after a full lifetime: the notification was their chance to see it
    if (document.hidden) resolveBubble(entry.data.id, "fade");
  }, entry.ttl);
}

function onVisibility() {
  if (document.hidden) {
    for (const entry of S.bubbles.values()) stopLife(entry);
    return;
  }
  touch();                      // being away is not being stuck
  for (const entry of S.bubbles.values()) { dropNotice(entry); startLife(entry); }
}

function showBubble(bubble) {
  if (!S.view || S.view.session.id !== bubble.session_id || S.bubbles.has(bubble.id)) return;
  const act = (action) => (event) => { event.stopPropagation(); resolveBubble(bubble.id, action); };
  const el = h("div", { class: "bubble-life" },
    h("div", { class: "bubble kind-" + bubble.kind },
      h("div", { class: "bubble-kind" }, t("k_" + bubble.kind)),
      h("div", { class: "bubble-text" }, bubble.text),
      bubble.why ? h("div", { class: "bubble-why" }, bubble.why) : null,
      h("div", { class: "bubble-actions" },
        h("button", { onclick: act("accept") }, "✓ " + t("b_accept")),
        h("button", { onclick: act("expand") }, "↗ " + t("b_expand")),
        h("button", { class: "quiet", onclick: act("ignore") }, "✕ " + t("b_ignore")))));
  const ttl = (bubble.ttl || 30) * 1000;
  const entry = { data: bubble, el, ttl, remaining: ttl, running: false, hovered: false,
    timer: null, startedAt: 0, notice: null, noticeTimer: null };
  // reading a bubble should never make it vanish: the clock stops under the pointer or focus
  const hold = () => { entry.hovered = true; stopLife(entry); };
  const release = () => { entry.hovered = false; startLife(entry); };
  el.addEventListener("mouseenter", hold);
  el.addEventListener("mouseleave", release);
  el.addEventListener("focusin", hold);
  el.addEventListener("focusout", release);
  S.bubbles.set(bubble.id, entry);
  $("#bubble-layer").prepend(el);
  if (document.hidden) notifyBubble(entry); else startLife(entry);
}

function clearBubbles() {
  for (const entry of S.bubbles.values()) { stopLife(entry); dropNotice(entry); entry.el.remove(); }
  S.bubbles.clear();
}

async function resolveBubble(id, action) {
  const entry = S.bubbles.get(id);
  if (!entry) return;
  S.bubbles.delete(id);
  stopLife(entry);
  dropNotice(entry);
  entry.done = true;
  entry.el.style.animationName = "none";   // let the leaving transition take over
  entry.el.classList.add("leaving", "left-" + action);
  setTimeout(() => entry.el.remove(), 350);
  const sid = entry.data.session_id;
  const mine = () => S.view && S.view.session.id === sid;
  if (action !== "fade") touch();
  if (action === "expand" && mine()) {
    S.view.messages.push({ role: "user", content: entry.data.text, meta: { type: "expand" } });
    renderMessages(true);
  }
  try {
    const out = await api("POST", `/api/bubbles/${id}/feedback`, { action });
    if (!mine()) return;
    state().adapt = out.adapt;
    state().shelf = out.shelf;
    S.view.params = out.params;
    if (action === "expand") {
      S.view.messages.pop();
      S.view.messages.push(...out.messages);
      renderMessages();
    }
    renderShelf();
    renderAgent();
  } catch (error) {
    if (action === "expand" && mine()) { S.view.messages.pop(); renderMessages(); }
    fail(error);
  }
}

// ------------------------------------------------------------- post-test
function closeModal() {
  $("#modal").classList.add("hidden");
  $("#modal").replaceChildren();
}

function openModal(...children) {
  const modal = $("#modal");
  modal.replaceChildren(h("div", { class: "modal-card" },
    h("button", { class: "x modal-x", onclick: closeModal }, "×"), children));
  modal.classList.remove("hidden");
}

function openPostTest() {
  const done = S.view.tests.find((x) => x.phase === "post");
  if (done) return showResults();
  const keys = S.config.likert.post;
  const answer = h("textarea", { rows: 4 });
  const body = h("div", {}, likertInput("post", keys),
    h("label", { class: "field" }, h("span", {}, t("q_open")), answer));
  const submit = h("button", { class: "primary" }, t("submit"));
  submit.addEventListener("click", async () => {
    submit.disabled = true; submit.textContent = t("grading");
    try {
      const out = await api("POST", `/api/sessions/${S.view.session.id}/test`, {
        phase: "post", likert: readLikert(body, "post", keys), answer: answer.value });
      S.view.tests = out.tests;
      showResults();
    } catch (error) { fail(error); submit.disabled = false; submit.textContent = t("submit"); }
  });
  openModal(h("h2", {}, t("post_title")), h("p", { class: "muted" }, t("post_sub")), body,
    h("div", { class: "row end" }, submit));
}

function showResults() {
  const pre = S.view.tests.find((x) => x.phase === "pre");
  const post = S.view.tests.find((x) => x.phase === "post");
  const num = (v) => (v === undefined || v === null ? "—" : v);
  const delta = (a, b) => {
    if (a === undefined || a === null || b === undefined || b === null) return h("td", {}, "—");
    const d = Math.round((b - a) * 100) / 100;
    return h("td", { class: d > 0 ? "up" : d < 0 ? "down" : "" }, (d > 0 ? "+" : "") + d);
  };
  const row = (label, a, b) => h("tr", {}, h("td", {}, label), h("td", {}, num(a)), h("td", {}, num(b)), delta(a, b));
  const head = () => h("tr", {}, h("th", {}), h("th", {}, t("pre")), h("th", {}, t("post")), h("th", {}, t("gain")));
  const pl = (pre || {}).likert || {}, ql = (post || {}).likert || {};
  const ps = (pre || {}).scores || {}, qs = (post || {}).scores || {};
  openModal(
    h("h2", {}, t("result_title")),
    pre ? null : h("p", { class: "muted" }, t("no_pre")),
    h("h4", {}, t("self_report")),
    h("table", { class: "table" }, head(), S.config.likert.post.map((key) => row(t("q_" + key), pl[key], ql[key]))),
    h("h4", {}, t("rubric")),
    h("table", { class: "table" }, head(),
      S.config.rubric.map((key) => row(t("r_" + key), ps[key], qs[key])),
      row(t("total"), ps.total, qs.total)),
    qs.comment ? h("p", {}, h("b", {}, t("judge_comment") + "： "), qs.comment) : null,
    h("div", { class: "row end" }, h("button", { class: "primary", onclick: closeModal }, t("close"))));
}

// -------------------------------------------------------------- settings
async function openSettings() {
  let current;
  try { current = await api("GET", "/api/settings"); } catch (error) { return fail(error); }
  const option = (value, label) => h("option", { value }, label);
  const provider = h("select", {}, option("anthropic", "Claude (Anthropic)"),
    option("openai", t("set_openai")), option("mock", t("set_mock")));
  provider.value = current.provider;
  const key = h("input", { type: "password", autocomplete: "off",
    placeholder: current.has_key ? t("set_key_saved") : "sk-…" });
  const model = h("input", { type: "text", value: current.provider === "mock" ? "" : current.model,
    placeholder: t("set_model_ph") });
  const baseUrl = h("input", { type: "text", value: current.base_url || "", placeholder: t("set_url_ph") });
  const effort = h("select", {}, ["low", "medium", "high", "none"].map((x) => option(x, x)));
  effort.value = current.effort || "medium";
  const remote = h("div", {},
    h("label", { class: "field" }, h("span", {}, "API Key"), key),
    h("label", { class: "field" }, h("span", {}, t("set_model")), model),
    h("label", { class: "field" }, h("span", {}, t("set_url")), baseUrl),
    h("label", { class: "field" }, h("span", {}, t("set_effort")), effort));
  const sync = () => remote.classList.toggle("hidden", provider.value === "mock");
  provider.addEventListener("change", () => { model.value = ""; baseUrl.value = ""; sync(); });
  sync();
  const save = h("button", { class: "primary" }, t("save"));
  save.addEventListener("click", async () => {
    save.disabled = true;
    try {
      await api("POST", "/api/settings", { provider: provider.value, api_key: key.value,
        model: model.value, base_url: baseUrl.value, effort: effort.value });
      S.config = await api("GET", "/api/config");
      renderSidebar();
      closeModal();
      toast(t("set_saved"));
    } catch (error) { fail(error); save.disabled = false; }
  });
  openModal(h("h2", {}, t("set_title")),
    h("label", { class: "field" }, h("span", {}, t("set_provider")), provider), remote,
    h("p", { class: "muted tiny" }, t("set_where") + " " + current.env_file),
    h("div", { class: "row end" }, save));
}

// ------------------------------------------------------------------- lab
async function renderLab() {
  const main = $("#main");
  main.replaceChildren(h("p", { class: "muted pad" }, "…"));
  let stats;
  try { stats = await api("GET", "/api/stats"); } catch (error) { return fail(error); }
  if (S.route !== "lab") return;
  const cell = (v) => h("td", {}, v === null || v === undefined ? "—" : v);
  const arrow = (a, b) => (a === null || b === null ? "—" : `${a} → ${b}`);
  const gain = (v) => h("td", { class: v > 0 ? "up" : v < 0 ? "down" : "" },
    v === null || v === undefined ? "—" : (v > 0 ? "+" : "") + v);
  const th = (...keys) => h("tr", {}, keys.map((key) => h("th", {}, t(key))));
  const agentName = (name) => t("agent_" + name).split(/[（(]/)[0].trim();

  const arenaBox = h("div", { id: "arena-box" });
  const problem = h("textarea", { rows: 3, placeholder: t("arena_ph") });
  const run = h("button", { class: "primary" }, t("arena_run"));
  run.addEventListener("click", async () => {
    if (!problem.value.trim()) { toast(t("need_problem"), true); return; }
    run.disabled = true; run.textContent = t("arena_running");
    try { S.arena = await api("POST", "/api/arena", { problem: problem.value }); renderArena(arenaBox); }
    catch (error) { fail(error); }
    run.disabled = false; run.textContent = t("arena_run");
  });

  main.replaceChildren(h("div", { class: "lab" },
    h("h1", {}, t("lab_title")), h("p", { class: "lead" }, t("lab_sub")),
    h("section", { class: "panel" },
      h("h3", {}, t("lab_prepost")), h("p", { class: "muted" }, t("lab_prepost_d")),
      stats.agents.length ? h("table", { class: "table" },
        th("c_agent", "c_sessions", "c_paired", "c_likert", "c_gain", "c_rubric", "c_gain", "c_ownership", "c_helpful"),
        stats.agents.map((r) => h("tr", {}, cell(agentName(r.agent)), cell(r.sessions), cell(r.paired),
          cell(arrow(r.pre_likert, r.post_likert)), gain(r.likert_gain),
          cell(arrow(r.pre_rubric, r.post_rubric)), gain(r.rubric_gain),
          cell(r.ownership), cell(r.helpful)))) : h("p", { class: "muted" }, t("no_data"))),
    h("section", { class: "panel" },
      h("h3", {}, t("lab_arena")), h("p", { class: "muted" }, t("lab_arena_d")),
      problem, h("div", { class: "row end" }, run), arenaBox,
      stats.arena.length ? h("div", {}, h("h4", {}, `${t("arena_results")} (${stats.arena_rounds})`),
        h("table", { class: "table" }, th("c_agent", "c_n", "c_mean", "c_best"),
          stats.arena.map((r) => h("tr", {}, cell(agentName(r.agent)), cell(r.n), cell(r.mean_rating), cell(r.best))))) : null,
      h("p", { class: "muted small" }, t("lab_offline"))),
    h("section", { class: "panel" },
      h("h3", {}, t("lab_bubbles")),
      h("table", { class: "table" }, th("c_kind", "c_shown", "c_accepted", "c_expanded", "c_ignored", "c_faded"),
        Object.entries(stats.bubbles).map(([kind, r]) => h("tr", {}, cell(t("k_" + kind)), cell(r.shown),
          cell(r.accepted), cell(r.expanded), cell(r.ignored), cell(r.faded))))),
    h("section", { class: "panel" },
      h("div", { class: "row" }, h("h3", {}, t("lab_profile")), h("span", { class: "spacer" }),
        h("button", { class: "ghost small", onclick: async () => {
          try { await api("POST", "/api/profile/reset", {}); renderLab(); } catch (error) { fail(error); } } }, t("reset"))),
      h("p", { class: "muted" }, t("lab_profile_d")),
      h("p", {}, `${t("agency")} ${Math.round(stats.profile.agency * 100)}%`),
      h("div", { class: "prefs wide" }, Object.entries(stats.profile.prefs).map(([kind, w]) => h("div", { class: "pref" },
        h("span", { class: "pref-name" }, t("k_" + kind)),
        h("div", { class: "bar" }, h("div", { class: "bar-fill kind-" + kind, style: `width:${Math.round(w * 100)}%` })))))),
    h("div", { class: "row" }, h("button", { class: "ghost", onclick: async () => {
      try { download("collaborator-data.json", JSON.stringify(await api("GET", "/api/export"), null, 2), "application/json"); }
      catch (error) { fail(error); } } }, t("lab_export")))));
  if (S.arena) renderArena(arenaBox);
}

function renderArena(box) {
  const arena = S.arena;
  if (!arena) { box.textContent = ""; return; }
  const revealed = arena.reveal;
  const submit = h("button", { class: "primary" }, t("arena_submit"));
  const grid = h("div", { class: "arena" }, arena.entries.map((entry) => h("div", { class: "arena-entry" },
    h("div", { class: "row" }, h("strong", {}, entry.label),
      revealed ? h("span", { class: "chip" }, t("agent_" + revealed[entry.label]).split(/[（(]/)[0]) : null),
    h("div", { class: "arena-text", html: md(entry.text) }),
    revealed ? null : h("div", { class: "arena-rate" },
      h("label", {}, t("arena_rate") + " ",
        h("select", { "data-label": entry.label }, ["", 1, 2, 3, 4, 5].map((n) => h("option", { value: String(n) }, n || "—")))),
      h("label", {}, h("input", { type: "radio", name: "arena-best", value: entry.label }), " " + t("arena_best"))))));
  submit.addEventListener("click", async () => {
    const ratings = {};
    grid.querySelectorAll("select[data-label]").forEach((el) => { if (el.value) ratings[el.dataset.label] = Number(el.value); });
    const best = grid.querySelector('input[name="arena-best"]:checked');
    try {
      const out = await api("POST", `/api/arena/${arena.id}/rate`, { ratings, best: best ? best.value : null });
      arena.reveal = out.reveal;
      renderLab();
    } catch (error) { fail(error); }
  });
  fill(box,h("p", { class: "arena-problem" }, arena.problem), grid,
    revealed ? null : h("div", { class: "row end" }, submit));
}

// ---------------------------------------------------------------- router
function renderSession() {
  const phase = S.view.session.phase;
  if (S.view.features.scaffold && phase === "frame") {
    clearBubbles();
    renderFrame();
  } else {
    renderWork();
    // let the first bubbles come soon after the workspace opens
    S.lastBubbleAt = Date.now() - Math.max(0, S.view.params.interval - 20) * 1000;
    S.lastCtx = "";
    touch();
  }
  renderSidebar();
}

async function navigate() {
  const hash = location.hash || "#/new";
  const match = hash.match(/^#\/s\/([0-9a-f]+)/);
  closeModal();
  if (match) {
    S.route = "session";
    const same = S.view && S.view.session.id === match[1];
    if (!same) {
      clearBubbles();
      S.picked = new Set();
      try { S.view = await api("GET", "/api/sessions/" + match[1]); }
      catch (error) { fail(error); location.hash = "#/new"; return; }
      S.picked = new Set(S.view.session.state.chosen || []);
    }
    renderSession();
  } else if (hash.startsWith("#/lab")) {
    S.route = "lab";
    clearBubbles();
    renderSidebar();
    renderLab();
  } else {
    S.route = "new";
    clearBubbles();
    renderSidebar();
    renderNew();
  }
}

async function boot() {
  try {
    S.config = await api("GET", "/api/config");
    await refreshSessions();
  } catch (error) { fail(error); return; }
  $("#btn-new").addEventListener("click", () => { location.hash = "#/new"; if (S.route === "new") renderNew(); });
  $("#btn-lab").addEventListener("click", () => { location.hash = "#/lab"; });
  $("#provider").addEventListener("click", openSettings);
  $("#btn-lang").addEventListener("click", () => {
    LANG = LANG === "zh" ? "en" : "zh";
    localStorage.setItem("lang", LANG);
    navigate();
  });
  $("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });
  window.addEventListener("hashchange", navigate);
  document.addEventListener("visibilitychange", onVisibility);
  ["keydown", "mousedown"].forEach((name) => document.addEventListener(name, touch, true));
  window.addEventListener("beforeunload", () => { if (S.notesTimer) saveNotes(); });
  setInterval(tick, 5000);
  navigate();
}

boot();
