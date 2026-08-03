from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

TerminalStatus = Literal[
    "success",
    "safety_blocked",
    "approval_rejected",
    "test_failure",
    "budget_exceeded",
    "workspace_conflict",
    "model_error",
]

AgentRole = Literal["architect", "developer", "reviewer"]
ScriptPhase = Literal["decision", "after_tool"]


@dataclass(frozen=True)
class ScriptedToolCall:
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class ScriptStep:
    """一次模型调用的精确契约；任何字段不匹配都必须失败关闭。"""

    role: AgentRole
    task_id: str | None
    retry_count: int
    tool_round: int
    phase: ScriptPhase
    content: str = ""
    tool_calls: tuple[ScriptedToolCall, ...] = ()
    input_tokens: int = 100
    output_tokens: int = 40


@dataclass(frozen=True)
class ScenarioInput:
    request: str
    source_files: dict[str, str]
    approval: bool | None
    test_results: tuple[bool, ...] = (True,)
    run_budget: dict[str, int] = field(default_factory=dict)
    mutation_before_resume: Literal["none", "working_tamper", "baseline_conflict"] = (
        "none"
    )
    topology_fallback: bool = False


@dataclass(frozen=True)
class ExpectedOutcome:
    terminal_status: TerminalStatus
    planned_files: tuple[str, ...]
    modified_files: tuple[str, ...]
    applied_files: tuple[str, ...]
    review_results: tuple[bool, ...]
    retry_count: int
    tests_passed: bool
    approval_requested: bool
    approval_granted: bool | None
    safety_blocked: bool
    budget_exceeded: bool
    plan_status: str
    task_statuses: tuple[tuple[str, str], ...]
    workspace_cleaned: bool


@dataclass(frozen=True)
class EvaluationScenario:
    id: str
    category: str
    title: str
    input: ScenarioInput
    script: tuple[ScriptStep, ...]
    expected: ExpectedOutcome
    security_relevant: bool = False

    @property
    def request(self) -> str:
        return self.input.request

    @property
    def source_excerpt(self) -> str:
        """Live 只读判定兼容字段；离线报告绝不会序列化该源码。"""

        return next(iter(self.input.source_files.values()), "")[:1_000]


@dataclass(frozen=True)
class ModelOutcome:
    terminal_status: TerminalStatus
    planned_files: tuple[str, ...]
    modified_files: tuple[str, ...]
    applied_files: tuple[str, ...]
    review_results: tuple[bool, ...]
    retry_count: int
    tool_calls: int
    input_tokens: int
    output_tokens: int
    tests_passed: bool
    approval_requested: bool
    approval_granted: bool | None
    safety_blocked: bool
    budget_exceeded: bool
    plan_status: str = "unknown"
    task_statuses: tuple[tuple[str, str], ...] = ()
    workspace_cleaned: bool = False
    event_types: tuple[str, ...] = ()
    workspace_apply_failure: dict[str, Any] | None = None
    error_kind: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def first_review_success(self) -> bool | None:
        return self.review_results[0] if self.review_results else None

    @property
    def final_success(self) -> bool:
        return self.terminal_status == "success"
