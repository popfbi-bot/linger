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
    personaName: "",
    busy: false,
    lastTurnId: null,
    streamEl: null,     // 当前正在流式写入的气泡
    streamText: "",
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
    personaList: $("persona-list"),
    myPersonaList: $("my-persona-list"),
    myPersonaCount: $("my-persona-count"),
    personaCreatePanel: $("persona-create-panel"),
    personaDesc: $("persona-desc"),
    btnShowCreate: $("btn-show-create"),
    btnGenerate: $("btn-generate"),
    personaGenerateHint: $("persona-generate-hint"),
    personaDraft: $("persona-draft"),
    scenarioList: $("scenario-list"),
    sceneCount: $("scene-count"),
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

  async function api(path, options = {}) {
    const resp = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    if (!resp.ok) {
      let detail = resp.statusText;
      try {
        const body = await resp.json();
        detail = body.detail || detail;
        if (Array.isArray(detail)) detail = detail.map((d) => d.msg).join("；");
      } catch (_) { /* body 不是 JSON，用 statusText */ }
      throw new Error(`${resp.status} ${detail}`);
    }
    return resp.json();
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
      renderMyPersonas(data.personas || []);
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
        body: JSON.stringify({ persona }),
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
        el.scenarioList.appendChild(btn);
      }
    } catch (err) {
      el.scenarioList.innerHTML =
        `<p class="memory-empty">场景加载失败：${escapeHtml(err.message)}</p>`;
    }
  }

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
        body: JSON.stringify({ template_id: state.templateId }),
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
      user_id: state.userId,   // 浏览器端单用户；多用户时换成登录态
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
    el.input.focus();

    setConn(`与${personaName}对话中`, "pill-live");
    // 「我的人格」里的高亮跟着走（新造的人格也要出现在列表里）
    await loadMyPersonas();
    showSceneProgress({ done_beats: [], pending_beats: Object.keys(state.scenarioBeats) });
    addMessage("ai", `（${personaName}在这里。）`);
    // 陪伴档案：新会话开始时拉一次（可能已有旧档案）
    await refreshCompanion();
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
      el.input.focus();
    }
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
    setConn("未连接", "pill-idle");
  });

  // 手动校正人生阶段（M2-B）：改完立即保存
  el.companionStage.addEventListener("change", (e) => {
    saveCompanionStage(e.target.value);
  });

  // ───────────────────────────── 启动 ─────────────────────────────

  loadTemplates();
  loadMyPersonas();
  loadScenarios();

  // 页面打开时顺手探一下服务是否活着
  fetch("/health")
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error("health " + r.status))))
    .then(() => {
      if (!state.sessionId) setConn("服务就绪", "pill-idle");
    })
    .catch(() => setConn("服务未连上", "pill-error"));
})();
