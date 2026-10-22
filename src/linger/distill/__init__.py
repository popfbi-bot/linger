"""聊天记录蒸馏（M3-E，「一键赋角」入口 C，docs/ARCHITECTURE.md §3.1.3）。

三层，职责不重叠：

    parse.py        脏文本 → [(speaker, content)]，多格式宽容解析
    fingerprint.py  确定性风格指纹（**无 LLM**）——句长 / emoji / 口癖 / 高频词
    compose.py      LLM 只补 meta / traits / values，voice 由指纹强制覆盖

合规红线（§3.1.3）：必须勾选授权声明，产物强制 `provenance.type = "cloned"`，
不允许上架模板市场。
"""

from .compose import (
    DISTILL_MARKER,
    PROVENANCE_CLONED,
    DistillResult,
    PersonaDistillError,
    distill_persona,
)
from .fingerprint import extract_fingerprint
from .parse import TranscriptError, parse_transcript

__all__ = [
    "DISTILL_MARKER",
    "PROVENANCE_CLONED",
    "DistillResult",
    "PersonaDistillError",
    "TranscriptError",
    "distill_persona",
    "extract_fingerprint",
    "parse_transcript",
]
