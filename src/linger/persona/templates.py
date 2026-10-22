"""内置人格模板：一键赋予角色的最小可用集合。

每条模板都是一份完整人格卡（不是"半成品+默认值"），因为模板的价值在于
**开箱即有味道**——用户点开就该感觉到"这是一个具体的人"。

新增模板：在 templates/personas/ 放一个 yaml 即可，无需改代码。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ..models import PersonaCard

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "personas"


def list_templates() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not _TEMPLATE_DIR.exists():
        return out
    for path in sorted(_TEMPLATE_DIR.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        meta = data.get("meta", {})
        out.append(
            {
                "id": path.stem,
                "name": meta.get("name", path.stem),
                "archetype": meta.get("archetype", ""),
                "identity": meta.get("identity", ""),
                "age": meta.get("age"),
                "tags": data.get("provenance", {}).get("tags", []),
                "sample": data.get("provenance", {}).get("sample_line", ""),
            }
        )
    return out


def load_template(template_id: str) -> PersonaCard:
    path = _TEMPLATE_DIR / f"{template_id}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"模板不存在: {template_id}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    # 模板本身不携带固定 id：每次实例化都应是一个新人格（否则多人共用同一份状态）
    data.pop("id", None)
    data.setdefault("provenance", {})
    data["provenance"].setdefault("type", "authored")
    data["provenance"].setdefault("template", template_id)
    return PersonaCard.model_validate(data)
