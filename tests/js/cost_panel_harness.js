/* 成本看板的执行器（在 node 里跑真实 app.js，不是复刻一遍）。
 *
 * 判据 6 说的是「DOM 里有可断言的元素，至少显示今日 / 近 7 日 token 与失败率」。
 * grep 能证明 index.html 里有 `id="cost-today-tokens"`，证明不了**那个元素里的
 * 数字是从 /v1/costs 来的**——而后者才是判据的内容。
 *
 * 所以：真 app.js + 假 DOM + 假 fetch。断言三件事：
 *   1. 页面启动时真的请求了 /v1/costs
 *   2. DOM 里出现的数字，逐个都能在喂进去的响应里找到出处
 *   3. cost 为 null 时显示「未知」而不是 $0（这条与判据 3 是同一件事的前端一侧）
 *
 * 入参（stdin JSON）：{ app_js, costs }
 * 出参（stdout JSON）：{ fetchCalls, today, weekFailure, weekCost, providers, hint, error }
 */
"use strict";

const fs = require("fs");
const vm = require("vm");

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const appJs = fs.readFileSync(input.app_js, "utf8");

// ── 极简 DOM（与 voice_harness.js 同源：只保留 app.js 真的会碰到的 API） ──
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

// 「当前可见文本」：元素自己的 textContent，加上子元素里拼出来的文字。
// 判据要看的是「屏幕上有没有这个数字」，所以两处都得算上——
// 直接 innerHTML 拼出来的那一行不产生子节点，textContent 也不会包含它。
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

// ── 假 fetch：/v1/costs 直接回喂进来那份响应，其余端点给空壳 ──
const fetchCalls = [];
function ok(body) {
  return { ok: true, status: 200, statusText: "OK", json: async () => body };
}
async function fakeFetch(url) {
  const u = String(url);
  fetchCalls.push(u);
  if (u.indexOf("/v1/costs") === 0) return ok(input.costs || {});
  if (u === "/v1/personas") return ok({ personas: [] });
  if (u === "/v1/personas/templates") return ok([]);
  if (u === "/v1/scenarios") return ok({ scenarios: [] });
  if (u === "/v1/market/listings") return ok({ count: 0, listings: [] });
  if (u === "/v1/market/mine") return ok({ count: 0, listings: [] });
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
  const out = { fetchCalls, error: null };
  try {
    const hooks = (ctx.window && ctx.window.__lingerTestHooks) || {};
    // 启动时已经调过 loadCosts（app.js 末尾），这里再等它一轮；
    // 若那个调用被改坏，hooks.loadCosts 仍能手动触发——两种都要覆盖。
    if (hooks.loadCosts) await hooks.loadCosts();
    await new Promise((r) => setTimeout(r, 40));

    out.today = visibleText(document.getElementById("cost-today-tokens"));
    out.weekFailure = visibleText(document.getElementById("cost-week-failure"));
    out.weekCost = visibleText(document.getElementById("cost-week-cost"));
    out.providersText = visibleText(document.getElementById("cost-providers"));
    out.providerRows = document
      .getElementById("cost-providers")
      .querySelectorAll(".cost-provider-row")
      .map((r) => visibleText(r));
    out.hint = visibleText(document.getElementById("cost-hint"));
    out.windowLabel = visibleText(document.getElementById("cost-window"));
    out.hasLoadCosts = typeof hooks.loadCosts === "function";
  } catch (e) {
    out.error = String((e && e.stack) || e);
  }
  out.fetchCalls = fetchCalls;
  process.stdout.write(JSON.stringify(out) + "\n");
}, 60);
