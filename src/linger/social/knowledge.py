"""从五份原始语料中抽取结构化行为条目，并提供按关系阶段查询的接口。

设计：
    原始文件是"人写的清单"，保留原文作为最完整的知识源；
    抽取模块把"数字标号条目"解析成可查询的条目列表，供引擎和编译器消费。

    每个条目都属于一个极性（positive/negative）和一个阶段（stage），
    阶段映射到关系引擎的等级（relation.level）：
        初遇   → level 0~1（刚认识，决定是否还有下次）
        升温   → level 2~3（朋友，决定是否更近一步）
        相处   → level 3~4（亲近，决定关系能否维持）
        陪伴   → 全阶段（人生阶段×角色，决定陪伴方式是否恰当）

    负面条目同时带有"red_flag"标记，关系引擎在用户反复触发时
    可以据此降低关系进度（unresolved_conflicts）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_DATA_DIR = Path(__file__).resolve().parent / "data"

# 语料 → 极性 / 阶段 / 文件名
_CORPORA: list[tuple[str, str, str]] = [
    ("初遇美好_正面清单.md", "positive", "初遇"),
    ("初遇糟糕_负面清单.md", "negative", "初遇"),
    ("暗生情愫_正面清单.md", "positive", "升温"),
    ("相处雷区_负面清单.md", "negative", "相处"),
    ("人生阶段陪伴需求与角色.md", "companion", "陪伴"),
]

# 每个文件的标题（去掉"一、二、…"前缀）
_STAGE_TITLES: dict[str, str] = {
    "初遇美好_正面清单.md": "初遇",
    "初遇糟糕_负面清单.md": "初遇",
    "暗生情愫_正面清单.md": "升温",
    "相处雷区_负面清单.md": "相处",
    "人生阶段陪伴需求与角色.md": "陪伴",
}


@dataclass
class SocialItem:
    """一条人际行为知识。"""

    corpus: str            # 来源文件
    stage: str             # 初遇 / 升温 / 相处 / 陪伴
    polarity: str          # positive / negative / companion
    text: str              # 条目原文（已去掉数字编号）
    section: str = ""      # 原文件的章节标题（如"目光交汇的瞬间"）
    red_flag: bool = False # 是否为关系红灯

    @property
    def as_prompt(self) -> str:
        """供编译器注入的紧凑格式。"""
        prefix = {"positive": "✓", "negative": "✗", "companion": "·"}[self.polarity]
        return f"{prefix} {self.text}"


class SocialKnowledge:
    """加载并索引全部语料。"""

    def __init__(self, data_dir: Path = _DATA_DIR) -> None:
        self.items: list[SocialItem] = []
        self._by_stage: dict[str, list[SocialItem]] = {}
        self._by_section: dict[tuple[str, str], list[SocialItem]] = {}
        self._load_all(data_dir)

    # ------------------------------------------------------------------ #
    def _load_all(self, data_dir: Path) -> None:
        for filename, polarity, stage in _CORPORA:
            path = data_dir / filename
            if not path.exists():
                continue
            section = ""
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line:
                    continue
                # 标题行（## 或 ### 开头）
                m = re.match(r"^#{2,3}\s+(.*)$", line)
                if m:
                    section = _clean_title(m.group(1), filename)
                    continue
                # 数字标号条目：1. **xxx** 或 1. xxx
                m = re.match(r"^\d+\.\s*(.*)$", line)
                if m:
                    text = _strip_bold(m.group(1)).strip()
                    if text:
                        item = SocialItem(
                            corpus=filename,
                            stage=stage,
                            polarity=polarity,
                            text=text,
                            section=section,
                            red_flag=(polarity == "negative"),
                        )
                        self.items.append(item)
        self._reclassify()
        self._index()

    def _reclassify(self) -> None:
        """把「相处雷区」中适用于早期关系的部分归到升温阶段。

        原始语料只有"相处"一个负面清单，但升温期（level 2~3）同样需要负面边界：
        「刚认识就动手动脚」「第一次见面就谈婚论嫁」这些雷区在升温期就该被禁止。
        按关键词二次分类，让每个阶段都有正负两极。
        """
        early_markers = (
            "刚认识", "第一次见面", "认识第", "刚加", "刚坐下", "一上来", "刚认识就",
            "初次", "第一天", "第一次约会", "初遇", "刚约会", "刚开始",
        )
        for item in self.items:
            if item.corpus != "相处雷区_负面清单.md":
                continue
            if any(m in item.text for m in early_markers):
                item.stage = "升温"

    def _index(self) -> None:
        for item in self.items:
            self._by_stage.setdefault(item.stage, []).append(item)
            key = (item.stage, item.section)
            self._by_section.setdefault(key, []).append(item)

    # ------------------------------------------------------------------ #
    # 查询接口
    # ------------------------------------------------------------------ #
    @property
    def total(self) -> int:
        return len(self.items)

    def by_stage(self, stage: str) -> list[SocialItem]:
        return self._by_stage.get(stage, [])

    def by_polarity(self, polarity: str) -> list[SocialItem]:
        return [i for i in self.items if i.polarity == polarity]

    def boundaries(self, stage: str, limit: int = 12) -> str:
        """生成"这个阶段什么该做、什么不该做"的行为边界文本，供编译器注入。

        按负面优先（雷区比甜点更能防止出戏），再用正面行为补充。
        """
        neg = [i for i in self.by_stage(stage) if i.polarity == "negative"]
        pos = [i for i in self.by_stage(stage) if i.polarity == "positive"]

        lines = [f"## 人际行为边界（{stage}阶段）"]
        if neg:
            lines.append("「不要做」：")
            lines.extend(f"- {i.text}" for i in neg[: limit // 2])
        if pos:
            lines.append("「可以这样做」：")
            lines.extend(f"- {i.text}" for i in pos[: limit // 2])
        return "\n".join(lines)

    def red_flags(self, stage: str, limit: int = 10) -> list[str]:
        """该阶段的"关系红灯"（会让人下头的行为），供关系引擎检测。"""
        return [i.text for i in self.by_stage(stage) if i.red_flag][:limit]

    def companion_profiles(self) -> list[str]:
        """人生阶段陪伴需求摘要（每阶段一条），供场景与编译器使用。"""
        return [i.text for i in self.by_stage("陪伴") if i.section == "上篇：按人生阶段梳理陪伴需求"]


def _clean_title(title: str, filename: str) -> str:
    """去掉章节标题里的"一、校园里""二、旅途中的"等前缀。"""
    if filename == "人生阶段陪伴需求与角色.md":
        return title
    return re.sub(r"^[一二三四五六七八九十]+、", "", title).strip()


def _strip_bold(text: str) -> str:
    """去掉 **加粗** 标记，保留内容。"""
    return re.sub(r"\*\*(.*?)\*\*", r"\1", text)