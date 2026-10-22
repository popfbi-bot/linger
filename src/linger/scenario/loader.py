"""从 templates/scenarios/*.yaml 加载场景。

与 persona/templates.py 同一套做法：新增场景只需放一个 yaml，不改代码。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .models import Scenario

_SCENARIO_DIR = Path(__file__).resolve().parent.parent / "templates" / "scenarios"


def _paths() -> list[Path]:
    return sorted(_SCENARIO_DIR.glob("*.yaml")) if _SCENARIO_DIR.exists() else []


def _brief(path: Path, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": data.get("id") or path.stem,
        "title": data.get("title") or path.stem,
        "setting": data.get("setting", ""),
        "user_role": data.get("user_role", ""),
        "beat_count": len(data.get("beats") or []),
    }


def list_scenarios() -> list[dict[str, Any]]:
    """场景摘要清单（供 GET /v1/scenarios）。"""
    out: list[dict[str, Any]] = []
    for path in _paths():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        out.append(_brief(path, data))
    return out


def load_scenario(scenario_id: str) -> Scenario:
    """按 id 加载完整场景；不存在抛 FileNotFoundError。

    id 先按文件名匹配，再回退到扫描（yaml 里的 id 与文件名不一致时也不会失效）。
    """
    path = _SCENARIO_DIR / f"{scenario_id}.yaml"
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        data.setdefault("id", scenario_id)
        return Scenario.model_validate(data)

    for candidate in _paths():
        data = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
        if data.get("id") == scenario_id:
            return Scenario.model_validate(data)

    raise FileNotFoundError(f"场景不存在: {scenario_id}")


def load_scenario_safe(scenario_id: str) -> Scenario | None:
    try:
        return load_scenario(scenario_id)
    except FileNotFoundError:
        return None
