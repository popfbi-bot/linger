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
    personaName: "",
    busy: false,
    lastTurnId: null,
    streamEl: null,     // 当前正在流式写入的气泡
    streamText: "",
  };

  // ───────────────────────────── DOM ─────────────────────────────

  const $ = (id) => document.getElementById(id);

  const el = {
    personaList: $("persona-list"),
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

      const body = document.createElement("div");
      body.textContent = m.content;

      item.appendChild(tag);
      item.appendChild(body);
      el.memoryList.appendChild(item);
    }
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
      // 1. 实例化人格（模板 → 库里的行）
      const persona = await api("/v1/personas", {
        method: "POST",
        body: JSON.stringify({ template_id: state.templateId }),
      });

      // 2. 开一个会话（带场景，如果选了）
      const body = {
        persona_id: persona.id,
        user_id: "u_web",   // 浏览器端单用户；多用户时换成登录态
      };
      if (state.scenarioId) body.scenario_id = state.scenarioId;

      const session = await api("/v1/sessions", {
        method: "POST",
        body: JSON.stringify(body),
      });

      state.personaId = persona.id;
      state.sessionId = session.session_id;

      // 3. 清空并解锁输入
      el.messages.innerHTML = "";
      renderMemories([]);
      el.tracePanel.innerHTML =
        '<p class="muted small">聊一句，这里会显示这一轮的召回与 token 分布。</p>';
      el.input.disabled = false;
      el.send.disabled = false;
      el.input.focus();

      setConn(`与${state.personaName}对话中`, "pill-live");
      showSceneProgress({ done_beats: [], pending_beats: Object.keys(state.scenarioBeats) });
      addMessage("ai", `（${state.personaName}在这里。）`);
    } catch (err) {
      showError("开始会话失败：" + err.message);
      setConn("未连接", "pill-error");
    } finally {
      state.busy = false;
    }
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
    setConn("未连接", "pill-idle");
  });

  // ───────────────────────────── 启动 ─────────────────────────────

  loadTemplates();
  loadScenarios();

  // 页面打开时顺手探一下服务是否活着
  fetch("/health")
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error("health " + r.status))))
    .then(() => {
      if (!state.sessionId) setConn("服务就绪", "pill-idle");
    })
    .catch(() => setConn("服务未连上", "pill-error"));
})();
