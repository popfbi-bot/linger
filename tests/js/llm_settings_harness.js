/* 模型配置面板的执行器（在 node 里跑真实 app.js，不是复刻一遍）。
 *
 * 为什么需要它：判据要的是「填入 key 之后真的发了 PUT，而且界面上的状态变了」。
 * grep 能证明 index.html 里有 `id="llm-api-key"`，证明不了**这个值会被提交**；
 * 也证明不了「输入框留空时不会把已有的 key 抹掉」——而后者才是最容易写错的地方。
 *
 * 入参（stdin JSON）：
 *   { app_js, scenario, settings }
 *   scenario: "open" | "save_with_key" | "save_without_key" | "save_rejected" | "clear" | "test_fail"
 *   settings: GET /v1/settings/llm 要回的那份 { effective: {...}, file: "..." }
 *
 * 出参（stdout JSON）：
 *   { puts, deletes, posts, panelHidden, providerValue, fieldsDisabled,
 *     hint, okText, errText, savedBody, error }
 */
"use strict";

const fs = require("fs");
const vm = require("vm");

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const appJs = fs.readFileSync(input.app_js, "utf8");

// ── 极简 DOM（与 cost_panel_harness.js 同源：只保留 app.js 真的会碰到的 API） ──
class FakeElement {
  constructor(tag) {
    this.tagName = String(tag || "div").toUpperCase();
    this.children = [];
    this.parentElement = null;
    this.dataset = {};
    this.style = {};
    this.classList = makeClassList();
    this._text = "";
    this._html = "";
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.value = "";
    this.id = "";
    this.title = "";
    this.type = "";
  }
  get className() { return this._cls || ""; }
  set className(v) {
    this._cls = String(v);
    this.classList._set.clear();
    for (const c of String(v).split(/\s+/)) if (c) this.classList._set.add(c);
  }
  get textContent() { return this._text; }
  set textContent(v) { this._text = String(v); this._html = ""; }
  get innerHTML() { return this._html; }
  set innerHTML(v) { this._html = String(v); this.children = []; }
  appendChild(node) { node.parentElement = this; this.children.push(node); return node; }
  insertBefore(node) { node.parentElement = this; this.children.push(node); return node; }
  removeChild(node) { this.children = this.children.filter((c) => c !== node); return node; }
  remove() { if (this.parentElement) this.parentElement.removeChild(this); }
  addEventListener(type, fn) { (this._handlers || (this._handlers = {}))[type] = fn; }
  dispatch(type, ev) { if (this._handlers && this._handlers[type]) return this._handlers[type](ev); }
  removeEventListener() {}
  querySelector(sel) {
    let hit = null;
    walk(this, (n) => { if (!hit && matches(n, sel)) hit = n; });
    return hit;
  }
  querySelectorAll(sel) {
    const out = [];
    walk(this, (n) => { if (matches(n, sel)) out.push(n); });
    return out;
  }
  closest() { return null; }
  focus() {}
  setAttribute() {}
  getAttribute() { return null; }
  scrollTo() {}
  get scrollTop() { return 0; }
  set scrollTop(_v) {}
  get scrollHeight() { return 0; }
}

function matches(node, sel) {
  const s = String(sel || "");
  if (s.startsWith(".")) {
    const parts = String(node._cls || "").split(/\s+/).concat(Array.from(node.classList._set));
    return parts.indexOf(s.slice(1)) !== -1;
  }
  return false;
}

function makeClassList() {
  const set = new Set();
  return {
    add: (...c) => c.forEach((x) => set.add(x)),
    remove: (...c) => c.forEach((x) => set.delete(x)),
    toggle: (c, on) => (on === undefined ? (set.has(c) ? set.delete(c) : set.add(c)) : (on ? set.add(c) : set.delete(c))),
    contains: (c) => set.has(c),
    _set: set,
  };
}

function walk(node, visit) {
  visit(node);
  for (const c of node.children) walk(c, visit);
}

function visibleText(node) {
  const own = String(node._text || "") + " " + String(node._html || "");
  const kids = node.children.map(visibleText).join(" ");
  return (own + " " + kids).replace(/\s+/g, " ").trim();
}

const elementsById = new Map();
const document = {
  documentElement: new FakeElement("html"),
  body: new FakeElement("body"),
  getElementById(id) {
    if (!elementsById.has(id)) {
      const n = new FakeElement("div");
      n.id = id;
      elementsById.set(id, n);
    }
    return elementsById.get(id);
  },
  createElement(tag) { return new FakeElement(tag); },
  querySelector(sel) {
    for (const node of elementsById.values()) {
      const hit = node.querySelector(sel);
      if (hit) return hit;
      if (matches(node, sel)) return node;
    }
    return null;
  },
  querySelectorAll(sel) {
    const out = [];
    for (const node of elementsById.values()) {
      if (matches(node, sel)) out.push(node);
      out.push(...node.querySelectorAll(sel));
    }
    return out;
  },
  addEventListener() {},
};

const storage = new Map();
const localStorage = {
  getItem: (k) => (storage.has(k) ? storage.get(k) : null),
  setItem: (k, v) => storage.set(k, String(v)),
  removeItem: (k) => storage.delete(k),
};

// ── 假 fetch：把方法、URL、body 全记下来 ──
const calls = [];
function ok(body) {
  return { ok: true, status: 200, statusText: "OK", json: async () => body };
}
function bad(status, detail) {
  return { ok: false, status, statusText: "Bad Request", json: async () => ({ detail }) };
}

