"""CodeSmells 的业务契约测试。

“保持原行为”测试锁定外部可观察结果，不约束函数内部结构，允许策略映射、依赖注入等重构。
“已知缺陷”测试描述期望修复后的健壮性，并以非严格 xfail 记录当前差距：缺陷尚未修复时
套件仍可作为重构回归基线，未来修复后转为 XPASS 也不会阻断 Reviewer。
"""

from collections.abc import Iterable

import pytest
from Calculator import calc
from services import UserManagementService


@pytest.mark.parametrize(
    ("left", "right", "operator", "expected"),
    [
        (2, 3, "j", 5),
        (2, 3, "c", 6),
        (8, 2, "d", 4),
    ],
)
def test_calculator_preserves_main_operations(
    left: float,
    right: float,
    operator: str,
    expected: float,
):
    """保持原行为：主要公开操作符继续产生相同计算结果。"""

    assert calc(left, right, operator) == expected


def test_calculator_preserves_invalid_operator_result():
    """保持原行为：未知操作符以 0 表示未执行计算。"""

    assert calc(1, 2, "unknown") == 0


def test_calculator_preserves_division_by_zero_exception():
    """保持原行为：除数为零时显式传播 Python 的 ZeroDivisionError。"""

    with pytest.raises(ZeroDivisionError):
        calc(1, 0, "d")


class StubDatabase:
    def __init__(self, users: Iterable[object] | None):
        self.users = users

    def fetch_all(self, table: str):
        if table != "users" or self.users is None:
            return []
        return list(self.users)


def _service_with_users(users: Iterable[object] | None) -> UserManagementService:
    service = UserManagementService()
    service.db = StubDatabase(users)
    return service


def test_admin_user_filter_preserves_names_and_roles():
    """保持原行为：只返回管理员姓名，并维持大写展示契约。"""

    service = _service_with_users(
        [
            {"id": 1, "name": "alice", "role": "admin"},
            {"id": 2, "name": "bob", "role": "user"},
            {"id": 3, "name": "carol", "role": "admin"},
        ]
    )

    assert service.get_admin_users() == ["ALICE", "CAROL"]


def test_missing_user_dataset_preserves_empty_result():
    """保持原行为：数据源缺失用户表时返回空管理员列表。"""

    assert _service_with_users(None).get_admin_users() == []


@pytest.mark.xfail(
    reason="已知缺陷：缺失 role 的记录应被跳过，而不是让整个筛选因 KeyError 失败",
    strict=False,
)
def test_missing_role_is_skipped_after_known_defect_is_fixed():
    """修复已知缺陷：单条不完整数据不能阻断其他有效管理员。"""

    service = _service_with_users(
        [
            {"id": 1, "name": "incomplete"},
            {"id": 2, "name": "alice", "role": "admin"},
        ]
    )

    assert service.get_admin_users() == ["ALICE"]


@pytest.mark.xfail(
    reason="已知缺陷：管理员记录缺失 name 时应被跳过，而不是抛出 KeyError",
    strict=False,
)
def test_missing_admin_name_is_skipped_after_known_defect_is_fixed():
    """修复已知缺陷：异常管理员数据应隔离，正常记录仍可返回。"""

    service = _service_with_users(
        [
            {"id": 1, "role": "admin"},
            {"id": 2, "name": "carol", "role": "admin"},
        ]
    )

    assert service.get_admin_users() == ["CAROL"]
