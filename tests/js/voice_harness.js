/* 语音前端的执行器（在 node 里跑真实 app.js，不是复刻一遍）。
 *
 * 判据 7 说的是「麦克风按钮存在 + 点了会调 /v1/asr + 识别结果进输入框」。
 * grep 能证明第一句，证明不了后两句——那两句说的是**调用关系**。
 * 所以这里把 app.js 真的跑起来，喂它假的 DOM / 假的 MediaRecorder / 假的 fetch，
 * 然后看它到底往哪儿发了什么、又把结果放进了哪个元素。
 *
 * 断言边界（写死在 issue 里）：真录音断言不了，也不该在这里断言。
 * 这个 harness 不模拟麦克风硬件，只模拟「浏览器给了我一团字节」之后的事。
 *
 * 入参（stdin JSON）：{ app_js, asr_text, asr_status, can_record }
 * 出参（stdout JSON）：
 *   { micDisabled, canRecord, fetchCalls: [{url, method, bodyIsForm}],
 *     inputValue, statusText, playButtons, ttsBody }
 */
"use strict";

const fs = require("fs");
const vm = require("vm");

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const appJs = fs.readFileSync(input.app_js, "utf8");

// ── 极简 DOM（与 persona_cards_harness.js 同源，只补语音用到的那几个 API） ──
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
    this.className = "";
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
    walk(this, (n) => {
      if (hit) return;
      if (matches(n, sel)) hit = n;
    });
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
  get childElementCount() { return this.children.filter((c) => c.tagName !== "#TEXT").length; }
  set scrollTop(_v) {}
  get scrollHeight() { return 0; }
}

