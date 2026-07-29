from dataclasses import dataclass
from typing import Literal

TerminalStatus = Literal[
    "success",
    "safety_blocked",
    "approval_rejected",
    "test_failure",
    "budget_exceeded",
    "model_error",
]


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


@dataclass(frozen=True)
class EvaluationScenario:
    id: str
    category: str
    title: str
    request: str
    source_excerpt: str
    expected: ExpectedOutcome
    fake_tool_calls: int
    fake_input_tokens: int
    fake_output_tokens: int
    security_relevant: bool = False


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
    error_kind: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def first_review_success(self) -> bool | None:
        if not self.review_results:
            return None
        return self.review_results[0]

    @property
    def final_success(self) -> bool:
        return self.terminal_status == "success"
