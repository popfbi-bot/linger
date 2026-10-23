"""从 templates/scenarios/*.yaml 加载场景。

与 persona/templates.py 同一套做法：新增场景只需放一个 yaml，不改代码。

**两处来源，用户优先**：
    · `~/.linger/scenarios/`  —— 用户自建（可写，`LINGER_SCENARIO_DIR` 可覆盖）
    · 包内 `templates/scenarios/` —— 内置场景（只读，装成包后不可写）
同 id 时用户场景覆盖内置——用户改过的版本应该生效。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .models import Scenario
from .writer import builtin_scenario_dir, user_scenario_dir


def _paths() -> list[Path]:
    """两处目录的 yaml；用户目录在前（优先）。"""
    out: list[Path] = []
    for d in (user_scenario_dir(), builtin_scenario_dir()):
        if d.exists():
            out.extend(sorted(d.glob("*.yaml")))
    return out


def _source_of(path: Path) -> str:
    """按**文件实际所在目录**判来源，不按 id 猜。

    为什么不按 id：loader 本来就支持「yaml 里的 id 与文件名不一致」，
    按 id 猜会猜错。文件躺在用户目录里，它就是用户的——哪怕 id 撞了内置场景
    （手工放的影子文件，读取机制保留，见 test_手工影子仍生效）。
    """
    try:
        path.relative_to(user_scenario_dir())
        return "user"
    except ValueError:
        return "builtin"


def _brief(path: Path, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": data.get("id") or path.stem,
        "title": data.get("title") or path.stem,
        "setting": data.get("setting", ""),
        "user_role": data.get("user_role", ""),
        "beat_count": len(data.get("beats") or []),
        "source": _source_of(path),
    }


def list_scenarios() -> list[dict[str, Any]]:
    """场景摘要清单（供 GET /v1/scenarios）。同 id 只出一次（用户版覆盖内置版）。"""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in _paths():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        sid = data.get("id") or path.stem
        if sid in seen:
            continue
        seen.add(sid)
        out.append(_brief(path, data))
    return out


def load_scenario(scenario_id: str) -> Scenario:
    """按 id 加载完整场景；不存在抛 FileNotFoundError。

    先按文件名在「用户目录 → 内置目录」里找，再回退到扫描
    （yaml 里的 id 与文件名不一致时也不会失效）。
    """
    for base in (user_scenario_dir(), builtin_scenario_dir()):
        path = base / f"{scenario_id}.yaml"
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
