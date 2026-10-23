"""M3-G 前端验收判据：编辑入口只给用户场景（issue #31 判据 2）。

用锚点切函数喂最小 DOM（延续 test_persona_generate_frontend 的做法，不引 jsdom）。
判据：
    1. scenarioItemNode 只对 source === "user" 的场景挂「编辑」入口
    2. 两个方向的变异都会红（恒真 / 恒假）——只测一边的话
       「永远不给编辑」也能骗过判据
    3. 编辑时 id 只读（改 id 会断掉已开会话的进度）
    4. 提交时编辑态带 overwrite: true
    5. lineToBeat / beatToLine 往返一致（表单一行 ↔ 节拍一个）
"""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "src" / "linger" / "server" / "static"
JS = (STATIC / "app.js").read_text(encoding="utf-8")
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
CSS = (STATIC / "style.css").read_text(encoding="utf-8")


def _fn(name: str) -> str:
    """从 app.js 里切出一个完整函数（按缩进回退到同级；兼容 async）。

    结尾锚是「换行 + 2 空格 + 单个 }」——之前写成 \\n  \\}\\}（两个 }），
    非贪婪的 .*? 会一路吞到后面对象字面量的 }}，切出残缺拼接：
    单跑对、测试挂，就是这么来的。
    """
    m = re.search(rf"  (?:async )?function {re.escape(name)}\(.*?\n  \}}(?!\}})", JS, re.S)
    assert m, f"app.js 里找不到函数 {name}"
    return m.group(0)


# --------------------------------------------------------------------------- #
# 1. 编辑入口只给 source === "user"
# --------------------------------------------------------------------------- #
def test_edit_entry_gated_by_source():
    """scenarioItemNode 里的编辑入口必须被 source === "user" 守着。

    反例 A：把条件改成恒真（if (true)）→ 内置场景也有编辑入口（红）
    反例 B：把条件改成恒假 → 用户场景没有编辑入口（红）
    """
    body = _fn("scenarioItemNode")
    assert 's.source === "user"' in body, (
        "编辑入口必须按 s.source === 'user' 判断（双方向变异都要能红）"
    )
    # 入口的真实行为：点编辑调 enterScenarioEdit
    assert "enterScenarioEdit" in body
    assert "stopPropagation" in body, "点编辑不能同时触发场景选中"


def test_scenario_item_is_a_function():
    """场景项必须是独立函数——内联在 loadScenarios 里没法被锚点切出来测。"""
    assert "function scenarioItemNode(" in JS


# --------------------------------------------------------------------------- #
# 2. 编辑态的行为
# --------------------------------------------------------------------------- #
def test_enter_edit_loads_detail_and_locks_id():
    """enterScenarioEdit：拉详情、预填表单、**id 只读**。

    改 id 会让已开会话的进度对不上——这是判据 3 的前端一半。
    反例：去掉 `el.scId.readOnly = true` → 红。
    """
    body = _fn("enterScenarioEdit")
    assert 'api(`/v1/scenarios/${encodeURIComponent(id)}`)' in body, "要拉详情预填"
    assert 'el.scId.readOnly = true' in body, "编辑态 id 必须只读"
    assert 'detail.source !== "user"' in body, "详情侧还要再验一次 source（防列表数据过期）"
    assert "beatToLine" in body, "节拍要反填成表单行"


def test_submit_in_edit_mode_sends_overwrite():
    """createScenario 在编辑态必须带 overwrite: true。

    反例：删掉 `if (editingScenarioId) body.overwrite = true` → 409（红）。
    """
    m = re.search(r"async function createScenario\(.*?\n  \}", JS, re.S)
    assert m, "找不到 createScenario"
    body = m.group(0)
    assert "editingScenarioId" in body, "提交逻辑要感知编辑态"
    assert re.search(r"if\s*\(\s*editingScenarioId\s*\)\s*body\.overwrite\s*=\s*true", body), (
        "编辑态必须带 body.overwrite = true"
    )
    assert "exitScenarioEdit" in body, "提交成功后要退出编辑态（否则按钮文案还是「保存修改」）"


def test_exit_edit_restores_create_mode():
    """exitScenarioEdit：id 解锁、按钮文案复原。"""
    body = _fn("exitScenarioEdit")
    assert 'el.scId.readOnly = false' in body
    assert "创建场景" in body


# --------------------------------------------------------------------------- #
# 3. 往返一致：beatToLine ∘ lineToBeat = id
# --------------------------------------------------------------------------- #
def test_beat_roundtrip():
    """表单一行 ↔ 节拍一个，往返后信息不丢。

    用 Node 跑真函数（锚点切出来拼最小环境），不 grep 文本——
    grep 证明不了「往返一致」这种行为判据。
    """
    import subprocess

    a = _fn("beatToLine")
    b = _fn("lineToBeat")
    script = f"{a}\n{b}\n"
    script += """
const cases = [
  {goal: "她提起辞职", trigger: ["辞职", "不想干了"]},
  {goal: "只是聊天", trigger: []},
  {goal: "带全角分隔", trigger: ["雨", "伞"]},
];
for (const beat of cases) {
  const line = beatToLine(beat);
  const back = lineToBeat(line);
  if (back.goal !== beat.goal) { console.error("goal 不一致", line, back); process.exit(1); }
  if (JSON.stringify(back.trigger) !== JSON.stringify(beat.trigger)) {
    console.error("trigger 不一致", line, back); process.exit(1);
  }
}
// 行→拍→行 也要稳（幂等）
const line2 = "她提起辞职｜辞职、不想干了";
const twice = beatToLine(lineToBeat(line2));
if (twice !== line2) { console.error("行→拍→行 不幂等", line2, twice); process.exit(1); }
console.log("roundtrip-ok");
"""
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"往返不一致：{r.stderr or r.stdout}"


# --------------------------------------------------------------------------- #
# 4. 资源
# --------------------------------------------------------------------------- #
def test_edit_button_has_style():
    assert ".scenario-edit" in CSS, "编辑入口要有样式，不能是裸文本"


def test_no_new_cdn_dependencies():
    for pat in (r'src="https?://', r'href="https?://', r"@import\s+url\(http"):
        assert not re.search(pat, HTML), f"引了外部资源: {pat}"
        assert not re.search(pat, CSS), f"样式引了外部资源: {pat}"
