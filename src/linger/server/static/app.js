/* Linger 前端逻辑
 *
 * 一个刻意的技术选择：消息端点是 POST，而 EventSource 只支持 GET。
 * 所以这里用 fetch + ReadableStream 手动解析 SSE 帧。代价是要自己处理
 * 「一个 chunk 把事件切成两半」的情况——下面的 parseSSE 就是干这个的。
 *
 * 三条前端契约（对应 docs/ROADMAP.md M1.5 A 组）：
 *   1. affect 到达 → 情绪指示器的文字与颜色都变（不只是 console.log）
 *   2. delta 到达   → 文本逐段追加（不是等 done 一次性刷出来）
 *   3. done 到达    → 记忆面板与 trace 面板自动刷新
 */

(() => {
  "use strict";

  // ───────────────────────────── 状态 ─────────────────────────────

  const state = {
    templateId: null,     // 当前选中的模板 id（人格未实例化前只有它）
    scenarioId: null,     // 当前选中的场景 id；null = 普通对话
    scenarioBeats: {},    // beat id → goal，用于进度面板显示
    personaId: null,
    sessionId: null,
    userId: ensureUserId(),   // 本机身份，localStorage 持久化（见 ensureUserId）
    // 账号（M3-H）。没登录时是 null，userId 就是本机的匿名 id——行为与今天完全一样。
    account: null,            // {user_id, email, ...}，来自 /v1/auth/me 或 login
    authMode: "login",        // "login" | "register"，登录浮层当前在哪个模式
    personaName: "",
    busy: false,
    lastTurnId: null,
    streamEl: null,     // 当前正在流式写入的气泡
    streamText: "",
    diaryHighlightId: null,   // 刚生成的那篇日记，用高亮标出来（M3-D）
    // 五步向导的工作区。**唯一真相**：每步输入框的值都先写回这里，
    // 切步时只换显示可见性，不重建 DOM，所以「上一步 / 下一步」来回走不丢数据。
    wizardStep: 1,
    wizardDraft: {},          // 与 POST /v1/personas/draft/preview 的 draft 同构
    // 市场（M4-C）。`marketListings` 是公共列表的缓存，
    // `marketMine` 是「我的条目」，`marketOpenId` 是当前展开的详情。
    // 三者都不参与对话链路——市场坏了不该让聊天挂掉。
    marketListings: [],
    marketMine: [],
    marketOpenId: null,
    // 人格摘要的缓存（含 owner / provenance）。上架按钮要靠 owner 判断归属，
    // 而卡片渲染时手上已经有整份摘要，不必再请求一次。
    personaCache: [],
    // 语音（M4-C）。只在「这一页要不要给麦克风按钮」上有影响：
    // 不支持录音的浏览器（http 局域网、老 Safari）按钮保持 disabled，
    // 文字输入一行不动——语音是加出来的一层，不是替换。
    recorder: null,     // MediaRecorder 实例
    chunks: [],         // 录音分片
    recording: false,
    audio: null,        // 当前在播的 Audio 元素（同一时刻只播一条）
    // 成本（M4-D）。存的是 `/v1/costs` 的**原始响应**，渲染时不再加工数字：
    // 成本怎么算只能有一个口径，那个口径在后端（`cost/pricing.py`）。
    costs: null,
  };

  /** 本机用户标识：localStorage 持久化。
   *
   * 为什么不能写死 "u_web"：后端所有状态（记忆 / 关系 / 情绪 / 陪伴档案）
   * 都按 (persona_id, user_id) 存。写死意味着同一个服务上所有浏览器共用一份记忆——
   * 谁都能读到别人的心事。而每次刷新换新 id 又会让用户「失忆」。
   * 所以：第一次访问生成一个，之后一直用它。
   */
  function ensureUserId() {
    const KEY = "linger.user_id";
    try {
      const saved = localStorage.getItem(KEY);
      if (saved) return saved;
      const id = "u_" + Math.random().toString(36).slice(2, 10);
      localStorage.setItem(KEY, id);
      return id;
    } catch (_) {
      // 隐私模式 / 禁用 storage：退化成「本次会话内」的临时身份。
      // 至少不会读到别人的记忆；代价是刷新后记忆看不到。
      return "u_" + Math.random().toString(36).slice(2, 10);
    }
  }

  // ───────────────────────────── DOM ─────────────────────────────

  const $ = (id) => document.getElementById(id);

  const el = {
    accountState: $("account-state"),
    btnShowAuth: $("btn-show-auth"),
    btnLogout: $("btn-logout"),
    authPanel: $("auth-panel"),
    authTitle: $("auth-title"),
    authHint: $("auth-hint"),
    authEmail: $("auth-email"),
    authPassword: $("auth-password"),
    authSubmit: $("btn-auth-submit"),
    btnAuthSwitch: $("btn-auth-switch"),
    btnAuthClose: $("btn-auth-close"),
    authError: $("auth-error"),
    authClaimHint: $("auth-claim-hint"),
    // 模型配置面板（M4 收口后补）
    btnShowLlm: $("btn-show-llm"),
    llmPanel: $("llm-panel"),
    llmProvider: $("llm-provider"),
    llmBaseUrl: $("llm-base-url"),
    llmModel: $("llm-model"),
    llmApiKey: $("llm-api-key"),
    llmKeyNote: $("llm-key-note"),
    llmFilePath: $("llm-file-path"),
    llmHint: $("llm-hint"),
    llmStatus: $("llm-status"),
    llmOk: $("llm-ok"),
    btnLlmSave: $("btn-llm-save"),
    btnLlmTest: $("btn-llm-test"),
    btnLlmClear: $("btn-llm-clear"),
    btnLlmClose: $("btn-llm-close"),
    personaList: $("persona-list"),
    myPersonaList: $("my-persona-list"),
    myPersonaCount: $("my-persona-count"),
    personaCreatePanel: $("persona-create-panel"),
    personaDesc: $("persona-desc"),
    btnShowCreate: $("btn-show-create"),
    btnGenerate: $("btn-generate"),
    personaGenerateHint: $("persona-generate-hint"),
    personaDraft: $("persona-draft"),
    btnShowDistill: $("btn-show-distill"),
    distillPanel: $("distill-panel"),
    distillTranscript: $("distill-transcript"),
    distillTarget: $("distill-target"),
    distillAuthorized: $("distill-authorized"),
    btnDistill: $("btn-distill"),
    distillHint: $("distill-hint"),
    distillResult: $("distill-result"),
    btnShowWizard: $("btn-show-wizard"),
    wizardPanel: $("wizard-panel"),
    wizardSteps: $("wizard-steps"),
    wizardProgress: $("wizard-progress"),
    wizardStepTitle: $("wizard-step-title"),
    wizardPrev: $("wizard-prev"),
    wizardNext: $("wizard-next"),
    wizardCreate: $("wizard-create"),
    wizardCancel: $("wizard-cancel"),
    wizardHint: $("wizard-hint"),
    wizardPreview: $("wizard-preview"),
    wizardPreviewLines: $("wizard-preview-lines"),
    wizardPreviewWarnings: $("wizard-preview-warnings"),
    wizardFinal: $("wizard-final"),
    wizName: $("wiz-name"),
    wizAge: $("wiz-age"),
    wizIdentity: $("wiz-identity"),
    wizArchetype: $("wiz-archetype"),
    wizOpenness: $("wiz-openness"),
    wizConscientiousness: $("wiz-conscientiousness"),
    wizExtraversion: $("wiz-extraversion"),
    wizAgreeableness: $("wiz-agreeableness"),
    wizNeuroticism: $("wiz-neuroticism"),
    wizRegister: $("wiz-register"),
    wizSentLo: $("wiz-sent-lo"),
    wizSentHi: $("wiz-sent-hi"),
    wizEmoji: $("wiz-emoji"),
    wizTics: $("wiz-tics"),
    wizForbidden: $("wiz-forbidden"),
    wizKnowsNot: $("wiz-knows-not"),
    wizLeakPolicy: $("wiz-leak-policy"),
    marketList: $("market-list"),
    marketCount: $("market-count"),
    marketHint: $("market-hint"),
    marketDetail: $("market-detail"),
    marketMineList: $("market-mine-list"),
    marketMineCount: $("market-mine-count"),
    marketMineEmpty: $("market-mine-empty"),
    btnMarketRefresh: $("btn-market-refresh"),
    btnMarketClose: $("btn-market-close"),
    scenarioList: $("scenario-list"),
    sceneCount: $("scene-count"),
    scenarioCreatePanel: $("scenario-create-panel"),
    btnShowScenarioCreate: $("btn-show-scenario-create"),
    scTitle: $("sc-title"),
    scId: $("sc-id"),
    scSetting: $("sc-setting"),
    scOpening: $("sc-opening"),
    scRole: $("sc-role"),
    scBeats: $("sc-beats"),
    btnCreateScenario: $("btn-create-scenario"),
    scHint: $("sc-hint"),
    sceneProgressCard: $("scene-progress-card"),
    sceneProgress: $("scene-progress"),
    messages: $("messages"),
    emptyState: $("empty-state"),
    input: $("input"),
    send: $("btn-send"),
    composer: $("composer"),
    connState: $("conn-state"),
    reset: $("btn-reset"),
    affectBadge: $("affect-badge"),
    affectLabel: $("affect-label"),
    padP: $("pad-p"),
    padA: $("pad-a"),
    padD: $("pad-d"),
    relLevel: $("rel-level"),
    relIntimacy: $("rel-intimacy"),
    memoryList: $("memory-list"),
    memoryCount: $("memory-count"),
    tracePanel: $("trace-panel"),
    // 成本看板（M4-D）。三块 KPI + 一行 provider 明细 + 一行脚注。
    costTodayTokens: $("cost-today-tokens"),
    costWeekFailure: $("cost-week-failure"),
    costWeekCost: $("cost-week-cost"),
    costProviders: $("cost-providers"),
    costHint: $("cost-hint"),
    costWindow: $("cost-window"),
    thinkingStrip: $("thinking-strip"),
    thinkingText: $("thinking-text"),
    companionEmpty: $("companion-empty"),
    companionBody: $("companion-body"),
    companionStage: $("companion-stage"),
    companionRolesRow: $("companion-roles-row"),
    companionRoles: $("companion-roles"),
    companionNeedsRow: $("companion-needs-row"),
    companionNeeds: $("companion-needs"),
    companionRisksRow: $("companion-risks-row"),
    companionRisks: $("companion-risks"),
    diaryCard: $("diary-card"),
    diaryList: $("diary-list"),
    diaryCount: $("diary-count"),
    btnWriteDiary: $("btn-write-diary"),
    diaryHint: $("diary-hint"),
    btnMic: $("btn-mic"),
    voiceStatus: $("voice-status"),
  };

  // ───────────────────────────── 小工具 ─────────────────────────────

  function setConn(text, cls) {
    el.connState.textContent = text;
    el.connState.className = "pill " + cls;
  }

  function showError(msg) {
    const old = document.querySelector(".error-banner");
    if (old) old.remove();
    const div = document.createElement("div");
    div.className = "error-banner";
    div.textContent = msg;
    el.messages.parentElement.insertBefore(div, el.messages.nextSibling);
    setTimeout(() => div.remove(), 6000);
  }

  // ───────────────────────────── 账号（M3-H） ─────────────────────────────

  /** 令牌存在 localStorage。
   *
   * 为什么不存 user_id 一起：user_id 的真相在 localStorage 的 `linger.user_id`，
   * 而「我是谁」的真相在服务端。本地只存令牌，刷新后靠 `/v1/auth/me` 换回账号——
   * 令牌一过期，本地那份 user_id 就该退回匿名，而不是留着一个假的登录态。
   */
  const TOKEN_KEY = "linger.token";

  function getToken() {
    try { return localStorage.getItem(TOKEN_KEY) || ""; } catch (_) { return ""; }
  }

  function setToken(token) {
    try {
      if (token) localStorage.setItem(TOKEN_KEY, token);
      else localStorage.removeItem(TOKEN_KEY);
    } catch (_) { /* 隐私模式：本次会话内仍然可用（state.account 在内存里） */ }
  }

  /** 给所有请求带上令牌。没令牌时不加头——**匿名请求与今天逐字节相同**（判据 5）。 */
  function authHeaders() {
    const token = getToken();
    return token ? { Authorization: `Bearer ${token}` } : {};
  }

  async function api(path, options = {}) {
    // `headers` 放在 ...options **之后**：调用方要能覆盖 Content-Type，
    // 但 Authorization 不能被一次 {...options} 展开整体冲掉。
    const resp = await fetch(path, {
      ...options,
      headers: { "Content-Type": "application/json", ...authHeaders(), ...(options.headers || {}) },
    });
    if (resp.status === 401 && getToken()) {
      // 令牌坏了就当场清掉，别让它一直污染后续请求（每个请求 401 会变成一片报错）
      setToken("");
      state.account = null;
      renderAccount();
    }
    if (!resp.ok) {
      let detail = resp.statusText;
      try {
        const body = await resp.json();
        detail = body.detail || detail;
        if (Array.isArray(detail)) detail = detail.map((d) => d.msg).join("；");
      } catch (_) { /* body 不是 JSON，用 statusText */ }
      throw new Error(`${resp.status} ${detail}`);
    }
    const out = await resp.json();
    return out;
  }

  /** 顶栏的登录态。**未登录时不隐藏任何功能**（判据 8：不弹登录墙）。 */
  function renderAccount() {
    if (state.account) {
      el.accountState.textContent = `已登录 · ${state.account.email}`;
      el.accountState.className = "pill pill-account";
      el.accountState.title = `账号 ${state.account.user_id}`;
      el.btnShowAuth.hidden = true;
      el.btnLogout.hidden = false;
    } else {
      el.accountState.textContent = "未登录 · 数据在本机";
      el.accountState.className = "pill pill-anon";
      el.accountState.title = "登录后换设备也能找回聊过的事";
      el.btnShowAuth.hidden = false;
      el.btnLogout.hidden = true;
    }
  }

  function openAuth(mode) {
    state.authMode = mode || "login";
    el.authPanel.hidden = false;
    el.authError.hidden = true;
    el.authClaimHint.hidden = true;
    const registering = state.authMode === "register";
    el.authTitle.textContent = registering ? "注册" : "登录";
    el.authHint.textContent = registering
      ? "注册后，本机现在这些对话会被认领到账号下——换设备也能接着聊。"
      : "登录后，换设备也能找回你和她们聊过的事。";
    el.authSubmit.textContent = registering ? "注册" : "登录";
    el.btnAuthSwitch.textContent = registering ? "已有账号？登录" : "没有账号？注册";
    el.authPassword.autocomplete = registering ? "new-password" : "current-password";
    el.authEmail.focus();
  }

  function closeAuth() {
    el.authPanel.hidden = true;
    el.authPassword.value = "";
  }

  /** 登录/注册成功之后：令牌落盘、user_id 换成账号的、把匿名数据认领过来。
   *
   * 三件事的顺序不能换：
   *   1. 先落令牌——后面两个请求都要靠它；
   *   2. 再换 user_id——否则认领之后的请求还会带着旧的匿名 id 写数据（会 409）；
   *   3. 最后认领——这一步把「登录之前聊的那些」搬到账号名下。
   *      没有第 3 步，用户会觉得「登录之后我之前聊的没了」，那比记不住更糟。
   */
  async function onAuthed(payload) {
    setToken(payload.token);
    state.account = { user_id: payload.user_id, email: payload.email };
    state.userId = payload.user_id;
    try { localStorage.setItem("linger.user_id", payload.user_id); } catch (_) { /* 忽略 */ }
    renderAccount();
    // 归属是服务端在登录后才盖章的，所以登录之后要把「我的人格」与市场重拉一遍：
    // 上架按钮/「我的条目」都依赖最新的 owner，旧的那份会漏掉刚认领的卡。
    refreshPersonaViews();

    const personas = [];
    if (state.personaId) personas.push(state.personaId);
    if (state.templateId) {
      try {
        const list = await api("/v1/personas");
        for (const p of list.personas || []) if (!personas.includes(p.id)) personas.push(p.id);
      } catch (_) { /* 认领是加分项，拉不到列表就不认领 */ }
    }

    if (personas.length) {
      try {
        const claimed = await api("/v1/auth/claim", {
          method: "POST",
          body: JSON.stringify({ persona_ids: personas }),
        });
        const n = (claimed.claimed?.memories || 0) + (claimed.claimed?.diaries || 0);
        if (n > 0) {
          el.authClaimHint.textContent =
            `已把本机的 ${claimed.claimed.memories} 条记忆、${claimed.claimed.diaries} 篇日记接到账号下。`;
          el.authClaimHint.hidden = false;
          if (state.sessionId) await refreshMemories();
        }
      } catch (err) {
        // 认领失败不该让登录失败：账号已经进去了，数据还在本机匿名 id 下（下次还能认）
        el.authClaimHint.textContent = "登录成功，但本机数据暂时没能认领：" + err.message;
        el.authClaimHint.hidden = false;
      }
    }
  }

  async function submitAuth() {
    const email = el.authEmail.value.trim();
    const password = el.authPassword.value;
    el.authError.hidden = true;
    if (!email || !password) {
      el.authError.textContent = "邮箱和密码都要填";
      el.authError.hidden = false;
      return;
    }
    el.authSubmit.disabled = true;
    try {
      const path = state.authMode === "register" ? "/v1/auth/register" : "/v1/auth/login";
      const payload = await api(path, {
        method: "POST",
        body: JSON.stringify({ email, password }),
      });
      await onAuthed(payload);
      el.authPassword.value = "";
      if (!el.authClaimHint.hidden) {
        // 有认领结果就先让用户看一眼，再关面板
        setTimeout(closeAuth, 1800);
      } else {
        closeAuth();
      }
    } catch (err) {
      el.authError.textContent =
        state.authMode === "register"
          ? "注册失败：" + err.message
          : "邮箱或密码不正确";   // 与后端文案一致：不区分「没这个邮箱」与「密码错」
      el.authError.hidden = false;
    } finally {
      el.authSubmit.disabled = false;
    }
  }

  /** 刷新页面时确认「这个令牌还算数吗」。失败就安静退回匿名，不弹任何东西。 */
  async function restoreAccount() {
    if (!getToken()) { renderAccount(); return; }
    try {
      const me = await api("/v1/auth/me");
      state.account = me;
      state.userId = me.user_id;
      try { localStorage.setItem("linger.user_id", me.user_id); } catch (_) { /* 忽略 */ }
    } catch (_) {
      setToken("");
      state.account = null;
    }
    renderAccount();
  }

  // 情绪标签 → CSS class。与 affect/engine.py::label_for 的六种取值一一对应。
  const MOOD_CLASS = {
    "雀跃": "is-joy",
    "平静欣慰": "is-calm",
    "中性": "is-neutral",
    "犹豫": "is-hesitate",
    "焦躁": "is-anxious",
    "低落": "is-low",
  };
  const MOOD_COLOR = {
    "雀跃": "--mood-joy",
    "平静欣慰": "--mood-calm",
    "中性": "--mood-neutral",
    "犹豫": "--mood-hesitate",
    "焦躁": "--mood-anxious",
    "低落": "--mood-low",
  };

  // ───────────────────────────── SSE 解析 ─────────────────────────────
  //
  // 输入是一串已经解码的文本，输出是若干完整事件。
  // 未闭合的尾部要返回给调用方，等下一个 chunk 拼上。
  function parseSSE(buffer) {
    const events = [];
    let rest = buffer;

    while (true) {
      const idx = rest.indexOf("\n\n");
      if (idx === -1) break;

      const frame = rest.slice(0, idx);
      rest = rest.slice(idx + 2);

      let eventName = null;
      const dataLines = [];
      for (const line of frame.split("\n")) {
        if (line.startsWith("event:")) {
          eventName = line.slice(6).trim();
        } else if (line.startsWith("data:")) {
          dataLines.push(line.slice(5).trim());
        }
        // 忽略注释行（: 开头）与空行
      }
      if (!dataLines.length) continue;

      try {
        const payload = JSON.parse(dataLines.join("\n"));
        events.push({
          type: eventName || payload.type || "message",
          data: payload.data || {},
          raw: payload,
        });
      } catch (err) {
        console.warn("SSE 帧解析失败：", frame.slice(0, 120), err);
      }
    }

    return { events, rest };
  }

  // ───────────────────────────── 消息渲染 ─────────────────────────────

  function hideEmptyState() {
    if (el.emptyState && el.emptyState.parentElement) el.emptyState.remove();
  }

  function addMessage(role, text) {
    hideEmptyState();
    const wrap = document.createElement("div");
    wrap.className = "msg " + (role === "user" ? "msg-user" : "msg-ai");

    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = text;

    // AI 的每一句都带「播放」入口；用户自己的话不需要（那是他自己说的）。
    if (role !== "user") attachPlayButton(bubble, text);

    wrap.appendChild(bubble);
    el.messages.appendChild(wrap);
    el.messages.scrollTop = el.messages.scrollHeight;
    return bubble;
  }

  function beginStreamBubble() {
    hideEmptyState();
    const wrap = document.createElement("div");
    wrap.className = "msg msg-ai";

    const bubble = document.createElement("div");
    bubble.className = "bubble mood-neutral";

    const text = document.createElement("span");
    const caret = document.createElement("span");
    caret.className = "caret";

    bubble.appendChild(text);
    bubble.appendChild(caret);
    wrap.appendChild(bubble);
    el.messages.appendChild(wrap);
    el.messages.scrollTop = el.messages.scrollHeight;

    state.streamEl = { bubble, text, caret };
    state.streamText = "";
    return state.streamEl;
  }

  function appendDelta(chunk) {
    if (!state.streamEl) beginStreamBubble();
    state.streamText += chunk;
    state.streamEl.text.textContent = state.streamText;
    el.messages.scrollTop = el.messages.scrollHeight;
  }

  function endStreamBubble() {
    if (state.streamEl && state.streamEl.caret) {
      state.streamEl.caret.remove();
    }
    state.streamEl = null;
    state.streamText = "";
  }

  // ───────────────────────────── 情绪指示器 ─────────────────────────────

  function setMood(label) {
    const cls = MOOD_CLASS[label] || "is-neutral";
    el.affectBadge.className = "affect-badge " + cls;
    el.affectLabel.textContent = label;

    // 让正在流式输出的气泡也跟着变色（情绪改变可观测输出）
    if (state.streamEl) {
      const moodClass = cls.replace("is-", "mood-");
      state.streamEl.bubble.className = "bubble " + moodClass;
    }

    const colorVar = MOOD_COLOR[label] || "--mood-neutral";
    const color = getComputedStyle(document.documentElement)
      .getPropertyValue(colorVar).trim();
    if (color) {
      el.padP.style.background = color;
      el.padA.style.background = color;
      el.padD.style.background = color;
    }
  }

  function setPad(pad) {
    if (!pad) return;
    // 愉悦 ∈ [-1,1] → 0~100%；唤醒/掌控 ∈ [0,1] → 0~100%
    const pct = (v, lo, hi) => {
      const t = (v - lo) / (hi - lo);
      return Math.max(0, Math.min(100, t * 100));
    };
    el.padP.style.width = pct(pad.pleasure ?? 0, -1, 1) + "%";
    el.padA.style.width = pct(pad.arousal ?? 0, 0, 1) + "%";
    el.padD.style.width = pct(pad.dominance ?? 0, 0, 1) + "%";
  }

  // ───────────────────────────── 记忆面板 ─────────────────────────────

  const LAYER_NAME = { 2: "L2 情景", 3: "L3 语义", 4: "L4 关系", 5: "L5 程序" };
  const SOURCE_NAME = {
    user_said: "他亲述",
    ai_inferred: "我的推断",
    system: "系统记录",
    user_corrected: "他更正过",
  };

  function renderMemories(memories) {
    el.memoryCount.textContent = String(memories.length);

    if (!memories.length) {
      el.memoryList.innerHTML =
        '<p class="memory-empty">还没有记住什么。多说几件具体的事——<br>' +
        '「我妈妈下周要去复查」比「我最近有点累」更容易被记住。</p>';
      return;
    }

    el.memoryList.innerHTML = "";
    for (const m of memories) {
      const item = document.createElement("div");
      item.className = "memory-item";

      const tag = document.createElement("span");
      tag.className = "memory-tag" + (m.source_type === "ai_inferred" ? " is-inferred" : "");
      tag.textContent =
        `${LAYER_NAME[m.layer] || "L" + m.layer} · ${SOURCE_NAME[m.source_type] || m.source_type}`;

      // 正文区：行内编辑时会临时替换成 textarea
      const body = document.createElement("div");
      body.className = "memory-body";
      body.textContent = m.content;

      // 操作栏：编辑 / 历史（M2-C）
      const actions = document.createElement("div");
      actions.className = "memory-actions";
      const btnEdit = document.createElement("button");
      btnEdit.type = "button";
      btnEdit.className = "memory-btn";
      btnEdit.textContent = "编辑";
      const btnHist = document.createElement("button");
      btnHist.type = "button";
      btnHist.className = "memory-btn";
      btnHist.textContent = "历史";
      actions.appendChild(btnEdit);
      actions.appendChild(btnHist);

      item.appendChild(tag);
      item.appendChild(body);
      item.appendChild(actions);
      el.memoryList.appendChild(item);

      // 行内编辑：点「编辑」→ 正文区变 textarea + 保存/取消
      btnEdit.addEventListener("click", () => startEditMemory(item, body, m));
      // 历史：展开 before/after 差异
      btnHist.addEventListener("click", async () => {
        const existing = item.querySelector(".memory-edits");
        if (existing) {
          existing.remove();
          return;
        }
        const histBox = document.createElement("div");
        histBox.className = "memory-edits";
        histBox.textContent = "加载中…";
        item.appendChild(histBox);
        try {
          const data = await api(`/v1/memories/${m.id}/edits`);
          if (!data.edits || !data.edits.length) {
            histBox.textContent = "没有编辑记录。";
            return;
          }
          histBox.innerHTML = "";
          for (const e of data.edits) {
            const row = document.createElement("div");
            row.className = "memory-edit-row";
            row.innerHTML =
              `<span class="memory-edit-old">${escapeHtml(e.old_content)}</span>` +
              `<span class="memory-edit-arrow">→</span>` +
              `<span class="memory-edit-new">${escapeHtml(e.new_content)}</span>` +
              `<span class="memory-edit-time">${escapeHtml((e.edited_at || "").slice(0, 16).replace("T", " "))}</span>`;
            histBox.appendChild(row);
          }
        } catch (err) {
          histBox.textContent = "读取编辑历史失败：" + (err.message || err);
        }
      });
    }
  }

  async function startEditMemory(item, body, m) {
    // 把正文区替换成 textarea + 保存/取消
    const textarea = document.createElement("textarea");
    textarea.className = "memory-edit-input";
    textarea.value = m.content;
    textarea.rows = 3;

    const save = document.createElement("button");
    save.type = "button";
    save.className = "memory-btn is-primary";
    save.textContent = "保存";

    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "memory-btn";
    cancel.textContent = "取消";

    const editor = document.createElement("div");
    editor.className = "memory-edit-editor";
    editor.appendChild(textarea);
    const btns = document.createElement("div");
    btns.className = "memory-edit-btns";
    btns.appendChild(save);
    btns.appendChild(cancel);
    editor.appendChild(btns);

    body.replaceWith(editor);

    const cleanup = () => {
      body.textContent = m.content;
      editor.replaceWith(body);
    };

    cancel.addEventListener("click", cleanup);

    save.addEventListener("click", async () => {
      const content = textarea.value;
      if (!content.trim()) {
        textarea.classList.add("is-error");
        return;
      }
      save.disabled = true;
      save.textContent = "保存中…";
      try {
        await api(`/v1/memories/${m.id}`, {
          method: "PATCH",
          body: JSON.stringify({ content: content.trim() }),
        });
        await refreshMemories();
      } catch (err) {
        save.disabled = false;
        save.textContent = "保存";
        textarea.classList.add("is-error");
        textarea.title = err.message || String(err);
      }
    });
  }

  async function refreshMemories() {
    if (!state.sessionId) return;
    try {
      const data = await api(`/v1/sessions/${state.sessionId}/memories`);
      renderMemories(data.memories || []);
    } catch (err) {
      console.warn("刷新记忆失败", err);
    }
  }

  // ───────────────────────────── 陪伴档案（M2-B） ─────────────────────────────

  function renderTags(container, row, items, cls) {
    const list = items || [];
    if (!list.length) {
      row.hidden = true;
      container.innerHTML = "";
      return;
    }
    row.hidden = false;
    container.innerHTML = list
      .map((t) => `<span class="tag ${cls || ""}">${escapeHtml(String(t))}</span>`)
      .join("");
  }

  function renderCompanion(profile) {
    const p = profile || {};
    const has = Boolean(
      p.life_stage || (p.roles || []).length || (p.needs || []).length || (p.risks || []).length
    );
    el.companionEmpty.hidden = has;
    el.companionBody.hidden = !has;
    if (!has) return;

    el.companionStage.value = p.life_stage || "";
    renderTags(el.companionRoles, el.companionRolesRow, p.roles, "");
    renderTags(el.companionNeeds, el.companionNeedsRow, p.needs, "is-need");
    renderTags(el.companionRisks, el.companionRisksRow, p.risks, "is-risk");
  }

  async function refreshCompanion() {
    if (!state.personaId) return;
    const userId = state.userId || "u_web";
    try {
      const p = await api(
        `/v1/companion/${encodeURIComponent(state.personaId)}/${encodeURIComponent(userId)}`
      );
      renderCompanion(p);
    } catch (err) {
      // 档案是可选状态：拉不到就显示引导文案，不打断主流程
      console.warn("刷新陪伴档案失败", err);
      renderCompanion(null);
    }
  }

  async function saveCompanionStage(lifeStage) {
    if (!state.personaId) return;
    const userId = state.userId || "u_web";
    try {
      const p = await api(
        `/v1/companion/${encodeURIComponent(state.personaId)}/${encodeURIComponent(userId)}`,
        { method: "PATCH", body: JSON.stringify({ life_stage: lifeStage }) }
      );
      renderCompanion(p);
    } catch (err) {
      showError("保存陪伴档案失败：" + (err && err.message ? err.message : "未知错误"));
      await refreshCompanion();
    }
  }

  // ───────────────────────────── 她的日记（M3-D） ─────────────────────────────
  //
  // 日记是**后处理**，不在对话链路里：读当天已有的 turn / 记忆 / 情绪，写一篇。
  // 所以它有自己的按钮，不和「发消息」抢时机——用户想看她写的时候再让她写。
  //
  // 一条纪律：一天只有一篇。重复点「写下今天」是覆盖当天那篇（后端按
  // (persona_id, user_id, date) upsert），所以这里不需要「已写过就别写」的判断。

  function renderDiary(entries) {
    el.diaryCount.textContent = String(entries.length);

    if (!entries.length) {
      el.diaryList.innerHTML =
        '<p class="diary-empty">还没有日记。聊过之后，让她写下今天。</p>';
      return;
    }

    el.diaryList.innerHTML = "";
    for (const d of entries) {
      const item = document.createElement("div");
      item.className = "diary-item";
      if (state.diaryHighlightId && d.id === state.diaryHighlightId) {
        item.classList.add("is-new");
      }
      // 日期 · 心情标签（心情为空就不显示，别留一个空胶囊）
      const head = document.createElement("div");
      head.className = "diary-head";
      head.innerHTML =
        `<span class="diary-date">${escapeHtml(d.date || "")}</span>` +
        (d.mood_label
          ? `<span class="diary-mood">${escapeHtml(d.mood_label)}</span>`
          : "");
      const body = document.createElement("div");
      body.className = "diary-content";
      body.textContent = d.content || "";

      item.appendChild(head);
      item.appendChild(body);

      // 当天最值得记的 1~3 条：她「记住」了什么，摆在日记下面
      if ((d.highlight_memories || []).length) {
        const hl = document.createElement("div");
        hl.className = "diary-highlights";
        for (const m of d.highlight_memories) {
          const chip = document.createElement("span");
          chip.className = "diary-highlight";
          chip.textContent = m;
          hl.appendChild(chip);
        }
        item.appendChild(hl);
      }

      el.diaryList.appendChild(item);
    }
  }

  async function refreshDiary() {
    if (!state.personaId) return;
    try {
      const data = await api(
        `/v1/personas/${encodeURIComponent(state.personaId)}/diary?user_id=` +
          encodeURIComponent(state.userId)
      );
      renderDiary(data.diary || []);
    } catch (err) {
      // 日记是可选状态：拉不到就显示空态，不打断主流程
      console.warn("刷新日记失败", err);
      renderDiary([]);
    }
  }

  async function writeDiary() {
    if (!state.personaId) return;
    state.diaryHighlightId = null;
    el.btnWriteDiary.disabled = true;
    el.btnWriteDiary.textContent = "正在写…";
    el.diaryHint.textContent = "";
    try {
      const data = await api(
        `/v1/personas/${encodeURIComponent(state.personaId)}/diary/generate`,
        {
          method: "POST",
          body: JSON.stringify({ user_id: state.userId }),
        }
      );
      // 新写的这篇要高亮：用户刚按了按钮，得看见「多出来的东西是什么」
      state.diaryHighlightId = (data.entry && data.entry.id) || null;
      await refreshDiary();
      // 流水账风险要告诉用户，但不用红色报错——日记已经写出来了
      const warns = data.warnings || [];
      el.diaryHint.textContent = warns.length ? warns[0] : "";
    } catch (err) {
      // api() 抛的是「422 今天还没有对话，写不出日记」这种带状态码的串。
      // 422 是「现在写不出来」，不是服务坏了，用提示而不是错误横幅。
      const msg = String((err && err.message) || err);
      if (msg.startsWith("422")) {
        el.diaryHint.textContent = msg.replace(/^422\s*/, "") || "今天还没有对话，写不出日记。";
      } else {
        showError("写日记失败：" + msg);
      }
    } finally {
      el.btnWriteDiary.disabled = false;
      el.btnWriteDiary.textContent = "写下今天";
    }
  }

  el.btnWriteDiary.addEventListener("click", writeDiary);

  // ────────────────────────── 成本看板（M4-D） ──────────────────────────

  /** 成本面板要渲染的全部数字，都来自 `/v1/costs` 的响应。
   *
   * 这里**故意不做除法**：失败率、token 合计都是后端算好的。前端每多算一次，
   * 就是多一个「看板和接口对不上」的机会，而那种 bug 最难判谁对。
   *
   * 唯一的判断是「是不是 null」，因为 null 与 0 在这块面板上是两件事：
   *   null → 「未知」：我们不知道它花了多少（单价不在表里 / 没有调用）
   *   0    → 「$0」：真的没花钱（本地模型，写明的 0 价）
   * 把「不知道」渲染成 0，就是告诉用户「很省钱」——那是比空白更糟的谎言。
   */
  const COST_UNKNOWN = "未知";

  function fmtTokens(v) {
    if (v === null || v === undefined) return COST_UNKNOWN;
    const n = Number(v);
    if (!isFinite(n)) return COST_UNKNOWN;
    if (n >= 10000) return (n / 1000).toFixed(1) + "k";
    return String(n);
  }

  function fmtCost(v) {
    if (v === null || v === undefined) return COST_UNKNOWN;
    const n = Number(v);
    if (!isFinite(n)) return COST_UNKNOWN;
    // 小额也要显示得出来：0.000123 用 toFixed(2) 会变成 $0.00，
    // 那正好是「看起来免费」，正是要避免的那种读法。
    if (n === 0) return "$0";
    if (n < 0.01) return "$" + n.toFixed(6).replace(/0+$/, "");
    return "$" + n.toFixed(2);
  }

  function fmtRate(v) {
    if (v === null || v === undefined) return COST_UNKNOWN;
    const n = Number(v);
    if (!isFinite(n)) return COST_UNKNOWN;
    return (n * 100).toFixed(1) + "%";
  }

  function renderCosts() {
    const data = state.costs;
    if (!data) {
      if (el.costTodayTokens) el.costTodayTokens.textContent = COST_UNKNOWN;
      return;
    }
    const today = data.today || {};
    const week = data.last_7_days || {};
    const totals = data.totals || {};

    el.costTodayTokens.textContent = fmtTokens(today.tokens_total);
    el.costWeekFailure.textContent = fmtRate(week.failure_rate);
    el.costWeekCost.textContent = fmtCost(week.cost);
    if (el.costWindow) {
      const days = (data.window && data.window.days) || 7;
      el.costWindow.textContent = "近 " + days + " 日";
    }

    const providers = data.by_provider || [];
    if (!providers.length) {
      el.costProviders.innerHTML =
        '<p class="muted small">这段时间还没有调用记录。</p>';
    } else {
      el.costProviders.innerHTML = "";
      for (const p of providers) {
        const row = document.createElement("div");
        row.className = "cost-provider-row";
        row.dataset.provider = String(p.provider || "");
        // 每一次调用都要能看到「成功几次、失败几次」：只显示成功数，
        // 重试风暴在面板上就完全隐形了。
        // 用 textContent 而不是 innerHTML：这里每一段都是**文本**，
        // 拼 HTML 只会白白多出一条 XSS 通道（见上面的 escapeHtml 讨论）。
        row.textContent =
          String(p.provider || "") + " · " +
          String(p.calls) + " 次 · " +
          fmtTokens(p.tokens_total) + " tok · 失败 " +
          String(p.failed) + " · " + fmtCost(p.cost);
        el.costProviders.appendChild(row);
      }
    }

    // 脚注说清两件事：这钱是怎么算的（单价快照），以及有没有算不出来的。
    const unknown = (totals.unknown_models || []).map((m) => m.provider + ":" + m.model);
    const bits = [];
    if (data.pricing_snapshot) bits.push("单价快照 " + data.pricing_snapshot);
    if (unknown.length) bits.push("单价未知：" + unknown.join("、") + "（显示为「" + COST_UNKNOWN + "」）");
    if (totals.calls) bits.push("共 " + totals.calls + " 次调用");
    el.costHint.textContent = bits.join(" · ");
  }

  async function loadCosts() {
    try {
      const data = await api("/v1/costs?days=7");
      state.costs = data;
      renderCosts();
    } catch (err) {
      // 看板挂了不该影响聊天：只在面板里说一句，别的地方一个字不动。
      if (el.costProviders) {
        el.costProviders.innerHTML =
          '<p class="muted small">成本取不到：' + escapeHtml(err.message || String(err)) + "</p>";
      }
    }
  }

  // ───────────────────────────── trace 面板 ─────────────────────────────

  const BLOCK_COLOR = {
    persona: "#8f6b45",
    constraints: "#b08a5e",
    ai_boundaries: "#c9a878",
    relation: "#6b8e7a",
    social: "#7a93a8",
    scenario: "#a8859e",
    affect: "#c2761b",
    memory: "#5d7fa3",
    history: "#b9b1a5",
  };

  function renderTrace(turnId, doneState) {
    el.tracePanel.innerHTML =
      '<p class="muted small">trace 加载中…</p>';

    api(`/v1/traces/${turnId}`)
      .then((trace) => {
        const parts = [];

        // 漏斗：候选 → 过闸门 → 注入
        parts.push('<div class="trace-funnel">');
        parts.push(`<div class="funnel-step"><b>${trace.candidates ?? 0}</b>候选</div>`);
        parts.push(`<div class="funnel-step"><b>${trace.passed_gate ?? 0}</b>过闸门</div>`);
        parts.push(`<div class="funnel-step"><b>${(trace.selected || []).length}</b>注入</div>`);
        parts.push("</div>");

        // 注入的记忆明细
        if ((trace.selected || []).length) {
          parts.push('<div style="margin:0 0 10px">');
          for (const s of trace.selected) {
            parts.push(
              `<div class="trace-row"><span class="k">L${s.layer} · ${(s.score ?? 0).toFixed(2)}</span>` +
              `<span class="v" style="max-width:62%;text-align:right">${escapeHtml(
                (s.text || "").slice(0, 26)
              )}</span></div>`
            );
          }
          parts.push("</div>");
        }

        // token 分布
        const tokens = trace.tokens_by_block || {};
        const entries = Object.entries(tokens).filter(([, v]) => v > 0);
        const total = entries.reduce((a, [, v]) => a + v, 0) || 1;

        parts.push('<div class="tokens-bar">');
        for (const [name, v] of entries) {
          const color = BLOCK_COLOR[name] || "#c9c2b6";
          parts.push(
            `<i style="width:${((v / total) * 100).toFixed(2)}%;background:${color}"></i>`
          );
        }
        parts.push("</div>");

        parts.push('<div class="legend">');
        for (const [name, v] of entries) {
          const color = BLOCK_COLOR[name] || "#c9c2b6";
          parts.push(
            `<span><em style="background:${color}"></em>${name} ${v}</span>`
          );
        }
        parts.push("</div>");

        // 情绪与关系摘要
        if (doneState) {
          parts.push('<div style="margin-top:10px">');
          const a = doneState.affect || {};
          parts.push(
            `<div class="trace-row"><span class="k">情绪</span><span class="v">${
              a.label || "—"
            }（P ${fmt(a.pleasure)} / A ${fmt(a.arousal)} / D ${fmt(a.dominance)}）</span></div>`
          );
          const r = doneState.relation || {};
          parts.push(
            `<div class="trace-row"><span class="k">关系</span><span class="v">level ${r.level ?? "—"} · 亲密 ${fmt(r.intimacy, 1)}</span></div>`
          );
          if (doneState.red_flags && doneState.red_flags.length) {
            parts.push(
              `<div class="trace-row"><span class="k">红灯</span><span class="v">${doneState.red_flags.length} 处</span></div>`
            );
          }
          if (doneState.toxic_signals && doneState.toxic_signals.length) {
            const cats = doneState.toxic_signals.map((t) => t.category).join("、");
            parts.push(
              `<div class="trace-row"><span class="k">识别到</span><span class="v">${escapeHtml(cats)}</span></div>`
            );
          }
          if (doneState.roles && doneState.roles.length) {
            const roleNames = doneState.roles.map((r) => r.name).join("、");
            parts.push(
              `<div class="trace-row"><span class="k">角色</span><span class="v">${escapeHtml(roleNames)}</span></div>`
            );
          }
          parts.push("</div>");
        }

        el.tracePanel.innerHTML = parts.join("");
      })
      .catch((err) => {
        el.tracePanel.innerHTML =
          `<p class="memory-empty">trace 取不到：${escapeHtml(err.message)}</p>`;
      });
  }

  function fmt(v, digits = 2) {
    return typeof v === "number" ? v.toFixed(digits) : "—";
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  // ───────────────────────────── 关系 ─────────────────────────────

  function setRelation(level, intimacy) {
    if (typeof level === "number") el.relLevel.textContent = String(level);
    if (typeof intimacy === "number") el.relIntimacy.textContent = "亲密 " + intimacy.toFixed(1);
  }

  // ───────────────────────────── 人格与会话 ─────────────────────────────

  async function loadTemplates() {
    try {
      const templates = await api("/v1/personas/templates");
      el.personaList.innerHTML = "";

      if (!templates.length) {
        el.personaList.innerHTML =
          '<p class="memory-empty">没有可用的模板。</p>';
        return;
      }

      for (const t of templates) {
        const btn = document.createElement("button");
        btn.className = "persona-item";
        btn.type = "button";
        btn.dataset.templateId = t.id;
        btn.dataset.name = t.name || t.id;

        btn.innerHTML =
          `<div class="persona-name">${escapeHtml(t.name || t.id)}</div>` +
          `<div class="persona-meta">${escapeHtml(t.archetype || "")}` +
          `${t.age ? " · " + t.age + " 岁" : ""}</div>` +
          (t.sample
            ? `<div class="persona-sample">「${escapeHtml(t.sample)}」</div>`
            : "");

        btn.addEventListener("click", () => {
          state.templateId = t.id;
          state.personaName = t.name || t.id;
          highlight(el.personaList, btn);
          startSession();
        });
        el.personaList.appendChild(btn);
      }
    } catch (err) {
      el.personaList.innerHTML =
        `<p class="memory-empty">模板加载失败：${escapeHtml(err.message)}</p>`;
      setConn("服务未连上", "pill-error");
    }
  }

  // ───────────────────────── 人格市场（M4-C） ─────────────────────────
  //
  // 三条纪律（对应 issue #39 的判据 1 / 3 / 4）：
  //
  // 1. **看不用登录**。公共列表与详情是匿名可读的端点，这里就不给它们加任何
  //    登录前置——不弹浮层、不拦点击、不跳转。未登录时唯一的变化是
  //    「安装」按钮换成「登录后可安装」，一个提示，不是一堵墙。
  // 2. **归属由服务端说，不由前端猜**。人格摘要里带 `owner`
  //    （`provenance.owner`），只有 `owner` 非空且等于 `state.userId` 才是自己的卡。
  //    「列表里的都是我的」这个假设是错的：模板实例化的人格是共享的一行，
  //    可能是别人先认领的。
  // 3. **市场失败不影响聊天**。每条请求各自 try/catch，出错只在市场面板里说，
  //    绝不落到 showError（那条横幅在中栏对话区上方，会把聊天打断的错觉放大）。

  const MARKET_STATUS_TEXT = {
    pending_review: "待审核",
    approved: "已上架",
    rejected: "未通过",
    unlisted: "已下架",
  };

  /** 自己的卡吗？判据 4 的唯一判据，**只此一处**。
   *
   * 未登录（`state.userId` 是匿名 id，`state.account` 为 null）时恒为 false：
   * 匿名期造出来的卡没有归属，后端上架必然 403——前端就不该给它一个入口。
   * 判断用 `state.userId` 而不是 `state.account.user_id`：登录后前者已被换成账号 id
   * （既有行为），两者一致；未登录时只有前者存在。
   */
  function isOwnPersona(p) {
    const me = state.account ? state.account.user_id : "";
    if (!me) return false;
    return Boolean(p && p.owner) && p.owner === me && p.owner === state.userId;
  }

  /** 创建人格时要带上的归属声明（判据 3）。
   *
   * 登录时带 `{owner: <账号 id>}`；**未登录时返回空对象**——
   * 匿名卡没有主人是后端既定语义（`provenance.owner` 缺省 = 归属不明 = 不可上架），
   * 前端不该替它编一个 id 出来。服务端会核对：声明的 id 不是当前账号就 403。
   *
   * 为什么不塞进 `persona.provenance`：人格卡是「她是谁」的描述，
   * 归属是「她归谁」的事实。前者用户能改，后者只能由服务端按账号盖章。
   */
  function ownerClaim() {
    const me = state.account ? state.account.user_id : "";
    return me ? { owner: me } : {};
  }

  /** 这张卡能不能上架，以及为什么不能。返回 `{ok, reason}`。
   *
   * 三个前置各有一句人话：不是自己的 / 匿名期的卡 / 蒸馏产物（红线）。
   * 蒸馏那条不是「先点了吃 403」——提前说清楚，用户不会以为自己被莫名拒绝。
   */
  function listingGate(p) {
    if (!p) return { ok: false, reason: "人格不存在" };
    if (String((p.provenance || {}).type || "").trim().toLowerCase() === "cloned") {
      return { ok: false, reason: "蒸馏产物不可上架" };
    }
    if (!state.account) return { ok: false, reason: "登录后可上架" };
    if (!isOwnPersona(p)) return { ok: false, reason: "只能上架自己名下的人格" };
    return { ok: true, reason: "" };
  }

  function personaById(id) {
    for (const p of state.personaCache || []) if (p.id === id) return p;
    return null;
  }

  async function loadMarket() {
    try {
      const data = await api("/v1/market/listings");
      state.marketListings = data.listings || [];
      renderMarketList();
    } catch (err) {
      el.marketList.innerHTML =
        `<p class="muted small">市场列表取不到：${escapeHtml(err.message || String(err))}</p>`;
    }
  }

  async function loadMarketMine() {
    if (!state.account) {
      state.marketMine = [];
      renderMarketMine();
      return;
    }
    try {
      const data = await api("/v1/market/mine");
      state.marketMine = data.listings || [];
    } catch (err) {
      state.marketMine = [];
      el.marketMineList.innerHTML =
        `<p class="muted small">我的条目取不到：${escapeHtml(err.message || String(err))}</p>`;
      el.marketMineCount.textContent = "0";
      return;
    }
    renderMarketMine();
  }

  /** 登录态变化后把「我的人格」+ 市场两张单子重拉一遍。
   *
   * 为什么合成一个函数：这三处数据是同一件事的三个视角（我的卡 / 公共条目 / 我的条目），
   * 分开调用迟早会漏一处，于是登录之后只有一半界面知道自己是谁。
   */
  async function refreshPersonaViews() {
    await loadMyPersonas();
    await loadMarket();
    await loadMarketMine();
  }

  function renderMarketList() {
    const items = state.marketListings;
    el.marketCount.textContent = String(items.length);

    if (!items.length) {
      el.marketList.innerHTML =
        '<p class="muted small">市场里还没有过审的人格。造一个，上架试试。</p>';
      return;
    }

    el.marketList.innerHTML = "";
    for (const it of items) {
      const row = document.createElement("div");
      row.className = "market-item";
      row.dataset.listingId = it.id;

      // 主体是按钮 = 看详情。**未登录也能点**（判据 1）
      const openBtn = document.createElement("button");
      openBtn.type = "button";
      openBtn.className = "market-open";
      const meta = [it.summary, it.author_id ? `by ${it.author_id}` : ""]
        .filter(Boolean).join(" · ");
      openBtn.innerHTML =
        `<div class="market-name">${escapeHtml(it.persona_name || it.title || it.id)}</div>` +
        (meta ? `<div class="market-meta">${escapeHtml(meta)}</div>` : "");
      openBtn.addEventListener("click", () => openMarketDetail(it.id));

      const installBtn = document.createElement("button");
      installBtn.type = "button";
      installBtn.className = "market-install";
      installBtn.textContent = state.account ? "安装" : "登录后可安装";
      installBtn.addEventListener("click", () => installListing(it, installBtn));

      row.appendChild(openBtn);
      row.appendChild(installBtn);
      el.marketList.appendChild(row);
    }
  }

  function renderMarketMine() {
    const items = state.marketMine;
    el.marketMineCount.textContent = String(items.length);

    if (!items.length) {
      el.marketMineList.innerHTML =
        `<p class="muted small">${state.account
          ? "还没有上架的条目。在「我的人格」里点「上架」。"
          : "登录后，这里会列出你上架的条目和审核状态。"}</p>`;
      return;
    }

    el.marketMineList.innerHTML = "";
    for (const it of items) {
      const row = document.createElement("div");
      row.className = "market-mine-item";
      row.dataset.listingId = it.id;
      const status = String(it.status || "");
      row.innerHTML =
        `<span class="grow">${escapeHtml(it.title || it.persona_name || it.id)}</span>` +
        `<span class="market-status is-${escapeHtml(status.replace("_review", ""))}">` +
        `${escapeHtml(MARKET_STATUS_TEXT[status] || status)}</span>`;

      // 在架的才能下架；待审的取消上架后端不支持（状态机只允许 approved → unlisted），
      // 所以这里不给按钮——给一个点了必然 409 的按钮，比不给更糟。
      if (status === "approved") {
        const unlistBtn = document.createElement("button");
        unlistBtn.type = "button";
        unlistBtn.className = "market-install";
        unlistBtn.textContent = "下架";
        unlistBtn.addEventListener("click", () => unlistListing(it, unlistBtn));
        row.appendChild(unlistBtn);
      }
      el.marketMineList.appendChild(row);
    }
  }

  /** 详情：公共端点，**未登录也读得到**（判据 1）。 */
  async function openMarketDetail(listingId) {
    state.marketOpenId = listingId;
    el.marketDetail.hidden = false;
    el.btnMarketClose.hidden = false;
    el.marketDetail.innerHTML = '<p class="muted small">读取中…</p>';
    try {
      const it = await api(`/v1/market/listings/${encodeURIComponent(listingId)}`);
      const persona = it.persona || {};
      const meta = persona.meta || {};
      const lines = (persona.samples || persona.sample_lines || []).slice(0, 6);
      el.marketDetail.innerHTML =
        `<h4>${escapeHtml(it.persona_name || it.title || it.id)}</h4>` +
        `<div>${escapeHtml([meta.identity, meta.archetype].filter(Boolean).join(" · "))}</div>` +
        `<div class="market-author">作者 ${escapeHtml(it.author_id || "—")}</div>` +
        (lines.length
          ? '<div class="market-lines">' +
            lines.map((l) => `「${escapeHtml(l)}」`).join("<br>") + "</div>"
          : "");
    } catch (err) {
      el.marketDetail.innerHTML =
        `<p class="muted small">详情取不到：${escapeHtml(err.message || String(err))}</p>`;
    }
  }

  function closeMarketDetail() {
    state.marketOpenId = null;
    el.marketDetail.hidden = true;
    el.btnMarketClose.hidden = true;
  }

  async function installListing(item, btn) {
    if (!state.account) {
      // 不弹登录墙：只在市场面板里说一句，用户看不看得到市场完全不受影响。
      el.marketHint.textContent = "安装需要登录——顶栏「登录」，本机的对话会一起带过去。";
      return;
    }
    btn.disabled = true;
    btn.textContent = "安装中…";
    try {
      const res = await api(
        `/v1/market/listings/${encodeURIComponent(item.id)}/install`,
        { method: "POST" }
      );
      btn.textContent = "已安装";
      el.marketHint.textContent = res.reused
        ? `「${res.persona.name}」你之前装过，用的还是那一份。`
        : `装好了：「${res.persona.name}」现在在你的人格列表里。`;
      // 装完必须出现在「我的人格」里，否则用户会以为没成功
      await loadMyPersonas();
      await loadMarketMine();
    } catch (err) {
      btn.disabled = false;
      btn.textContent = "安装";
      el.marketHint.textContent = "安装失败：" + (err.message || String(err));
    }
  }

  /** 上架自己的一个人格。
   *
   * 归属（`owner`）不在这里传：它已经落在人格卡的 `provenance.owner` 上
   * （创建时由服务端按当前账号写入）。上架只报 persona_id，
   * 「这张卡归谁」由服务端拿库里的卡去判——前端说自己是谁不算数。
   */
  async function listPersona(p, btn) {
    const gate = listingGate(p);
    if (!gate.ok) {
      el.marketHint.textContent = gate.reason;
      return;
    }
    if (btn) {
      btn.disabled = true;
      btn.textContent = "上架中…";
    }
    try {
      const res = await api("/v1/market/listings", {
        method: "POST",
        body: JSON.stringify({ persona_id: p.id }),
      });
      el.marketHint.textContent = res.created
        ? `「${res.title}」已送审，通过后会出现在市场里。`
        : `「${res.title}」已经在市场里了（${MARKET_STATUS_TEXT[res.status] || res.status}）。`;
      await loadMarketMine();
      await loadMarket();
    } catch (err) {
      el.marketHint.textContent = "上架失败：" + (err.message || String(err));
      if (btn) {
        btn.disabled = false;
        btn.textContent = "上架";
      }
    }
  }

  async function unlistListing(item, btn) {
    btn.disabled = true;
    btn.textContent = "下架中…";
    try {
      await api(`/v1/market/listings/${encodeURIComponent(item.id)}`, { method: "DELETE" });
      el.marketHint.textContent = `「${item.title || item.id}」已下架，已装过的人手里的副本不受影响。`;
      await loadMarketMine();
      await loadMarket();
    } catch (err) {
      btn.disabled = false;
      btn.textContent = "下架";
      el.marketHint.textContent = "下架失败：" + (err.message || String(err));
    }
  }

  el.btnMarketRefresh.addEventListener("click", async () => {
    el.marketHint.textContent = "";
    await loadMarket();
    await loadMarketMine();
  });
  el.btnMarketClose.addEventListener("click", closeMarketDetail);

  // ───────────────────── 我的人格：列表 + 删除（M3-C） ─────────────────────
  //
  // 与上面的模板列表是两张不同的东西：
  //   模板 = 「可以去找谁」（推荐位，2 个，点一下实例化）
  //   我的人格 = 「已经在的人」（清单，含模板实例化的 + 一句话生成的）
  // 所以做成同区块的第二个列表，不是 tab ——「造出来的东西在哪」不该再藏在二级入口。
  //
  // 删除是软删（后端只把 status 标成 deleted，不动会话与记忆），
  // 但**语义**是「这个人格不要了」：当前正在聊的那一个被删掉，
  // 就必须清空对话区并锁住输入，绝不能让用户对着一个已经不存在的人格继续说话。

  async function loadMyPersonas() {
    try {
      const data = await api("/v1/personas");
      state.personaCache = data.personas || [];
      renderMyPersonas(state.personaCache);
    } catch (err) {
      el.myPersonaList.innerHTML =
        `<p class="my-persona-empty">人格列表加载失败：${escapeHtml(err.message)}</p>`;
    }
  }

  function renderMyPersonas(personas) {
    el.myPersonaCount.textContent = String(personas.length);
    el.myPersonaList.innerHTML = "";

    if (!personas.length) {
      el.myPersonaList.innerHTML =
        '<p class="my-persona-empty">还没有自己造的角色——上面用一句话造一个。</p>';
      return;
    }

    for (const p of personas) {
      const item = document.createElement("div");
      item.className = "my-persona-item";
      item.dataset.personaId = p.id;

      // 主体是按钮：点击直接开会话（openSession 已在 M3-A 抽出，两条入口共用）
      const openBtn = document.createElement("button");
      openBtn.type = "button";
      openBtn.className = "my-persona-open";
      // identity / archetype 有什么显示什么，都没有就不留空行
      const sub = [p.identity, p.archetype].filter(Boolean).join(" · ");
      openBtn.innerHTML =
        `<div class="my-persona-name">${escapeHtml(p.name || p.id)}</div>` +
        (sub ? `<div class="my-persona-meta">${escapeHtml(sub)}</div>` : "");
      openBtn.addEventListener("click", async () => {
        if (state.busy) return;
        try {
          await openSession(p.id, p.name || p.id);
          highlight(el.myPersonaList, item);
        } catch (err) {
          showError("打开人格失败：" + (err.message || err));
        }
      });

      const delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.className = "my-persona-delete";
      delBtn.title = "删除这个人格";
      delBtn.textContent = "删除";
      delBtn.addEventListener("click", () => deletePersona(p, delBtn));

      item.appendChild(openBtn);
      // 上架入口**只在自己的卡上出现**（判据 4）。
      // 别人先认领了的模板行、匿名期造出来的无主卡，这里连按钮都不生——
      // 不是渲染了再置灰，是根本不进 DOM：前者看不见就点不到，
      // 后者（置灰）会让用户反复点一个永远不会成的事。
      const gate = listingGate(p);
      if (gate.ok) {
        const listBtn = document.createElement("button");
        listBtn.type = "button";
        listBtn.className = "my-persona-list-entry";
        listBtn.dataset.action = "list";
        listBtn.title = "上架到人格市场";
        listBtn.textContent = "上架";
        listBtn.addEventListener("click", () => listPersona(p, listBtn));
        item.appendChild(listBtn);
      }
      item.appendChild(delBtn);
      el.myPersonaList.appendChild(item);

      if (p.id === state.personaId) item.classList.add("is-active");
    }
  }

  async function deletePersona(persona, btn) {
    const name = persona.name || persona.id;
    // 二次确认：删除不可从界面撤销（软删只保住了数据，没保住入口）
    if (!confirm(`删除「${name}」？聊天记录会保留，但这个角色不会再出现在列表里。`)) return;

    btn.disabled = true;
    btn.textContent = "删除中…";
    try {
      await api(`/v1/personas/${encodeURIComponent(persona.id)}`, { method: "DELETE" });
      // 删的就是当前正在聊的人格 → 不能假装还能继续
      if (persona.id === state.personaId) lockAfterPersonaDeleted(name);
      await loadMyPersonas();
    } catch (err) {
      btn.disabled = false;
      btn.textContent = "删除";
      showError("删除失败：" + (err.message || err));
      // 404（已被删过）也刷新一次：列表与库对齐，别继续显示一个不存在的人格
      await loadMyPersonas();
    }
  }

  /** 当前人格被删之后：清空对话区、锁住输入、明确告诉用户下一步。
   *
   * 为什么不保留会话留在屏幕上「只读」：用户会以为还能接着说。
   * 删人格的语义是「这个人格不要了」，那就把输入关掉，让他重新选。
   */
  function lockAfterPersonaDeleted(name) {
    state.sessionId = null;
    state.personaId = null;
    state.lastTurnId = null;
    state.streamEl = null;
    state.streamText = "";

    el.messages.innerHTML =
      '<div class="empty-state" id="empty-state">' +
      '<p class="empty-title">人格已删除</p>' +
      `<p class="muted small">「${escapeHtml(name)}」已经不在了。在左边重新选一个，或者再造一个。</p>` +
      "</div>";
    el.emptyState = $("empty-state");
    el.input.value = "";
    el.input.disabled = true;
    el.send.disabled = true;
    renderMemories([]);
    renderCompanion(null);
    state.diaryHighlightId = null;
    el.btnWriteDiary.disabled = true;
    el.diaryHint.textContent = "";
    el.diaryList.innerHTML =
      '<p class="diary-empty">还没有日记。聊过之后，让她写下今天。</p>';
    el.diaryCount.textContent = "0";
    el.tracePanel.innerHTML =
      '<p class="muted small">聊一句，这里会显示这一轮的召回与 token 分布。</p>';
    el.affectBadge.className = "affect-badge is-neutral";
    el.affectLabel.textContent = "中性";
    el.relLevel.textContent = "—";
    el.relIntimacy.textContent = "";
    setConn("人格已删除，重新选一个", "pill-idle");
  }

  // ───────────────────── 一句话造一个人格（M3-A） ─────────────────────

  function toggleCreatePanel() {
    const opening = el.personaCreatePanel.hidden;
    el.personaCreatePanel.hidden = !opening;
    el.btnShowCreate.textContent = opening ? "− 收起" : "+ 用一句话造一个";
    if (opening) el.personaDesc.focus();
  }

  async function generateDraft() {
    const desc = el.personaDesc.value.trim();
    if (desc.length < 2) {
      el.personaGenerateHint.textContent = "至少写两个字。";
      return;
    }
    el.btnGenerate.disabled = true;
    el.personaGenerateHint.textContent = "正在想这个人长什么样…";
    el.personaDraft.hidden = true;
    try {
      const draft = await api("/v1/personas/generate", {
        method: "POST",
        body: JSON.stringify({ description: desc }),
      });
      renderDraft(draft);
      el.personaGenerateHint.textContent = "";
    } catch (err) {
      el.personaGenerateHint.textContent = "生成失败：" + (err.message || err);
    } finally {
      el.btnGenerate.disabled = false;
    }
  }

  function renderDraft(draft) {
    const p = draft.persona || {};
    const meta = p.meta || {};
    const parts = [];

    parts.push('<div class="draft-head">');
    parts.push(`<div class="draft-name">${escapeHtml(meta.name || "（无名）")}</div>`);
    const sub = [meta.identity, meta.archetype].filter(Boolean).map(escapeHtml).join(" · ");
    if (sub) parts.push(`<div class="draft-meta">${sub}</div>`);
    parts.push("</div>");

    // 6 条台词：让人格“可试听”，比一堆 JSON 字段直观得多
    if ((draft.preview || []).length) {
      parts.push('<div class="draft-lines">');
      for (const line of draft.preview) {
        parts.push(`<div class="draft-line">「${escapeHtml(line)}」</div>`);
      }
      parts.push("</div>");
    }

    // 自动修正过的矛盾要告诉用户：这是「我们帮你改了」，不是「模型很完美」
    if ((draft.warnings || []).length) {
      parts.push('<div class="draft-warnings">');
      parts.push(`<div class="draft-warn-title">自动修正了 ${draft.warnings.length} 处矛盾：</div>`);
      for (const w of draft.warnings) {
        parts.push(`<div class="draft-warn">· ${escapeHtml(w)}</div>`);
      }
      parts.push("</div>");
    }

    parts.push('<div class="draft-actions">');
    parts.push('<button type="button" class="btn-primary" id="btn-confirm-persona">用这个开始</button>');
    parts.push("</div>");

    el.personaDraft.innerHTML = parts.join("");
    el.personaDraft.hidden = false;

    const confirmBtn = document.getElementById("btn-confirm-persona");
    if (confirmBtn) {
      confirmBtn.addEventListener("click", () =>
        confirmDraft(p, meta.name || "自定义人格")
      );
    }
  }

  async function confirmDraft(persona, name) {
    const btn = document.getElementById("btn-confirm-persona");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "创建中…";
    }
    try {
      const created = await api("/v1/personas", {
        method: "POST",
        body: JSON.stringify({ persona, ...ownerClaim() }),
      });
      // 收掉生成面板，避免用户以为还要再点一次
      el.personaDraft.hidden = true;
      el.personaCreatePanel.hidden = true;
      el.btnShowCreate.textContent = "+ 用一句话造一个";
      el.personaDesc.value = "";
      await openSession(created.id, created.name || name);
    } catch (err) {
      el.personaGenerateHint.textContent = "创建失败：" + (err.message || err);
      if (btn) {
        btn.disabled = false;
        btn.textContent = "用这个开始";
      }
    }
  }

  el.btnShowCreate.addEventListener("click", toggleCreatePanel);
  el.btnGenerate.addEventListener("click", generateDraft);
  // Ctrl/Cmd+Enter 直接生成，省得伸手点按钮
  el.personaDesc.addEventListener("keydown", (ev) => {
    if ((ev.metaKey || ev.ctrlKey) && ev.key === "Enter") {
      ev.preventDefault();
      generateDraft();
    }
  });

  // ──────────────────── 聊天记录蒸馏（M3-E，入口 C） ────────────────────

  // 授权是整个入口的闸门，所以「能不能点蒸馏」只由它决定——
  // 不因为「记录够长了」就放行。合规检查不能靠用户自觉点一下才知道。
  function syncDistillGate() {
    const ok = el.distillAuthorized.checked;
    el.btnDistill.disabled = !ok;
    // 授权没勾时给整行一个视觉提示（对勾本身也看得见，但小屏容易漏）
    const row = el.distillAuthorized.closest(".distill-consent");
    if (row) row.classList.toggle("is-blocking", !ok);
    el.distillHint.textContent = ok
      ? "只会读出说话习惯，不会入库；确认后再创建。"
      : "勾选上面的授权声明才能开始。";
  }

  function toggleDistillPanel() {
    const opening = el.distillPanel.hidden;
    el.distillPanel.hidden = !opening;
    el.btnShowDistill.textContent = opening ? "− 收起" : "+ 从聊天记录复刻一个人";
    if (opening) el.distillTranscript.focus();
  }

  async function distillDraft() {
    // 双保险：按钮 disabled 已经挡住，但键盘/脚本能绕过 click 的禁用态
    if (!el.distillAuthorized.checked) {
      el.distillHint.textContent = "勾选上面的授权声明才能开始。";
      return;
    }
    const transcript = el.distillTranscript.value.trim();
    if (!transcript) {
      el.distillHint.textContent = "把聊天记录贴进来。";
      return;
    }
    el.btnDistill.disabled = true;
    el.distillHint.textContent = "正在数他说话的方式…";
    el.distillResult.hidden = true;
    try {
      const data = await api("/v1/personas/distill", {
        method: "POST",
        body: JSON.stringify({
          transcript,
          target_name: el.distillTarget.value.trim() || null,
          authorized: true,
        }),
      });
      renderDistillResult(data);
      el.distillHint.textContent = "";
    } catch (err) {
      el.distillHint.textContent = "蒸馏失败：" + (err.message || err);
    } finally {
      syncDistillGate();
    }
  }

  // 指纹是这一屏的主角：它回答「我们凭什么这么判断」。
  // 它比 6 条台词更值得摆在前面——台词是模型的想象，这些数字是原文里的事实。
  function renderFingerprint(fp) {
    const chips = [];
    const span = fp.avg_sentence_len || [0, 0];
    chips.push(["句长", `${span[0]}~${span[1]} 字`]);
    chips.push(["emoji 率", `${Math.round((fp.emoji_rate || 0) * 100)}%`]);
    chips.push(["语体", fp.register || "—"]);
    if (fp.punctuation_policy) chips.push(["标点", fp.punctuation_policy]);
    const parts = ['<div class="fp-block">'];
    parts.push('<div class="fp-title">我们统计出了什么</div>');
    parts.push('<div class="fp-chips">');
    for (const [k, v] of chips) {
      parts.push(
        `<span class="fp-chip"><b>${escapeHtml(k)}</b>${escapeHtml(String(v))}</span>`
      );
    }
    parts.push("</div>");
    const tics = fp.verbal_tics || [];
    if (tics.length) {
      parts.push('<div class="fp-row"><span class="fp-key">口癖</span>');
      for (const t of tics) parts.push(`<span class="fp-tag">${escapeHtml(t)}</span>`);
      parts.push("</div>");
    }
    const words = fp.high_freq_words || [];
    if (words.length) {
      parts.push('<div class="fp-row"><span class="fp-key">高频词</span>');
      for (const w of words) parts.push(`<span class="fp-tag">${escapeHtml(w)}</span>`);
      parts.push("</div>");
    }
    const emojis = fp.emoji_set || [];
    if (emojis.length) {
      parts.push('<div class="fp-row"><span class="fp-key">常用表情</span>');
      for (const e of emojis) parts.push(`<span class="fp-tag">${escapeHtml(e)}</span>`);
      parts.push("</div>");
    }
    parts.push("</div>");
    return parts.join("");
  }

  function renderDistillResult(data) {
    const p = data.persona || {};
    const meta = p.meta || {};
    const parts = [];

    parts.push('<div class="draft-head">');
    parts.push(`<div class="draft-name">${escapeHtml(meta.name || "（无名）")}</div>`);
    const sub = [meta.identity, meta.archetype].filter(Boolean).map(escapeHtml).join(" · ");
    if (sub) parts.push(`<div class="draft-meta">${sub}</div>`);
    parts.push('<div class="draft-meta cloned-mark">克隆产物 · 仅供自用，不能上架模板市场</div>');
    parts.push("</div>");

    parts.push(renderFingerprint(data.fingerprint || {}));

    if ((data.preview || []).length) {
      parts.push('<div class="draft-lines">');
      for (const line of data.preview) {
        parts.push(`<div class="draft-line">「${escapeHtml(line)}」</div>`);
      }
      parts.push("</div>");
    }

    if ((data.warnings || []).length) {
      parts.push('<div class="draft-warnings">');
      parts.push(`<div class="draft-warn-title">自动修正了 ${data.warnings.length} 处：</div>`);
      for (const w of data.warnings) {
        parts.push(`<div class="draft-warn">· ${escapeHtml(w)}</div>`);
      }
      parts.push("</div>");
    }

    parts.push('<div class="draft-actions">');
    parts.push('<button type="button" class="btn-primary" id="btn-confirm-distill">用这个开始</button>');
    parts.push("</div>");

    el.distillResult.innerHTML = parts.join("");
    el.distillResult.hidden = false;

    const confirmBtn = document.getElementById("btn-confirm-distill");
    if (confirmBtn) {
      confirmBtn.addEventListener("click", () =>
        confirmDistill(p, meta.name || "克隆人格")
      );
    }
  }

  // 落库走同一个 POST /v1/personas：persona 里的 provenance.type 原样带回去，
  // 服务端尊重它（cloned 不会被改写成 generated）。
  async function confirmDistill(persona, name) {
    const btn = document.getElementById("btn-confirm-distill");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "创建中…";
    }
    try {
      const created = await api("/v1/personas", {
        method: "POST",
        body: JSON.stringify({ persona, ...ownerClaim() }),
      });
      el.distillResult.hidden = true;
      el.distillPanel.hidden = true;
      el.btnShowDistill.textContent = "+ 从聊天记录复刻一个人";
      el.distillTranscript.value = "";
      el.distillTarget.value = "";
      // 授权是**一次性**的：复刻完就重置，下一次必须重新确认。
      el.distillAuthorized.checked = false;
      syncDistillGate();
      await openSession(created.id, created.name || name);
    } catch (err) {
      el.distillHint.textContent = "创建失败：" + (err.message || err);
      if (btn) {
        btn.disabled = false;
        btn.textContent = "用这个开始";
      }
    }
  }

  el.btnShowDistill.addEventListener("click", toggleDistillPanel);
  el.distillAuthorized.addEventListener("change", syncDistillGate);
  el.btnDistill.addEventListener("click", distillDraft);
  syncDistillGate();

  // ──────────────────── 五步向导（M3-D，入口 D） ────────────────────
  //
  // 与入口 B（一句话生成）的分工：B 是「说一句，模型猜」，快但看不见中间产物；
  // 这里（D）是「一项项填，我看得见每个字段」，慢但精确。所以这一块的重点
  // 不是生成，而是**每一步都把用户的输入留住，并且每一步都能看效果**。
  //
  // 状态怎么保持（判据「上一步/下一步已填内容不能丢」的实现方式）：
  //   1. 五步全部渲染在 DOM 里（#wizard-steps 的五个 section），切步只切
  //      `hidden` 与 `.is-current`——**不重建、不清空**，输入框里的字天然留下；
  //   2. 每次输入/滑动 `collectWizardDraft()` 把当前五个 section 的值读进
  //      `state.wizardDraft`，所以「切到最后一步再切回来」也一致；
  //   3. 预览与创建都用 `state.wizardDraft` 那份快照，而不是临时读 DOM，
  //      因此请求发出去之后用户再改，也不会把两次结果混在一起。

  const WIZARD_TITLES = ["他是谁", "他的性子", "他怎么说", "他的边界", "看一眼"];
  const WIZARD_LAST = WIZARD_TITLES.length;

  /** 逗号分隔 → 数组。中英文逗号、顿号都当分隔符，去掉空白与空项。 */
  function splitList(text) {
    return String(text || "")
      .split(/[,，、]/)
      .map((x) => x.trim())
      .filter(Boolean);
  }

  /** 把五个 section 的当前值读成 draft。没填的字段直接不出现——
   *  后端把缺的字段用 PersonaCard 默认值补齐（不落库的预览，缺省即默认）。 */
  function collectWizardDraft() {
    const d = {};

    const name = el.wizName.value.trim();
    const meta = { name };
    const age = el.wizAge.value.trim();
    if (age !== "") meta.age = Number(age);
    const identity = el.wizIdentity.value.trim();
    if (identity) meta.identity = identity;
    const archetype = el.wizArchetype.value.trim();
    if (archetype) meta.archetype = archetype;
    // 名字为空也照发：草稿允许残缺，后端用「（未命名）」占位。
    // 拦不拦是「下一步」按钮的事，不是请求体的事。
    d.meta = meta;

    d.traits = {
      openness: Number(el.wizOpenness.value),
      conscientiousness: Number(el.wizConscientiousness.value),
      extraversion: Number(el.wizExtraversion.value),
      agreeableness: Number(el.wizAgreeableness.value),
      neuroticism: Number(el.wizNeuroticism.value),
    };

    const voice = {};
    const register = el.wizRegister.value.trim();
    if (register) voice.register = register;
    const lo = el.wizSentLo.value.trim();
    const hi = el.wizSentHi.value.trim();
    if (lo !== "" && hi !== "") voice.avg_sentence_len = [Number(lo), Number(hi)];
    else if (lo !== "") voice.avg_sentence_len = [Number(lo), Number(lo)];
    else if (hi !== "") voice.avg_sentence_len = [Number(hi), Number(hi)];
    voice.emoji_rate = Number(el.wizEmoji.value);
    voice.verbal_tics = splitList(el.wizTics.value);
    voice.forbidden_phrases = splitList(el.wizForbidden.value);
    d.voice = voice;

    const knowledge = {};
    knowledge.knows_not = splitList(el.wizKnowsNot.value);
    const leak = el.wizLeakPolicy.value.trim();
    if (leak) knowledge.leak_policy = leak;
    d.knowledge = knowledge;

    state.wizardDraft = d;
    return d;
  }

  /** draft → 每个输入框。取消后重开时用，也用于「把默认值填进界面」。 */
  function applyWizardDraft(draft) {
    const meta = draft.meta || {};
    el.wizName.value = meta.name || "";
    el.wizAge.value = meta.age == null ? "" : String(meta.age);
    el.wizIdentity.value = meta.identity || "";
    el.wizArchetype.value = meta.archetype || "";

    const t = draft.traits || {};
    el.wizOpenness.value = String(t.openness ?? 0.5);
    el.wizConscientiousness.value = String(t.conscientiousness ?? 0.5);
    el.wizExtraversion.value = String(t.extraversion ?? 0.5);
    el.wizAgreeableness.value = String(t.agreeableness ?? 0.5);
    el.wizNeuroticism.value = String(t.neuroticism ?? 0.5);

    const v = draft.voice || {};
    el.wizRegister.value = v.register || "";
    const span = v.avg_sentence_len || [];
    el.wizSentLo.value = span.length ? String(span[0]) : "";
    el.wizSentHi.value = span.length ? String(span[1]) : "";
    el.wizEmoji.value = String(v.emoji_rate ?? 0.2);
    el.wizTics.value = (v.verbal_tics || []).join(", ");
    el.wizForbidden.value = (v.forbidden_phrases || []).join(", ");

    const k = draft.knowledge || {};
    el.wizKnowsNot.value = (k.knows_not || []).join(", ");
    el.wizLeakPolicy.value = k.leak_policy || "";

    syncWizardValues();
  }

  /** 滑杆旁边的数字。滑杆不动也要显示当前值——「0~1 的滑杆不显示数值」
   *  等于让用户猜自己拖到了哪儿。 */
  function syncWizardValues() {
    for (const input of el.wizardPanel.querySelectorAll(".wizard-range")) {
      const out = input.parentElement.querySelector(".wizard-value");
      if (out) out.textContent = Number(input.value).toFixed(2);
    }
  }

  function wizardHasName() {
    return el.wizName.value.trim().length > 0;
  }

  /** 按 state.wizardStep 刷新：标题、进度、按钮态、预览区可见性。 */
  function renderWizardStep() {
    const step = state.wizardStep;
    el.wizardProgress.textContent = `第 ${step} / ${WIZARD_LAST} 步`;
    el.wizardStepTitle.textContent = WIZARD_TITLES[step - 1] || "";

    for (const section of el.wizardSteps.querySelectorAll(".wizard-step")) {
      const n = Number(section.dataset.step);
      const current = n === step;
      section.hidden = !current;
      section.classList.toggle("is-current", current);
    }

    el.wizardPrev.disabled = step <= 1;
    el.wizardNext.hidden = step >= WIZARD_LAST;
    el.wizardCreate.hidden = step < WIZARD_LAST;

    // 第 1 步的「下一步」：名字为空时禁用，并直说为什么
    if (step === 1 && !wizardHasName()) {
      el.wizardNext.disabled = true;
      el.wizardHint.textContent = "先给他起个名字";
    } else {
      el.wizardNext.disabled = false;
      el.wizardHint.textContent = "";
    }

    // 预览区：第 2 步之后才有意义（第 1 步还没填名字，也就没语气可听）
    el.wizardPreview.hidden = step < 2;

    if (step === WIZARD_LAST) renderWizardFinal();
  }

  async function refreshWizardPreview() {
    const draft = collectWizardDraft();
    // 第 1 步还没填名字 → 不请求（没名字的人没有语气可预览）
    if (!draft.meta.name) {
      el.wizardPreview.hidden = true;
      return;
    }
    el.wizardPreview.hidden = false;
    el.wizardPreviewLines.innerHTML = '<div class="draft-line muted">正在想他会怎么说话…</div>';
    try {
      const data = await api("/v1/personas/draft/preview", {
        method: "POST",
        body: JSON.stringify({ draft }),
      });
      renderWizardPreview(data);
    } catch (err) {
      el.wizardPreviewLines.innerHTML =
        `<div class="draft-line">预览失败：${escapeHtml(err.message || String(err))}</div>`;
      el.wizardPreviewWarnings.hidden = true;
    }
  }

  function renderWizardPreview(data) {
    const lines = data.preview || [];
    el.wizardPreviewLines.innerHTML = lines.length
      ? lines.map((l) => `<div class="draft-line">「${escapeHtml(l)}」</div>`).join("")
      : '<div class="draft-line muted">这一次没写出台词，可以继续填。</div>';

    const warns = data.warnings || [];
    el.wizardPreviewWarnings.hidden = !warns.length;
    if (warns.length) {
      el.wizardPreviewWarnings.innerHTML =
        `<div class="draft-warn-title">自动修正了 ${warns.length} 处矛盾：</div>` +
        warns.map((w) => `<div class="draft-warn">· ${escapeHtml(w)}</div>`).join("");
    }

    // 第 5 步「看一眼」把补全后的卡摊开——这是「表单即 Schema 可视化」的收尾：
    // 用户填的那些字段，最后长成了一张什么样的人格卡。
    const p = data.persona || {};
    const meta = p.meta || {};
    const traits = p.traits || {};
    const voice = p.voice || {};
    const span = voice.avg_sentence_len || [];
    const rows = [
      ["名字", meta.name || ""],
      ["年龄", meta.age == null ? "—" : String(meta.age)],
      ["身份", meta.identity || "—"],
      ["原型", meta.archetype || "—"],
      ["语体", voice.register || "—"],
      ["句长", span.length ? `${span[0]}~${span[1]} 字` : "—"],
      ["表情", Number(voice.emoji_rate ?? 0).toFixed(2)],
      ["口头禅", (voice.verbal_tics || []).join("、") || "—"],
      ["不懂", ((p.knowledge || {}).knows_not || []).join("、") || "—"],
      ["外向", Number(traits.extraversion ?? 0).toFixed(2)],
      ["宜人", Number(traits.agreeableness ?? 0).toFixed(2)],
    ];
    el.wizardFinal.innerHTML = rows
      .map(
        ([k, v]) =>
          `<div class="wizard-final-row"><span class="k">${escapeHtml(k)}</span>` +
          `<span class="v">${escapeHtml(String(v))}</span></div>`
      )
      .join("");
  }

  function renderWizardFinal() {
    // 第 5 步进入时预览已经请求过（下一步时就请求了），这里只兜一次空态
    if (!el.wizardFinal.innerHTML.trim()) {
      el.wizardFinal.innerHTML = '<p class="wizard-hint">还没有预览。点「上一步」再回来一次。</p>';
    }
  }

  function toggleWizardPanel() {
    const opening = el.wizardPanel.hidden;
    el.wizardPanel.hidden = !opening;
    // 标题始终是「+ 一步步来」，展开与否靠 .is-open 的强调色表达：
    // 互换「+ / −」会让三条入口的标题在展开时变形，用户要重新找一次入口在哪
    el.btnShowWizard.classList.toggle("is-open", opening);
    if (opening) {
      resetWizard();
      el.wizName.focus();
    }
  }

  /** 回到初始状态。取消 / 收起面板 / 创建成功之后都走这里。
   *  刻意不落库：草稿只活在 state 与 DOM 里，关掉就没了。 */
  function resetWizard() {
    state.wizardStep = 1;
    state.wizardDraft = {};
    applyWizardDraft({});
    el.wizardPreview.hidden = true;
    el.wizardPreviewLines.innerHTML = "";
    el.wizardPreviewWarnings.hidden = true;
    el.wizardPreviewWarnings.innerHTML = "";
    el.wizardFinal.innerHTML = "";
    el.wizardHint.textContent = "";
    renderWizardStep();
  }

  async function wizardNext() {
    // 双保险：按钮 disabled 已挡住，但键盘/脚本能绕过 click 的禁用态
    if (state.wizardStep === 1 && !wizardHasName()) {
      el.wizardHint.textContent = "先给他起个名字";
      return;
    }
    if (state.wizardStep >= WIZARD_LAST) return;
    collectWizardDraft();
    state.wizardStep += 1;
    renderWizardStep();
    // 每进下一步自动请求一次预览（第 1 步之前不请求：那时还没名字）
    if (state.wizardStep >= 2) await refreshWizardPreview();
  }

  function wizardPrev() {
    if (state.wizardStep <= 1) return;
    collectWizardDraft();
    state.wizardStep -= 1;
    renderWizardStep();
  }

  function wizardCancel() {
    resetWizard();
    el.wizardPanel.hidden = true;
    el.btnShowWizard.classList.remove("is-open");
  }

  async function wizardCreate() {
    // 创建用的是**预览补全后**的那张卡吗？不是——刻意用用户填的 draft 再走一次
    // 后端的补全（POST /v1/personas 接受完整卡）。这样「用户看到的那张卡」
    // 与「落库的那张卡」是同一条补全路径产出的，不会因为前端抄了一遍而分叉。
    const draft = collectWizardDraft();
    if (!draft.meta.name) {
      el.wizardHint.textContent = "先给他起个名字";
      state.wizardStep = 1;
      renderWizardStep();
      return;
    }
    el.wizardCreate.disabled = true;
    el.wizardCreate.textContent = "创建中…";
    el.wizardHint.textContent = "";
    try {
      // 先用 preview 拿补全后的完整卡（含冲突消解结果），再落库——
      // 这样落库的就是用户在第 5 步看到的那张卡。
      const preview = await api("/v1/personas/draft/preview", {
        method: "POST",
        body: JSON.stringify({ draft }),
      });
      const created = await api("/v1/personas", {
        method: "POST",
        body: JSON.stringify({ persona: preview.persona, ...ownerClaim() }),
      });
      const name = created.name || draft.meta.name;
      resetWizard();
      el.wizardPanel.hidden = true;
      el.btnShowWizard.classList.remove("is-open");
      await openSession(created.id, name);
    } catch (err) {
      el.wizardHint.textContent = "创建失败：" + (err.message || String(err));
    } finally {
      el.wizardCreate.disabled = false;
      el.wizardCreate.textContent = "用这个开始";
    }
  }

  // 输入即入草稿：这样「切步不丢」不只靠 DOM 留存，state 里也始终是最新的一份
  el.wizardPanel.addEventListener("input", () => {
    collectWizardDraft();
    syncWizardValues();
    // 第 1 步名字从空变成非空（或反过来）要立刻影响「下一步」的可点性
    if (state.wizardStep === 1) renderWizardStep();
  });
  el.btnShowWizard.addEventListener("click", toggleWizardPanel);
  el.wizardNext.addEventListener("click", wizardNext);
  el.wizardPrev.addEventListener("click", wizardPrev);
  el.wizardCancel.addEventListener("click", wizardCancel);
  el.wizardCreate.addEventListener("click", wizardCreate);
  renderWizardStep();

  // ───────────────────────────── 场景选择 ─────────────────────────────

  async function loadScenarios() {
    try {
      const data = await api("/v1/scenarios");
      const scenarios = data.scenarios || [];
      el.scenarioList.innerHTML = "";
      el.sceneCount.textContent = String(scenarios.length);

      // 「不设定场景」永远排第一：普通对话是最常用的
      const noneBtn = document.createElement("button");
      noneBtn.className = "scenario-item is-none is-active";
      noneBtn.type = "button";
      noneBtn.innerHTML =
        '<div class="scenario-title"><span>不设定场景</span><span class="scenario-beats">普通对话</span></div>' +
        '<div class="scenario-setting">没有剧本，就是日常。她也还是她。</div>';
      noneBtn.addEventListener("click", () => {
        state.scenarioId = null;
        state.scenarioBeats = {};
        highlight(el.scenarioList, noneBtn);
        el.sceneProgressCard.hidden = true;
        if (state.templateId) startSession();
      });
      el.scenarioList.appendChild(noneBtn);

      for (const s of scenarios) {
        el.scenarioList.appendChild(scenarioItemNode(s));
      }
    } catch (err) {
      el.scenarioList.innerHTML =
        `<p class="memory-empty">场景加载失败：${escapeHtml(err.message)}</p>`;
    }
  }

  // ──────────────────── 场景卡片（M3-G：编辑入口只给用户场景） ────────────────────

  function scenarioItemNode(s) {
    const btn = document.createElement("button");
    btn.className = "scenario-item";
    btn.type = "button";
    btn.dataset.scenarioId = s.id;
    btn.innerHTML =
      `<div class="scenario-title"><span>${escapeHtml(s.title || s.id)}</span>` +
      `<span class="scenario-beats">${s.beat_count ?? "?"} 节拍</span></div>` +
      `<div class="scenario-setting">${escapeHtml(s.setting || "")}</div>`;

    btn.addEventListener("click", async () => {
      state.scenarioId = s.id;
      highlight(el.scenarioList, btn);
      await cacheBeats(s.id);
      if (state.templateId) startSession();
      else showSceneProgress(null);
    });

    // 编辑入口只给用户场景。内置场景是产品资产，改它走源码，
    // 运行时覆盖会被 409（后端拦）——前端不给入口，双保险。
    if (s.source === "user") {
      const edit = document.createElement("span");
      edit.className = "scenario-edit";
      edit.textContent = "编辑";
      edit.title = "编辑这个场景";
      edit.addEventListener("click", (e) => {
        e.stopPropagation(); // 别触发选中
        enterScenarioEdit(s.id);
      });
      btn.querySelector(".scenario-title").appendChild(edit);
    }
    return btn;
  }

  let editingScenarioId = null; // null = 新建模式

  async function enterScenarioEdit(id) {
    let detail;
    try {
      detail = await api(`/v1/scenarios/${encodeURIComponent(id)}`);
    } catch (err) {
      showScHint("读取场景失败：" + err.message);
      return;
    }
    if (detail.source !== "user") {
      showScHint("内置场景不能在这里编辑。");
      return;
    }
    editingScenarioId = id;
    el.scenarioCreatePanel.hidden = false;
    el.btnShowScenarioCreate.textContent = "− 收起";
    el.scTitle.value = detail.title || "";
    el.scId.value = detail.id || "";
    el.scId.readOnly = true; // 编辑不改 id——改了会让已开会话的进度对不上
    el.scSetting.value = detail.setting || "";
    el.scRole.value = detail.user_role || "";
    el.scOpening.value = detail.opening_narration || "";
    el.scBeats.value = (detail.beats || []).map(beatToLine).join("\n");
    el.btnCreateScenario.textContent = "保存修改";
    el.scTitle.focus();
    showScHint(`正在编辑「${detail.title}」。改完点「保存修改」。`);
  }

  function exitScenarioEdit() {
    editingScenarioId = null;
    el.scId.readOnly = false;
    el.btnCreateScenario.textContent = "创建场景";
  }

  function beatToLine(b) {
    // 与 lineToBeat 对偶：表单一行 ↔ 节拍一个
    const goal = (b && b.goal) || "";
    const trig = (b && b.trigger || []).join("、");
    return trig ? `${goal}｜${trig}` : goal;
  }

  function lineToBeat(line) {
    // 表单一行 → 节拍：支持「目标｜触发词1、触发词2」（｜或|分隔，全半角都认）。
    // 没有触发词就只给 goal——后端 _normalize 会补空 trigger（空触发不自动推进，
    // 比"猜一个触发词"诚实，见 writer.py 的注释）。
    const s = String(line || "").trim();
    if (!s) return null;
    const sep = s.indexOf("｜") >= 0 ? "｜" : s.indexOf("|") >= 0 ? "|" : null;
    if (sep === null) return { goal: s, trigger: [] };
    const goal = s.slice(0, s.indexOf(sep)).trim();
    const trigger = s.slice(s.indexOf(sep) + 1).split(/[、,，]/).map((t) => t.trim()).filter(Boolean);
    return goal ? { goal, trigger } : null;
  }

  // ──────────────────── 自建场景（M3-F） ────────────────────

  function toggleScenarioCreate() {
    const opening = el.scenarioCreatePanel.hidden;
    el.scenarioCreatePanel.hidden = !opening;
    el.btnShowScenarioCreate.textContent = opening
      ? "− 收起"
      : "+ 自己写一个场景";
    if (opening) el.scTitle.focus();
  }

  /** 代号留空就自动生一个。用户只关心「叫什么」，不该被 slug 规则拦住。 */
  function autoScenarioId() {
    return "sc_" + Math.random().toString(36).slice(2, 8);
  }

  function setScHint(text) {
    el.scHint.textContent = text || "";
  }

  async function createScenario() {
    const title = el.scTitle.value.trim();
    if (!title) {
      setScHint("先给场景起个标题。");
      return;
    }

    // 节拍：一行一个。用户不需要懂 YAML 结构。
    const beats = el.scBeats.value
      .split("\n")
      .map((s) => s.trim())
      .map((line) => lineToBeat(line))
      .filter(Boolean);

    const body = {
      id: editingScenarioId || el.scId.value.trim() || autoScenarioId(),
      title,
      setting: el.scSetting.value.trim(),
      opening_narration: el.scOpening.value.trim(),
      user_role: el.scRole.value.trim(),
      beats,
    };
    // 编辑模式：带 overwrite。id 沿用原值（enterScenarioEdit 里已锁只读），
    // 已开会话的进度靠 id 对得上——改 id 就等于断了进度。
    if (editingScenarioId) body.overwrite = true;

    el.btnCreateScenario.disabled = true;
    setScHint(editingScenarioId ? "正在保存…" : "正在创建…");
    try {
      const created = await api("/v1/scenarios", {
        method: "POST",
        body: JSON.stringify(body),
      });
      // 收面板 + 清表单
      el.scenarioCreatePanel.hidden = true;
      el.btnShowScenarioCreate.textContent = "+ 自己写一个场景";
      for (const node of [el.scTitle, el.scId, el.scSetting, el.scOpening, el.scRole, el.scBeats]) {
        node.value = "";
      }
      exitScenarioEdit();
      await loadScenarios();
      setScHint(editingScenarioId ? `已保存「${created.title}」` : `已创建「${created.title}」`);
      editingScenarioId = null;
    } catch (err) {
      // 409 是「代号撞了」：编辑态与新建态的提示分开——
      // 编辑态撞 409 只可能是撞了内置（自己的场景本来就该 overwrite 过）
      const msg = String(err.message || err);
      if (editingScenarioId) {
        setScHint("保存失败：" + msg);
      } else {
        setScHint(
          msg.startsWith("409")
            ? "这个代号已被占用，换一个（或者把代号留空自动生成）。"
            : "创建失败：" + msg
        );
      }
    } finally {
      el.btnCreateScenario.disabled = false;
    }
  }

  el.btnShowScenarioCreate.addEventListener("click", toggleScenarioCreate);
  el.btnCreateScenario.addEventListener("click", createScenario);

  /** 拉一次场景详情，缓存 beat id → goal，进度面板要用 */
  async function cacheBeats(scenarioId) {
    try {
      const detail = await api(`/v1/scenarios/${scenarioId}`);
      state.scenarioBeats = {};
      for (const b of detail.beats || []) {
        state.scenarioBeats[b.id] = b.goal;
      }
      showSceneProgress({ done_beats: [], pending_beats: Object.keys(state.scenarioBeats) });
    } catch (err) {
      console.warn("场景详情加载失败", err);
      state.scenarioBeats = {};
    }
  }

  function showSceneProgress(sceneState) {
    if (!state.scenarioId) {
      el.sceneProgressCard.hidden = true;
      return;
    }
    el.sceneProgressCard.hidden = false;

    const done = new Set((sceneState && sceneState.done_beats) || []);
    const ids = Object.keys(state.scenarioBeats);
    if (!ids.length) {
      elsafeSet(el.sceneProgress, '<p class="memory-empty">加载中…</p>');
      return;
    }

    el.sceneProgress.innerHTML = "";
    for (const id of ids) {
      const isDone = done.has(id);
      const row = document.createElement("div");
      row.className = "beat-row " + (isDone ? "is-done" : "is-pending");
      row.innerHTML =
        `<span class="beat-mark">✓</span>` +
        `<span class="beat-text">${escapeHtml(state.scenarioBeats[id])}</span>`;
      el.sceneProgress.appendChild(row);
    }
  }

  function elsafeSet(node, html) {
    node.innerHTML = html;
  }

  function highlight(container, activeBtn) {
    for (const b of container.querySelectorAll("button")) {
      b.classList.toggle("is-active", b === activeBtn);
    }
  }

  // ───────────────────────────── 会话 ─────────────────────────────

  async function startSession() {
    if (state.busy || !state.templateId) return;
    state.busy = true;
    setConn("准备中…", "pill-busy");

    try {
      // 入口 A：模板实例化（模板 → 库里的行）
      const persona = await api("/v1/personas", {
        method: "POST",
        body: JSON.stringify({ template_id: state.templateId, ...ownerClaim() }),
      });
      await openSession(persona.id, state.personaName);
    } catch (err) {
      showError("开始会话失败：" + err.message);
      setConn("未连接", "pill-error");
    } finally {
      state.busy = false;
    }
  }

  /** 人格已落库之后：开会话、清空界面、解锁输入。
   *
   * 两条入口共用（模板实例化 / 一句话生成确认）——
   * 「人格从哪来」不该影响「会话怎么开」。
   */
  async function openSession(personaId, personaName) {
    const body = {
      persona_id: personaId,
      // 登录后 user_id 换成账号的（判据 8）：所有状态都按 (persona_id, user_id) 存，
      // 所以「换设备登录 → 同一个 user_id → 记忆跟过来」是这一行在起作用。
      user_id: state.userId,
    };
    if (state.scenarioId) body.scenario_id = state.scenarioId;

    const session = await api("/v1/sessions", {
      method: "POST",
      body: JSON.stringify(body),
    });

    state.personaId = personaId;
    state.sessionId = session.session_id;
    state.personaName = personaName;

    // 清空并解锁输入
    el.messages.innerHTML = "";
    renderMemories([]);
    el.tracePanel.innerHTML =
      '<p class="muted small">聊一句，这里会显示这一轮的召回与 token 分布。</p>';
    el.input.disabled = false;
    el.send.disabled = false;
    el.btnMic.disabled = !canRecord();
    el.input.focus();

    setConn(`与${personaName}对话中`, "pill-live");
    // 「我的人格」里的高亮跟着走（新造的人格也要出现在列表里）
    await loadMyPersonas();
    showSceneProgress({ done_beats: [], pending_beats: Object.keys(state.scenarioBeats) });
    addMessage("ai", `（${personaName}在这里。）`);
    // 陪伴档案：新会话开始时拉一次（可能已有旧档案）
    await refreshCompanion();
    // 日记：新会话开始时拉一次（可能之前写过），并放开「写下今天」
    state.diaryHighlightId = null;
    el.btnWriteDiary.disabled = false;
    el.diaryHint.textContent = "";
    await refreshDiary();
  }

  // ───────────────────────────── 发消息（SSE 主流程） ─────────────────────────────

  async function sendMessage(text) {
    if (!state.sessionId || state.busy || !text.trim()) return;

    state.busy = true;
    el.input.value = "";
    autoGrow();
    el.send.disabled = true;
    setConn("说话中…", "pill-busy");
    el.thinkingStrip.hidden = false;
    el.thinkingText.textContent = "正在回忆…";

    addMessage("user", text);
    beginStreamBubble();

    let doneState = null;
    let turnId = null;

    try {
      const resp = await fetch(`/v1/sessions/${state.sessionId}/messages`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: text }),
      });

      if (!resp.ok) {
        let detail = resp.statusText;
        try {
          const body = await resp.json();
          detail = body.detail ? JSON.stringify(body.detail) : detail;
        } catch (_) { /* 忽略 */ }
        throw new Error(`${resp.status} ${detail}`);
      }

      const reader = resp.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const parsed = parseSSE(buffer);
        buffer = parsed.rest;

        for (const ev of parsed.events) {
          handleEvent(ev);
        }
      }

      // 收尾：处理 buffer 里可能残留的最后一帧
      if (buffer.trim()) {
        const parsed = parseSSE(buffer + "\n\n");
        for (const ev of parsed.events) handleEvent(ev);
      }

      function handleEvent(ev) {
        switch (ev.type) {
          case "thinking": {
            const stage = ev.data.stage;
            if (stage === "recall") {
              const n = ev.data.count ?? 0;
              el.thinkingText.textContent =
                n > 0 ? `想起了 ${n} 件相关的事…` : "没有想起什么…";
            } else if (stage === "compose") {
              el.thinkingText.textContent = "在斟酌措辞…";
            }
            break;
          }
          case "affect":
            setMood(ev.data.label || "中性");
            setPad(ev.data);
            if (typeof ev.data.relation_level === "number") {
              setRelation(ev.data.relation_level, undefined);
            }
            break;
          case "delta":
            appendDelta(ev.data.text || "");
            break;
          case "done":
            turnId = ev.data.turn_id;
            doneState = ev.data.state || {};
            state.lastTurnId = turnId;
            if (doneState.affect) {
              setMood(doneState.affect.label || "中性");
              setPad(doneState.affect);
            }
            if (doneState.relation) {
              setRelation(doneState.relation.level, doneState.relation.intimacy);
            }
            // 场景节拍进度：done 里带 done_beats / pending_beats
            if (doneState.scenario && doneState.scenario.scenario_id) {
              showSceneProgress(doneState.scenario);
            }
            break;
          default:
            break;
        }
      }

      endStreamBubble();

      // done 之后自动刷新两个面板（A 组判据）
      if (turnId != null) {
        await refreshMemories();
        renderTrace(turnId, doneState);
      }
      // 陪伴档案：每轮之后刷新（M2-B 判据）
      await refreshCompanion();

      setConn(`与${state.personaName}对话中`, "pill-live");
    } catch (err) {
      endStreamBubble();
      showError("发送失败：" + err.message);
      setConn("出错了", "pill-error");
      // 失败时不要把用户输入吞掉
      if (!el.input.value) {
        el.input.value = text;
        autoGrow();
      }
    } finally {
      state.busy = false;
      el.thinkingStrip.hidden = true;
      el.input.disabled = false;
      el.send.disabled = false;
      el.btnMic.disabled = !canRecord();
      el.input.focus();
    }
  }

  // ───────────────────────────── 语音（M4-C） ─────────────────────────────
  // 三条边界写在这里，免得后面有人把它们当成「还没做完」：
  //   1. 录音只在浏览器里做（MediaRecorder）。服务端只吃字节——
  //      不引 pyaudio / sounddevice，那类依赖在 python:3.11-slim 里装不上。
  //   2. 识别结果是**填进输入框**，不自动发送。人说话会打磕巴、会重来，
  //      直接发出去等于替用户决定「这句就算数了」。
  //   3. 播放是「每一句自己点」。不做自动朗读——那会盖掉用户正在读字的节奏，
  //      而且一条长回复自动念出来是很吓人的体验。

  /** 这个浏览器能不能录音。用能力探测而不是 UA 判断。 */
  function canRecord() {
    return typeof navigator !== "undefined"
      && !!navigator.mediaDevices
      && typeof navigator.mediaDevices.getUserMedia === "function"
      && typeof MediaRecorder !== "undefined";
  }

  function setVoiceStatus(text, isError) {
    if (!el.voiceStatus) return;
    el.voiceStatus.textContent = text || "";
    el.voiceStatus.hidden = !text;
    el.voiceStatus.className = "voice-status" + (isError ? " is-error" : "");
  }

  /** 麦克风按钮上的字。取不到就静默跳过——标签只是装饰，缺它不该拖垮录音。 */
  function setMicLabel(text) {
    const label = el.btnMic && el.btnMic.querySelector ? el.btnMic.querySelector(".mic-label") : null;
    if (label) label.textContent = text;
  }

  /** 麦克风点一下开始、再点一下结束。 */
  async function toggleRecording() {
    if (state.recording) {
      stopRecording();
      return;
    }
    if (!canRecord()) {
      setVoiceStatus("这个浏览器不支持录音（需要 https 或 localhost）。打字一样可以。", true);
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const recorder = new MediaRecorder(stream);
      state.recorder = recorder;
      state.chunks = [];
      state.recording = true;
      el.btnMic.classList.add("is-recording");
      setMicLabel("停止");
      setVoiceStatus("正在听… 说完点一下停止。", false);

      recorder.addEventListener("dataavailable", (e) => {
        if (e.data && e.data.size > 0) state.chunks.push(e.data);
      });
      recorder.addEventListener("stop", () => {
        // 轨道必须显式停掉：不然浏览器的录音指示灯一直亮着——
        // 用户会以为被偷听，这在陪伴类产品里是致命的信任问题。
        for (const track of stream.getTracks()) track.stop();
        uploadRecording(new Blob(state.chunks, { type: recorder.mimeType || "audio/webm" }));
      });
      recorder.start();
    } catch (err) {
      state.recording = false;
      el.btnMic.classList.remove("is-recording");
      setMicLabel("按住说话");
      setVoiceStatus("拿不到麦克风权限：" + (err && err.message ? err.message : err), true);
    }
  }

  function stopRecording() {
    state.recording = false;
    el.btnMic.classList.remove("is-recording");
    setMicLabel("按住说话");
    setVoiceStatus("识别中…", false);
    try {
      if (state.recorder && state.recorder.state !== "inactive") state.recorder.stop();
    } catch (_) {
      setVoiceStatus("录音器状态异常，请再试一次。", true);
    }
  }

  /** 把录音送到 /v1/asr，结果填进输入框（不自动发送）。 */
  async function uploadRecording(blob) {
    if (!blob || blob.size === 0) {
      setVoiceStatus("没有录到声音，再试一次？", true);
      return;
    }
    const form = new FormData();
    // 文件名只是给服务端日志看的，扩展名跟着浏览器实际给的 mime 走
    const ext = (blob.type || "audio/webm").indexOf("ogg") !== -1 ? "ogg" : "webm";
    form.append("audio", blob, "voice." + ext);
    try {
      // 不复用 api()：它固定写 Content-Type: application/json，
      // 而 multipart 的 boundary 必须由浏览器自己生成。
      // 也不在这里写 FormData 检测或守卫——直接交给浏览器；
      // 没装 FormData 的环境（node harness）由测试侧补一个假件。
      const resp = await fetch("/v1/asr", {
        method: "POST",
        headers: authHeaders(),
        body: form,
      });
      if (!resp.ok) {
        let detail = resp.statusText;
        try {
          const body = await resp.json();
          if (body && body.detail) detail = body.detail;
        } catch (_) { /* 不是 JSON，用 statusText */ }
        throw new Error(detail);
      }
      // 顺序敏感：先取文本、再读 body。两个都读的响应体只能读一次，
      // 所以这里不允许「先 log 一下整个 body」——那会把 json() 吃空。
      const out = await resp.json();
      const text = (out && out.text ? String(out.text) : "").trim();
      if (!text) {
        setVoiceStatus("没听清，再说一次？", true);
        return;
      }
      // 追加而不是覆盖：用户可能已经打了几个字，识别结果接在后面
      el.input.value = el.input.value ? el.input.value + " " + text : text;
      autoGrow();
      el.input.focus();
      setVoiceStatus("", false);
    } catch (err) {
      setVoiceStatus("识别失败：" + (err && err.message ? err.message : err), true);
    }
  }

  /** AI 气泡上的「播放」按钮。点一下合成并播这一句。 */
  async function playMessage(text, btn) {
    if (!state.personaId) return;
    if (state.audio) {
      state.audio.pause();
      state.audio = null;
    }
    if (btn) btn.disabled = true;
    try {
      const resp = await fetch("/v1/tts", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        // user_id 决定读哪一份情绪：她的语气跟这个用户的对话状态走。
        body: JSON.stringify({ text: text, persona_id: state.personaId, user_id: state.userId }),
      });
      if (!resp.ok) {
        let detail = resp.statusText;
        try {
          const body = await resp.json();
          if (body && body.detail) detail = body.detail;
        } catch (_) { /* 忽略 */ }
        throw new Error(detail);
      }
      const buf = await resp.arrayBuffer();
      const url = URL.createObjectURL(new Blob([buf], { type: "audio/wav" }));
      const audio = new Audio(url);
      state.audio = audio;
      audio.addEventListener("ended", () => URL.revokeObjectURL(url));
      await audio.play();
    } catch (err) {
      setVoiceStatus("播放失败：" + (err && err.message ? err.message : err), true);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  /** 在一段 AI 文本下面挂「播放」。DOM 里有个稳定 id/class 给测试钉住。 */
  function attachPlayButton(container, text) {
    if (!container || !text) return null;
    let btn = container.querySelector(".msg-play");
    if (!btn) {
      btn = document.createElement("button");
      btn.type = "button";
      btn.className = "msg-play";
      btn.textContent = "▶ 听她念";
      btn.title = "用她的声音说这一段";
      btn.addEventListener("click", () => playMessage(btn.dataset.text || "", btn));
      container.appendChild(btn);
    }
    btn.dataset.text = text;
    return btn;
  }

  // ───────────────────────────── 输入框行为 ─────────────────────────────

  function autoGrow() {
    el.input.style.height = "auto";
    el.input.style.height = Math.min(el.input.scrollHeight, 132) + "px";
  }

  el.input.addEventListener("input", autoGrow);

  el.input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage(el.input.value);
    }
  });

  el.composer.addEventListener("submit", (e) => {
    e.preventDefault();
    sendMessage(el.input.value);
  });

  el.reset.addEventListener("click", () => {
    if (state.busy) return;
    state.sessionId = null;
    state.personaId = null;
    state.lastTurnId = null;
    state.streamEl = null;
    state.streamText = "";

    el.messages.innerHTML =
      '<div class="empty-state" id="empty-state">' +
      '<p class="empty-title">还没有开始</p>' +
      '<p class="muted small">在左边选一个人格，然后说第一句话。<br>她会记得你说过的事。</p>' +
      "</div>";
    el.emptyState = $("empty-state");
    el.input.value = "";
    el.input.disabled = true;
    el.send.disabled = true;
    el.affectBadge.className = "affect-badge is-neutral";
    el.affectLabel.textContent = "中性";
    el.padP.style.width = "50%";
    el.padA.style.width = "35%";
    el.padD.style.width = "40%";
    el.relLevel.textContent = "—";
    el.relIntimacy.textContent = "";
    renderMemories([]);
    el.tracePanel.innerHTML =
      '<p class="muted small">聊一句，这里会显示这一轮的召回与 token 分布。</p>';
    for (const b of el.personaList.querySelectorAll(".persona-item")) {
      b.classList.remove("is-active");
    }
    // 场景保留在「不设定」，进度清空
    const noneBtn = el.scenarioList.querySelector(".is-none");
    if (noneBtn) highlight(el.scenarioList, noneBtn);
    state.scenarioId = null;
    state.scenarioBeats = {};
    el.sceneProgressCard.hidden = true;
    renderCompanion(null);
    state.diaryHighlightId = null;
    el.btnWriteDiary.disabled = true;
    el.diaryHint.textContent = "";
    el.diaryList.innerHTML =
      '<p class="diary-empty">还没有日记。聊过之后，让她写下今天。</p>';
    el.diaryCount.textContent = "0";
    setConn("未连接", "pill-idle");
  });

  // 语音（M4-C）。按钮在不支持录音的环境里保持 disabled（见 canRecord）。
  if (el.btnMic) {
    el.btnMic.addEventListener("click", toggleRecording);
    el.btnMic.disabled = !canRecord();
    if (!canRecord()) {
      el.btnMic.title = "这个浏览器不支持录音（需要 https 或 localhost）";
    }
  }

  // 账号（M3-H）
  el.btnShowAuth.addEventListener("click", () => openAuth("login"));
  el.btnLogout.addEventListener("click", () => {
    setToken("");
    state.account = null;
    // 内存里保留当前会话，但身份退回「本机匿名 id」——
    // 必须先记住匿名 id 再退出，否则退出后写到账号名下就成了跨账号写（后端会 409）。
    state.userId = ensureUserId();
    renderAccount();
    // 退出后 owner 不再是自己的 → 上架入口必须立刻消失（判据 4 的反向）
    refreshPersonaViews();
    setConn("已退出 · 数据在本机", "pill-idle");
  });
  el.btnAuthSwitch.addEventListener("click", () => {
    openAuth(state.authMode === "register" ? "login" : "register");
  });
  el.btnAuthClose.addEventListener("click", closeAuth);
  el.authSubmit.addEventListener("click", submitAuth);
  el.authPassword.addEventListener("keydown", (e) => {
    if (e.key === "Enter") submitAuth();
  });

  // 手动校正人生阶段（M2-B）：改完立即保存
  el.companionStage.addEventListener("change", (e) => {
    saveCompanionStage(e.target.value);
  });

  // ────────────────── 模型配置（界面里填 key，免重启） ──────────────────
  //
  // 这个面板存在的原因很朴素：换真模型只有「改环境变量 + 重启」一条路，
  // 而用户根本不知道 `LINGER_LLM=openai` 这种词该怎么拼。
  //
  // 三条界面上的纪律：
  //   1. **key 不回填**。服务端只返回是否配了 + 末四位提示，输入框永远是空的；
  //      空着提交 = 不修改（否则「只想换个模型名」会把 key 抹掉）。
  //   2. **保存前先试一次**。连通性失败要当场说，不能存进去以后一聊天就报错。
  //   3. **mock 是合法选项**，不是「没配置」——所以置灰字段而不是隐藏它们。

  function setLlmMsg(ok, text) {
    el.llmStatus.hidden = true;
    el.llmOk.hidden = true;
    if (!text) return;
    const node = ok ? el.llmOk : el.llmStatus;
    node.textContent = text;
    node.hidden = false;
  }

  function syncLlmFields() {
    const isRemote = el.llmProvider.value !== "mock";
    // 置灰而不是隐藏：隐藏会让人以为「没有这个选项」，置灰才看得出「这里能填，只是现在不用」
    for (const node of [el.llmBaseUrl, el.llmModel, el.llmApiKey]) {
      node.disabled = !isRemote;
    }
    // mock 下清空显示：留着上次的 openai 地址会让人以为「我没填怎么就有了」。
    // 切回 openai 时把之前的值放回去，省得重打一遍。
    if (isRemote) {
      for (const [node, key] of [[el.llmBaseUrl, "savedBase"], [el.llmModel, "savedModel"]]) {
        if (!node.value && node.dataset[key]) node.value = node.dataset[key];
      }
    } else {
      el.llmBaseUrl.dataset.savedBase = el.llmBaseUrl.value || el.llmBaseUrl.dataset.savedBase || "";
      el.llmModel.dataset.savedModel = el.llmModel.value || el.llmModel.dataset.savedModel || "";
      el.llmBaseUrl.value = "";
      el.llmModel.value = "";
    }
    const ph = isRemote ? null : "（mock 不需要）";
    el.llmBaseUrl.placeholder = ph || "https://api.deepseek.com/v1";
    el.llmModel.placeholder = ph || "deepseek-chat";
    el.llmApiKey.placeholder = ph || "sk-...";
  }

  function renderLlmSettings(payload) {
    const eff = (payload && payload.effective) || {};
    const isMock = !eff.provider || eff.provider === "mock";
    el.llmProvider.value = isMock ? "mock" : "openai";
    // 先把服务端的值放进 dataset，再由 syncLlmFields 决定「现在该不该显示」
    el.llmBaseUrl.value = eff.base_url || "";
    el.llmModel.value = eff.model || "";
    el.llmBaseUrl.dataset.savedBase = eff.base_url || "";
    el.llmModel.dataset.savedModel = eff.model || "";
    el.llmApiKey.value = ""; // 永远不回填
    if (payload && payload.file) el.llmFilePath.textContent = payload.file;

    const bits = [isMock ? "mock（免密钥，不联网）" : eff.provider];
    if (!isMock) {
      if (eff.model) bits.push(eff.model);
      bits.push(eff.api_key_set ? `key ${eff.api_key_hint}` : "未配置 key");
    }
    el.llmHint.textContent = "当前生效：" + bits.join(" · ");
    syncLlmFields();
  }

  async function loadLlmSettings() {
    try {
      renderLlmSettings(await api("/v1/settings/llm"));
    } catch (err) {
      setLlmMsg(false, `读不到当前配置：${err.message}`);
    }
  }

  function openLlmPanel() {
    el.llmPanel.hidden = false;
    setLlmMsg(true, "");
    el.llmHint.textContent = "正在读取当前配置…";
    loadLlmSettings();
  }

  function closeLlmPanel() {
    el.llmPanel.hidden = true;
  }

  async function saveLlmSettings() {
    const provider = el.llmProvider.value;
    const body = { provider };
    if (provider !== "mock") {
      body.base_url = el.llmBaseUrl.value.trim();
      body.model = el.llmModel.value.trim();
    }
    // 只有真填了新 key 才带上；留空＝保持原样（三态语义见后端注释）
    const typed = el.llmApiKey.value.trim();
    if (typed) body.api_key = typed;

    const btn = el.btnLlmSave;
    const label = btn.textContent;
    btn.disabled = true;
    btn.textContent = "保存中…";
    setLlmMsg(true, "正在保存并试连（最多几秒）…");
    try {
      const out = await api("/v1/settings/llm", { method: "PUT", body: JSON.stringify(body) });
      renderLlmSettings(out);
      const probe = out.probe || {};
      if (probe.ok) {
        setLlmMsg(
          true,
          `已生效 · ${probe.provider || provider} · ${probe.latency_ms}ms · 回了一句「${probe.reply || ""}」。回聊天区继续就行，会话不会断。`
        );
      } else {
        setLlmMsg(false, `保存了但试连没通：${probe.error || "未知原因"}`);
      }
    } catch (err) {
      // 后端在试连失败时会回滚并返回 400，所以这里文字要让人知道「没生效」
      setLlmMsg(false, `${err.message}`);
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  }

  async function testLlm() {
    setLlmMsg(true, "正在试连…");
    try {
      const out = await api("/v1/settings/llm/test", { method: "POST" });
      if (out.ok) {
        setLlmMsg(true, `通了 · ${out.provider || ""} · ${out.latency_ms}ms · 回了一句「${out.reply || ""}」`);
      } else {
        setLlmMsg(false, `没通：${out.error || "未知原因"}`);
      }
    } catch (err) {
      setLlmMsg(false, `试连失败：${err.message}`);
    }
  }

  async function clearLlmSettings() {
    setLlmMsg(true, "正在清除…");
    try {
      renderLlmSettings(await api("/v1/settings/llm", { method: "DELETE" }));
      setLlmMsg(true, "已清除，回到免密钥（mock）。下次启动也还是 mock。");
    } catch (err) {
      setLlmMsg(false, `清除失败：${err.message}`);
    }
  }

  el.btnShowLlm.addEventListener("click", openLlmPanel);
  el.btnLlmClose.addEventListener("click", closeLlmPanel);
  el.btnLlmSave.addEventListener("click", saveLlmSettings);
  el.btnLlmTest.addEventListener("click", testLlm);
  el.btnLlmClear.addEventListener("click", clearLlmSettings);
  el.llmProvider.addEventListener("change", syncLlmFields);
  // 在面板里按「模型/地址/模型名/key」任意一处回车，等同点「保存并测试」
  for (const node of [el.llmBaseUrl, el.llmModel, el.llmApiKey]) {
    node.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        saveLlmSettings();
      }
    });
  }
  el.llmPanel.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeLlmPanel();
  });
  // 点遮罩空白处关闭（和常见弹层的预期一致）
  el.llmPanel.addEventListener("click", (e) => {
    if (e.target === el.llmPanel) closeLlmPanel();
  });
  syncLlmFields();

  // ───────────────────────────── 启动 ─────────────────────────────

  // 先确认登录态再拉数据：人格列表与「我的人格」都按 user_id 过滤，
  // 顺序反了会先按匿名 id 渲染一遍再闪一下换成账号的。
  // 给自动化测试留的把手（#39）。**不是 API**：只暴露「页面按钮绑定的那几个函数」，
  // 让测试能不打 DOM 就驱动四条创建入口——否则每条判据都要模拟一整张表单，
  // 测的就不再是「有没有带 owner」而是「我的假 DOM 写得对不对」。
  // 生产环境里它就是几个挂在 window 上的函数引用，不改变任何行为。
  window.__lingerTestHooks = {
    // 向导只认 DOM 里的输入值，所以测试得先把名字填进去（页面里用户也是这么做的）
    fillWizardName: (name) => { el.wizName.value = name; },
    startSession,
    confirmDraft,
    confirmDistill,
    wizardCreate,
    openSession,
    isOwnPersona,
    listingGate,
    ownerClaim,
    // 语音（M4-C）。前端判据只能断到「按钮存在 + 点了会调 /v1/asr +
    // 结果进了输入框」——真录音断言不了，所以不引入浏览器自动化。
    toggleRecording,
    uploadRecording,
    playMessage,
    attachPlayButton,
    canRecord,
    // 成本看板（M4-D）。判据 6 只能断到「面板真的渲染出了 /v1/costs 响应里的数字」——
    // grep 证明不了这一点，所以这两个入口要能被 harness 调到。
    loadCosts,
    renderCosts,
    // 模型配置（M4 收口后补）。判据要能断到「填了 key 真的走了 PUT 且生效了」，
    // 而不是「页面上有个输入框」——所以这几个入口也得能被 harness 调到。
    openLlmPanel,
    renderLlmSettings,
    saveLlmSettings,
    testLlm,
    clearLlmSettings,
  };

  restoreAccount().finally(() => {
    loadTemplates();
    loadMyPersonas();
    loadScenarios();
    // 市场与上面三块互不依赖：失败了只影响市场面板，聊天照旧。
    loadMarket();
    loadMarketMine();
    loadCosts();
  });

  // 页面打开时顺手探一下服务是否活着
  fetch("/health")
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error("health " + r.status))))
    .then(() => {
      if (!state.sessionId) setConn("服务就绪", "pill-idle");
    })
    .catch(() => setConn("服务未连上", "pill-error"));
})();
