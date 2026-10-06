"""retry：确定性重试与熔断策略内核。

对外入口：
    BackoffPolicy    指数退避：倍增、封顶与注入式抖动
    CircuitBreaker   三态熔断器：连续失败开闸、打开时长、半开配额、连续成功复位
    RetryKernel      重试链：最大尝试次数、超时判定与统计计数
    RetryResult      一次重试链的结果快照
    CLOSED / OPEN / HALF_OPEN   熔断器状态
    OK / FAIL / TIMEOUT         一次尝试的结果
"""

from .core import (
    BackoffPolicy,
    CircuitBreaker,
    CLOSED,
    FAIL,
    HALF_OPEN,
    OK,
    OPEN,
    OUTCOMES,
    RetryKernel,
    RetryResult,
    TIMEOUT,
)

__all__ = [
    "BackoffPolicy",
    "CircuitBreaker",
    "CLOSED",
    "FAIL",
    "HALF_OPEN",
    "OK",
    "OPEN",
    "OUTCOMES",
    "RetryKernel",
    "RetryResult",
    "TIMEOUT",
]
