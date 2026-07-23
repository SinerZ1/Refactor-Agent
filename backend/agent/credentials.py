"""运行时模型凭据的短生命周期隔离存储。

这里刻意不把 API Key 放进 LangGraph 的 ``configurable``。Checkpointer 会把其中的
标量复制进 checkpoint metadata，因此直接传递密钥会让 Redis/MemorySaver 从“状态
持久化”越界承担“秘密存储”的职责。凭据仓库只保存于当前进程内，图配置中仅携带
不可反推出密钥的随机引用；进程重启后引用自然失效，符合最小持久化原则。
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass

from pydantic import SecretStr


class CredentialReferenceError(RuntimeError):
    """凭据引用不存在或已过期，调用方应要求用户重新提交密钥。"""


@dataclass(frozen=True)
class _CredentialEntry:
    secret: SecretStr
    expires_at: float


class EphemeralCredentialVault:
    """线程安全的进程内凭据仓库。

    FastAPI 的同步 SSE 生成器会在线程池中运行，而 LangGraph 节点也可能跨线程访问
    凭据，因此使用可重入锁保护生命周期操作。TTL 是纵深防御：即使客户端在中断后
    没有恢复工作流，遗留凭据也不会伴随 Checkpointer 长期存在。
    """

    def __init__(self, ttl_seconds: float = 3600.0) -> None:
        if ttl_seconds <= 0:
            raise ValueError("凭据 TTL 必须大于 0")
        self._ttl_seconds = ttl_seconds
        self._entries: dict[str, _CredentialEntry] = {}
        self._lock = threading.RLock()

    def store(self, api_key: str) -> str | None:
        """保存非空密钥并返回随机引用；空密钥继续由服务端环境变量处理。"""

        if not api_key:
            return None
        reference = secrets.token_urlsafe(32)
        with self._lock:
            self._purge_expired()
            self._entries[reference] = _CredentialEntry(
                secret=SecretStr(api_key),
                expires_at=time.monotonic() + self._ttl_seconds,
            )
        return reference

    def resolve(self, reference: str) -> str:
        """解析有效引用，错误信息不包含引用或密钥本身。"""

        with self._lock:
            self._purge_expired()
            entry = self._entries.get(reference)
            if entry is None:
                raise CredentialReferenceError("模型凭据已过期，请重新提交连接配置")
            return entry.secret.get_secret_value()

    def revoke(self, reference: str | None) -> None:
        """在一次请求完成或客户端断开后立即撤销凭据。"""

        if not reference:
            return
        with self._lock:
            self._entries.pop(reference, None)

    def _purge_expired(self) -> None:
        now = time.monotonic()
        expired = [
            reference
            for reference, entry in self._entries.items()
            if entry.expires_at <= now
        ]
        for reference in expired:
            self._entries.pop(reference, None)


runtime_credentials = EphemeralCredentialVault()
