"""熔断器（M4-A）。

一个 provider 连续失败到阈值，就别再为它白等一次超时了——直接跳过，
让请求立刻落到备 provider 上。冷却之后再放**一次**试探，成功则恢复。

为什么失败计数只认「连续」：陪伴产品的模型故障几乎都是整段整段的
（网关挂了、配额空了），连续的语义就是「这台机器现在不行」。
把零星失败也累计进去，会让一个健康但偶尔抖一下的 provider 被判死。
"""

from __future__ import annotations

import time
from collections.abc import Callable

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"


class CircuitBreaker:
    """三态熔断器：closed → open → half_open →（closed | open）。

    时间来源可注入（`now`），否则测试只能靠 sleep 等冷却，
    而靠 sleep 的测试既慢又会偶发失败。
    """

    def __init__(
        self,
        fail_threshold: int = 5,
        cooldown_seconds: float = 60.0,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        if fail_threshold < 1:
            raise ValueError("fail_threshold 至少为 1")
        if cooldown_seconds < 0:
            raise ValueError("cooldown_seconds 不能为负")
        self.fail_threshold = fail_threshold
        self.cooldown_seconds = cooldown_seconds
        self._now = now
        self._state = CLOSED
        self._consecutive_failures = 0
        self._opened_at = 0.0
        # half_open 只放一次试探：放出去的这一次还没回来时，别的请求不许再进。
        self._probe_in_flight = False

    # ------------------------------------------------------------------ #
    @property
    def state(self) -> str:
        """当前状态。读的时候会顺带做「冷却到点 → half_open」的转换。

        为什么要在这里转换：冷却是被动到期的，没有定时器。
        如果只在 `allow()` 里转，`breaker.state` 就会一直显示 open，
        观测端点与测试都会读到过期的事实。
        """
        if self._state == OPEN and self._cooldown_elapsed():
            self._state = HALF_OPEN
            self._probe_in_flight = False
        return self._state

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def allow(self) -> bool:
        """是否放行。**

        open 且冷却未到 → False（不发起请求）。
        half_open → 只放**一次**：第一个进来的拿到试探权，其余继续被挡。
        """
        current = self.state
        if current == CLOSED:
            return True
        if current == HALF_OPEN:
            if self._probe_in_flight:
                return False
            self._probe_in_flight = True
            return True
        return False

    def record_success(self) -> None:
        self._state = CLOSED
        self._consecutive_failures = 0
        self._probe_in_flight = False
        self._opened_at = 0.0

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._state == HALF_OPEN:
            # 试探失败：回到 open，并**重置**冷却——刚证明它还是坏的，
            # 不该立刻再试一次。
            self._trip()
            return
        if self._state == CLOSED and self._consecutive_failures >= self.fail_threshold:
            self._trip()

    def snapshot(self, name: str = "") -> dict:
        """观测用的只读快照。"""
        return {
            "name": name,
            "state": self.state,
            "consecutive_failures": self._consecutive_failures,
        }

    # ------------------------------------------------------------------ #
    def _trip(self) -> None:
        self._state = OPEN
        self._opened_at = self._now()
        self._probe_in_flight = False

    def _cooldown_elapsed(self) -> bool:
        return (self._now() - self._opened_at) >= self.cooldown_seconds

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"CircuitBreaker(state={self.state}, "
            f"failures={self._consecutive_failures}/{self.fail_threshold})"
        )
