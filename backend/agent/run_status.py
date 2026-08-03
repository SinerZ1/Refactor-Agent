"""可恢复读取、容量受限且脱敏的 run 生命周期状态注册表。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

LifecycleStatus = Literal[
    "running",
    "waiting_for_hitl",
    "cancelling",
    "cleanup_pending",
    "cleanup_completed",
    "cleanup_failed",
    "completed",
    "failed",
]


class RunStatusSnapshot(TypedDict):
    version: Literal[1]
    run_id: str
    lifecycle_status: LifecycleStatus
    terminal_status: Literal["completed", "failed"] | None
    termination_reason: str | None
    workspace_retained: bool
    cleanup_errors: list[str]
    apply_failure: dict[str, Any] | None


@dataclass
class _StatusRecord:
    thread_id: str
    expires_at: float
    snapshot: RunStatusSnapshot
    updated_at: float = field(default=0.0)


class RunStatusRegistry:
    """保存有限时间的安全投影，不把 Graph State 或异常原文复制进来。"""

    def __init__(
        self,
        *,
        ttl_seconds: float = 30 * 60,
        capacity: int = 512,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0 or capacity <= 0:
            raise ValueError("run 状态 TTL 与容量必须为正数")
        self._ttl_seconds = ttl_seconds
        self._capacity = capacity
        self._clock = clock
        self._records: dict[str, _StatusRecord] = {}
        self._lock = threading.RLock()

    def _prune(self, now: float, *, reserve_slot: bool = False) -> None:
        for run_id in [
            key for key, record in self._records.items() if record.expires_at <= now
        ]:
            self._records.pop(run_id, None)
        overflow = len(self._records) - self._capacity + (1 if reserve_slot else 0)
        if overflow > 0:
            oldest = sorted(self._records.items(), key=lambda item: item[1].updated_at)[
                :overflow
            ]
            for run_id, _record in oldest:
                self._records.pop(run_id, None)

    def start(self, run_id: str, thread_id: str) -> RunStatusSnapshot:
        now = self._clock()
        snapshot: RunStatusSnapshot = {
            "version": 1,
            "run_id": run_id,
            "lifecycle_status": "running",
            "terminal_status": None,
            "termination_reason": None,
            "workspace_retained": False,
            "cleanup_errors": [],
            "apply_failure": None,
        }
        with self._lock:
            self._prune(now, reserve_slot=True)
            self._records[run_id] = _StatusRecord(
                thread_id=thread_id,
                expires_at=now + self._ttl_seconds,
                snapshot=snapshot,
                updated_at=now,
            )
        return deepcopy(snapshot)

    def update(
        self,
        run_id: str,
        *,
        lifecycle_status: LifecycleStatus | None = None,
        terminal_status: Literal["completed", "failed"] | None = None,
        termination_reason: str | None = None,
        workspace_retained: bool | None = None,
        cleanup_errors: list[str] | None = None,
        apply_failure: dict[str, Any] | None = None,
    ) -> RunStatusSnapshot | None:
        now = self._clock()
        with self._lock:
            self._prune(now)
            record = self._records.get(run_id)
            if record is None:
                return None
            snapshot = record.snapshot
            if lifecycle_status is not None:
                snapshot["lifecycle_status"] = lifecycle_status
            if terminal_status is not None:
                snapshot["terminal_status"] = terminal_status
            if termination_reason is not None:
                snapshot["termination_reason"] = termination_reason
            if workspace_retained is not None:
                snapshot["workspace_retained"] = workspace_retained
            if cleanup_errors is not None:
                snapshot["cleanup_errors"] = list(cleanup_errors)[:8]
            if apply_failure is not None:
                snapshot["apply_failure"] = deepcopy(apply_failure)
            record.updated_at = now
            record.expires_at = now + self._ttl_seconds
            return deepcopy(snapshot)

    def get(self, run_id: str, thread_id: str) -> RunStatusSnapshot | None:
        now = self._clock()
        with self._lock:
            self._prune(now)
            record = self._records.get(run_id)
            if record is None or record.thread_id != thread_id:
                return None
            return deepcopy(record.snapshot)

    def __len__(self) -> int:
        with self._lock:
            self._prune(self._clock())
            return len(self._records)


run_statuses = RunStatusRegistry()
