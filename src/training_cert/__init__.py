"""培训补考证书管理服务端。

仅依赖 Python 标准库：SQLite 持久化、hashlib 证据链、http.server 接口。
"""
from .service import CertService, DomainError
from .store import Store

__all__ = ["CertService", "DomainError", "Store"]
