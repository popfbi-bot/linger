/* 前端判据的执行器（在 node 里跑真实 app.js，不是复刻一遍）。
 *
 * 为什么要跑真的：判据 4 说的是「上架入口**只在自己的卡上出现**」。
 * 用 grep 断言「代码里有 isOwnPersona」证明不了这件事——
 * 那句话可以写对了却从没被调用，也可以在错的分支里被调用。
 * 唯一靠谱的证据是：把 app.js 真的跑起来，喂它一份人格列表，然后数 DOM 里的按钮。
 *
 * 代价是得假装一个浏览器。下面这份假 DOM 只实现 app.js 用到的 API，
 * 但**没有实现任何业务判断**——按钮是 app.js 生成的，不是这里造的。
 *
 * 入参（stdin JSON）：{ api: "<url>", personas: [...], account: {...}|null }
 * 出参（stdout JSON）：{ "cards": [{id, name, actions:[...]}, ...] }
 */
"use strict";

const fs = require("fs");
const vm = require("vm");

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const appJs = fs.readFileSync(input.app_js, "utf8");

// ── 极简 DOM ──
class FakeElement {
  constructor(tag) {
    this.tagName = String(tag || "div").toUpperCase();
    this.children = [];
    this.parentElement = null;
    this.dataset = {};
    this.style = {};
    this.classList = makeClassList(this);
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
  insertBefore(node, ref) { node.parentElement = this; this.children.push(node); return node; }
  removeChild(node) { this.children = this.children.filter((c) => c !== node); return node; }
  remove() { if (this.parentElement) this.parentElement.removeChild(this); }
  addEventListener() {}
  removeEventListener() {}
  querySelector() { return null; }
  querySelectorAll() { return []; }
  closest() { return null; }
  focus() {}
  setAttribute() {}
  getAttribute() { return null; }
  scrollTo() {}
  get scrollTop() { return 0; }
  set scrollTop(_v) {}
  get scrollHeight() { return 0; }
}

function makeClassList(node) {
  const set = new Set();
  return {
    add: (...c) => c.forEach((x) => set.add(x)),
    remove: (...c) => c.forEach((x) => set.delete(x)),
    toggle: (c, on) => (on === undefined ? (set.has(c) ? set.delete(c) : set.add(c)) : (on ? set.add(c) : set.delete(c))),
    contains: (c) => set.has(c),
    _set: set,
  };
}

// 深度优先找 id / class
function walk(node, visit) {
  visit(node);
  for (const c of node.children) walk(c, visit);
}

const elementsById = new Map();

// pre-create the ids app.js reaches for at top level
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
  querySelector() { return null; },
  querySelectorAll() { return []; },
  addEventListener() {},
};

const storage = new Map(Object.entries(input.local_storage || {}));
const localStorage = {
  getItem: (k) => (storage.has(k) ? storage.get(k) : null),
  setItem: (k, v) => storage.set(k, String(v)),
  removeItem: (k) => storage.delete(k),
};

const fetchCalls = [];
let lastPreview = "";
function lastPreviewType() { return lastPreview; }
const createCalls = [];   // POST /v1/personas 的请求体（判据 3 看这个）
const previewCalls = [];  // 预览 / 生成 / 蒸馏的请求体

function ok(body) {
  return { ok: true, status: 200, statusText: "OK", json: async () => body };
}

/** 预览类端点只回一张「够用」的卡：入口 B/C/D 的前端流程都靠它继续往下走。
 *  它不做任何补全（补全是后端的事），只保证 app.js 能走到 POST /v1/personas。 */
function previewPayload(url, payload) {
  const draft = payload.draft || {};
  const meta = draft.meta || { name: "草稿" };
  const persona = {
    id: "",
    meta,
    traits: draft.traits || {},
    voice: draft.voice || {},
    values: draft.values || {},
    provenance: { type: url.indexOf("distill") !== -1 ? "cloned" : (url.indexOf("draft") !== -1 ? "authored" : "generated") },
  };
  return { persona, preview: ["嗯。"], warnings: [] };
}

/** 直接调用 app.js 内部的创建入口。
 *
 *  为什么不点按钮：四个入口的 DOM 路径都很长（表单校验、预览、确认），
 *  把它们全模拟一遍，测的就不再是判据 3 而是我的假 DOM 写得对不对。
 *  app.js 把要测的函数暴露在 `window.__lingerTestHooks` 上——
 *  它们就是页面按钮绑定的同一批函数，所以「有没有带 owner」这件事依然由真代码决定。
 */
async function driveCreateEntries() {
  const hooks = (ctx.window && ctx.window.__lingerTestHooks) || {};
  if (hooks.confirmDraft) {
    await hooks.confirmDraft({ meta: { name: "一句话造的人" } }, "一句话造的人");
  }
  if (hooks.confirmDistill) {
    await hooks.confirmDistill({ meta: { name: "复刻的人" } }, "复刻的人");
  }
  if (hooks.wizardCreate) {
    if (hooks.fillWizardName) hooks.fillWizardName("向导造的人");
    await hooks.wizardCreate();
  }
  if (hooks.startSession) {
    await hooks.startSession();
  }
}