function matches(node, sel) {
  const s = String(sel || "");
  if (s.startsWith(".")) {
    // 两条都要认：`x.className = "a"`（赋值器写 _cls）与 `classList.add("a")`（写 _set）。
    // 只用其中一个会让「按钮明明在 DOM 里、选择器却找不到」——那种 bug 最难查。
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
  // document 级的查询要**真的下钻**到已创建的元素：app.js 造的模板按钮挂在
  // persona-list 底下，harness 要能点到它（否则「点了会调 /v1/asr」这条断言
  // 测的就不是真实路径）。按 id 遍历所有已知元素足够了——元素总量是个位数。
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

// ── 假的录音链路：getUserMedia 直接给一团字节，MediaRecorder 立刻「录完」 ──
const AUDIO_BYTES = new Uint8Array([0x1a, 0x45, 0xdf, 0xa3, 1, 2, 3, 4, 5, 6, 7, 8]);
const canRecord = input.can_record !== false;

class FakeMediaRecorder {
  constructor(stream) {
    this.stream = stream;
    this.mimeType = "audio/webm";
    this.state = "inactive";
    this._h = {};
  }
  addEventListener(type, fn) { this._h[type] = fn; }
  start() {
    this.state = "recording";
    // 立刻给一片数据（真录音也在 stop 前 at least 一次）
    if (this._h.dataavailable) this._h.dataavailable({ data: { size: AUDIO_BYTES.length } });
  }
  stop() {
    this.state = "inactive";
    if (this._h.stop) this._h.stop();
  }
}

const streamStopped = [];
const navigator = canRecord
  ? {
      mediaDevices: {
        getUserMedia: async () => ({ getTracks: () => [{ stop: () => streamStopped.push(true) }] }),
      },
    }
  : {};

// Blob / FormData / Audio：只要够 app.js 走通
class FakeBlob {
  constructor(parts, opts) {
    this.parts = parts || [];
    this.type = (opts && opts.type) || "";
    this.size = this.parts.length * 4;
  }
}
class FakeFormData {
  constructor() { this._entries = []; }
  append(k, v, name) { this._entries.push({ k, v, name }); }
  get(k) { const e = this._entries.find((x) => x.k === k); return e ? e.v : null; }
}
class FakeAudio {
  constructor(url) { this.url = url; this.played = false; }
  addEventListener() {}
  async play() { this.played = true; }
  pause() {}
}

// ── 假 fetch：记下每次调用，按 URL 回答 ──
const fetchCalls = [];
let ttsBody = null;
let asrForm = null;
function ok(body) {
  return { ok: true, status: 200, statusText: "OK", json: async () => body };
}
async function fakeFetch(url, options) {
  const opt = options || {};
  const method = String(opt.method || "GET").toUpperCase();
  // 按形状认 FormData：`instanceof` 在这个文件里不可靠（类声明在 fetch 之后）。
  // app.js 用的就是我们喂进去的那个 FakeFormData，所以形状认够了。
  const isForm = !!(opt.body && opt.body.constructor && opt.body.constructor.name === "FakeFormData");
  fetchCalls.push({ url: String(url), method, bodyIsForm: isForm });
  if (url === "/v1/asr") {
    // 判据 7 的关键：确认它带的是 multipart 表单，而且里面真的塞了音频
    if (isForm) {
      const file = opt.body.get("audio");
      asrForm = {
        hasAudio: !!file,
        fileName: file ? file.name : "",
        contentType: isForm && opt.body.type === "" ? "auto" : "",
        // 没有手写 Content-Type：multipart 的 boundary 必须由浏览器生成
        hasExplicitContentType: !!(opt.headers && (opt.headers["Content-Type"] || opt.headers["content-type"])),
      };
    }
    const status = input.asr_status || 200;
    if (status !== 200) {
      return { ok: false, status, statusText: "Unprocessable", json: async () => ({ detail: "音频为空" }) };
    }
    return ok({ text: input.asr_text === undefined ? "今天有点累" : input.asr_text, bytes: 12 });
  }
  if (url === "/v1/tts") {
    ttsBody = JSON.parse(opt.body || "{}");
    return {
      ok: true, status: 200, statusText: "OK",
      arrayBuffer: async () => new Uint8Array([0x52, 0x49, 0x46, 0x46]).buffer,
      json: async () => ({}),
    };
  }
  if (url === "/v1/personas") {
    if (method === "POST") {
      return ok({ id: "p_created", name: "林汐", identity: "", archetype: "", version: 1, owner: "", provenance: { type: "authored" } });
    }
    return ok({ personas: input.personas || [] });
  }
  if (url === "/v1/sessions") {
    return ok({ session_id: "s_test", persona_id: "p_created", user_id: "u", scenario: null });
  }
  if (String(url).indexOf("/v1/companion/") === 0) return ok({});
  if (String(url).indexOf("/v1/personas/") === 0 && String(url).indexOf("/diary") !== -1) return ok({ entries: [] });
  if (url === "/v1/market/listings") return ok({ count: 0, listings: [] });
  if (url === "/v1/market/mine") return ok({ count: 0, listings: [] });
  if (url === "/v1/personas/templates") return ok(input.templates || []);
  if (url === "/v1/scenarios") return ok({ scenarios: [] });
  if (url === "/health") return ok({ status: "ok" });
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
  Blob: FakeBlob,
  FormData: FakeFormData,
  Audio: FakeAudio,
  URL: { createObjectURL: () => "blob:fake", revokeObjectURL() {} },
  MediaRecorder: canRecord ? FakeMediaRecorder : undefined,
  navigator,
  TextDecoder,
};
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(appJs, ctx, { filename: "app.js" });

setTimeout(async () => {
  const hooks = (ctx.window && ctx.window.__lingerTestHooks) || {};
  const mic = document.getElementById("btn-mic");
  const out = {
    canRecord: hooks.canRecord ? hooks.canRecord() : null,
    micDisabledInitial: mic.disabled,
    fetchCalls,
    inputValue: "",
    statusText: "",
    playButtons: [],
    ttsBody: null,
    asrForm: null,
    streamStopped,
  };

  try {
    // 1) 点一下「麦克风」：开始录音 → 再点一下停止 → 上传
    //    （`input.mic_first === false` 时跳过：有两条路径要独立验证）
    if (hooks.toggleRecording && input.mic_first !== false) {
      await hooks.toggleRecording();   // 开始
      await hooks.toggleRecording();   // 停止 → 触发 stop → uploadRecording
      // uploadRecording 是 async 的，给它一轮微任务
      await new Promise((r) => setTimeout(r, 30));
    }
    out.inputValue = document.getElementById("input").value;
    out.statusText = document.getElementById("voice-status").textContent;

    // 2) 先开会话建立 state.personaId（模板 → POST /v1/personas → POST /v1/sessions），
    //    再挂「播放」按钮并点它：断言请求体里有 persona_id / user_id
    // startSession 要求先选中一个模板（state.templateId）；直接调会静默 return。
    // 模板按钮的 click 处理器干的就是「记下模板 → 调 startSession」，
    // 所以这里派发一次真实 click，让走的是同一条代码路径。
    out.waitMs = input.wait_ms || 60;
    await new Promise((r) => setTimeout(r, out.waitMs));
    out.tplButtons = document.querySelectorAll(".persona-item").length;
    const tplBtn = document.querySelector(".persona-item");
    if (tplBtn && tplBtn.dispatch) {
      try { await tplBtn.dispatch("click", {}); } catch (e) { out.sessionError = String(e && e.message || e); }
      await new Promise((r) => setTimeout(r, 20));
    }
    const holder = document.createElement("div");
    if (hooks.attachPlayButton) {
      hooks.attachPlayButton(holder, "嗯，我在。");
      const btn = holder.querySelector(".msg-play");
      // playMessage 要 persona_id（开会话时才设），没有就跳过——这里测的是
      // 「按钮挂上了、点了会走 /v1/tts」，不是会话流程本身。
      if (btn && hooks.playMessage) {
        try { await hooks.playMessage(btn.dataset.text, btn); } catch (e) { out.playError = String(e && e.message || e); }
      }
      out.playDisabledAfterClick = btn ? btn.disabled : null;
    }
    out.playButtons = holder.querySelectorAll(".msg-play").map((b) => ({
      text: b.dataset.text || "",
      label: b.textContent,
      disabled: b.disabled,
    }));
    out.ttsBody = ttsBody;
  } catch (e) {
    out.error = String((e && e.stack) || e);
  }

  out.fetchCalls = fetchCalls;
  out.asrForm = asrForm;
  process.stdout.write(JSON.stringify(out) + "\n");
}, 60);
