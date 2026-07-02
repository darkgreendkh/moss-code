"""统一的时间戳来源。

为什么存在：
session/checkpoint/trace/memory 都需要"现在"的 UTC 时间戳。集中到一个
模块，所有落盘工件的时间格式保持一致（ISO 8601, UTC）；以后如果要在
测试里冻结时间，也只有这一处注入点。
"""

from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat()
