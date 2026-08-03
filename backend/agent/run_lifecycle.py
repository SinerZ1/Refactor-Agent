"""SSE 运行的资源所有权、终态与幂等回收。

LangGraph checkpoint 负责“状态可恢复”，但不会自动拥有 HTTP 连接、线程、凭据引用或
隔离目录。本模块把这些异构资源收敛为一个 run 级生命周期对象：HITL 是唯一允许跨请求
保留 checkpoint/workspace/lease 的状态，其余终态都执行有界、可重复的补偿式回收。
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from .run_status import run_statuses


class RunTermination(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    BUDGET_EXCEEDED = "budget_exceeded"
    DISCONNECTED = "disconnect"
    GENERATOR_CLOSED = "generator_closed"
    CANCELLATION = "cancellation"
    PRODUCER_CANCELLED = "producer_cancelled"
    HITL_PAUSED = "hitl_paused"
    HITL_APPROVED = "hitl_approved"
    HITL_REJECTED = "hitl_rejected"
    RECOVERY_FAILED = "recovery_failed"
    SERVICE_SHUTDOWN = "service_shutdown"


@dataclass(frozen=True)
class HitlRetention:
    workspace_id: str
    approval_id: str
    workspace_snapshot_digest: str

    @property
    def is_valid(self) -> bool:
        return bool(
            self.workspace_id
            and self.approval_id
            and len(self.workspace_snapshot_digest) == 64
        )


@dataclass
class CleanupReport:
    reason: RunTermination
    producer_timed_out: bool = False
    producer_error: str | None = None
    workspace_retained: bool = False
    workspace_cleaned: bool = False
    checkpoint_deleted: bool = False
    lease_released: bool = False
    credential_revoked: bool = False
    cleanup_complete: bool = False
    cleanup_errors: list[str] = field(default_factory=list)


def _log_lifecycle(run_id: str, event: str, **fields: object) -> None:
    """只记录枚举、布尔值和异常类型，绝不记录 Prompt、源码或凭据。"""

    payload = {"event": event, "run_id": run_id, **fields}
    print(f"[RunLifecycle] {json.dumps(payload, ensure_ascii=False, sort_keys=True)}")


class RunLifecycle:
    """一次逻辑 run 的资源所有者。

    producer 仍运行在专用线程中，因为 LangGraph 的同步 ``stream`` API 及模型 SDK 可能
    阻塞；线程无法被 Python 安全强杀。因此断连先设置协作式停止信号，再有界等待线程
    join。若超时，生命周期对象继续持有 lease/凭据/workspace，并由受跟踪的延迟清理
    task 在线程真正退出后回收，避免为了快速返回而删除仍被 producer 使用的资源。
    """

    def __init__(
        self,
        *,
        run_id: str,
        thread_id: str,
        credential_ref: str | None,
        finish_lease: Callable[[], bool],
        revoke_credential: Callable[[str | None], None],
        delete_checkpoint: Callable[[str], None],
        cleanup_workspace: Callable[[str], None],
        resolve_workspace: Callable[[], str | None],
        unregister: Callable[[str, "RunLifecycle"], None],
        producer_timeout_seconds: float,
    ) -> None:
        self.run_id = run_id
        self.thread_id = thread_id
        self.credential_ref = credential_ref
        self.stop_requested = threading.Event()
        self.workspace_id: str | None = None
        self.hitl_retention: HitlRetention | None = None
        self._finish_lease = finish_lease
        self._revoke_credential = revoke_credential
        self._delete_checkpoint = delete_checkpoint
        self._cleanup_workspace = cleanup_workspace
        self._resolve_workspace = resolve_workspace
        self._unregister = unregister
        self._producer_timeout_seconds = producer_timeout_seconds
        self._producer_thread: threading.Thread | None = None
        self._producer_waiter: asyncio.Task[None] | None = None
        self._producer_error: BaseException | None = None
        self._cleanup_task: asyncio.Task[CleanupReport] | None = None
        self._deferred_cleanup_task: asyncio.Task[None] | None = None
        self._report: CleanupReport | None = None
        self._credential_revoked = False
        self._checkpoint_deleted = False
        self._lease_released = False
        self._workspace_cleaned = False
        run_statuses.start(run_id, thread_id)

    @property
    def producer_alive(self) -> bool:
        return bool(self._producer_thread and self._producer_thread.is_alive())

    def set_workspace(self, workspace_id: str | None) -> None:
        if workspace_id:
            self.workspace_id = workspace_id

    def retain_for_hitl(self, retention: HitlRetention) -> None:
        if not retention.is_valid:
            raise ValueError("HITL 保留信息不完整")
        self.workspace_id = retention.workspace_id
        self.hitl_retention = retention
        run_statuses.update(
            self.run_id,
            lifecycle_status="waiting_for_hitl",
            workspace_retained=True,
        )

    def start_producer(self, target: Callable[[], None]) -> None:
        if self._producer_thread is not None:
            raise RuntimeError("producer 已启动")

        def guarded_target() -> None:
            try:
                target()
            except BaseException as exc:
                # 线程异常必须由生命周期所有者获取；只保留对象用于类型化观测，绝不
                # 把可能包含模型响应或凭据的异常文本写入日志。
                self._producer_error = exc

        self._producer_thread = threading.Thread(
            target=guarded_target,
            name=f"sse-run-{self.run_id}",
            daemon=True,
        )
        self._producer_thread.start()
        self._producer_waiter = asyncio.create_task(
            asyncio.to_thread(self._producer_thread.join),
            name=f"sse-producer-wait-{self.run_id}",
        )

    async def cleanup(
        self,
        reason: RunTermination,
        *,
        retain_for_hitl: bool = False,
    ) -> CleanupReport:
        """幂等回收；外层 cancellation 发生时先完成关键清理，再原样传播。"""

        terminal_after_retention = bool(
            self._cleanup_task is not None
            and self._cleanup_task.done()
            and self._report is not None
            and self._report.workspace_retained
            and not retain_for_hitl
        )
        retry_incomplete_cleanup = bool(
            self._cleanup_task is not None
            and self._cleanup_task.done()
            and self._report is not None
            and not self._report.cleanup_complete
            and not self._report.producer_timed_out
        )
        if (
            self._cleanup_task is None
            or terminal_after_retention
            or retry_incomplete_cleanup
        ):
            self._cleanup_task = asyncio.create_task(
                self._cleanup_impl(reason, retain_for_hitl=retain_for_hitl),
                name=f"run-cleanup-{self.run_id}",
            )
        else:
            _log_lifecycle(self.run_id, "duplicate_cleanup_ignored", reason=reason)

        try:
            return await asyncio.shield(self._cleanup_task)
        except asyncio.CancelledError:
            # shield 保护内部 task，但调用者仍会立即收到 cancellation。这里显式等待
            # 有界清理结束，随后重新抛出，保持 Python 3.12 的取消语义。
            await asyncio.shield(self._cleanup_task)
            raise

    async def _cleanup_impl(
        self,
        reason: RunTermination,
        *,
        retain_for_hitl: bool,
    ) -> CleanupReport:
        report = CleanupReport(reason=reason)
        self._report = report
        _log_lifecycle(self.run_id, "cleanup_started", reason=reason)
        run_statuses.update(
            self.run_id,
            lifecycle_status="cancelling",
            termination_reason=reason.value,
        )
        self.stop_requested.set()

        if self._producer_waiter is not None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._producer_waiter),
                    timeout=self._producer_timeout_seconds,
                )
            except TimeoutError:
                report.producer_timed_out = True
                report.cleanup_errors.append("producer_timeout")
                _log_lifecycle(
                    self.run_id,
                    "producer_timeout",
                    timeout_seconds=self._producer_timeout_seconds,
                )
                self._deferred_cleanup_task = asyncio.create_task(
                    self._finish_after_producer(reason),
                    name=f"deferred-run-cleanup-{self.run_id}",
                )
                run_statuses.update(
                    self.run_id,
                    lifecycle_status="cleanup_pending",
                    termination_reason=reason.value,
                    cleanup_errors=report.cleanup_errors,
                )
                return report

        self._observe_producer(report)
        await self._release_owned_resources(
            report,
            retain_for_hitl=retain_for_hitl,
        )
        return report

    async def _finish_after_producer(self, reason: RunTermination) -> None:
        assert self._producer_waiter is not None
        try:
            await asyncio.shield(self._producer_waiter)
            report = self._report or CleanupReport(reason=reason)
            # timeout 是进入延迟回收的历史原因；producer 最终退出后不应继续把一次
            # 成功的补偿清理标成 cleanup_failed。
            report.cleanup_errors = [
                error for error in report.cleanup_errors if error != "producer_timeout"
            ]
            self._observe_producer(report)
            await self._release_owned_resources(report, retain_for_hitl=False)
            _log_lifecycle(self.run_id, "deferred_cleanup_completed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            report = self._report or CleanupReport(reason=reason)
            report.cleanup_errors.append(f"deferred_cleanup:{exc.__class__.__name__}")
            run_statuses.update(
                self.run_id,
                lifecycle_status="cleanup_failed",
                terminal_status="failed",
                termination_reason=reason.value,
                cleanup_errors=report.cleanup_errors,
            )
            _log_lifecycle(
                self.run_id,
                "cleanup_failure",
                resource="deferred_cleanup",
                error_type=exc.__class__.__name__,
            )

    def _observe_producer(self, report: CleanupReport) -> None:
        if self._producer_error is None:
            return
        report.producer_error = self._producer_error.__class__.__name__
        _log_lifecycle(
            self.run_id,
            "producer_exception_retrieved",
            error_type=report.producer_error,
        )
        self._producer_error = None

    async def _release_owned_resources(
        self,
        report: CleanupReport,
        *,
        retain_for_hitl: bool,
    ) -> None:
        if self.workspace_id is None:
            try:
                self.set_workspace(await asyncio.to_thread(self._resolve_workspace))
            except Exception as exc:
                report.cleanup_errors.append(
                    f"workspace_discovery:{exc.__class__.__name__}"
                )
                _log_lifecycle(
                    self.run_id,
                    "cleanup_failure",
                    resource="workspace_discovery",
                    error_type=exc.__class__.__name__,
                )
        valid_retention = bool(
            retain_for_hitl
            and self.hitl_retention is not None
            and self.hitl_retention.is_valid
            and self.workspace_id == self.hitl_retention.workspace_id
        )

        await self._run_once(
            report,
            "credential",
            lambda: self._revoke_credential(self.credential_ref),
            "_credential_revoked",
        )
        report.credential_revoked = self._credential_revoked

        if valid_retention:
            report.workspace_retained = True
            report.cleanup_complete = True
            _log_lifecycle(
                self.run_id,
                "workspace_retained_for_hitl",
                workspace_id=self.workspace_id,
            )
            run_statuses.update(
                self.run_id,
                lifecycle_status="waiting_for_hitl",
                termination_reason=report.reason.value,
                workspace_retained=True,
                cleanup_errors=report.cleanup_errors,
            )
            return
        if retain_for_hitl:
            report.cleanup_errors.append("invalid_hitl_retention")
            _log_lifecycle(self.run_id, "cleanup_failure", resource="hitl_retention")

        await self._run_once(
            report,
            "checkpoint",
            lambda: self._delete_checkpoint(self.thread_id),
            "_checkpoint_deleted",
        )
        report.checkpoint_deleted = self._checkpoint_deleted
        await self._run_once(
            report,
            "lease",
            self._finish_lease,
            "_lease_released",
        )
        report.lease_released = self._lease_released
        if self.workspace_id:
            await self._run_once(
                report,
                "workspace",
                lambda: self._cleanup_workspace(self.workspace_id or ""),
                "_workspace_cleaned",
            )
        else:
            self._workspace_cleaned = True
        report.workspace_cleaned = self._workspace_cleaned
        report.cleanup_complete = not report.cleanup_errors
        if report.cleanup_complete:
            self._unregister(self.run_id, self)
        _log_lifecycle(
            self.run_id,
            "workspace_cleaned",
            cleaned=report.workspace_cleaned,
            reason=report.reason,
        )
        successful_termination = report.reason in {
            RunTermination.COMPLETED,
            RunTermination.HITL_APPROVED,
        }
        run_statuses.update(
            self.run_id,
            lifecycle_status=(
                "cleanup_completed" if report.cleanup_complete else "cleanup_failed"
            ),
            terminal_status=(
                "completed"
                if successful_termination and report.cleanup_complete
                else "failed"
            ),
            termination_reason=report.reason.value,
            workspace_retained=False,
            cleanup_errors=report.cleanup_errors,
        )

    async def _run_once(
        self,
        report: CleanupReport,
        resource: str,
        operation: Callable[[], object],
        flag_name: str,
    ) -> None:
        if getattr(self, flag_name):
            return
        try:
            await asyncio.to_thread(operation)
            setattr(self, flag_name, True)
        except Exception as exc:
            report.cleanup_errors.append(f"{resource}:{exc.__class__.__name__}")
            _log_lifecycle(
                self.run_id,
                "cleanup_failure",
                resource=resource,
                error_type=exc.__class__.__name__,
            )


class ActiveRunRegistry:
    """追踪活动及 HITL 保留 run，支持 ASGI shutdown 的有界统一回收。"""

    def __init__(self) -> None:
        self._runs: dict[str, RunLifecycle] = {}

    def register(self, lifecycle: RunLifecycle) -> None:
        self._runs[lifecycle.run_id] = lifecycle

    def unregister(self, run_id: str, lifecycle: RunLifecycle) -> None:
        if self._runs.get(run_id) is lifecycle:
            self._runs.pop(run_id, None)

    def get(self, run_id: str) -> RunLifecycle | None:
        return self._runs.get(run_id)

    async def shutdown(self) -> list[CleanupReport | BaseException]:
        runs = list(self._runs.values())
        if not runs:
            return []
        return await asyncio.gather(
            *(lifecycle.cleanup(RunTermination.SERVICE_SHUTDOWN) for lifecycle in runs),
            return_exceptions=True,
        )


active_runs = ActiveRunRegistry()
