"""场景（剧本）运行时：角色扮演的第二层（docs/ARCHITECTURE.md §3.5）。

人格回答「她是谁」，场景回答「我们在哪、发生什么」。
同一人格进入不同场景，是这类产品的核心玩法——所以她必须能被放进另一个世界，
而不会把上一个世界的记忆和处境带过去。
"""

from .loader import list_scenarios, load_scenario
from .models import Beat, Scenario

__all__ = ["Beat", "Scenario", "list_scenarios", "load_scenario"]
