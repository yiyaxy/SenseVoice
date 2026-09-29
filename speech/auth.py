"""服务端鉴权辅助函数。"""

from __future__ import annotations

import secrets
from typing import Any


def constant_time_equal(left: Any, right: Any) -> bool:
    """以 UTF-8 字节做常量时间比较；异常类型或非字符串输入一律拒绝。"""
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    try:
        return secrets.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
    except (UnicodeError, TypeError):
        return False


__all__ = ["constant_time_equal"]
