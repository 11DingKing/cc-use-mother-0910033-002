"""领域错误类型。"""
from __future__ import annotations


class DomainError(Exception):
    """业务规则冲突，HTTP 层映射为 4xx。"""

    status = 400


class NotFoundError(DomainError):
    status = 404


class ConflictError(DomainError):
    """并发或唯一性冲突。"""

    status = 409
