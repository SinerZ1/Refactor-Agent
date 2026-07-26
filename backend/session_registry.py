import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Callable


class SessionRegistryError(RuntimeError):
    """会话或审批状态不满足安全前置条件。"""


class SessionAuthorizationError(SessionRegistryError):
    """会话不存在、已过期或令牌不匹配。"""


class ApprovalStateError(SessionRegistryError):
    """审批 ID 与当前挂起点不一致，或审批状态非法。"""


class RunStateError(SessionRegistryError):
    """同一会话存在并发运行，或调用方尝试操作过期运行。"""


@dataclass(frozen=True)
class SessionCredentials:
    thread_id: str
    session_token: str


@dataclass
class _SessionRecord:
    token_digest: str
    expires_at: float
    approval_id: str | None = None
    approval_decision: bool | None = None
    active_run_id: str | None = None


class SessionRegistry:
    """进程内短期会话注册表。

    HITL 本质上是对状态机中断点的能力授权。仅依赖可见的 thread_id 会把“定位状态”
    错当成“拥有状态”；因此这里另发不可预测令牌，并用一次性 approval_id 把用户决定
    精确绑定到当前中断，防止跨会话或旧弹窗重放。

    ``thread_id`` 表示可持续多轮对话的 Checkpoint 命名空间，``run_id`` 表示其中一次
    从 START 到终态（可跨 HITL 暂停/恢复）的执行租约。同一 thread 同时只允许一个
    active run，防止两个 SSE 请求交错覆盖同一个 LangGraph Checkpoint。
    """

    def __init__(
        self,
        ttl_seconds: int = 4 * 60 * 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._records: dict[str, _SessionRecord] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _prune_expired(self, now: float) -> None:
        expired = [
            thread_id
            for thread_id, record in self._records.items()
            if record.expires_at <= now
        ]
        for thread_id in expired:
            self._records.pop(thread_id, None)

    def create(self) -> SessionCredentials:
        now = self._clock()
        thread_id = f"session_{secrets.token_hex(16)}"
        session_token = secrets.token_urlsafe(32)
        with self._lock:
            self._prune_expired(now)
            self._records[thread_id] = _SessionRecord(
                token_digest=self._digest(session_token),
                expires_at=now + self._ttl_seconds,
            )
        return SessionCredentials(thread_id, session_token)

    def require(self, thread_id: str, session_token: str) -> None:
        now = self._clock()
        with self._lock:
            self._prune_expired(now)
            record = self._records.get(thread_id)
            if not record or not hmac.compare_digest(
                record.token_digest, self._digest(session_token)
            ):
                raise SessionAuthorizationError("会话无效或已过期")
            record.expires_at = now + self._ttl_seconds

    def begin_run(self, thread_id: str, session_token: str) -> str:
        self.require(thread_id, session_token)
        with self._lock:
            record = self._records[thread_id]
            if record.active_run_id is not None:
                raise RunStateError("当前会话已有运行中的 DAG，请等待其完成或审批恢复")
            record.active_run_id = f"run_{secrets.token_hex(16)}"
            return record.active_run_id

    def require_active_run(self, thread_id: str, session_token: str) -> str:
        self.require(thread_id, session_token)
        with self._lock:
            run_id = self._records[thread_id].active_run_id
            if run_id is None:
                raise RunStateError("当前会话没有可恢复的 DAG 运行")
            return run_id

    def finish_run(
        self,
        thread_id: str,
        session_token: str,
        run_id: str,
    ) -> bool:
        """只释放调用方持有的租约，旧请求的 finally 不得清除较新的运行。"""

        self.require(thread_id, session_token)
        with self._lock:
            record = self._records[thread_id]
            if record.active_run_id != run_id:
                return False
            record.active_run_id = None
            record.approval_id = None
            record.approval_decision = None
            return True

    def begin_approval(
        self,
        thread_id: str,
        session_token: str,
        run_id: str,
    ) -> str:
        self.require(thread_id, session_token)
        with self._lock:
            record = self._records[thread_id]
            if record.active_run_id != run_id:
                raise RunStateError("审批挂起点不属于当前活动运行")
            if record.approval_id and record.approval_decision is None:
                return record.approval_id
            record.approval_id = secrets.token_urlsafe(18)
            record.approval_decision = None
            return record.approval_id

    def decide(
        self,
        thread_id: str,
        session_token: str,
        approval_id: str,
        approved: bool,
    ) -> None:
        self.require(thread_id, session_token)
        with self._lock:
            record = self._records[thread_id]
            if not record.approval_id or not hmac.compare_digest(
                record.approval_id, approval_id
            ):
                raise ApprovalStateError("审批请求已失效，请等待当前中断重新推送")
            if record.approval_decision is not None:
                if record.approval_decision == approved:
                    return
                raise ApprovalStateError("当前审批已经处理，不能覆盖原决定")
            record.approval_decision = approved

    def consume_decision(
        self, thread_id: str, session_token: str
    ) -> tuple[str, bool, str] | None:
        self.require(thread_id, session_token)
        with self._lock:
            record = self._records[thread_id]
            if not record.approval_id or record.approval_decision is None:
                return None
            if record.active_run_id is None:
                raise RunStateError("审批决定缺少对应的活动运行")
            decision = (
                record.approval_id,
                record.approval_decision,
                record.active_run_id,
            )
            record.approval_id = None
            record.approval_decision = None
            return decision


runtime_sessions = SessionRegistry()
