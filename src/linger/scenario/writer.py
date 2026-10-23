"""场景写入：用户自建场景落成 YAML。

## 为什么写用户目录，不写包目录

内置场景在 `src/linger/templates/scenarios/`——那是**包内资源**。
`pip install` 之后它在 site-packages 里，普通用户没有写权限；
就算能写，重装/升级也会被覆盖。所以用户自建场景一律写
`~/.linger/scenarios/`（可用 `LINGER_SCENARIO_DIR` 覆盖），
`loader` 会同时扫两处，用户场景优先展示。

## 为什么是 YAML 而不是数据库

与 `persona/templates.py` 保持同一套做法：一个场景就是一个可读、可手改、
可提交到 git 的文本文件。用户想微调某句开场白，用记事本就能改，
不需要连数据库。代价是多实例部署时文件不共享——真到那一步再迁 DB。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from .models import Scenario

# id 要能当文件名用：小写字母/数字/下划线，2~32 位，不能以数字串打头（避免歧义）
SCENARIO_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")


class ScenarioWriteError(ValueError):
    """场景内容不合法（用户填错了）。HTTP 层映射 422。"""


class ScenarioExistsError(ScenarioWriteError):
    """同 id 场景已存在且未要求覆盖。HTTP 层映射 409。"""


def user_scenario_dir() -> Path:
    """用户场景目录；可用 `LINGER_SCENARIO_DIR` 覆盖（测试与部署都用它）。"""
    raw = os.environ.get("LINGER_SCENARIO_DIR", "").strip()
    return Path(raw) if raw else Path.home() / ".linger" / "scenarios"


def builtin_scenario_dir() -> Path:
    """内置场景目录（包内资源，只读）。"""
    return Path(__file__).resolve().parent.parent / "templates" / "scenarios"


def _normalize(payload: dict[str, Any]) -> dict[str, Any]:
    """校验并补全，返回可直接 dump 的 dict。

    节拍允许只给 `goal`（用户不想写触发词），此时自动编号 b1/b2…
    并给一个空 trigger —— 空触发的节拍永远不会自动推进，
    这比「猜一个触发词」诚实：用户可以手动标记完成。
    """
    if not isinstance(payload, dict):
        raise ScenarioWriteError("场景必须是一个对象")

    data = dict(payload)

    sid = str(data.get("id") or "").strip()
    if not SCENARIO_ID_RE.match(sid):
        raise ScenarioWriteError(
            "场景 id 只能用小写字母、数字、下划线，2~32 位，且以字母开头"
        )
    data["id"] = sid

    title = str(data.get("title") or "").strip()
    if not title:
        raise ScenarioWriteError("场景标题不能为空")
    if len(title) > 60:
        raise ScenarioWriteError("场景标题不能超过 60 字")
    data["title"] = title

    for key, limit in (
        ("world", 200),
        ("setting", 2000),
        ("opening_narration", 2000),
        ("user_role", 200),
    ):
        value = data.get(key) or ""
        if not isinstance(value, str):
            raise ScenarioWriteError(f"{key} 必须是字符串")
        if len(value) > limit:
            raise ScenarioWriteError(f"{key} 不能超过 {limit} 字")
        data[key] = value.strip()

    beats_raw = data.get("beats") or []
    if not isinstance(beats_raw, list):
        raise ScenarioWriteError("beats 必须是数组")
    if len(beats_raw) > 20:
        raise ScenarioWriteError("节拍最多 20 个")

    beats: list[dict[str, Any]] = []
    for i, raw in enumerate(beats_raw, start=1):
        if isinstance(raw, str):
            raw = {"goal": raw}
        if not isinstance(raw, dict):
            raise ScenarioWriteError(f"第 {i} 个节拍格式不对")
        goal = str(raw.get("goal") or "").strip()
        if not goal:
            raise ScenarioWriteError(f"第 {i} 个节拍缺少描述（goal）")
        if len(goal) > 200:
            raise ScenarioWriteError(f"第 {i} 个节拍描述超过 200 字")
        trigger = raw.get("trigger") or []
        if isinstance(trigger, str):
            trigger = [trigger]
        if not isinstance(trigger, list):
            raise ScenarioWriteError(f"第 {i} 个节拍的 trigger 必须是数组")
        beats.append(
            {
                "id": str(raw.get("id") or f"b{i}").strip() or f"b{i}",
                "goal": goal,
                "trigger": [str(t).strip() for t in trigger if str(t).strip()],
                "done": False,
            }
        )

    # 节拍 id 不能重复（重复会让「完成进度」互相覆盖）
    seen: set[str] = set()
    for b in beats:
        if b["id"] in seen:
            raise ScenarioWriteError(f"节拍 id 重复：{b['id']}")
        seen.add(b["id"])

    data["beats"] = beats

    for key, limit in (("constraints", 20), ("exit_conditions", 20)):
        items = data.get(key) or []
        if not isinstance(items, list):
            raise ScenarioWriteError(f"{key} 必须是数组")
        data[key] = [str(x).strip() for x in items if str(x).strip()][:limit]

    return data


def save_scenario(payload: dict[str, Any], *, overwrite: bool = False) -> Path:
    """校验并写入用户场景目录，返回文件路径。

    `overwrite=False` 时同 id 已存在 → ScenarioExistsError（HTTP 409）。

    内置场景拦截（M3-G）：写入的 id 撞**内置**场景、且用户目录里还没有这个文件
    → ScenarioExistsError（HTTP 409）。
    只拦「写下去会**新造**影子」这一步——用户目录里已有同名文件的
    （历史遗留 / 手工放的），它就是用户的场景，照改不误，
    否则用户永远改不动自己那个场景。
    """
    data = _normalize(payload)

    # 先过一遍模型：保证写出去的 YAML 能被 load_scenario 正常读回
    try:
        Scenario.model_validate(data)
    except Exception as exc:  # pydantic 校验失败
        raise ScenarioWriteError(f"场景数据不合法：{exc}") from None

    target_dir = user_scenario_dir()
    path = target_dir / f"{data['id']}.yaml"

    if not path.exists():
        builtin = builtin_scenario_dir() / f"{data['id']}.yaml"
        if builtin.exists():
            # 会新造一个「影子覆盖内置」的文件 → 拒绝。
            # 内置场景是产品资产：改它的正确方式是改源码里的 yaml，不是运行时覆盖。
            raise ScenarioExistsError(
                f"{data['id']} 是内置场景，不能覆盖。想改它请改"
                f" src/linger/templates/scenarios/{data['id']}.yaml 后重新打包"
            )

    if path.exists() and not overwrite:
        raise ScenarioExistsError(f"场景已存在：{data['id']}")

    target_dir.mkdir(parents=True, exist_ok=True)
    # newline="\n"：不写 CRLF。Windows 文本模式默认会写 \r\n，
    # 让场景文件在 git 里显示为整文件变更（本窗口踩过三次）。
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False, width=100)

    return path


def delete_scenario(scenario_id: str) -> bool:
    """删除用户场景。内置场景不可删（返回 False）。

    只删用户目录里的文件——内置场景是产品资产，不该被运行时删掉。
    """
    if not SCENARIO_ID_RE.match(scenario_id or ""):
        return False
    path = user_scenario_dir() / f"{scenario_id}.yaml"
    if not path.exists():
        return False
    path.unlink()
    return True


__all__ = [
    "SCENARIO_ID_RE",
    "ScenarioExistsError",
    "ScenarioWriteError",
    "builtin_scenario_dir",
    "delete_scenario",
    "save_scenario",
    "user_scenario_dir",
]