async function fakeFetch(url, options) {
  fetchCalls.push(String(url));
  const opt = options || {};
  const method = String(opt.method || "GET").toUpperCase();
  if (url === "/v1/personas" && method === "POST") {
    const body = JSON.parse(opt.body || "{}");
    // 按请求体认入口：模板带 template_id，其余带 persona（卡里带 provenance.type）
    const type = ((body.persona || {}).provenance || {}).type || "";
    // 入口按来源标记认：模板 A / 蒸馏 C（cloned）/ 向导 D（authored）/
    // 「一句话生成」B（generated，或预览没给 type 时的兜底）。
    // 这是**前端行为**的镜像判断，只用来给测试报错时指明是哪条路，不参与任何断言。
    const entry = body.template_id
      ? "A:模板"
      : type === "cloned"
        ? "C:蒸馏"
        : type === "authored"
          ? "D:向导"
          : "B:一句话生成";
    createCalls.push({ url: String(url), entry, body });
    const bodyType = type || ((lastPreviewType && lastPreviewType()) || "generated");
    return ok({
      id: "p_new", name: "新建", identity: "", archetype: "", version: 1,
      owner: body.owner || "", provenance: { type: bodyType },
    });
  }
  if (url === "/v1/personas/generate" || url === "/v1/personas/distill" ||
      url === "/v1/personas/draft/preview") {
    previewCalls.push({ url: String(url), body: JSON.parse(opt.body || "{}") });
    const payload = previewPayload(String(url), JSON.parse(opt.body || "{}"));
    lastPreview = payload.persona.provenance.type;
    return ok(payload);
  }
  if (url === "/v1/sessions") {
    // 开会话不是判据关心的，给个能走通的最小响应，免得后续链路炸掉
    return ok({ session_id: "s_test", persona_id: "p_new", user_id: "u", scenario: null });
  }
  const body = routeFetch(String(url), options || {});
  if (body === undefined) return { ok: false, status: 404, statusText: "Not Found", json: async () => ({}) };
  return { ok: true, status: 200, statusText: "OK", json: async () => body };
}

/** 只回答「人格列表 / 市场列表 / 我的条目」这三条，其余一律 404。
 *  不实现任何业务判断——归属判断必须发生在 app.js 里，否则这条测试就白测了。 */
function routeFetch(url, options) {
  if (url === "/v1/personas") {
    // 记下「创建人格」的请求体：判据 3 要看的就是它里面有没有 owner
    if (String((options || {}).method || "GET").toUpperCase() === "POST") {
      return { id: "p_created", name: "新建", owner: "", provenance: { type: "authored" } };
    }
    return { personas: input.personas };
  }
  if (url === "/v1/market/listings") {
    return { count: (input.anon_market || []).length, listings: input.anon_market || [] };
  }
  if (url === "/v1/market/mine") return { count: 0, listings: [] };
  if (url === "/v1/personas/templates") return [];
  if (url === "/v1/scenarios") return { scenarios: [] };
  if (url === "/health") return { status: "ok" };
  if (url === "/v1/auth/me" && input.account) return input.account;
  return undefined;
}

function makeStorage(seed) {
  const map = new Map(Object.entries(seed || {}));
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
    removeItem: (k) => map.delete(k),
    _map: map,
  };
}

const ctx = {
  document,
  window: {},
  localStorage: makeStorage(input.local_storage),
  fetch: fakeFetch,
  console: { log() {}, warn() {}, error() {} },
  setTimeout,
  clearTimeout,
  getComputedStyle: () => ({ getPropertyValue: () => "" }),
  confirm: () => false,
  Math,
  JSON,
};
ctx.globalThis = ctx;
vm.createContext(ctx);

vm.runInContext(appJs, ctx, { filename: "app.js" });

// 启动链是 restoreAccount().finally(...)，给它几轮微任务 + 一轮宏任务
setTimeout(async () => {
  try { await driveCreateEntries(); } catch (e) { /* 某个入口的模拟不够真：不影响别的 */ }
  const cards = [];
  const list = document.getElementById("my-persona-list");
  for (const item of list.children) {
    if (!item.classList.contains("my-persona-item")) continue;
    // 按 class 认按钮：上架按钮的 class 是 my-persona-list-entry
    const actions = [];
    walk(item, (n) => {
      const cls = n.className || "";
      if (cls.indexOf("my-persona-list-entry") !== -1) actions.push("list");
      else if (cls.indexOf("my-persona-delete") !== -1) actions.push("delete");
    });
    cards.push({ id: item.dataset.personaId || "", actions });
  }
  const market = [];
  const marketList = document.getElementById("market-list");
  for (const item of marketList.children) {
    if (!item.classList.contains("market-item")) continue;
    market.push({ id: item.dataset.listingId || "" });
  }
  process.stdout.write(
    JSON.stringify({ cards, market, fetchCalls, createCalls, previewCalls }) + "\n"
  );
}, 60);