const eff = ((input.settings || {}).effective) || {};
const settingsBody = { effective: eff, file: (input.settings || {}).file || "data/llm_settings.json" };

async function fakeFetch(url, options) {
  const u = String(url);
  const method = String((options && options.method) || "GET").toUpperCase();
  const body = options && options.body ? JSON.parse(options.body) : null;
  calls.push({ url: u, method, body });

  if (u === "/v1/settings/llm" && method === "GET") return ok(settingsBody);
  if (u === "/v1/settings/llm" && method === "PUT") {
    if (input.scenario === "save_rejected") {
      return bad(400, "连不上：401 Unauthorized（key 无效）");
    }
    return ok({
      saved: true,
      effective: {
        provider: (body && body.provider) || "openai",
        base_url: (body && body.base_url) || eff.base_url || "",
        model: (body && body.model) || eff.model || "",
        api_key_set: true,
        api_key_hint: "sk-1…wxyz",
      },
      file: "data/llm_settings.json",
      probe: { ok: true, latency_ms: 128, reply: "连通了", provider: "openai" },
    });
  }
  if (u === "/v1/settings/llm" && method === "DELETE") {
    return ok({
      cleared: true,
      effective: { provider: "mock", base_url: "", model: "", api_key_set: false, api_key_hint: "" },
    });
  }
  if (u === "/v1/settings/llm/test" && method === "POST") {
    if (input.scenario === "test_fail") {
      return ok({ ok: false, latency_ms: 3000, error: "APITimeoutError: timed out", reply: "" });
    }
    return ok({ ok: true, latency_ms: 96, reply: "连通了", provider: "openai" });
  }
  if (u === "/v1/personas") return ok({ personas: [] });
  if (u === "/v1/personas/templates") return ok([]);
  if (u === "/v1/scenarios") return ok({ scenarios: [] });
  if (u === "/v1/market/listings") return ok({ count: 0, listings: [] });
  if (u === "/v1/market/mine") return ok({ count: 0, listings: [] });
  if (u === "/v1/costs") return ok({ totals: {} });
  if (u === "/health") return ok({ status: "ok" });
  return { ok: false, status: 404, statusText: "Not Found", json: async () => ({}) };
}

const ctx = {
  document,
  window: {},
  localStorage,
  fetch: fakeFetch,
  console: { log() {}, warn() {}, error() {} },
  setTimeout,
  clearTimeout,
  getComputedStyle: () => ({ getPropertyValue: () => "" }),
  confirm: () => false,
  Math,
  JSON,
  TextDecoder,
  navigator: {},
};
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(appJs, ctx, { filename: "app.js" });

setTimeout(async () => {
  const out = { scenario: input.scenario, error: null };
  try {
    const hooks = (ctx.window && ctx.window.__lingerTestHooks) || {};
    if (typeof hooks.openLlmPanel !== "function") throw new Error("没有 openLlmPanel 这个把手");

    await hooks.openLlmPanel();
    await new Promise((r) => setTimeout(r, 30));

    const el = (id) => document.getElementById(id);
    const key = el("llm-api-key");
    const prov = el("llm-provider");

    if (input.scenario === "save_with_key") {
      prov.value = "openai";
      prov.dispatch("change", { target: prov });
      el("llm-base-url").value = "https://api.deepseek.com/v1";
      el("llm-model").value = "deepseek-chat";
      key.value = "sk-1234567890abcdefwxyz";
      await hooks.saveLlmSettings();
    } else if (input.scenario === "save_without_key") {
      // 只想换模型名：key 输入框留空 → 不能把已配好的 key 抹掉
      prov.value = "openai";
      prov.dispatch("change", { target: prov });
      el("llm-model").value = "deepseek-reasoner";
      key.value = "";
      await hooks.saveLlmSettings();
    } else if (input.scenario === "save_rejected") {
      prov.value = "openai";
      prov.dispatch("change", { target: prov });
      el("llm-base-url").value = "https://bad.example/v1";
      key.value = "sk-bad";
      await hooks.saveLlmSettings();
    } else if (input.scenario === "clear") {
      await hooks.clearLlmSettings();
    } else if (input.scenario === "test_fail") {
      await hooks.testLlm();
    }
    await new Promise((r) => setTimeout(r, 40));

    out.panelHidden = el("llm-panel").hidden;
    out.providerValue = prov.value;
    out.baseUrlValue = el("llm-base-url").value;
    out.modelValue = el("llm-model").value;
    out.fieldsDisabled = {
      baseUrl: el("llm-base-url").disabled,
      model: el("llm-model").disabled,
      key: key.disabled,
    };
    out.hint = visibleText(el("llm-hint"));
    out.okText = visibleText(el("llm-ok"));
    out.errText = visibleText(el("llm-status"));
    out.okHidden = el("llm-ok").hidden;
    out.errHidden = el("llm-status").hidden;
    out.keyInputValue = key.value; // 必须始终为空：界面不持有密钥
    out.filePath = visibleText(el("llm-file-path"));
  } catch (e) {
    out.error = String((e && e.stack) || e);
  }
  out.calls = calls.map((c) => ({ url: c.url, method: c.method, body: c.body }));
  process.stdout.write(JSON.stringify(out) + "\n");
}, 60);
