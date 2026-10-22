"""日记（M3-D）：巩固管线第 6 步，但**不在对话链路里**。

它不是 orchestrator 的一环——日记是后处理：读当天已有的 turn / 记忆 / 情绪，
写下一篇第一人称的感受。所以它可以被随时触发（前端一个按钮），
也不会拖慢任何一轮对话。
"""

from .flow import DIARY_MARKER, DiaryGenerateError, flow_check, generate_diary

__all__ = ["DIARY_MARKER", "DiaryGenerateError", "flow_check", "generate_diary"]
