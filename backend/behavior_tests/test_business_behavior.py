"""CodeSmells 的可信业务契约测试。

测试与 Agent 可写源码分属不同信任边界；这里只锁定外部可观察行为，不约束重构后的
内部结构。已知缺陷使用非严格 xfail，修复后 XPASS 不会阻断 Reviewer。
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
    assert calc(left, right, operator) == expected


def test_calculator_preserves_invalid_operator_result():
    assert calc(1, 2, "unknown") == 0


def test_calculator_preserves_division_by_zero_exception():
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
    service = _service_with_users(
        [
            {"id": 1, "name": "alice", "role": "admin"},
            {"id": 2, "name": "bob", "role": "user"},
            {"id": 3, "name": "carol", "role": "admin"},
        ]
    )

    assert service.get_admin_users() == ["ALICE", "CAROL"]


def test_missing_user_dataset_preserves_empty_result():
    assert _service_with_users(None).get_admin_users() == []


@pytest.mark.xfail(
    reason="已知缺陷：缺失 role 的记录应被跳过，而不是让整个筛选因 KeyError 失败",
    strict=False,
)
def test_missing_role_is_skipped_after_known_defect_is_fixed():
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
    service = _service_with_users(
        [
            {"id": 1, "role": "admin"},
            {"id": 2, "name": "carol", "role": "admin"},
        ]
    )

    assert service.get_admin_users() == ["CAROL"]
