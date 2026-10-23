"""人格市场（M4-B）：上架 / 审核 / 分发。

红线在 `service.blocks_listing()`：`provenance.type == "cloned"` 的人格不许上架。

**这里刻意不在 import 时拉起 `service`**：`storage/base.py` 要用
`market.models` 里的两个 dataclass，而 `market.service` 又要用 `Repository`。
把 service 的导入留给调用方（`from ..market import service` / `from ..market.service import ...`），
两个方向都不会在 import 期互相等待。
"""
