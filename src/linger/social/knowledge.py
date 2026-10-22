"""从原始语料中抽取结构化行为条目，并提供按关系阶段 / 人生阶段查询的接口。

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

    第六份语料「陪伴缺失与有毒关系」是"陪伴"维度的负面镜像：
    上篇按人生阶段列陪伴缺失（胎儿期→老年期），下篇按关系类型列有毒陪伴。
    它不混入 stage 边界（避免污染 boundaries()），而是挂在独立的
    dimension="companion_negative" 维度下，供陪伴模式识别「有毒关系」信号。
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
    ("陪伴缺失与有毒关系_负面清单.md", "negative", "陪伴"),
]

# 每个文件的标题（去掉"一、二、…"前缀）
_STAGE_TITLES: dict[str, str] = {
    "初遇美好_正面清单.md": "初遇",
    "初遇糟糕_负面清单.md": "初遇",
    "暗生情愫_正面清单.md": "升温",
    "相处雷区_负面清单.md": "相处",
    "人生阶段陪伴需求与角色.md": "陪伴",
    "陪伴缺失与有毒关系_负面清单.md": "陪伴",
}

# 人生阶段规范名（用于 companion 维度的阶段索引）
_LIFE_STAGES = (
    "胎儿期", "婴儿期", "幼儿期", "童年期", "青春期",
    "青年期", "中年期", "老年期",
)


@dataclass
class SocialItem:
    """一条人际行为知识。"""

    corpus: str            # 来源文件
    stage: str             # 初遇 / 升温 / 相处 / 陪伴
    polarity: str          # positive / negative / companion
    text: str              # 条目原文（已去掉数字编号）
    section: str = ""      # 原文件的章节标题（如"目光交汇的瞬间"）
    red_flag: bool = False # 是否为关系红灯
    dimension: str = "stage"   # stage=关系阶段边界 / companion_negative=陪伴缺失与有毒关系
    toxic: bool = False    # 是否为「有毒关系」条目（下篇）

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
        self._by_life_stage: dict[str, list[SocialItem]] = {}
        self._load_all(data_dir)

    # ------------------------------------------------------------------ #
    def _load_all(self, data_dir: Path) -> None:
        for filename, polarity, stage in _CORPORA:
            path = data_dir / filename
            if not path.exists():
                continue
            section = ""
            dimension = "companion_negative" if filename == "陪伴缺失与有毒关系_负面清单.md" else "stage"
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line:
                    continue
                # 标题行（## 或 ### 开头）
                m = re.match(r"^#{2,3}\s+(.*)$", line)
                if m:
                    section = _clean_title(m.group(1), filename)
                    # 下篇从「九、」开始（九=9 及以后）
                    toxic = dimension == "companion_negative" and bool(
                        re.match(r"^[九]|^十", m.group(1))
                    )
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
                            dimension=dimension,
                            toxic=toxic,
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
            if item.dimension == "companion_negative":
                # 上篇：按人生阶段索引（胎儿期/婴儿期/…/老年期）
                for life_stage in _LIFE_STAGES:
                    if life_stage in item.section:
                        self._by_life_stage.setdefault(life_stage, []).append(item)
                        break

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

    # ------------------------------------------------------------------ #
    # 陪伴缺失与有毒关系（dimension="companion_negative"）
    # ------------------------------------------------------------------ #
    def companion_pitfalls(self, life_stage: str | None = None, limit: int = 10) -> list[str]:
        """「陪伴缺失 / 有毒关系」负面信号。

        life_stage：胎儿期/婴儿期/幼儿期/童年期/青春期/青年期/中年期/老年期。
            传 None 返回全部（下篇的有毒关系类型）。
        """
        if life_stage is None:
            items = [i for i in self.items if i.dimension == "companion_negative"]
        else:
            items = self._by_life_stage.get(life_stage, [])
            if not items:
                # 上篇未命中时，回退到下篇（有毒关系类型）最相关的
                items = [i for i in self.items if i.dimension == "companion_negative" and i.toxic]
        return [i.text for i in items][:limit]

    def positive_demands(self, life_stage: str | None = None, limit: int = 400) -> list[str]:
        """「人生各阶段陪伴需求」的正面条目（上篇，polarity="companion"）。

        与 `companion_pitfalls()` 正好是一对：那边是「陪伴缺失会怎样」，
        这边是「该被怎么陪伴」。两处共用同一套人生阶段标题（`一、胎儿期（出生前）`），
        但 `companion_pitfalls` 只索引了负面语料，所以正面需求要单独按标题前缀取。
        """
        items = [i for i in self.by_stage("陪伴") if i.polarity == "companion"]
        if life_stage:
            items = [i for i in items if life_stage in i.section]
        return [i.text for i in items][:limit]

    def toxic_relation_types(self) -> list[str]:
        """下篇的有毒关系类型清单（亲情/友情/职场/亲密/其他）。"""
        return list(dict.fromkeys(
            i.section for i in self.items
            if i.dimension == "companion_negative" and i.toxic
        ))

    def toxic_relation_items(self, section: str | None = None, limit: int = 15) -> list[str]:
        """按有毒关系类型返回条目。section 如「亲情里的伤害」「友情里的伤害」等。"""
        items = [
            i for i in self.items
            if i.dimension == "companion_negative" and i.toxic
            and (section is None or i.section == section)
        ]
        return [i.text for i in items][:limit]

    def companion_recovery(self, limit: int = 10) -> list[str]:
        """「如何走出孤独与有毒关系」的行动建议。"""
        return [i.text for i in self.items
                if i.dimension == "companion_negative" and i.section == "如何走出孤独与有毒关系"][:limit]


def _clean_title(title: str, filename: str) -> str:
    """去掉章节标题里的"一、校园里""二、旅途中的"等前缀。

    特殊处理：
    - 「人生阶段陪伴需求与角色.md」保留原标题（章节名即人生阶段）。
    - 「陪伴缺失与有毒关系_负面清单.md」：上篇（一~八）保留人生阶段标题，
      下篇（九~十三）去掉序号前缀，只留类型名（如「亲情里的伤害」）。
    """
    if filename == "陪伴缺失与有毒关系_负面清单.md":
        if re.match(r"^[一二三四五六七八]、", title):
            return title  # 上篇：保留人生阶段标题
        # 下篇：去掉序号前缀，并截断括号补充说明（如「亲情里的伤害（最亲的人最狠的刀）」→「亲情里的伤害」）
        cleaned = re.sub(r"^[一二三四五六七八九十]+、", "", title).strip()
        return re.split(r"[（(]", cleaned)[0].strip()
    if filename == "人生阶段陪伴需求与角色.md":
        return title
    return re.sub(r"^[一二三四五六七八九十]+、", "", title).strip()


def _strip_bold(text: str) -> str:
    """去掉 **加粗** 标记，保留内容。"""
    return re.sub(r"\*\*(.*?)\*\*", r"\1", text)